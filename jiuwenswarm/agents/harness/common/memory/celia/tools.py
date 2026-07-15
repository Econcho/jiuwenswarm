"""Model-facing Celia tool schemas and disabled payloads."""

from __future__ import annotations

from typing import Any

CORE_TOOLS = {
    "memory_store",
    "memory_forget",
    "memory_scene_load",
    "memory_record_search",
    "memory_chat_history_search",
    "memory_scene_list_load",
    "memory_get_global_summary",
    "memory_flush",
    "memory_list",
}

ADVANCED_TOOLS = {
    "memory_dump",
    "dream_status",
    "dream_run_summary",
    "dream_recent_runs",
}


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


def tool_schemas(advanced: set[str] | None = None) -> list[dict[str, Any]]:
    schemas = [
        _schema("memory_store", "Record durable user memory for the current conversation.", {"text": {"type": "string"}}, ["text"]),
        _schema("memory_forget", "Forget a specific memory or find candidate memories to forget.", {"memoryId": {"type": "string"}, "query": {"type": "string"}}),
        _schema("memory_scene_load", "Load up to five Celia memory scenes by path.", {"paths": {"type": "array", "items": {"type": "string"}, "maxItems": 5}}, ["paths"]),
        _schema("memory_record_search", "Search atomic long-term memories.", {"query": {"type": "string"}, "top_k": {"type": "integer"}, "is_procedural": {"type": "boolean"}, "time_hint": {"type": "string"}, "dedup_policy": {"type": "string"}}, ["query"]),
        _schema("memory_chat_history_search", "Search raw historical conversation memory.", {"query": {"type": "string"}, "top_k": {"type": "integer"}, "sessionIdFilter": {"type": "string"}}, ["query"]),
        _schema("memory_scene_list_load", "List the available Celia memory scenes.", {}),
        _schema("memory_get_global_summary", "Get the user's Celia global memory summary.", {"tier": {"type": ["integer", "string"]}}),
        _schema("memory_flush", "Flush pending Celia memory ingestion.", {"timeoutMs": {"type": "integer"}}),
        _schema("memory_list", "List memories by Celia semantic layer.", {"categories": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer"}, "offset": {"type": "integer"}}),
    ]
    advanced = advanced or set()
    if "memory_dump" in advanced:
        schemas.append(_schema("memory_dump", "Dump Celia memory data for diagnostics.", {"category": {"type": "string"}}))
    if "dream_status" in advanced:
        schemas.append(_schema("dream_status", "Get Celia dream status.", {}))
    if "dream_run_summary" in advanced:
        schemas.append(_schema("dream_run_summary", "Get a Celia dream run summary.", {"runId": {"type": "string"}}))
    if "dream_recent_runs" in advanced:
        schemas.append(_schema("dream_recent_runs", "List recent Celia dream runs.", {"limit": {"type": "integer"}}))
    return schemas


def disabled_payload(tool_name: str) -> str:
    return '{"ok":false,"status":"memory_disabled","tool":"' + tool_name + '"}'
