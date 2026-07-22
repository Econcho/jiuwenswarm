import asyncio
import json
import time
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import (
    XiaoyiChannel,
    XiaoyiChannelConfig,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi import (
    xiaoyi_connect as connect_module,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.push import (
    PushDeliveryResult,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.formatter import (
    build_status_update_response,
    should_send_as_status_update,
)


def _build_channel(
    *,
    enable_streaming: bool = True,
    config: XiaoyiChannelConfig | None = None,
    with_ws: bool = True,
) -> tuple[XiaoyiChannel, list[dict[str, Any]]]:
    channel = XiaoyiChannel(
        config or XiaoyiChannelConfig(
            agent_id="agent-1",
            enable_streaming=enable_streaming,
        ),
        RobotMessageRouter(),
    )
    sent: list[dict[str, Any]] = []

    async def fake_safe_ws_send(url_key: str, payload: dict[str, Any]) -> None:
        assert url_key == "ws_url1"
        sent.append(payload)

    if with_ws:
        channel._ws_connections = {"ws_url1": object()}
    channel._safe_ws_send = fake_safe_ws_send
    return channel, sent


def _message(
    event_type: EventType,
    payload: dict[str, Any],
    *,
    session_id: str = "xiaoyi-session-1",
    task_id: str = "xiaoyi-task-1",
) -> Message:
    return Message(
        id="request-1",
        type="event",
        channel_id="xiaoyi",
        session_id="jiuwen-session-1",
        params={},
        timestamp=time.time(),
        ok=True,
        payload=payload,
        event_type=event_type,
        metadata={
            "xiaoyi_session_id": session_id,
            "xiaoyi_task_id": task_id,
        },
    )


def _result(wrapper: dict[str, Any]) -> dict[str, Any]:
    return json.loads(wrapper["msgDetail"])["result"]


def test_processing_status_is_a_status_update() -> None:
    assert should_send_as_status_update(EventType.CHAT_PROCESSING_STATUS)
    assert build_status_update_response("task-1", "working", "working")["final"] is False
    assert build_status_update_response("task-1", "done", "completed")["final"] is True
    assert build_status_update_response("task-1", "failed", "failed")["final"] is True
    assert build_status_update_response("task-1", "cancelled", "canceled")["final"] is True


@pytest.mark.asyncio
async def test_streaming_final_text_is_sent_as_terminal_artifact() -> None:
    channel, sent = _build_channel()
    summary = "抖音已经帮你打开了"

    await channel.send(
        _message(
            EventType.CHAT_FINAL,
            {"event_type": "chat.final", "content": summary},
        )
    )
    await channel.send(
        _message(
            EventType.CHAT_PROCESSING_STATUS,
            {
                "event_type": "chat.processing_status",
                "is_processing": False,
                "is_complete": True,
            },
        )
    )

    assert len(sent) == 1
    artifact = _result(sent[0])
    assert artifact["kind"] == "artifact-update"
    assert artifact["append"] is False
    assert artifact["lastChunk"] is True
    assert artifact["final"] is True
    assert artifact["artifact"]["parts"] == [{"kind": "text", "text": summary}]


@pytest.mark.asyncio
async def test_processing_started_is_non_terminal_status_update() -> None:
    channel, sent = _build_channel()

    await channel.send(
        _message(
            EventType.CHAT_PROCESSING_STATUS,
            {
                "event_type": "chat.processing_status",
                "is_processing": True,
                "is_complete": False,
            },
        )
    )

    assert len(sent) == 1
    status = _result(sent[0])
    assert status["kind"] == "status-update"
    assert status["status"]["state"] == "working"
    assert status["final"] is False


@pytest.mark.asyncio
async def test_completed_status_is_terminal_without_final_text() -> None:
    channel, sent = _build_channel()

    await channel.send(
        _message(
            EventType.CHAT_PROCESSING_STATUS,
            {
                "event_type": "chat.processing_status",
                "is_processing": False,
                "is_complete": True,
            },
        )
    )

    assert len(sent) == 1
    status = _result(sent[0])
    assert status["kind"] == "status-update"
    assert status["status"]["state"] == "completed"
    assert status["final"] is True


@pytest.mark.asyncio
async def test_completed_final_pushes_without_timeout_or_websocket() -> None:
    config = XiaoyiChannelConfig(
        agent_id="agent-1",
        mode="xiaoyi_claw",
        api_id="api-1",
        uid="uid-1",
        api_key="key-1",
        push_on_task_complete=True,
    )
    channel, _ = _build_channel(config=config, with_ws=False)
    channel._get_or_create_task_delivery_state(
        "xiaoyi-session-1",
        "xiaoyi-task-1",
        push_id="push-1",
    )
    channel._mark_session_active("xiaoyi-session-1", "xiaoyi-task-1")
    calls: list[dict[str, Any]] = []

    async def fake_push(text: str, push_text: str, **kwargs: Any) -> PushDeliveryResult:
        calls.append({"text": text, "push_text": push_text, **kwargs})
        return PushDeliveryResult(True, 200, trace_id="trace-1")

    channel._send_push_notification = fake_push

    await channel.send(
        _message(
            EventType.CHAT_FINAL,
            {"event_type": "chat.final", "content": "任务结果"},
        )
    )

    assert calls == [
        {
            "text": "任务结果",
            "push_text": "后台任务已完成：任务结果",
            "push_id": "push-1",
            "notification_id": calls[0]["notification_id"],
            "state": calls[0]["state"],
        }
    ]
    assert calls[0]["state"].push_attempted is True
    assert channel._task_delivery_states == {}


@pytest.mark.asyncio
async def test_status_then_final_uses_final_text_and_pushes_once() -> None:
    config = XiaoyiChannelConfig(
        agent_id="agent-1",
        mode="xiaoyi_claw",
        api_id="api-1",
        uid="uid-1",
        api_key="key-1",
        push_on_task_complete=True,
    )
    channel, sent = _build_channel(config=config)
    channel._get_or_create_task_delivery_state(
        "xiaoyi-session-1",
        "xiaoyi-task-1",
        push_id="push-1",
    )
    channel._mark_session_active("xiaoyi-session-1", "xiaoyi-task-1")
    calls: list[str] = []

    async def fake_push(text: str, push_text: str, **kwargs: Any) -> PushDeliveryResult:
        calls.append(text)
        return PushDeliveryResult(True, 200)

    channel._send_push_notification = fake_push
    status_task = asyncio.create_task(
        channel.send(
            _message(
                EventType.CHAT_PROCESSING_STATUS,
                {"event_type": "chat.processing_status", "is_processing": False, "is_complete": True},
            )
        )
    )
    await asyncio.sleep(0)
    await channel.send(
        _message(EventType.CHAT_FINAL, {"event_type": "chat.final", "content": "最终正文"})
    )
    await status_task

    assert calls == ["最终正文"]
    assert len(sent) == 1
    assert _result(sent[0])["artifact"]["parts"] == [{"kind": "text", "text": "最终正文"}]


@pytest.mark.asyncio
async def test_task_push_ids_are_isolated_for_concurrent_tasks() -> None:
    config = XiaoyiChannelConfig(
        agent_id="agent-1",
        mode="xiaoyi_claw",
        api_id="api-1",
        uid="uid-1",
        api_key="key-1",
        push_on_task_complete=True,
    )
    channel, _ = _build_channel(config=config, with_ws=False)
    channel._get_or_create_task_delivery_state("session", "task-a", push_id="push-a")
    channel._get_or_create_task_delivery_state("session", "task-b", push_id="push-b")
    channel._mark_session_active("session", "task-a")
    channel._mark_session_active("session", "task-b")
    calls: list[tuple[str, str]] = []

    async def fake_push(text: str, push_text: str, **kwargs: Any) -> PushDeliveryResult:
        calls.append((kwargs["push_id"], text))
        return PushDeliveryResult(True, 200)

    channel._send_push_notification = fake_push
    await channel.send(_message(EventType.CHAT_FINAL, {"content": "结果 A"}, session_id="session", task_id="task-a"))
    await channel.send(_message(EventType.CHAT_FINAL, {"content": "结果 B"}, session_id="session", task_id="task-b"))

    assert calls == [("push-a", "结果 A"), ("push-b", "结果 B")]


@pytest.mark.asyncio
async def test_incremental_and_cumulative_text_build_the_push_summary() -> None:
    config = XiaoyiChannelConfig(
        agent_id="agent-1",
        mode="xiaoyi_claw",
        api_id="api-1",
        uid="uid-1",
        api_key="key-1",
        push_on_task_complete=True,
    )
    channel, _ = _build_channel(config=config, with_ws=False)
    channel._get_or_create_task_delivery_state("session", "task", push_id="push-1")
    channel._mark_session_active("session", "task")
    calls: list[str] = []

    async def fake_push(text: str, push_text: str, **kwargs: Any) -> PushDeliveryResult:
        calls.append(text)
        return PushDeliveryResult(True, 200)

    channel._send_push_notification = fake_push
    await channel.send(_message(EventType.CHAT_DELTA, {"content": "hello "}, session_id="session", task_id="task"))
    await channel.send(_message(EventType.CHAT_FINAL, {"content": "hello world"}, session_id="session", task_id="task"))

    assert calls == ["hello world"]


@pytest.mark.asyncio
async def test_transient_push_retries_reuse_the_notification_id(monkeypatch) -> None:
    config = XiaoyiChannelConfig(
        agent_id="agent-1",
        mode="xiaoyi_claw",
        api_id="api-1",
        uid="uid-1",
        api_key="key-1",
    )
    channel, _ = _build_channel(config=config, with_ws=False)
    state = channel._get_or_create_task_delivery_state("session", "task", push_id="push-1")
    calls: list[str] = []
    results = [
        PushDeliveryResult(False, 503, retryable=True),
        PushDeliveryResult(False, 429, retryable=True),
        PushDeliveryResult(True, 200),
    ]

    class FakePushService:
        def __init__(self, config: Any) -> None:
            pass

        async def send_push(self, text: str, push_text: str, *, notification_id: str) -> PushDeliveryResult:
            calls.append(notification_id)
            return results.pop(0)

    delays: list[int] = []

    async def fake_sleep(seconds: int) -> None:
        delays.append(seconds)

    monkeypatch.setattr(connect_module, "XiaoYiPushService", FakePushService)
    monkeypatch.setattr(connect_module.asyncio, "sleep", fake_sleep)

    result = await channel._send_push_notification(
        "摘要",
        "正文",
        push_id="push-1",
        notification_id="stable-id",
        state=state,
    )

    assert result.accepted is True
    assert calls == ["stable-id", "stable-id", "stable-id"]
    assert delays == [1, 5]
    assert state.push_attempts == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    [
        EventType.CHAT_USAGE_METADATA,
        EventType.CHAT_USAGE_SUMMARY,
        EventType.CONTEXT_USAGE,
    ],
)
async def test_internal_events_do_not_create_blank_artifacts(
    event_type: EventType,
) -> None:
    channel, sent = _build_channel()

    await channel.send(
        _message(
            event_type,
            {"event_type": event_type.value, "usage": {"total_tokens": 10}},
        )
    )

    assert sent == []


@pytest.mark.asyncio
async def test_non_streaming_final_text_remains_terminal_artifact() -> None:
    channel, sent = _build_channel(enable_streaming=False)

    await channel.send(
        _message(
            EventType.CHAT_FINAL,
            {"event_type": "chat.final", "content": "已完成"},
        )
    )

    assert len(sent) == 1
    artifact = _result(sent[0])
    assert artifact["kind"] == "artifact-update"
    assert artifact["append"] is False
    assert artifact["lastChunk"] is True
    assert artifact["final"] is True


@pytest.mark.asyncio
async def test_direct_gui_response_is_not_forwarded_as_user_message() -> None:
    channel, _ = _build_channel()
    gui_events: list[dict[str, Any]] = []
    user_messages: list[Message] = []
    channel.register_gui_agent_handler(gui_events.append)
    channel.on_message(user_messages.append)
    raw = {
        "jsonrpc": "2.0",
        "id": "xiaoyi-task-1",
        "method": "message/stream",
        "sessionId": "xiaoyi-session-1",
        "params": {
            "id": "xiaoyi-task-1",
            "sessionId": "params-session-1",
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": "xiaoyi-task-1",
                "parts": [
                    {
                        "kind": "data",
                        "data": {
                            "events": [
                                {
                                    "header": {
                                        "namespace": "ClawAgent",
                                        "name": "InvokeJarvisGUIAgentResponse",
                                    },
                                    "payload": {
                                        "isFinal": True,
                                        "streamInfo": {
                                            "streamContent": "layout analysis failed"
                                        },
                                    },
                                }
                            ]
                        },
                    }
                ],
            },
        },
    }

    await channel._handle_raw_message(json.dumps(raw))

    assert len(gui_events) == 1
    assert user_messages == []
