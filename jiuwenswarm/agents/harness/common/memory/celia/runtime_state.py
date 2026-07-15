"""Read-only MEMORYSTATE compatibility with OpenClaw."""

from __future__ import annotations

import os
from pathlib import Path


def _parse(value: str | None) -> bool:
    if value is None:
        return False
    value = value.strip().strip("\"'").lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return False


def _runtime_paths(explicit: str = "") -> list[Path]:
    paths: list[Path] = []
    if explicit:
        paths.append(Path(explicit).expanduser())
    config_dir = os.getenv("CELIA_CONFIG_DIR")
    if config_dir:
        paths.append(Path(config_dir).expanduser() / ".xiaoyiruntime")
    paths.append(Path.home() / ".openclaw" / ".xiaoyiruntime")
    return paths


def read_memory_state(explicit_path: str = "") -> bool:
    """Return MEMORYSTATE; missing or malformed state is closed by default."""
    for path in _runtime_paths(explicit_path):
        try:
            if not path.is_file():
                continue
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() == "MEMORYSTATE":
                    return _parse(value)
        except OSError:
            continue
    return _parse(os.getenv("MEMORYSTATE"))
