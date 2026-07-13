# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Build and validate JiuwenSwarm external CLI agent declarations."""

from __future__ import annotations

import importlib.util
import logging
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from openjiuwen.agent_teams.schema.team import ExternalCliAgentSpec

logger = logging.getLogger(__name__)

SUPPORTED_CLI_AGENT = "claude"
CLAUDE_MEMBER_NAME = "claude-coder"
CLAUDE_DISPLAY_NAME = "Claude Code"


def _selected_team_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the same first ``modes.team`` entry used by TeamConfigLoader."""
    modes = config.get("modes")
    if not isinstance(modes, Mapping):
        return {}
    teams = modes.get("team")
    if not isinstance(teams, Mapping):
        return {}
    return next((item for item in teams.values() if isinstance(item, Mapping)), {})


def get_external_cli_config_entries(config: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Read raw External CLI entries without mutating the config object."""
    raw = _selected_team_config(config).get("external_cli_agents", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("modes.team.<team>.external_cli_agents must be a list")
    entries: list[Mapping[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"external_cli_agents[{index}] must be a mapping")
        entries.append(item)
    return entries


def external_cli_enabled(config: Mapping[str, Any]) -> bool:
    """Return whether at least one explicitly valid entry is enabled."""
    for index, entry in enumerate(get_external_cli_config_entries(config)):
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"external_cli_agents[{index}].enabled must be a boolean")
        if enabled:
            return True
    return False


def _string_argv(value: Any, *, field: str, allow_none: bool) -> list[str] | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty list of strings")
    if any(not isinstance(part, str) or not part.strip() for part in value):
        raise ValueError(f"{field} must contain only non-empty strings")
    return list(value)


def _string_env(value: Any, *, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a string-to-string mapping")
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        raise ValueError(f"{field} must be a string-to-string mapping")
    return dict(value)


def _resolve_cwd(
    configured: Any,
    *,
    field: str,
    project_dir: str | None,
    team_workspace: str | None,
) -> str:
    if configured is not None and (not isinstance(configured, str) or not configured.strip()):
        raise ValueError(f"{field} must be a non-empty absolute path or null")

    if isinstance(configured, str) and configured.strip():
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ValueError(f"{field} must be absolute; use request project_dir for dynamic projects")
    else:
        fallback = str(project_dir or "").strip() or str(team_workspace or "").strip()
        if not fallback:
            raise ValueError(f"{field} could not be resolved: project_dir and team workspace are empty")
        path = Path(fallback).expanduser()

    path = path.resolve()
    if not path.is_dir():
        raise ValueError(f"{field} does not exist or is not a directory: {path}")
    return str(path)


def build_external_cli_agent_specs(
    config: Mapping[str, Any],
    *,
    mode: str,
    project_dir: str | None,
    team_workspace: str | None,
) -> list[ExternalCliAgentSpec]:
    """Convert the selected Team config into validated AgentCore specs."""
    if mode not in {"team", "code.team", "team.plan"}:
        return []

    result: list[ExternalCliAgentSpec] = []
    seen: set[str] = set()
    for index, entry in enumerate(get_external_cli_config_entries(config)):
        prefix = f"external_cli_agents[{index}]"
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"{prefix}.enabled must be a boolean")
        if not enabled:
            continue

        cli_agent = entry.get("cli_agent")
        if cli_agent != SUPPORTED_CLI_AGENT:
            raise ValueError(f"{prefix}.cli_agent must be '{SUPPORTED_CLI_AGENT}' for the MVP")
        if cli_agent in seen:
            raise ValueError(f"duplicate external CLI adapter configured: {cli_agent}")
        seen.add(cli_agent)

        inject_mcp = entry.get("inject_mcp", True)
        if inject_mcp is not True:
            raise ValueError(f"{prefix}.inject_mcp must be true for a Team Member")

        result.append(
            ExternalCliAgentSpec(
                cli_agent=cli_agent,
                command=_string_argv(entry.get("command"), field=f"{prefix}.command", allow_none=True),
                cwd=_resolve_cwd(
                    entry.get("cwd"),
                    field=f"{prefix}.cwd",
                    project_dir=project_dir,
                    team_workspace=team_workspace,
                ),
                inject_mcp=True,
                mcp_server_command=_string_argv(
                    entry.get("mcp_server_command", ["openjiuwen-team-mcp"]),
                    field=f"{prefix}.mcp_server_command",
                    allow_none=False,
                ) or [],
                env=_string_env(entry.get("env", {}), field=f"{prefix}.env"),
            )
        )

    if result:
        logger.info(
            "[EXTERNAL_AGENT_CONFIG_LOADED] cli_agents=%s mode=%s project_dir=%s",
            [item.cli_agent for item in result],
            mode,
            project_dir or "",
        )
    return result


def _executable_available(command: str) -> bool:
    path = Path(command).expanduser()
    if path.is_absolute() or path.parent != Path("."):
        return path.is_file()
    return shutil.which(command) is not None


def validate_external_cli_runtime(spec: Any, configs: list[ExternalCliAgentSpec]) -> None:
    """Fail fast when an enabled External CLI team cannot work cross-process."""
    if not configs:
        return
    if str(getattr(spec, "spawn_mode", "")).lower() != "process":
        raise ValueError("External CLI agents require team spawn_mode='process'")

    transport = getattr(spec, "transport", None)
    if transport is None or str(getattr(transport, "type", "")).lower() != "pyzmq":
        raise ValueError("External CLI agents require team transport.type='pyzmq'")
    params = getattr(transport, "params", {}) or {}
    for field in ("direct_addr", "pubsub_publish_addr", "pubsub_subscribe_addr"):
        if not isinstance(params.get(field), str) or not params[field].strip():
            raise ValueError(f"External CLI pyzmq transport requires params.{field}")
    metadata = params.get("metadata", {})
    if not isinstance(metadata, Mapping) or metadata.get("pubsub_bind") is not True:
        raise ValueError("External CLI leader transport requires params.metadata.pubsub_bind=true")
    if importlib.util.find_spec("zmq") is None:
        raise ValueError("External CLI agents require pyzmq; install JiuwenSwarm with --extra external-cli")

    storage = getattr(spec, "storage", None)
    if storage is None or str(getattr(storage, "type", "")).lower() != "sqlite":
        raise ValueError("External CLI MVP requires file-backed team storage.type='sqlite'")
    storage_params = getattr(storage, "params", {}) or {}
    connection = str(storage_params.get("connection_string", "") or "").strip().lower()
    if not connection:
        raise ValueError("External CLI agents require a file-backed SQLite connection_string")
    if connection in {":memory:", "sqlite:///:memory:"} or "mode=memory" in connection:
        raise ValueError("External CLI agents cannot use in-memory SQLite storage")

    for config in configs:
        command = (config.command or [config.cli_agent])[0]
        if not _executable_available(command):
            raise ValueError(f"External CLI executable is not available: {command}")
        mcp_command = config.mcp_server_command[0]
        if not _executable_available(mcp_command):
            raise ValueError(f"Team MCP executable is not available: {mcp_command}")


__all__ = [
    "CLAUDE_DISPLAY_NAME",
    "CLAUDE_MEMBER_NAME",
    "SUPPORTED_CLI_AGENT",
    "build_external_cli_agent_specs",
    "external_cli_enabled",
    "get_external_cli_config_entries",
    "validate_external_cli_runtime",
]
