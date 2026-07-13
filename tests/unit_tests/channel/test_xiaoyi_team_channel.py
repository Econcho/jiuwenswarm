import time
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import (
    XiaoyiChannel,
    XiaoyiChannelConfig,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.formatter import (
    should_send_as_status_update,
)
from jiuwenswarm.gateway.routing.keys import XiaoyiDeliveryTarget
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget


def _channel(mode: str = "xiaoyi_claw") -> XiaoyiChannel:
    return XiaoyiChannel(
        XiaoyiChannelConfig(
            enabled=True,
            mode=mode,
            agent_id="xiaoyi-agent",
            uid="uid",
            api_key="api-key",
        ),
        RobotMessageRouter(),
    )


def _team_event(event_type: EventType, event: dict) -> Message:
    return Message(
        id="event-1",
        type="event",
        channel_id="xiaoyi",
        session_id="conversation-1",
        params={},
        payload={"event": event},
        timestamp=0.0,
        ok=True,
        event_type=event_type,
    )


def test_xiaoyi_claw_inbound_requests_are_pinned_to_team_mode() -> None:
    params = _channel()._build_inbound_params("修复除零异常", "task-1", {})

    assert params == {
        "query": "修复除零异常",
        "task_id": "task-1",
        "mode": "team",
    }


def test_regular_xiaoyi_inbound_request_keeps_existing_mode_selection() -> None:
    params = _channel("xiaoyi_channel")._build_inbound_params("你好", "task-1", {})

    assert "mode" not in params


def test_team_member_event_uses_claude_code_display_name() -> None:
    channel = _channel()
    content = channel._extract_team_content(
        _team_event(
            EventType.TEAM_MEMBER,
            {"type": "team.member.spawned", "member_id": "claude-coder"},
        )
    )

    assert EventType.TEAM_MEMBER in channel._TEAM_ALLOWED_EVENTS
    assert content == "[团队成员] Claude Code 已加入团队"


def test_team_task_event_exposes_title_assignee_and_not_unrelated_payload() -> None:
    channel = _channel()
    content = channel._extract_team_content(
        _team_event(
            EventType.TEAM_TASK,
            {
                "type": "team.task.claimed",
                "task_id": "task-1",
                "title": "修复 calculator 的除零异常",
                "assignee": "claude-coder",
                "env": {"API_KEY": "must-not-leak"},
            },
        )
    )

    assert EventType.TEAM_TASK in channel._TEAM_ALLOWED_EVENTS
    assert content == "[团队任务] 已认领任务：修复 calculator 的除零异常（执行者：Claude Code）"
    assert "must-not-leak" not in content


def test_team_message_redacts_credentials_before_delivery() -> None:
    content = _channel()._extract_team_content(
        _team_event(
            EventType.TEAM_MESSAGE,
            {
                "type": "team.message.p2p",
                "from_member": "claude-coder",
                "to_member": "leader",
                "content": "测试已通过；api_key=must-not-leak；Bearer abc123",
            },
        )
    )

    assert "Claude Code" in content
    assert "must-not-leak" not in content
    assert "abc123" not in content
    assert "[REDACTED]" in content


@pytest.mark.asyncio
async def test_team_member_event_is_forwarded_to_the_active_claw_session() -> None:
    channel = _channel()
    channel._active_push_sessions["user-1"] = ("ws-session", "task-1", "", time.time())
    channel._send_ws_to_user = AsyncMock()
    message = _team_event(
        EventType.TEAM_MEMBER,
        {"type": "team.member.spawned", "member_id": "claude-coder"},
    )
    target = RoutingTarget(
        intent="godview",
        delivery=XiaoyiDeliveryTarget(agent_id="user-1"),
    )

    await channel._send_team(message, target)

    channel._send_ws_to_user.assert_awaited_once_with(
        "ws-session", "task-1", message, "[团队成员] Claude Code 已加入团队"
    )


def test_processing_status_is_sent_as_a_status_update() -> None:
    assert should_send_as_status_update(EventType.CHAT_PROCESSING_STATUS)
