from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from openjiuwen.agent_teams.schema.team import ExternalCliAgentSpec

from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs
from jiuwenswarm.agents.swarm.external_cli_specs import (
    build_external_cli_agent_specs,
    external_cli_enabled,
    validate_external_cli_runtime,
)


def _config(entry: dict) -> dict:
    return {"modes": {"team": {"jiuwen_team": {"external_cli_agents": [entry]}}}}


def _enabled_entry(**updates) -> dict:
    entry = {
        "cli_agent": "claude",
        "enabled": True,
        "command": None,
        "cwd": None,
        "inject_mcp": True,
        "mcp_server_command": ["openjiuwen-team-mcp"],
        "env": {},
    }
    entry.update(updates)
    return entry


def test_build_external_cli_spec_uses_project_dir_without_mutating_config(tmp_path: Path) -> None:
    config = _config(_enabled_entry(env={"CLAUDE_MODEL": "sonnet"}))
    original = deepcopy(config)

    specs = build_external_cli_agent_specs(
        config,
        mode="team",
        project_dir=str(tmp_path),
        team_workspace=None,
    )

    assert config == original
    assert len(specs) == 1
    assert specs[0].cli_agent == "claude"
    assert specs[0].cwd == str(tmp_path.resolve())
    assert specs[0].env == {"CLAUDE_MODEL": "sonnet"}


def test_disabled_external_cli_is_ignored(tmp_path: Path) -> None:
    config = _config(_enabled_entry(enabled=False))

    assert external_cli_enabled(config) is False
    assert build_external_cli_agent_specs(
        config,
        mode="team",
        project_dir=str(tmp_path),
        team_workspace=None,
    ) == []


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"cli_agent": "codex"}, "must be 'claude'"),
        ({"command": "claude"}, "command must be"),
        ({"env": {"TOKEN": 1}}, "string-to-string"),
        ({"inject_mcp": False}, "inject_mcp must be true"),
        ({"mcp_server_command": []}, "mcp_server_command must be"),
    ],
)
def test_invalid_external_cli_config_fails(tmp_path: Path, updates: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_external_cli_agent_specs(
            _config(_enabled_entry(**updates)),
            mode="team",
            project_dir=str(tmp_path),
            team_workspace=None,
        )


def test_explicit_cwd_must_exist_and_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        build_external_cli_agent_specs(
            _config(_enabled_entry(cwd="relative/project")),
            mode="team",
            project_dir=str(tmp_path),
            team_workspace=None,
        )
    with pytest.raises(ValueError, match="does not exist"):
        build_external_cli_agent_specs(
            _config(_enabled_entry(cwd=str(tmp_path / "missing"))),
            mode="team",
            project_dir=None,
            team_workspace=None,
        )


def test_external_cli_runtime_requires_cross_process_infra(monkeypatch) -> None:
    config = ExternalCliAgentSpec(cli_agent="claude")
    spec = SimpleNamespace(spawn_mode="inprocess", transport=None, storage=None)
    with pytest.raises(ValueError, match="spawn_mode='process'"):
        validate_external_cli_runtime(spec, [config])

    spec.spawn_mode = "process"
    with pytest.raises(ValueError, match="transport.type='pyzmq'"):
        validate_external_cli_runtime(spec, [config])

    spec.transport = SimpleNamespace(
        type="pyzmq",
        params={
            "direct_addr": "tcp://127.0.0.1:1",
            "pubsub_publish_addr": "tcp://127.0.0.1:2",
            "pubsub_subscribe_addr": "tcp://127.0.0.1:3",
            "metadata": {"pubsub_bind": True},
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.external_cli_specs.importlib.util.find_spec",
        lambda name: object(),
    )
    spec.storage = SimpleNamespace(type="memory", params={})
    with pytest.raises(ValueError, match="storage.type='sqlite'"):
        validate_external_cli_runtime(spec, [config])

    spec.storage = SimpleNamespace(type="sqlite", params={"connection_string": ":memory:"})
    with pytest.raises(ValueError, match="in-memory"):
        validate_external_cli_runtime(spec, [config])

    spec.storage = SimpleNamespace(type="sqlite", params={})
    with pytest.raises(ValueError, match="file-backed SQLite connection_string"):
        validate_external_cli_runtime(spec, [config])

    spec.storage = SimpleNamespace(type="sqlite", params={"connection_string": "team.db"})
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.external_cli_specs._executable_available",
        lambda command: True,
    )
    validate_external_cli_runtime(spec, [config])


def test_enabled_external_cli_adds_leader_routing_rail(tmp_path: Path) -> None:
    config = _config(_enabled_entry(cwd=str(tmp_path)))

    leader_rails, _ = build_member_capability_specs(config, "team", "leader")
    teammate_rails, _ = build_member_capability_specs(config, "team", "teammate")

    assert registry.EXTERNAL_CLI_ROUTING in {item.type for item in leader_rails}
    assert registry.EXTERNAL_CLI_ROUTING not in {item.type for item in teammate_rails}
