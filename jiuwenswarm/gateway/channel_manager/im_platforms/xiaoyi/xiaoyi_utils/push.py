# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""XiaoYi Push Message Service - 主动推送消息服务."""

import logging
import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

import aiohttp


logger = logging.getLogger(__name__)

PUSH_URL = "https://hag.cloud.huawei.com/open-ability-agent/v1/agent-webhook"


@dataclass
class PushConfig:
    """Push 消息配置."""
    mode: str = ""
    api_id: str = ""
    push_id: str = ""
    ak: str = ""
    sk: str = ""
    uid: str = ""
    api_key: str = ""
    push_url: str = ""


@dataclass(frozen=True)
class PushDeliveryResult:
    """Result of one HAG Push HTTP request."""

    accepted: bool
    http_status: int | None
    response_body: str = ""
    trace_id: str = ""
    error_code: str = ""
    error_message: str = ""
    retryable: bool = False


def _mask(value: str, *, visible: int = 4) -> str:
    """Return a stable, non-sensitive representation for logs."""

    value = str(value or "")
    if not value:
        return "<empty>"
    if len(value) <= visible:
        return "*" * len(value)
    return f"{value[:visible]}***"


def _response_summary(body: str, *, limit: int = 512) -> str:
    """Keep diagnostic response logging bounded and free of common credentials."""

    body = str(body or "").strip()
    if not body:
        return ""
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return f"<non-json response length={len(body)}>"

    sensitive_keys = {"apikey", "pushid", "uid", "token", "authorization"}

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: _mask(str(item))
                if str(key).replace("_", "").lower() in sensitive_keys
                else redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    summary = json.dumps(redact(parsed), ensure_ascii=False, separators=(",", ":"))
    return summary if len(summary) <= limit else f"{summary[:limit]}..."


class XiaoYiPushService:
    """
    华为小艺主动推送服务.
    通过 HTTP Webhook API 向用户设备发送推送通知.
    """
    def __init__(self, config: PushConfig):
        self.config = config

    @staticmethod
    def _generate_uuid() -> str:
        """生成 UUID."""
        return str(uuid.uuid4())

    def _generate_signature(self, timestamp: str) -> str:
        """生成 HMAC-SHA256 签名 (Base64 编码)."""
        h = hmac.new(
            self.config.sk.encode("utf-8"),
            timestamp.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(h.digest()).decode("utf-8")

    async def send_push(
        self,
        text: str,
        push_text: str,
        *,
        notification_id: str | None = None,
    ) -> PushDeliveryResult:
        """
        发送推送通知.

        Args:
            text: 摘要文本 (如前30个字符)
            push_text: 推送通知文本 (如"任务已完成：xxx...")

        Returns:
            Structured delivery result for one HTTP request.
        """

        trace_id = self._generate_uuid()
        message_id = notification_id or self._generate_uuid()
        try:
            timestamp = str(int(time.time() * 1000))

            payload = {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "id": self._generate_uuid(),
                    "apiId": self.config.api_id,
                    "pushId": self.config.push_id,
                    "pushText": text,
                    "kind": "task",
                    "artifacts": [{
                        "artifactId": self._generate_uuid(),
                        "parts": [{
                            "kind": "text",
                            "text": push_text,
                        }]
                    }],
                    "status": {"state": "completed"}
                }
            }

            logger.info(
                "[PUSH_REQUEST] endpoint=%s api_id=%s push_id=%s trace_id=%s "
                "notification_id=%s summary_length=%s content_length=%s",
                self.config.push_url or PUSH_URL,
                self.config.api_id,
                _mask(self.config.push_id),
                trace_id,
                message_id,
                len(text),
                len(push_text),
            )
            if self.config.mode == "xiaoyi_claw":
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "x-hag-trace-id": trace_id,
                    "x-uid": self.config.uid,
                    "x-api-key": self.config.api_key,
                    "x-request-from": "openclaw"
                } 
            else:
                signature = self._generate_signature(timestamp)
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "x-hag-trace-id": trace_id,
                    "X-Access-Key": self.config.ak,
                    "X-Sign": signature,
                    "X-Ts": timestamp,
                }
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.config.push_url or PUSH_URL,
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                ) as response:
                    response_text = await response.text()
                    summary = _response_summary(response_text)
                    error_code = ""
                    error_message = ""
                    try:
                        response_json = json.loads(response_text) if response_text.strip() else None
                    except json.JSONDecodeError:
                        response_json = None

                    if isinstance(response_json, dict):
                        rpc_error = response_json.get("error")
                        if isinstance(rpc_error, dict):
                            error_code = str(rpc_error.get("code") or "jsonrpc_error")
                            error_message = str(rpc_error.get("message") or "JSON-RPC error")
                        elif rpc_error:
                            error_code = "jsonrpc_error"
                            error_message = str(rpc_error)
                        else:
                            business_code = response_json.get("code")
                            if business_code is not None and str(business_code).lower() not in {
                                "0", "200", "success", "ok",
                            }:
                                error_code = str(business_code)
                                error_message = str(
                                    response_json.get("message")
                                    or response_json.get("desc")
                                    or "Push business error"
                                )

                    accepted = 200 <= response.status < 300 and not error_code
                    retryable = response.status == 429 or response.status >= 500
                    logger.info(
                        "[PUSH_RESPONSE] trace_id=%s notification_id=%s http_status=%s "
                        "accepted=%s retryable=%s error_code=%s error_message=%s body=%s",
                        trace_id,
                        message_id,
                        response.status,
                        accepted,
                        retryable,
                        error_code or "<none>",
                        error_message or "<none>",
                        summary or "<empty>",
                    )
                    return PushDeliveryResult(
                        accepted=accepted,
                        http_status=response.status,
                        response_body=summary,
                        trace_id=trace_id,
                        error_code=error_code,
                        error_message=error_message,
                        retryable=retryable,
                    )

        except aiohttp.ClientError as e:
            logger.warning(
                "[PUSH_RESPONSE] trace_id=%s notification_id=%s network_error=%s",
                trace_id,
                message_id,
                type(e).__name__,
            )
            return PushDeliveryResult(
                accepted=False,
                http_status=None,
                trace_id=trace_id,
                error_code="network_error",
                error_message=str(e),
                retryable=True,
            )
        except TimeoutError as e:
            logger.warning(
                "[PUSH_RESPONSE] trace_id=%s notification_id=%s timeout",
                trace_id,
                message_id,
            )
            return PushDeliveryResult(
                accepted=False,
                http_status=None,
                trace_id=trace_id,
                error_code="timeout",
                error_message=str(e),
                retryable=True,
            )
        except Exception as e:
            logger.exception(
                "[PUSH_RESPONSE] trace_id=%s notification_id=%s unexpected_error=%s",
                trace_id,
                message_id,
                type(e).__name__,
            )
            return PushDeliveryResult(
                accepted=False,
                http_status=None,
                trace_id=trace_id,
                error_code="unexpected_error",
                error_message=str(e),
                retryable=False,
            )

    async def send_push_with_directives(
        self,
        push_id: str,
        session_id: str,
        directives: list[dict[str, Any]],
    ) -> bool:
        try:
            timestamp = str(int(time.time() * 1000))
            payload = {
                "jsonrpc": "2.0",
                "id": self._generate_uuid(),
                "result": {
                    "id": self._generate_uuid(),
                    "apiId": self.config.api_id,
                    "pushId": push_id,
                    "pushText": "",
                    "pushType": 101,
                    "kind": "task",
                    "sessionId": session_id,
                    "artifacts": [
                        {
                            "artifactId": self._generate_uuid(),
                            "parts": [
                                {
                                    "kind": "data",
                                    "data": {"directives": directives},
                                }
                            ],
                        }
                    ],
                },
            }
            if self.config.mode == "xiaoyi_claw":
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "x-hag-trace-id": self._generate_uuid(),
                    "x-uid": self.config.uid,
                    "x-api-key": self.config.api_key,
                    "x-request-from": "openclaw",
                }
            else:
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "x-hag-trace-id": self._generate_uuid(),
                    "X-Access-Key": self.config.ak,
                    "X-Sign": self._generate_signature(timestamp),
                    "X-Ts": timestamp,
                }

            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.config.push_url or PUSH_URL,
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                ) as response:
                    if response.status == 200:
                        logger.info("[PUSH] Directive push sent successfully")
                        return True
                    error_text = await response.text()
                    logger.error(
                        "[PUSH] Directive push failed: HTTP %s body=%s",
                        response.status,
                        error_text,
                    )
                    return False
        except aiohttp.ClientError as exc:
            logger.error("[PUSH] Directive push network error: %s", exc)
            return False
        except Exception as exc:
            logger.error("[PUSH] Directive push error: %s", exc)
            return False
