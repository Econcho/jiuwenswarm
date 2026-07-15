"""Unit coverage for the Jiuwen Celia Memory adapter."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.memory.celia.client import CeliaMcpClient
from jiuwenswarm.agents.harness.common.memory.celia import rail as celia_rail_module
from jiuwenswarm.agents.harness.common.memory.celia.config import (
    CeliaConfig,
    CeliaEndpointConfig,
)
from jiuwenswarm.agents.harness.common.memory.celia.provider import CeliaMemoryProvider
from jiuwenswarm.agents.harness.common.memory.celia.runtime_state import read_memory_state
from jiuwenswarm.agents.harness.common.memory.celia.runtime_store import CeliaRuntimeStore
from jiuwenswarm.agents.harness.common.memory.celia.sanitizer import clean_turn_events
from jiuwenswarm.agents.harness.common.memory.celia.session import CeliaSessionManager
from jiuwenswarm.agents.harness.common.memory.external_memory_config import get_external_memory_config
from jiuwenswarm.agents.harness.common.memory.external_memory_builder import build_external_memory_rail


def _config() -> CeliaConfig:
    return CeliaConfig(
        server_binary_path="/opt/celia_memory_mcp_server",
        db_path="/tmp/celia.db",
        log_path="/tmp/celia.log",
        tenant_id="tenant-a",
        user_id="user-a",
        scope_id="user",
        embed=CeliaEndpointConfig(
            base_url="https://embed.example",
            api_key="embed-secret",
            model="embed-model",
            headers={"x-api-key": "header-secret", "x-request-from": "jiuwen"},
        ),
        chat=CeliaEndpointConfig(
            base_url="https://chat.example",
            api_key="chat-secret",
            model="chat-model",
        ),
    )


def test_child_env_keeps_dedicated_headers_out_of_extra_headers():
    env = _config().child_env({})
    assert env["OPENAI_EMBED_API_KEY"] == "embed-secret"
    assert env["OPENAI_EMBED_HEADERS_JSON"] == '{"x-request-from":"jiuwen"}'
    assert "x-api-key" not in env["OPENAI_EMBED_HEADERS_JSON"]


def test_external_builder_dispatches_celia_provider(monkeypatch):
    config = {
        "memory": {
            "engine": "external",
            "external": {"provider": "CELIA", "celia": {"tenant_id": "t"}},
        }
    }
    assert get_external_memory_config(config)["provider"] == "celia"
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.memory.external_memory_builder._build_celia_rail",
        lambda config, ext_cfg, *, session_id: "celia-rail",
    )
    assert build_external_memory_rail(config, session_id="conversation-a") == "celia-rail"


def test_runtime_state_is_fail_closed(tmp_path, monkeypatch):
    runtime = tmp_path / ".xiaoyiruntime"
    runtime.write_text("MEMORYSTATE=1\n", encoding="utf-8")
    assert read_memory_state(str(runtime)) is True
    runtime.write_text("MEMORYSTATE=invalid\n", encoding="utf-8")
    assert read_memory_state(str(runtime)) is False
    monkeypatch.delenv("MEMORYSTATE", raising=False)
    assert read_memory_state(str(tmp_path / "missing")) is False


def test_runtime_store_limits_and_consumes_urgent():
    store = CeliaRuntimeStore()
    key = "tenant:user:conversation"
    for i in range(20):
        store.append_prompt(key, f"memory-{i}")
    assert len(store.prompt_values(key)) == 16
    store.mark_urgent(key)
    assert store.consume_urgent(key) is True
    assert store.consume_urgent(key) is False
    store.record_l1_paths(key, ["scene/a", "scene/b"])
    assert store.served_l1_paths(key) == ["scene/a", "scene/b"]


def test_rail_reads_typed_openjiuwen_model_response_and_sanitizes_ids():
    response = SimpleNamespace(
        content="assistant answer",
        reasoning_content="private reasoning",
        tool_calls=[
            {"id": "call-1", "name": "memory_record_search", "arguments": "{\"query\":\"x\"}"}
        ],
    )
    ctx = SimpleNamespace(inputs=SimpleNamespace(response=response))
    event = celia_rail_module.CeliaMemoryRail._model_event(ctx)
    assert event["text"] == "assistant answer"
    cleaned = clean_turn_events([event])
    assert cleaned[0]["toolCall"][0]["name"] == "memory_record_search"
    assert "id" not in cleaned[0]["toolCall"][0]


class _FakeClient:
    def __init__(self):
        self.calls = []
        self.restart_callbacks = []

    def add_restart_callback(self, callback):
        self.restart_callbacks.append(callback)

    async def call_tool(self, name, args, **kwargs):
        self.calls.append((name, args))
        await asyncio.sleep(0)
        if name == "memory_search_l2":
            return {"results": [{"id": "m-1", "score": 0.95, "content": "remembered"}]}
        if name == "memory_add":
            return {"status": 0}
        if name == "memory_open":
            return {"status": 0}
        return {"status": 0}

    async def load_l1_batch(self, *args, **kwargs):
        self.calls.append(("memory_load_l1", {"paths": args[0] if args else []}))
        return {"entries": []}


class _FakeSessions:
    async def ensure_tool_session(self, user_id):
        return f"tools-{user_id}"


@pytest.mark.asyncio
async def test_session_manager_deduplicates_concurrent_memory_open():
    client = _FakeClient()
    manager = CeliaSessionManager(client)
    original = client.call_tool
    open_count = 0

    async def counted(name, args, **kwargs):
        nonlocal open_count
        if name == "memory_open":
            open_count += 1
            await asyncio.sleep(0.01)
        return await original(name, args, **kwargs)

    client.call_tool = counted
    values = await asyncio.gather(
        manager.ensure_tool_session("alice"),
        manager.ensure_tool_session("alice"),
    )
    assert values == ["tools-alice", "tools-alice"]
    assert open_count == 1


@pytest.mark.asyncio
async def test_provider_maps_l2_and_urgent_memory_add(monkeypatch):
    monkeypatch.setenv("MEMORYSTATE", "1")
    provider = CeliaMemoryProvider(_config(), user_id="alice", scope_id="user", session_id="conversation-a")
    client = _FakeClient()
    provider._lease = SimpleNamespace(client=client, sessions=_FakeSessions())
    provider._initialized = True

    result = json.loads(await provider.handle_tool_call("memory_record_search", {"query": "where"}))
    assert result["result"][0]["id"] == "m-1"
    assert client.calls[0][0] == "memory_search_l2"
    assert client.calls[0][1]["sessionId"] == "tools-alice"

    await provider.handle_tool_call("memory_store", {"text": "keep this"})
    await provider.sync_turn("user question", "assistant answer")
    add_call = next(args for name, args in client.calls if name == "memory_add")
    assert add_call["ingestMode"] == "deferred-urgent"
    assert add_call["userId"] == "alice"


@pytest.mark.asyncio
async def test_provider_preserves_openclaw_memory_state_zero_write(monkeypatch):
    monkeypatch.setenv("MEMORYSTATE", "0")
    provider = CeliaMemoryProvider(_config(), user_id="alice", scope_id="user", session_id="conversation-a")
    client = _FakeClient()
    provider._lease = SimpleNamespace(client=client, sessions=_FakeSessions())
    provider._initialized = True

    disabled = await provider.handle_tool_call("memory_record_search", {"query": "where"})
    assert "memory_disabled" in disabled
    await provider.sync_turn("user question", "assistant answer")
    add_call = next(args for name, args in client.calls if name == "memory_add")
    assert add_call["memoryState"] == 0


@pytest.mark.asyncio
async def test_client_decodes_double_encoded_tool_payload(monkeypatch):
    client = CeliaMcpClient(_config())

    async def fake_start():
        return None

    async def fake_request(method, params, *, timeout, generation=None):
        assert method == "tools/call"
        return {"content": [{"text": json.dumps(json.dumps({"status": 0}))}]}

    monkeypatch.setattr(client, "start", fake_start)
    monkeypatch.setattr(client, "_request", fake_request)
    assert await client.call_tool("memory_flush", {}) == {"status": 0}
