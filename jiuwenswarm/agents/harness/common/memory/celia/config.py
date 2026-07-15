"""Celia configuration and OpenClaw-compatible child environment mapping."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..config import get_embed_config


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    value = str(value).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _first(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


@dataclass(frozen=True)
class CeliaEndpointConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    uid: str = ""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CeliaConfig:
    server_binary_path: str
    db_path: str
    log_path: str
    tenant_id: str
    user_id: str
    scope_id: str
    vector_dim: int | None = None
    embed: CeliaEndpointConfig = field(default_factory=CeliaEndpointConfig)
    chat: CeliaEndpointConfig = field(default_factory=CeliaEndpointConfig)
    rerank: CeliaEndpointConfig = field(default_factory=CeliaEndpointConfig)
    procedural_dir: str = ""
    procedural_learn_debug: bool = False
    dreaming_enabled: bool = False
    startup_timeout: float = 20.0
    request_timeout: float = 10.0
    flush_timeout: float = 120.0
    fail_open: bool = True
    runtime_state_path: str = ""

    @property
    def normalized_binary_path(self) -> str:
        path = Path(self.server_binary_path).expanduser()
        if path.parent == Path(".") and shutil.which(str(path)):
            return str(path)
        return str(path.absolute())

    @property
    def normalized_db_path(self) -> str:
        return str(Path(self.db_path).expanduser().absolute())

    @property
    def fingerprint(self) -> tuple[str, ...]:
        def secret(value: str) -> str:
            return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""

        def headers(value: Mapping[str, str]) -> str:
            encoded = json.dumps(dict(value), sort_keys=True, ensure_ascii=False)
            return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

        return (
            self.normalized_binary_path,
            self.normalized_db_path,
            self.tenant_id,
            self.embed.base_url,
            secret(self.embed.api_key),
            self.embed.model,
            headers(self.embed.headers),
            self.chat.base_url,
            secret(self.chat.api_key),
            self.chat.model,
            headers(self.chat.headers),
            self.rerank.base_url,
            secret(self.rerank.api_key),
            self.rerank.model,
            headers(self.rerank.headers),
        )

    @property
    def db_identity(self) -> str:
        return self.normalized_db_path

    def is_available(self) -> bool:
        """Perform only local static checks; never start a process or call a network."""
        if platform.system().lower() != "linux":
            return False
        if platform.machine().lower() not in {"aarch64", "arm64"}:
            return False
        binary = Path(self.normalized_binary_path)
        if not binary.is_file() or not os.access(binary, os.X_OK):
            return False
        db = Path(self.normalized_db_path)
        parent = db.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        return parent.exists() and os.access(parent, os.W_OK)

    def child_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ if base is None else base)

        def put(name: str, value: Any) -> None:
            value = _text(value)
            if value:
                env[name] = value

        def put_endpoint(prefix: str, endpoint: CeliaEndpointConfig) -> None:
            put(f"OPENAI_{prefix}_BASE_URL", endpoint.base_url)
            put(f"OPENAI_{prefix}_API_KEY", endpoint.api_key)
            put(f"OPENAI_{prefix}_MODEL", endpoint.model)
            headers = {
                str(k): str(v)
                for k, v in endpoint.headers.items()
                if str(k).lower() not in {"x-api-key", "x-uid"}
            }
            if headers:
                env[f"OPENAI_{prefix}_HEADERS_JSON"] = json.dumps(
                    headers, ensure_ascii=False, separators=(",", ":")
                )

        put_endpoint("EMBED", self.embed)
        put_endpoint("CHAT", self.chat)
        put_endpoint("RERANK", self.rerank)
        put("CELIA_EMBED_UID", self.embed.uid)
        put("CELIA_CHAT_UID", self.chat.uid)
        put("CELIA_TENANT_ID", self.tenant_id)
        put("CELIA_VECTOR_DIM", self.vector_dim)
        put("CELIA_PROCEDURAL_DIR", self.procedural_dir)
        put("CELIA_PROCEDURAL_LEARN_DEBUG", str(self.procedural_learn_debug).lower())
        put("CELIA_DREAMING_ENABLED", str(self.dreaming_enabled).lower())
        return env


def _model_defaults(config: Mapping[str, Any]) -> dict[str, Any]:
    models = config.get("models") if isinstance(config, Mapping) else None
    defaults = models.get("defaults") if isinstance(models, Mapping) else None
    if not isinstance(defaults, list):
        return {}
    for item in defaults:
        if isinstance(item, Mapping) and item.get("is_default"):
            return _mapping(item.get("model_client_config"))
    first = defaults[0] if defaults else {}
    return _mapping(first.get("model_client_config")) if isinstance(first, Mapping) else {}


def _endpoint(
    section: Mapping[str, Any],
    *,
    fallback: Mapping[str, Any] | None = None,
    env_prefix: str,
    uid_env: str | None = None,
    uid_fallback: Any = None,
) -> CeliaEndpointConfig:
    fallback = fallback or {}
    headers = _mapping(section.get("headers"))
    if not headers:
        headers = _mapping(section.get("custom_headers"))
    if not headers:
        headers = _mapping(fallback.get("headers"))
    if not headers:
        headers = _mapping(fallback.get("custom_headers"))
    return CeliaEndpointConfig(
        base_url=_first(
            section.get("base_url"),
            section.get("api_base"),
            fallback.get("base_url"),
            fallback.get("api_base"),
            os.getenv(f"OPENAI_{env_prefix}_BASE_URL"),
        ),
        api_key=_first(
            section.get("api_key"),
            fallback.get("api_key"),
            os.getenv(f"OPENAI_{env_prefix}_API_KEY"),
        ),
        model=_first(
            section.get("model"),
            section.get("model_name"),
            fallback.get("model"),
            fallback.get("model_name"),
            os.getenv(f"OPENAI_{env_prefix}_MODEL"),
        ),
        uid=_first(section.get("uid"), os.getenv(uid_env or ""), uid_fallback),
        headers={str(k): str(v) for k, v in headers.items()},
    )


def build_celia_config(config: Mapping[str, Any], ext_cfg: Mapping[str, Any]) -> CeliaConfig:
    section = _mapping(ext_cfg.get("celia"))
    embed_section = _mapping(section.get("embed"))
    chat_section = _mapping(section.get("chat"))
    rerank_section = _mapping(section.get("rerank"))

    embed = get_embed_config() or {}
    top_embed = _mapping(config.get("embed"))
    embed_fallback = {
        "api_key": _first(embed.get("api_key"), top_embed.get("embed_api_key")),
        "base_url": _first(embed.get("base_url"), top_embed.get("embed_base_url")),
        "model": _first(embed.get("model"), top_embed.get("embed_model")),
    }
    chat_fallback = _model_defaults(config)
    service_url = _text(os.getenv("SERVICE_URL"))
    if service_url and not chat_section.get("base_url") and not chat_fallback.get("api_base"):
        chat_fallback = dict(chat_fallback)
        chat_fallback["api_base"] = service_url.rstrip("/") + "/celia-claw/v1/sse-api"
    if not chat_section.get("api_key") and not chat_fallback.get("api_key"):
        chat_fallback = dict(chat_fallback)
        chat_fallback["api_key"] = _text(os.getenv("PERSONAL_API_KEY"))

    default_db = Path.home() / ".jiuwenswarm" / "memory" / "celia_memory.db"
    return CeliaConfig(
        server_binary_path=_first(
            section.get("server_binary_path"),
            section.get("binary_path"),
            os.getenv("CELIA_MEMORY_BINARY_PATH"),
        ),
        db_path=_first(section.get("db_path"), os.getenv("CELIA_MEMORY_DB_PATH"), default_db),
        log_path=_first(section.get("log_path"), os.getenv("CELIA_MEMORY_LOG_PATH")),
        tenant_id=_first(
            section.get("tenant_id"),
            os.getenv("CELIA_TENANT_ID"),
            os.getenv("CELIA_MEMORY_TENANT_ID"),
            "default",
        ),
        user_id=_first(ext_cfg.get("user_id"), os.getenv("MEMORY_USER_ID"), "openclaw-user"),
        scope_id=_first(ext_cfg.get("scope_id"), "user"),
        vector_dim=_integer(_first(section.get("vector_dim"), os.getenv("CELIA_VECTOR_DIM"))),
        embed=_endpoint(embed_section, fallback=embed_fallback, env_prefix="EMBED", uid_env="CELIA_EMBED_UID"),
        chat=_endpoint(
            chat_section,
            fallback=chat_fallback,
            env_prefix="CHAT",
            uid_env="CELIA_CHAT_UID",
            uid_fallback=os.getenv("PERSONAL_UID"),
        ),
        rerank=_endpoint(rerank_section, env_prefix="RERANK"),
        procedural_dir=_first(section.get("procedural_dir"), os.getenv("CELIA_PROCEDURAL_DIR")),
        procedural_learn_debug=_bool(section.get("procedural_learn_debug"), False),
        dreaming_enabled=_bool(section.get("dreaming_enabled"), False),
        startup_timeout=_number(section.get("startup_timeout"), 20.0),
        request_timeout=_number(section.get("request_timeout"), 10.0),
        flush_timeout=_number(section.get("flush_timeout"), 120.0),
        fail_open=_bool(section.get("fail_open"), True),
        runtime_state_path=_first(
            section.get("runtime_state_path"),
            os.getenv("CELIA_XIAOYI_RUNTIME_PATH"),
        ),
    )
