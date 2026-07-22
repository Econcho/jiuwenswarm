from __future__ import annotations

import aiohttp
import pytest

from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils import (
    push as push_module,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.push import (
    PushConfig,
    XiaoYiPushService,
)


class _FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def text(self) -> str:
        return self._body


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    def post(self, url: str, **kwargs):
        return self._response


def _service() -> XiaoYiPushService:
    return XiaoYiPushService(
        PushConfig(
            mode="xiaoyi_claw",
            api_id="api-1",
            push_id="push-1",
            uid="uid-1",
            api_key="key-1",
        )
    )


@pytest.mark.asyncio
async def test_push_200_jsonrpc_error_is_not_accepted(monkeypatch) -> None:
    monkeypatch.setattr(
        push_module.aiohttp,
        "ClientSession",
        lambda: _FakeSession(_FakeResponse(200, '{"error":{"code":1001,"message":"invalid push"}}')),
    )

    result = await _service().send_push("摘要", "正文", notification_id="notification-1")

    assert result.accepted is False
    assert result.http_status == 200
    assert result.error_code == "1001"
    assert result.retryable is False


@pytest.mark.asyncio
async def test_push_empty_2xx_response_is_accepted(monkeypatch) -> None:
    monkeypatch.setattr(
        push_module.aiohttp,
        "ClientSession",
        lambda: _FakeSession(_FakeResponse(204, "")),
    )

    result = await _service().send_push("摘要", "正文")

    assert result.accepted is True
    assert result.response_body == ""


@pytest.mark.asyncio
async def test_push_429_and_5xx_are_retryable(monkeypatch) -> None:
    monkeypatch.setattr(
        push_module.aiohttp,
        "ClientSession",
        lambda: _FakeSession(_FakeResponse(503, "temporarily unavailable")),
    )
    server_result = await _service().send_push("摘要", "正文")

    monkeypatch.setattr(
        push_module.aiohttp,
        "ClientSession",
        lambda: _FakeSession(_FakeResponse(429, "rate limited")),
    )
    rate_limit_result = await _service().send_push("摘要", "正文")

    assert server_result.accepted is False
    assert server_result.retryable is True
    assert rate_limit_result.accepted is False
    assert rate_limit_result.retryable is True


@pytest.mark.asyncio
async def test_push_4xx_is_not_retryable(monkeypatch) -> None:
    monkeypatch.setattr(
        push_module.aiohttp,
        "ClientSession",
        lambda: _FakeSession(_FakeResponse(400, "invalid request")),
    )

    result = await _service().send_push("摘要", "正文")

    assert result.accepted is False
    assert result.retryable is False


@pytest.mark.asyncio
async def test_push_network_error_is_retryable(monkeypatch) -> None:
    class _FailingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def post(self, url: str, **kwargs):
            raise aiohttp.ClientError("network down")

    monkeypatch.setattr(push_module.aiohttp, "ClientSession", _FailingSession)

    result = await _service().send_push("摘要", "正文")

    assert result.accepted is False
    assert result.error_code == "network_error"
    assert result.retryable is True
