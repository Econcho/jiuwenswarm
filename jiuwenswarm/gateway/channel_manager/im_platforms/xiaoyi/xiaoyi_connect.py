# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""XiaoyiChannel - 华为小艺 A2A 协议客户端."""

from __future__ import annotations

import logging
import asyncio
import base64
import hmac
import hashlib
import inspect
import json
import os
import ssl
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional
from urllib.parse import urlparse

import aiohttp

from jiuwenswarm.common.device_rpc.models import DeviceCommandRequest
from jiuwenswarm.gateway.channel_manager.base import BaseChannel, ChannelMetadata, RobotMessageRouter
from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.push import (
    PushConfig,
    PushDeliveryResult,
    XiaoYiPushService,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.formatter import (
    get_status_state_for_event,
    get_status_text_for_event,
    should_send_as_reasoning_text,
    should_send_as_status_update,
    should_send_as_text,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.a2a_vars import (
    extract_model_name,
)

logger = logging.getLogger(__name__)


def _is_data_event_status_success(status: Any) -> bool:
    if status is True:
        return True
    if status is None or status is False:
        return False
    return str(status).strip().lower() in ("success", "succeed", "successful", "ok")


def _gui_response_session_id(message: dict[str, Any]) -> str:
    session_id = str(message.get("sessionId") or "").strip()
    if session_id:
        return session_id
    params = message.get("params")
    if isinstance(params, dict):
        session_id = str(params.get("sessionId") or "").strip()
        if session_id:
            return session_id
    detail = message.get("msgDetail")
    if isinstance(detail, str):
        try:
            parsed = json.loads(detail)
        except json.JSONDecodeError:
            return ""
        detail_params = parsed.get("params")
        if isinstance(detail_params, dict):
            return str(detail_params.get("sessionId") or "").strip()
    return ""


FILE_TYPE_TO_MIME_TYPE: dict[str, str] = {
    "txt": "text/plain",
    "html": "text/html",
    "css": "text/css",
    "js": "application/javascript",
    "json": "application/json",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "pdf": "application/pdf",
    "zip": "application/zip",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "mp3": "audio/mpeg",
    "mp4": "video/mp4",
}

# 全局 XiaoyiChannel 实例引用（供手机端工具调用使用）
_xiaoyi_channel_instance: Optional["XiaoyiChannel"] = None


def get_xiaoyi_channel() -> Optional["XiaoyiChannel"]:
    """获取全局 XiaoyiChannel 实例（供手机端工具调用使用）."""
    return _xiaoyi_channel_instance


@dataclass
class DataEvent:
    """Data-only 事件数据结构（工具执行结果）."""
    intent_name: str
    outputs: dict
    status: str
    session_id: str = ""
    task_id: str = ""


TaskDeliveryKey = tuple[str, str]


@dataclass
class XiaoyiTaskDeliveryState:
    """Delivery state owned by one XiaoYi session/task pair."""

    session_id: str
    task_id: str
    push_id: str = ""
    final_text: str = ""
    terminal_state: str = ""
    terminal_status_text: str = ""
    finalized: bool = False
    websocket_delivered: bool = False
    push_attempted: bool = False
    push_attempts: int = 0
    notification_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_timeout_task: asyncio.Task | None = field(default=None, repr=False)
    session_timeout_task: asyncio.Task | None = field(default=None, repr=False)
    terminal_settle_task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def key(self) -> TaskDeliveryKey:
        return (self.session_id, self.task_id)


@dataclass
class XiaoyiChannelConfig:
    """小艺通道配置（客户端模式）."""

    enabled: bool = False
    mode: str = "xiaoyi_channel"  # xiaoyi_channel or xiaoyi_claw
    ak: str = ""
    sk: str = ""
    agent_id: str = ""
    ws_url1: str = ""
    ws_url2: str = ""
    enable_streaming: bool = True
    # Push notification configuration
    uid: str = ""
    api_key: str = ""
    api_id: str = ""
    push_id: str = ""
    push_url: str = ""
    file_upload_url: str = ""
    # Disabled by default so an upgrade does not alter existing notification behavior.
    push_on_task_complete: bool = False
    # Task timeout in milliseconds (default: 1 hour)
    task_timeout_ms: int = 3600000
    # Session cleanup timeout in milliseconds (default: 1 hour)
    session_cleanup_timeout_ms: int = 3600000


def _generate_signature(sk: str, timestamp: str) -> str:
    """生成 HMAC-SHA256 签名（Base64 编码）."""
    h = hmac.new(
        sk.encode("utf-8"),
        timestamp.encode("utf-8"),
        hashlib.sha256,
    )
    return base64.b64encode(h.digest()).decode("utf-8")


class XYFileUploadService:
    def __init__(self, base_url: str, api_key: str, uid: str):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.uid = uid
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.session.close()

    async def upload_file(self, file_path: str, object_type: str = "TEMPORARY_MATERIAL_DOC") -> Optional[str]:
        try:
            with open(file_path, 'rb') as f:
                file_content = f.read()

            file_name = os.path.basename(file_path)
            file_size = len(file_content)
            file_sha256 = hashlib.sha256(file_content).hexdigest()

            prepare_url = f"{self.base_url}/osms/v1/file/manager/prepare"
            prepare_data = {
                "objectType": object_type,
                "fileName": file_name,
                "fileSha256": file_sha256,
                "fileSize": file_size,
                "fileOwnerInfo": {
                    "uid": self.uid,
                    "teamId": self.uid,
                },
                "useEdge": False,
            }

            headers = {
                "Content-Type": "application/json",
                "x-uid": self.uid,
                "x-api-key": self.api_key,
                "x-request-from": "openclaw",
            }

            async with self.session.post(prepare_url, json=prepare_data, headers=headers) as resp:
                if not resp.ok:
                    raise Exception(f"Prepare failed: HTTP {resp}")

                prepare_resp = await resp.json()
                if prepare_resp.get("code") != "0":
                    raise RuntimeError(f"Prepare failed: {prepare_resp.get('desc', 'Unknown error')}")

            object_id = prepare_resp.get("objectId")
            draft_id = prepare_resp.get("draftId")
            upload_infos = prepare_resp.get("uploadInfos", [])

            if not upload_infos:
                raise RuntimeError("No upload information returned")

            upload_info = upload_infos[0]
            upload_url = upload_info.get("url")
            upload_method = upload_info.get("method", "PUT")
            upload_headers = upload_info.get("headers", {})

            async with self.session.request(
                    upload_method,
                    upload_url,
                    data=file_content,
                    headers=upload_headers
            ) as resp:
                if not resp.ok:
                    raise RuntimeError(f"Upload failed: HTTP {resp.status}")

            complete_url = f"{self.base_url}/osms/v1/file/manager/complete"
            complete_data = {
                "objectId": object_id,
                "draftId": draft_id,
            }

            async with self.session.post(complete_url, json=complete_data, headers=headers) as resp:
                if not resp.ok:
                    raise RuntimeError(f"Complete failed: HTTP {resp.status}")

                complete_resp = await resp.json()
                if complete_resp.get("code") != "0":
                    raise RuntimeError(f"Complete failed: {complete_resp.get('desc', 'Unknown error')}")

            return object_id

        except Exception as e:
            logger.error(f"[XY File Upload] Error: {e}")
            return None


def _generate_auth_headers(config: XiaoyiChannelConfig) -> dict[str, str]:
    """生成鉴权 Header."""
    if config.mode == "xiaoyi_claw":
        return {
            "x-uid": config.uid,
            "x-api-key": config.api_key,
            "x-agent-id": config.agent_id,
            "x-request-from": "openclaw"
        }
    timestamp = str(int(time.time() * 1000))
    signature = _generate_signature(config.sk, timestamp)
    return {
        "x-access-key": config.ak,
        "x-sign": signature,
        "x-ts": timestamp,
        "x-agent-id": config.agent_id
    }


class XiaoyiChannel(BaseChannel):
    """小艺通道：作为客户端连接到小艺服务器，实现 A2A 协议."""

    name = "xiaoyi"
    _TERMINAL_SETTLE_SECONDS = 0.3
    _FINALIZED_TASK_DEDUP_SECONDS = 300.0

    def __init__(self, config: XiaoyiChannelConfig, router: RobotMessageRouter):
        super().__init__(config, router)
        self.config: XiaoyiChannelConfig = config
        self._ws_connections: dict[str, Any] = {}  # Dual channel connections
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._running = False
        self._heartbeat_tasks: dict[str, asyncio.Task] = {}  # Heartbeat tasks for each channel
        self._connect_tasks: dict[str, asyncio.Task] = {}  # Connection tasks for each channel
        self._session_task_map: dict[str, str] = {}
        self._session_heartbeat_tasks: dict[str, asyncio.Task] = {}  # Response heartbeat tasks for each session
        self._stream_text_buffers: dict[str, str] = {}
        self._task_last_activity: dict[str, float] = {}
        self._on_message_cb: Callable[[Message], Any] | None = None
        # Task delivery management.  Every value that can affect final delivery is
        # scoped to a platform task rather than to the whole conversation.
        self._active_task_keys: set[TaskDeliveryKey] = set()
        self._task_delivery_states: dict[TaskDeliveryKey, XiaoyiTaskDeliveryState] = {}
        self._recently_finalized_tasks: dict[TaskDeliveryKey, float] = {}
        self._task_timeout_tasks: dict[TaskDeliveryKey, asyncio.Task] = {}
        self._session_timeout_tasks: dict[TaskDeliveryKey, asyncio.Task] = {}
        self._task_timeout_notified: set[TaskDeliveryKey] = set()
        # Session cleanup management
        self._sessions_marked_for_cleanup: dict[str, dict[str, Any]] = {}  # Session cleanup state
        # File upload service configuration
        self.file_upload_config = {
            "baseUrl": config.file_upload_url,
            "apiKey": config.api_key,
            "uid": config.uid,
        }
        # Save additional configuration fields
        self.api_id = config.api_id
        self.push_id = config.push_id
        # Data-event 处理器：intent_name -> list of handlers
        self._data_event_handlers: dict[str, List[Callable[[DataEvent], Any]]] = {}
        # InvokeJarvisGUIAgentResponse 原始事件回调列表
        self._gui_agent_handlers: List[Callable[[dict[str, Any]], Any]] = []
        # GUI 工具互斥：避免并发注册多个 handler 导致回包串单；不影响其他工具并发
        self._gui_tool_lock = asyncio.Lock()
        self._device_command_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._privilege_check_lock = asyncio.Lock()
        self._scheduled_device_command_lock = asyncio.Lock()

    @property
    def channel_id(self) -> str:
        return self.name

    @property
    def gui_tool_lock(self) -> asyncio.Lock:
        """供 xiaoyi_gui_agent 串行执行，避免多路 GUI 回包互相唤醒。"""
        return self._gui_tool_lock

    @property
    def is_ready(self) -> bool:
        """当前 Channel 是否至少持有一条可发送的小艺连接."""
        return self._running and any(self._ws_connections.values())

    @property
    def clients(self) -> set[Any]:
        return set()

    def on_message(self, callback: Callable[[Message], None]) -> None:
        self._on_message_cb = callback

    async def start(self) -> None:
        if self._running:
            logger.warning("XiaoyiChannel 已在运行")
            return
        if not self.config.enabled:
            logger.warning("XiaoyiChannel 未启用（enabled=False）")
            return
        if self.config.mode == "xiaoyi_channel":
            if not self.config.ak or not self.config.sk or not self.config.agent_id:
                logger.error("XiaoyiChannel 未配置 ak/sk/agent_id")
                return

        self._running = True
        # 注册全局实例（供 tools 使用）
        global _xiaoyi_channel_instance
        _xiaoyi_channel_instance = self
        logger.info("XiaoyiChannel 已注册为全局实例")

        # Start dual channel connections
        for url_key, url in [("ws_url1", self.config.ws_url1), ("ws_url2", self.config.ws_url2)]:
            if url:
                self._connect_tasks[url_key] = asyncio.create_task(self._reconnect_loop(url_key, url))
        logger.info("XiaoyiChannel 已启动（客户端模式，双通道）")

    async def stop(self) -> None:
        global _xiaoyi_channel_instance

        self._running = False
        # 注销全局实例
        if _xiaoyi_channel_instance is self:
            _xiaoyi_channel_instance = None
            logger.info("XiaoyiChannel 已注销全局实例")
        # Cancel all heartbeat tasks
        for url_key in list(self._heartbeat_tasks.keys()):
            if self._heartbeat_tasks[url_key]:
                self._heartbeat_tasks[url_key].cancel()
                self._heartbeat_tasks[url_key] = None
        # Cancel all connection tasks
        for url_key in list(self._connect_tasks.keys()):
            if self._connect_tasks[url_key]:
                self._connect_tasks[url_key].cancel()
                self._connect_tasks[url_key] = None
        # Cancel all session heartbeat tasks
        for session_id in list(self._session_heartbeat_tasks.keys()):
            if self._session_heartbeat_tasks[session_id]:
                self._session_heartbeat_tasks[session_id].cancel()
                self._session_heartbeat_tasks[session_id] = None
        # Cancel all task timeout tasks
        for task_key in list(self._task_timeout_tasks.keys()):
            if self._task_timeout_tasks[task_key]:
                self._task_timeout_tasks[task_key].cancel()
                self._task_timeout_tasks[task_key] = None
        # Cancel all session timeout tasks
        for task_key in list(self._session_timeout_tasks.keys()):
            if self._session_timeout_tasks[task_key]:
                self._session_timeout_tasks[task_key].cancel()
                self._session_timeout_tasks[task_key] = None
        for state in self._task_delivery_states.values():
            if state.terminal_settle_task and not state.terminal_settle_task.done():
                state.terminal_settle_task.cancel()
        # Close all websocket connections
        for url_key, ws in list(self._ws_connections.items()):
            if ws:
                try:
                    await ws.close()
                except Exception as e:
                    logger.warning(f"关闭 WebSocket 连接失败 ({url_key}): {e}")
                self._ws_connections[url_key] = None
        self._heartbeat_tasks.clear()
        self._connect_tasks.clear()
        self._session_heartbeat_tasks.clear()
        self._task_timeout_tasks.clear()
        self._session_timeout_tasks.clear()
        self._ws_connections.clear()
        self._active_task_keys.clear()
        self._task_delivery_states.clear()
        self._recently_finalized_tasks.clear()
        self._task_timeout_notified.clear()
        self._sessions_marked_for_cleanup.clear()
        logger.info("XiaoyiChannel 已停止")

    def _extract_platform_receive_info(self, msg: Message) -> tuple[str, str]:
        """
        从消息中提取小艺平台会话 ID 与任务 ID。
        优先使用 metadata（避免 \new_session 覆盖 session_id 后无法回发），否则回退到 session_id 与 _session_task_map。
        """
        meta = getattr(msg, "metadata", None) or {}
        platform_session_id = (meta.get("xiaoyi_session_id") or "").strip()
        platform_task_id = (meta.get("xiaoyi_task_id") or "").strip()
        if platform_session_id or platform_task_id:
            return (
                platform_session_id or (msg.session_id or ""),
                platform_task_id or platform_session_id,
            )
        task_id = msg.id or ""
        session_id = self._session_task_map.get(task_id, task_id)
        return session_id, task_id

    async def send(self, msg: Message) -> None:
        """发送消息到小艺服务端（A2A 格式，双通道发送）."""
        session_id, task_id = self._extract_platform_receive_info(msg)
        task_key = (session_id, task_id)
        if self._is_recently_finalized_task(task_key):
            logger.info(
                "[PUSH_STATE] session_id=%s task_id=%s action=ignored_duplicate_terminal_event",
                session_id,
                task_id,
            )
            return
        state = self._get_or_create_task_delivery_state(session_id, task_id)
        if not self._ws_connections:
            if str(msg.channel_id or "").strip().lower() == "xiaoyi":
                logger.warning(
                    "[GUI_AGENT_DIAG] phase=XIAOYI_WS_UNAVAILABLE "
                    "message_id=%s reason=no_ws_connections payload=%r",
                    msg.id,
                    msg.payload,
                )
        logger.info(
            "[GUI_AGENT_DIAG] phase=XIAOYI_SEND_MESSAGE message_id=%s "
            "session_id=%s event_type=%s payload=%r enable_streaming=%s",
            msg.id,
            msg.session_id,
            getattr(msg.event_type, "value", msg.event_type),
            msg.payload,
            self.config.enable_streaming,
        )
        logger.info(f"XiaoyiChannel 发送消息: {msg}")
        # Handle chat.file event
        if self.config.mode == "xiaoyi_claw" and msg.event_type == EventType.CHAT_FILE:
            files = msg.payload.get("files", {}) if isinstance(msg.payload, dict) else {}
            if files:
                for file_info in files:
                    # Convert file path to file info dict if it's a string
                    if isinstance(file_info, dict):
                        file_path = file_info.get("path", "")
                        file_name = file_info.get("name", os.path.basename(file_path))
                    else:
                        file_path = str(file_info)
                        file_name = os.path.basename(file_path)
                    file_info = {
                        "success": True,
                        "result_type": "file_created",
                        "fullPath": file_path,
                        "fileName": file_name
                    }

                    # Send file response
                    for url_key, ws in self._ws_connections.items():
                        if ws:
                            try:
                                await self._send_file_response(session_id, task_id, file_info, url_key)
                            except Exception as e:
                                logger.warning(f"XiaoyiChannel 发送文件响应失败 ({url_key}): {e}")
            return

        if should_send_as_status_update(msg.event_type):
            status_text = get_status_text_for_event(msg.event_type, msg.payload)
            status_state = get_status_state_for_event(msg.event_type, msg.payload)
            if status_state in {"completed", "failed", "canceled"} and session_id:
                state.terminal_state = status_state
                state.terminal_status_text = status_text
                if state.finalized:
                    return
                if status_state == "completed":
                    await self._settle_completed_status(state)
                else:
                    await self._finalize_task(
                        state,
                        terminal_state=status_state,
                        final_text=state.final_text,
                        send_text=False,
                        send_status=True,
                    )
            else:
                for url_key, ws in list(self._ws_connections.items()):
                    if ws:
                        await self._send_status_update_with_state(
                            task_id, session_id, status_text, status_state, url_key
                        )
            return

        # 问卷降级保底：端侧无选项点选卡片能力，把 chat.ask_user_question 降级为纯文本，
        # 走 text part 管道（A2A artifact-update kind:"text"），保证端侧一定能渲染出问卷。
        # 必须放在下方 non_user_visible SKIPPED 判断之前——chat.ask_user_question 既非
        #   status_update 也非 reasoning/text，会被那段直接 SKIPPED，降级就永远到不了。
        # 协议帧与普通正文回复末帧完全一致：append=False, lastChunk=True, final=True, kind="text"。
        #   last_chunk=True：一次性整段输出，按协议流式输出必须以 lastChunk=True 结束，
        #   且 last_chunk=True 才走 kind:"text" 正文管道（False 会走 reasoningText 思考管道）。
        #   is_final=True：与普通回复末帧一致，本轮结束；用户回答问卷作为新一轮请求重新进来，
        #   agent 拿到回答后再给出方案，与纯文本 ask_user 的处理方式完全相同。
        if msg.event_type == EventType.CHAT_ASK_USER_QUESTION:
            ask_payload = msg.payload if isinstance(msg.payload, dict) else {}
            ask_questions = ask_payload.get("questions", []) or []
            if ask_questions:
                ask_lines = ["为了帮你更好地完成任务，请回答以下几个问题：", ""]
                for q in ask_questions:
                    q_header = q.get("header", "") or q.get("question", "") or "问题"
                    q_text = q.get("question", "")
                    if q_header and q_header != q_text:
                        ask_lines.append(f"【{q_header}】{q_text}")
                    else:
                        ask_lines.append(q_text)
                    q_opts = q.get("options", []) or []
                    for opt in q_opts:
                        if isinstance(opt, dict):
                            opt_label = opt.get("label", "") or opt.get("description", "")
                        else:
                            opt_label = str(opt)
                        if opt_label:
                            ask_lines.append(f"  • {opt_label}")
                ask_lines.append("（直接回复你的选择或补充具体信息即可，我会据此继续处理）")
                ask_text = "\n".join(ask_lines)
                for url_key in list(self._ws_connections.keys()):
                    await self._send_text_response(
                        session_id,
                        task_id,
                        ask_text,
                        url_key,
                        append=False,
                        last_chunk=True,
                        is_final=True,  # 与普通回复末帧一致，本轮结束；用户回答作为新请求重新进来
                    )
            return

        if not (
            should_send_as_reasoning_text(msg.event_type)
            or should_send_as_text(msg.event_type)
        ):
            logger.info(
                "[GUI_AGENT_DIAG] phase=XIAOYI_EVENT_SKIPPED message_id=%s "
                "session_id=%s event_type=%s reason=non_user_visible_event "
                "payload=%r",
                msg.id,
                session_id,
                getattr(msg.event_type, "value", msg.event_type),
                msg.payload,
            )
            return

        content = ""
        cron_job_name = ""
        if isinstance(msg.payload, dict):
            content = msg.payload.get("content", "\n")
            if isinstance(content, dict):
                content = content.get("output", str(content))
            content = str(content)
            cron_job_name = msg.payload.get("cron", {}).get("job_name", "")
        elif msg.payload:
            content = str(msg.payload)

        # 推送消息发送
        if msg.id.startswith("cron-push"):
            await self._send_push_notification(
                cron_job_name,
                content,
                push_id=self.config.push_id,
            )
            # Cron Push has no platform task lifecycle, so do not retain the
            # transient delivery state created at the top of send().
            self._task_delivery_states.pop(task_key, None)
            return

        payload = msg.payload if isinstance(msg.payload, dict) else {}
        is_delta = msg.event_type == EventType.CHAT_DELTA
        is_chat_final = msg.event_type == EventType.CHAT_FINAL

        # 如果禁用流式，总是作为完整消息发送。
        if not self.config.enable_streaming:
            append = False
            last_chunk = True
            final = True
        else:
            # chat.final 携带完整正文，直接作为独立终帧发送，避免客户端
            # 等待后续空 status 帧提交正文。
            if is_chat_final:
                append = False
                last_chunk = True
                final = True
            else:
                append = True
                last_chunk = bool(payload.get("is_complete", False))
                final = last_chunk

        self._record_task_text(state, content)

        logger.info(
            "[GUI_AGENT_DIAG] phase=XIAOYI_TEXT_FLAGS message_id=%s "
            "platform_session_id=%s platform_task_id=%s event_type=%s "
            "payload_is_complete=%r append=%s last_chunk=%s final=%s "
            "content=%r",
            msg.id,
            session_id,
            task_id,
            getattr(msg.event_type, "value", msg.event_type),
            (
                msg.payload.get("is_complete")
                if isinstance(msg.payload, dict)
                else None
            ),
            append,
            last_chunk,
            final,
            content,
        )

        if final and session_id:
            await self._finalize_task(
                state,
                terminal_state="completed",
                final_text=state.final_text,
                send_text=True,
                text_append=append,
            )
            return

        await self._send_text_to_connections(
            state,
            content,
            append=append,
            last_chunk=last_chunk,
            is_final=False,
        )

    def _get_or_create_task_delivery_state(
        self,
        session_id: str,
        task_id: str,
        *,
        push_id: str = "",
    ) -> XiaoyiTaskDeliveryState:
        key = (session_id, task_id)
        state = self._task_delivery_states.get(key)
        if state is None:
            state = XiaoyiTaskDeliveryState(
                session_id=session_id,
                task_id=task_id,
                push_id=push_id,
            )
            self._task_delivery_states[key] = state
        elif push_id:
            state.push_id = push_id
        return state

    def _prune_recently_finalized_tasks(self) -> None:
        expires_before = time.monotonic() - self._FINALIZED_TASK_DEDUP_SECONDS
        for key, finalized_at in list(self._recently_finalized_tasks.items()):
            if finalized_at <= expires_before:
                self._recently_finalized_tasks.pop(key, None)

    def _is_recently_finalized_task(self, key: TaskDeliveryKey) -> bool:
        self._prune_recently_finalized_tasks()
        return key in self._recently_finalized_tasks

    @staticmethod
    def _record_task_text(state: XiaoyiTaskDeliveryState, content: str) -> None:
        """Accept either cumulative text frames or delta-only text frames."""

        if not content:
            return
        previous = state.final_text
        if not previous or content.startswith(previous):
            state.final_text = content
        else:
            state.final_text = previous + content

    async def _settle_completed_status(self, state: XiaoyiTaskDeliveryState) -> None:
        """Wait briefly for a CHAT_FINAL which may follow terminal status."""

        if state.terminal_settle_task is None or state.terminal_settle_task.done():
            state.terminal_settle_task = asyncio.create_task(
                self._settle_completed_status_after_delay(state)
            )
        try:
            await asyncio.shield(state.terminal_settle_task)
        except asyncio.CancelledError:
            # CHAT_FINAL won the race and performed finalization itself.
            pass

    async def _settle_completed_status_after_delay(
        self,
        state: XiaoyiTaskDeliveryState,
    ) -> None:
        try:
            await asyncio.sleep(self._TERMINAL_SETTLE_SECONDS)
            if not state.finalized:
                await self._finalize_task(
                    state,
                    terminal_state="completed",
                    final_text=state.final_text,
                    send_text=False,
                    send_status=True,
                )
        except asyncio.CancelledError:
            raise

    async def _finalize_task(
        self,
        state: XiaoyiTaskDeliveryState,
        *,
        terminal_state: str,
        final_text: str,
        send_text: bool,
        send_status: bool = False,
        text_append: bool = False,
    ) -> None:
        """Perform the one and only terminal delivery sequence for a task."""

        if state.finalized:
            return
        state.finalized = True
        state.terminal_state = terminal_state
        if final_text:
            state.final_text = final_text

        settle_task = state.terminal_settle_task
        if (
            settle_task
            and settle_task is not asyncio.current_task()
            and not settle_task.done()
        ):
            settle_task.cancel()

        logger.info(
            "[PUSH_STATE] session_id=%s task_id=%s terminal_state=%s "
            "push_id=%s final_text_length=%s push_enabled=%s",
            state.session_id,
            state.task_id,
            terminal_state,
            self._mask_push_id(state.push_id),
            len(state.final_text),
            self.config.push_on_task_complete,
        )

        if send_text:
            await self._send_text_to_connections(
                state,
                state.final_text,
                append=text_append,
                last_chunk=True,
                is_final=True,
            )
        elif send_status:
            for url_key, ws in list(self._ws_connections.items()):
                if ws:
                    delivered = await self._send_status_update_with_state(
                        state.task_id,
                        state.session_id,
                        state.terminal_status_text or self._terminal_status_fallback(terminal_state),
                        terminal_state,
                        url_key,
                    )
                    state.websocket_delivered = state.websocket_delivered or delivered

        if self._should_push_task_completion(state):
            summary = self._task_push_summary(state.final_text)
            state.push_attempted = True
            await self._send_push_notification(
                summary,
                f"后台任务已完成：{summary}",
                push_id=state.push_id,
                notification_id=state.notification_id,
                state=state,
            )

        await self._release_task_delivery_state(state)

    def _should_push_task_completion(self, state: XiaoyiTaskDeliveryState) -> bool:
        return bool(
            self.config.mode == "xiaoyi_claw"
            and self.config.push_on_task_complete
            and state.terminal_state == "completed"
            and state.push_id
        )

    @staticmethod
    def _task_push_summary(final_text: str) -> str:
        text = final_text.strip() or "后台任务已完成"
        return f"{text[:30]}..." if len(text) > 30 else text

    @staticmethod
    def _terminal_status_fallback(terminal_state: str) -> str:
        return {
            "completed": "任务已完成",
            "failed": "任务执行失败",
            "canceled": "任务已取消",
        }.get(terminal_state, "任务已结束")

    async def _send_text_to_connections(
        self,
        state: XiaoyiTaskDeliveryState,
        content: str,
        *,
        append: bool,
        last_chunk: bool,
        is_final: bool,
    ) -> None:
        for url_key, ws in list(self._ws_connections.items()):
            if not ws:
                continue
            delivered = await self._send_text_response(
                state.session_id,
                state.task_id,
                content,
                url_key,
                append=append,
                last_chunk=last_chunk,
                is_final=is_final,
            )
            state.websocket_delivered = state.websocket_delivered or delivered

    async def _release_task_delivery_state(self, state: XiaoyiTaskDeliveryState) -> None:
        key = state.key
        self._clear_task_timeout(state.session_id, state.task_id)
        self._clear_session_timeout(state.session_id, state.task_id)
        self._active_task_keys.discard(key)
        self._task_timeout_notified.discard(key)
        self._task_delivery_states.pop(key, None)
        self._recently_finalized_tasks[key] = time.monotonic()

        if not self._is_session_active(state.session_id):
            await self._stop_session_heartbeat(state.session_id)
        if self._is_session_pending_cleanup(state.session_id) and not self._is_session_active(state.session_id):
            self._force_cleanup_session(state.session_id)

    @staticmethod
    def _mask_push_id(push_id: str) -> str:
        push_id = str(push_id or "")
        if not push_id:
            return "<empty>"
        return f"{push_id[:4]}***" if len(push_id) > 4 else "*" * len(push_id)

    def get_metadata(self) -> ChannelMetadata:
        return ChannelMetadata(
            channel_id=self.channel_id,
            source="websocket",
            extra={
                "mode": "client",
                "ws_url1": self.config.ws_url1,
                "ws_url2": self.config.ws_url2,
                "agent_id": self.config.agent_id,
            },
        )

    async def _reconnect_loop(self, url_key: str, url: str) -> None:
        """自动重连循环（双通道）."""
        while self._running:
            try:
                await self._connect(url_key, url)
                if not self._running:
                    break
                # 连接被远端正常关闭时也做退避，避免瞬时重连刷屏。
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"XiaoyiChannel 连接失败 ({url}): {e}")
                await asyncio.sleep(5)

    async def _connect(self, url_key: str, url: str) -> None:
        """连接到小艺服务器（双通道）."""
        import websockets

        headers = _generate_auth_headers(self.config)
        parsed = urlparse(url)
        is_ip = bool(parsed.hostname and parsed.hostname.replace(".", "").isdigit())

        ssl_context = ssl.create_default_context()
        if is_ip:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        async with websockets.connect(
                url,
                additional_headers=headers,
                # ssl=ssl_context,
                ping_interval=15,
                ping_timeout=15,
                close_timeout=5,
        ) as ws:
            self._ws_connections[url_key] = ws
            self._send_locks[url_key] = asyncio.Lock()
            logger.info(f"XiaoyiChannel 已连接 {url_key}: {url}")

            # 发送初始化消息（必须在 heartbeat 之前）
            await self._send_init_message(url_key)

            # 启动心跳
            self._heartbeat_tasks[url_key] = asyncio.create_task(self._heartbeat_loop(url_key))

            try:
                async for raw in ws:
                    await self._handle_raw_message(raw, url_key)
            except Exception as e:
                logger.warning(f"XiaoyiChannel 连接异常 ({url_key}): {e}")
            finally:
                if self._heartbeat_tasks.get(url_key):
                    self._heartbeat_tasks[url_key].cancel()
                    self._heartbeat_tasks[url_key] = None
                self._ws_connections[url_key] = None
                self._send_locks.pop(url_key, None)
                close_code = getattr(ws, "close_code", None)
                close_reason = getattr(ws, "close_reason", None)
                logger.info(
                    f"XiaoyiChannel 连接关闭 {url_key}: {url} (code={close_code}, reason={close_reason})"
                )

    async def _send_init_message(self, url_key: str) -> None:
        """发送初始化消息 (clawd_bot_init) 到指定通道."""
        ws = self._ws_connections.get(url_key)
        if not ws:
            return
        init_message = {
            "msgType": "clawd_bot_init",
            "agentId": self.config.agent_id,
        }
        try:
            await self._safe_ws_send(url_key, init_message)
            logger.info(f"XiaoyiChannel 已发送初始化消息 ({url_key})")
        except Exception as e:
            logger.warning(f"XiaoyiChannel 发送初始化消息失败 ({url_key}): {e}")
            raise

    async def _heartbeat_loop(self, url_key: str) -> None:
        """应用层心跳循环（20秒间隔）."""
        while self._running and self._ws_connections.get(url_key):
            try:
                heartbeat = {"msgType": "heartbeat", "agentId": self.config.agent_id}
                await self._safe_ws_send(url_key, heartbeat)
            except Exception as e:
                logger.warning(f"XiaoyiChannel 心跳发送失败 ({url_key}): {e}")
                ws = self._ws_connections.get(url_key)
                if ws:
                    try:
                        await ws.close()
                    except Exception as close_error:
                        logger.warning(f"XiaoyiChannel 关闭连接失败 ({url_key}): {close_error}")
                break
            await asyncio.sleep(20)

    async def _handle_raw_message(self, raw: str | bytes, url_key: str | None = None) -> None:
        """处理接收到的原始消息，转换为 JiuwenSwarm 内部格式."""
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            is_gui_response_frame = "InvokeJarvisGUIAgentResponse" in raw
            message = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f"XiaoyiChannel JSON 解析失败: {e}")
            return
        msg_type = message.get("msgType")
        method = message.get("method")
        if is_gui_response_frame:
            logger.info(
                "[GUI_RPC_TRACE] phase=CHANNEL_RAW_GUI_FRAME "
                "msg_type=%s method=%s session_id=%s has_msg_detail=%s",
                msg_type,
                method,
                str(message.get("sessionId") or ""),
                isinstance(message.get("msgDetail"), str),
            )
            logger.info(
                "[GUI_AGENT_DIAG] phase=XIAOYI_RAW_GUI_FRAME raw=%s parsed=%r",
                raw,
                message,
            )

        # 添加详细日志用于诊断工具消息
        if method or (msg_type and msg_type != "heartbeat"):
            logger.info(f"[XiaoyiChannel] _handle_raw_message: msg_type={msg_type},"
                        f"method={method}, sessionId={message.get('sessionId', 'N/A')}")

        if msg_type == "heartbeat":
            return
        # MemoryQuery is a command data event (direct or wrapped A2A), not a
        # normal user message. Consume it before the generic data-event parser.
        from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.memory_query import (
            extract_memory_query,
            handle_memory_query,
            memory_query_command,
            configured_runtime_state_path,
        )

        memory_query = extract_memory_query(message)
        if memory_query is not None:
            from jiuwenswarm.common.utils import get_agent_workspace_dir

            answer = await asyncio.to_thread(
                handle_memory_query,
                memory_query,
                workspace_dir=get_agent_workspace_dir(),
                runtime_state_path=configured_runtime_state_path(),
            )
            await self.send_xiaoyi_phone_tools_command(
                memory_query.session_id,
                memory_query.task_id,
                memory_query.message_id,
                memory_query_command(memory_query.action, answer),
                final=True,
            )
            return
        # ── A2A CronQuery detection (AgentEvent.CronQuery) ──────────────
        # Detect CronQuery directives at root level or inside parts directives.
        # Handle and respond via WebSocket before falling through to normal flow.
        try:
            from jiuwenswarm.gateway.cron.cron_query_handler import (
                extract_cron_query_from_message,
                dispatch_cron_query,
                build_a2a_response_envelope,
            )
            from jiuwenswarm.gateway.cron.controller import CronController

            cron_payload = extract_cron_query_from_message(message)
            if cron_payload is not None:
                logger.info(
                    "[XiaoyiChannel] CronQuery action=%s detected",
                    cron_payload.get("action"),
                )
                try:
                    cc = CronController.get_instance()
                except RuntimeError:
                    cc = None
                # session_id_ctx: params.sessionId (conversationId, 带 _CronQuery 后缀)
                session_id_ctx = ""
                params_root = message.get("params")
                if isinstance(params_root, dict):
                    sid_val = params_root.get("sessionId")
                    if isinstance(sid_val, str) and sid_val.strip():
                        session_id_ctx = sid_val.strip()
                # rpc_id: JSON-RPC request id (用于响应关联)
                rpc_id = message.get("id") or ""
                task_id = ""
                if isinstance(params_root, dict):
                    task_id = params_root.get("id") or ""
                result = await dispatch_cron_query(
                    cron_payload,
                    cron_controller=cc,
                    session_id=session_id_ctx,
                )
                # dispatch returns {action, status:True, ans} on success,
                # or {action, ans:{error}} on error (no status field).
                # build_a2a_response_envelope: status=True → include payload.status;
                # status=False → omit payload.status (error per protocol doc).
                is_ok = bool(result.get("status", False))
                response_envelope = build_a2a_response_envelope(
                    result.get("action", cron_payload.get("action", "")),
                    result.get("ans", {}),
                    status=is_ok,
                )
                if url_key:
                    try:
                        # CronQuery 信封包装在标准 JSON-RPC artifact-update 响应里，
                        # 与正常 agent_response 路径一致（jsonrpc/id/result/artifact/parts）。
                        # 下行响应用 data.commands（与正常消息流一致），元素为 {header, payload} 信封。
                        cron_rpc_response = {
                            "jsonrpc": "2.0",
                            "id": rpc_id,
                            "result": {
                                "taskId": task_id,
                                "kind": "artifact-update",
                                "append": False,
                                "lastChunk": True,
                                "final": True,
                                "artifact": {
                                    "artifactId": str(uuid.uuid4()),
                                    "parts": [
                                        {
                                            "kind": "data",
                                            "data": {"commands": [response_envelope]},
                                        }
                                    ],
                                },
                            },
                        }
                        cron_wrapper = {
                            "msgType": "agent_response",
                            "agentId": self.config.agent_id,
                            "sessionId": session_id_ctx,
                            "taskId": task_id,
                            "msgDetail": json.dumps(cron_rpc_response, ensure_ascii=False),
                        }
                        await self._safe_ws_send(url_key, cron_wrapper)
                    except Exception as send_err:
                        logger.warning(
                            "[XiaoyiChannel] CronQuery response send failed: %s",
                            send_err,
                        )
                return  # CronQuery handled; do not fall through to message dispatch
        except ImportError:
            logger.debug("[XiaoyiChannel] cron_query_handler not available, skipping CronQuery detection")
        except Exception as exc:
            logger.warning("[XiaoyiChannel] CronQuery handling error: %s", exc)

        # MemoryQuery is a command data event (direct or wrapped A2A), not a
        # normal user message. Consume it before the generic data-event parser.
        from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.memory_query import (
            extract_memory_query,
            handle_memory_query,
            memory_query_command,
            configured_runtime_state_path,
        )

        memory_query = extract_memory_query(message)
        if memory_query is not None:
            from jiuwenswarm.common.utils import get_agent_workspace_dir

            answer = await asyncio.to_thread(
                handle_memory_query,
                memory_query,
                workspace_dir=get_agent_workspace_dir(),
                runtime_state_path=configured_runtime_state_path(),
            )
            await self.send_xiaoyi_phone_tools_command(
                memory_query.session_id,
                memory_query.task_id,
                memory_query.message_id,
                memory_query_command(memory_query.action, answer),
                final=True,
            )
            return

        # 根级直连 A2A（jsonrpc 2.0）须含 params.sessionId，否则整帧丢弃
        if message.get("jsonrpc") == "2.0":
            params_root = message.get("params")
            if not isinstance(params_root, dict):
                params_root = {}
            sid = params_root.get("sessionId")
            if sid is None or (isinstance(sid, str) and not sid.strip()):
                logger.warning(
                    "XiaoyiChannel 直连 A2A 缺少有效 params.sessionId，跳过本帧（与 xy_channel 一致）"
                )
                return

        await self._dispatch_gui_agent_events(message)

        # 检查是否是 data-only 消息（工具执行结果）
        data_event = self._extract_data_event(message)
        if data_event:
            logger.info(f"XiaoyiChannel 收到 data-event: {data_event.intent_name}, status={data_event.status}")
            await self._handle_data_event(data_event)
            return

        # Direct A2A GUI responses also use method=message/stream. They have
        # already been consumed by the GUI handler and must not become a new
        # empty user request, which would cancel the active Agent stream.
        if is_gui_response_frame:
            logger.info(
                "[GUI_AGENT_DIAG] phase=XIAOYI_GUI_FRAME_CONSUMED "
                "session_id=%s method=%s reason=handled_by_gui_rpc",
                str(message.get("sessionId") or ""),
                method,
            )
            return

        # GUI / UploadExeResult 等已在 _dispatch_gui_agent_events 与 _extract_data_event 中处理，勿再落 unknown method。
        if msg_type == "data":
            return

        method = message.get("method")
        if method == "message/stream":
            await self._handle_message_stream(message)
        elif method == "clearContext":
            await self._handle_clear_context(message)
        elif method == "tasks/cancel":
            await self._handle_tasks_cancel(message)
        else:
            # 服务端 JSON-RPC 仅含 data parts 的工具回包（如纯 GUI 响应）无 method 字段
            if not method and not msg_type and message.get("jsonrpc") == "2.0":
                parts = self._get_a2a_parts(message)
                if parts and all(p.get("kind") == "data" for p in parts):
                    return
            logger.warning(f"XiaoyiChannel 未知方法: {method}")

    async def _handle_message_stream(self, message: dict[str, Any]) -> None:
        """处理 message/stream 消息，转换为 JiuwenSwarm Message."""
        # 优先用 params.sessionId（= conversationId，真·对话 id），跨多轮稳定、与 task 解耦；
        # 缺失时回退到顶层 sessionId。
        session_id = message.get("params", {}).get("sessionId") or message.get("sessionId", "")
        task_id = message.get("params", {}).get("id", ) or ""
        root_session_id = message.get("sessionId", "")
        user_message = message.get("params", {}).get("message", {})
        parts = user_message.get("parts", [])

        # ==================== PROCESS PARTS (TEXT & FILES) ====================
        text = ""
        file_attachments: list[str] = []
        media_files: list[dict[str, Any]] = []
        request_push_id = ""

        for part in parts:
            kind = part.get("kind")
            if kind == "text" and part.get("text"):
                text += part.get("text", "")
            elif kind == "file" and part.get("file"):
                file_info = part["file"]
                uri = file_info.get("uri")
                mime_type = file_info.get("mimeType", "")
                name = file_info.get("name", "")

                if not uri:
                    logger.warning(f"XiaoYi: File part without URI, skipping: {name}")
                    continue

                try:
                    media_files.append({"uri": uri, "mime_type": mime_type, "name": name})

                    # For text-based files, extract content inline
                    from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.media import \
                        is_text_mime_type, extract_text_from_url
                    if is_text_mime_type(mime_type):
                        try:
                            text_content = await extract_text_from_url(uri, 5_000_000, 30_000)
                            text += f"\n\n[文件内容: {name}]\n{text_content}"
                            file_attachments.append(f"[文件: {name}]")
                            logger.info(f"XiaoYi: Successfully extracted text from: {name}")
                        except Exception:
                            logger.warning(f"XiaoYi: Text extraction failed for {name}, will download as binary")
                            file_attachments.append(f"[文件: {name}]")
                    else:
                        file_attachments.append(f"[文件: {name}]")
                except Exception as e:
                    logger.error(f"XiaoYi: Failed to process file {name}: {e}")
                    file_attachments.append(f"[文件处理失败: {name}]")
            elif kind == "data":
                data = part.get("data", {})
                if isinstance(data, dict):
                    push_id = data.get("variables", {}).get("systemVariables", {}).get("push_id", "")
                    request_push_id = str(push_id or "").strip() or request_push_id
        # =================================================================

        # Store the request-scoped Push target before the Agent is allowed to
        # emit any event.  config.push_id remains the cron fallback only.
        if request_push_id:
            self.config.push_id = request_push_id
        task_state = self._get_or_create_task_delivery_state(
            session_id,
            task_id,
            push_id=request_push_id,
        )
        self._mark_session_active(session_id, task_id)
        self._session_task_map[task_id] = session_id

        # Log summary of processed attachments
        if file_attachments:
            logger.info(f"XiaoYi: Processed {len(file_attachments)} file(s): {', '.join(file_attachments)}")

        # ==================== DOWNLOAD AND SAVE MEDIA FILES ====================
        media_payload: dict[str, Any] = {}
        if media_files:
            logger.info(f"XiaoYi: Downloading {len(media_files)} media file(s)...")
            from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.media import (
                MediaFile,
                MediaDownloadOptions,
                download_and_save_media_list,
                build_xiaoyi_media_payload,
            )
            files_to_download = [
                MediaFile(uri=f["uri"], mime_type=f["mime_type"], name=f["name"])
                for f in media_files
            ]
            options = MediaDownloadOptions(max_bytes=30_000_000, timeout_ms=60_000)
            downloaded_media = await download_and_save_media_list(files_to_download, options)
            logger.info(f"XiaoYi: Successfully downloaded {len(downloaded_media)}/{len(media_files)} file(s)")
            media_payload = build_xiaoyi_media_payload(downloaded_media)
        # =================================================================

        # 将最近一次可回发的小艺身份写入 config.yaml，供 cron 推送时使用
        try:
            from jiuwenswarm.common.config import update_channel_in_config

            rpc_id = message.get("id")
            update_channel_in_config(
                "xiaoyi",
                {
                    "last_session_id": session_id or "",
                    "last_task_id": task_id or "",
                    "last_message_id": str(rpc_id) if rpc_id is not None else "",
                },
            )
        except Exception as config_error:
            logger.warning(f"XiaoyiChannel 更新配置失败: {config_error}")

        # ==================== BUILD MESSAGE AND ROUTE ====================
        # 平台身份写入 metadata，供回发时使用（与 session_id 解耦，\new_session 后仍可正确回发）
        metadata = {
            "method": "message/stream",
            "xiaoyi_session_id": session_id,
            "xiaoyi_root_session_id": root_session_id or session_id,
            "xiaoyi_params_session_id": message.get("params", {}).get("sessionId", ""),
            "xiaoyi_task_id": task_id,
            "xiaoyi_rpc_id": str(message.get("id") or ""),
            "xiaoyi_push_id": task_state.push_id,
            "xiaoyi_user_id": str(self.config.uid or ""),
            "celia_user_id": str(self.config.uid or ""),
            "conversation_id": session_id,
        }
        try:
            from jiuwenswarm.agents.harness.common.memory.celia.runtime_state import update_runtime_info
            from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.memory_query import (
                configured_runtime_state_path,
            )

            await asyncio.to_thread(
                update_runtime_info,
                root_session_id or session_id,
                session_id,
                task_id,
                configured_runtime_state_path(),
            )
        except OSError:
            logger.warning("XiaoyiChannel failed to update .xiaoyiruntime", exc_info=True)
        # Add media payload to metadata
        params = {"query": text, "task_id": task_id}
        if media_payload:
            params["files"] = media_payload
        # Per-request model (same key as Web chat.send); agent last-mile overrides model=
        model_name = extract_model_name(parts)
        if model_name:
            params["model_name"] = model_name
            logger.info("[Xiaoyi] Found modelName: %s", model_name)

        user_message = Message(
            id=message.get("id", ""),
            type="req",
            channel_id=self.channel_id,
            session_id=session_id,
            params=params,
            timestamp=time.time(),
            is_stream=self.config.enable_streaming,
            ok=True,
            req_method=ReqMethod.CHAT_SEND,
            chat_id=session_id,
            metadata=metadata,
        )

        # ==================== START TASK TIMEOUT PROTECTION ====================
        # Start 1-hour task timeout timer
        task_timeout_ms = self.config.task_timeout_ms
        logger.info(f"[TASK TIMEOUT] Starting {task_timeout_ms}ms task timeout protection for session {session_id}")

        async def task_timeout_handler():
            """1-hour task timeout handler."""
            try:
                await asyncio.sleep(task_timeout_ms / 1000)
                logger.info(f"[TASK TIMEOUT] 1-hour timeout triggered for session {session_id}")
                # Send default message with is_final=true
                for url_key in list(self._ws_connections.keys()):
                    await self._send_text_response(session_id, task_id, "任务还在处理中~", url_key, is_final=True)
                # Timeout controls progress messaging only; task completion Push
                # is always decided by the terminal coordinator.
                self._task_timeout_notified.add(task_state.key)
            except asyncio.CancelledError:
                pass

        task_state.task_timeout_task = asyncio.create_task(task_timeout_handler())
        self._task_timeout_tasks[task_state.key] = task_state.task_timeout_task

        # Start 60-second periodic timeout for status updates
        async def periodic_timeout_handler():
            """60-second periodic timeout for status updates."""
            try:
                while task_state.key in self._active_task_keys:
                    await asyncio.sleep(60)
                    # The long-task notice has already been sent.
                    if task_state.key in self._task_timeout_notified:
                        break
                    # Send status update
                    await self._send_status_update(task_id, session_id, "任务正在处理中，请稍后~")
            except asyncio.CancelledError:
                pass

        task_state.session_timeout_task = asyncio.create_task(periodic_timeout_handler())
        self._session_timeout_tasks[task_state.key] = task_state.session_timeout_task
        # =================================================================

        handled = False
        if self._on_message_cb is not None:
            result = self._on_message_cb(user_message)
            if inspect.isawaitable(result):
                result = await result
            handled = bool(result)

        if not handled:
            await self.bus.route_user_message(user_message)

        # Start session heartbeat to prevent xiaoyi client timeout
        if not self.config.enable_streaming and session_id:
            await self._start_session_heartbeat(session_id, task_id)

    async def _start_session_heartbeat(self, session_id: str, task_id: str) -> None:
        """启动会话心跳任务，每隔5秒发送空消息直到final消息发出."""
        await self._stop_session_heartbeat(session_id)

        async def heartbeat_loop():
            try:
                while self._running:
                    await asyncio.sleep(5)
                    # Send empty heartbeat message (non-final)
                    for url_key, ws in self._ws_connections.items():
                        if ws:
                            try:
                                await self._send_text_response(
                                    session_id,
                                    task_id,
                                    "",
                                    url_key,
                                    append=True,
                                    is_final=False,
                                )
                            except Exception as e:
                                logger.warning(f"XiaoyiChannel 发送心跳消息失败 ({url_key}): {e}")
            except asyncio.CancelledError:
                logger.info(f"XiaoyiChannel 会话心跳已停止: {session_id}")
            except Exception as e:
                logger.warning(f"XiaoyiChannel 会话心跳异常 ({session_id}): {e}")

        self._session_heartbeat_tasks[session_id] = asyncio.create_task(heartbeat_loop())
        logger.info(f"XiaoyiChannel 会话心跳已启动: {session_id}")

    async def _stop_session_heartbeat(self, session_id: str) -> None:
        """停止会话心跳任务."""
        if session_id in self._session_heartbeat_tasks:
            task = self._session_heartbeat_tasks[session_id]
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._session_heartbeat_tasks.pop(session_id, None)
            logger.info(f"XiaoyiChannel 会话心跳已停止: {session_id}")

    async def _send_status_update(self, task_id: str, session_id: str, message: str) -> None:
        """发送状态更新消息（A2A 格式）."""
        response = {
            "jsonrpc": "2.0",
            "id": f"msg_{int(time.time() * 1000)}",
            "result": {
                "taskId": task_id,
                "kind": "status-update",
                "final": False,
                "status": {
                    "message": {
                        "role": "agent",
                        "parts": [{"kind": "text", "text": message}],
                    },
                    "state": "working",
                },
            },
        }
        # Send to all active connections
        for url_key in list(self._ws_connections.keys()):
            await self._send_agent_response(session_id, task_id, response, url_key)

    async def _send_status_update_with_state(
            self, task_id: str, session_id: str, message: str, state: str, url_key: str
    ) -> bool:
        """发送状态更新消息（A2A 格式），支持自定义状态."""
        is_final = state in {"completed", "failed", "canceled"}
        response = {
            "jsonrpc": "2.0",
            "id": f"msg_{int(time.time() * 1000)}",
            "result": {
                "taskId": task_id,
                "kind": "status-update",
                "final": is_final,
                "status": {
                    "message": {
                        "role": "agent",
                        "parts": [{"kind": "text", "text": message}],
                    },
                    "state": state,
                },
            },
        }
        logger.info(
            "[GUI_AGENT_DIAG] phase=XIAOYI_STATUS_RESPONSE_BUILT "
            "session_id=%s task_id=%s connection=%s state=%s "
            "final=%s text=%r response=%r",
            session_id,
            task_id,
            url_key,
            state,
            is_final,
            message,
            response,
        )
        return await self._send_agent_response(session_id, task_id, response, url_key)

    def _is_session_active(self, session_id: str) -> bool:
        """检查会话是否有活跃任务."""
        return any(key_session_id == session_id for key_session_id, _ in self._active_task_keys)

    def _mark_session_active(self, session_id: str, task_id: str) -> None:
        """Mark one platform task as active without affecting sibling tasks."""
        self._active_task_keys.add((session_id, task_id))

    def _mark_session_completed(self, session_id: str, task_id: str) -> None:
        """Mark one platform task as completed without affecting siblings."""
        self._active_task_keys.discard((session_id, task_id))

    def _is_session_pending_cleanup(self, session_id: str) -> bool:
        """检查会话是否待清理."""
        return session_id in self._sessions_marked_for_cleanup

    def _mark_session_for_cleanup(self, session_id: str, reason: str = "unknown") -> None:
        """标记会话待清理."""
        self._sessions_marked_for_cleanup[session_id] = {
            "reason": reason,
            "marked_at": time.time(),
        }

    def _force_cleanup_session(self, session_id: str) -> None:
        """强制清理会话."""
        self._sessions_marked_for_cleanup.pop(session_id, None)
        for task_id, mapped_session_id in list(self._session_task_map.items()):
            if mapped_session_id == session_id:
                self._session_task_map.pop(task_id, None)

    async def _handle_clear_context(self, message: dict[str, Any]) -> None:
        """处理清空上下文请求."""
        session_id = message.get("sessionId", "")
        logger.info(f"XiaoyiChannel 清空上下文: {session_id}")

        # Check if there's an active task for this session
        if self._is_session_active(session_id):
            logger.info(f"[CLEAR] Active task exists for session {session_id}, will continue in background")
            # Mark session for cleanup (delayed cleanup)
            self._mark_session_for_cleanup(session_id, "user_cleared")
        else:
            logger.info(f"[CLEAR] No active task for session {session_id}, clean up immediately")
            self._force_cleanup_session(session_id)

        response = {
            "jsonrpc": "2.0",
            "id": message.get("id", ""),
            "result": {"status": {"state": "cleared"}},
        }
        # Send response to all active connections
        for url_key in list(self._ws_connections.keys()):
            await self._send_agent_response(session_id, session_id, response, url_key)

    async def _handle_tasks_cancel(self, message: dict[str, Any]) -> None:
        """处理取消任务请求."""
        session_id = message.get("sessionId", "")
        task_id = message.get("params", {}).get("id") or message.get("taskId", "")
        logger.info(f"XiaoyiChannel 取消任务: {session_id} {task_id}")
        response = {
            "jsonrpc": "2.0",
            "id": message.get("id", ""),
            "result": {"id": message.get("id", ""), "status": {"state": "canceled"}},
        }
        # Send response to all active connections
        for url_key in list(self._ws_connections.keys()):
            await self._send_agent_response(session_id, task_id, response, url_key)

        state = self._get_or_create_task_delivery_state(session_id, task_id)
        if not state.finalized:
            state.finalized = True
            state.terminal_state = "canceled"
            await self._release_task_delivery_state(state)

    async def _send_text_response(
            self,
            session_id: str,
            task_id: str,
            text: str,
            url_key: str,
            *,
            append: bool = False,
            last_chunk: bool = True,
            is_final: bool = True,
    ) -> bool:
        """发送文本响应（A2A 格式）到指定通道."""
        if last_chunk:
            data = {"kind": "text", "text": text}
        else:
            data = {"kind": "reasoningText", "reasoningText": text}
        response = {
            "jsonrpc": "2.0",
            "id": f"msg_{int(time.time() * 1000)}",
            "result": {
                "taskId": task_id,
                "kind": "artifact-update",
                "append": append,
                "lastChunk": last_chunk,
                "final": is_final,
                "artifact": {
                    "artifactId": f"artifact_{int(time.time() * 1000)}",
                    "parts": [data],
                },
            },
        }
        logger.info(
            "[GUI_AGENT_DIAG] phase=XIAOYI_TEXT_RESPONSE_BUILT "
            "session_id=%s task_id=%s connection=%s append=%s "
            "last_chunk=%s final=%s text=%r response=%r",
            session_id,
            task_id,
            url_key,
            append,
            last_chunk,
            is_final,
            text,
            response,
        )
        return await self._send_agent_response(session_id, task_id, response, url_key)

    async def _send_agent_response(
        self,
        session_id: str,
        task_id: str,
        response: dict[str, Any],
        url_key: str,
    ) -> bool:
        """发送 agent_response 包装的消息（A2A 格式）到指定通道."""
        wrapper = {
            "msgType": "agent_response",
            "agentId": self.config.agent_id,
            "sessionId": session_id,
            "taskId": task_id,
            "msgDetail": json.dumps(response),
        }
        result = response.get("result")
        is_status_response = (
            isinstance(result, dict) and result.get("kind") == "status-update"
        )
        artifact = result.get("artifact") if isinstance(result, dict) else None
        parts = artifact.get("parts") if isinstance(artifact, dict) else None
        is_text_response = bool(
            isinstance(parts, list)
            and any(
                isinstance(part, dict)
                and part.get("kind") in ("text", "reasoningText")
                for part in parts
            )
        )
        try:
            if is_text_response:
                logger.info(
                    "[GUI_AGENT_DIAG] phase=XIAOYI_WS_TEXT_SEND_BEGIN "
                    "session_id=%s task_id=%s connection=%s wrapper=%r",
                    session_id,
                    task_id,
                    url_key,
                    wrapper,
                )
            elif is_status_response:
                logger.info(
                    "[GUI_AGENT_DIAG] phase=XIAOYI_WS_STATUS_SEND_BEGIN "
                    "session_id=%s task_id=%s connection=%s wrapper=%r",
                    session_id,
                    task_id,
                    url_key,
                    wrapper,
                )
            await self._safe_ws_send(url_key, wrapper)
            if is_text_response:
                logger.info(
                    "[GUI_AGENT_DIAG] phase=XIAOYI_WS_TEXT_SEND_DONE "
                    "session_id=%s task_id=%s connection=%s",
                    session_id,
                    task_id,
                    url_key,
                )
            elif is_status_response:
                logger.info(
                    "[GUI_AGENT_DIAG] phase=XIAOYI_WS_STATUS_SEND_DONE "
                    "session_id=%s task_id=%s connection=%s",
                    session_id,
                    task_id,
                    url_key,
                )
            return True
        except Exception as e:
            if is_text_response:
                logger.exception(
                    "[GUI_AGENT_DIAG] phase=XIAOYI_WS_TEXT_SEND_FAILED "
                    "session_id=%s task_id=%s connection=%s error_type=%s",
                    session_id,
                    task_id,
                    url_key,
                    type(e).__name__,
                )
            elif is_status_response:
                logger.exception(
                    "[GUI_AGENT_DIAG] phase=XIAOYI_WS_STATUS_SEND_FAILED "
                    "session_id=%s task_id=%s connection=%s error_type=%s",
                    session_id,
                    task_id,
                    url_key,
                    type(e).__name__,
                )
            logger.warning(f"XiaoyiChannel 发送响应失败 ({url_key}): {e}")
            return False

    async def _send_file_response_base64(self, session_id: str, task_id: str, file_info: dict, url_key: str) -> None:
        """发送文件响应（Base64 格式）到指定通道."""
        try:
            file_path = file_info.get("fullPath", "")
            if not file_path or not os.path.exists(file_path):
                logger.error(f"send file failed, caused by file not exist. file path: {file_path}")
                return
            file_name = os.path.basename(file_info.get("fileName", ""))
            file_name = file_name if file_name else os.path.basename(file_path)

            # Check file size (limit to 20MB for Base64)
            base_url = self.file_upload_config.get("baseUrl")
            api_key = self.file_upload_config.get("apiKey")
            uid = self.file_upload_config.get("uid")

            if not all([base_url, api_key, uid]):
                logger.error("XiaoyiChannel OSMS配置不完整，无法上传大文件")
                return

            object_id = ""
            mime_type = FILE_TYPE_TO_MIME_TYPE.get(file_name.split(".")[-1], "text/plain")
            async with XYFileUploadService(base_url, api_key, uid) as upload_service:
                object_id = await upload_service.upload_file(file_path)
                logger.info(f"file upload success: {object_id}")
                if object_id:
                    # Send file reference response
                    payload = {
                        "jsonrpc": "2.0",
                        "id": task_id,
                        "result": {
                            "kind": "artifact-update",
                            "append": True,
                            "lastChunk": False,
                            "isFinal": False,
                            "artifact": {
                                "artifactId": task_id,
                                "parts": [
                                    {
                                        "kind": "file",
                                        "file": {
                                            "fileId": object_id,
                                            "name": file_name,
                                            "mimeType": mime_type
                                        }
                                    }
                                ],
                            },
                        },
                        "error": {
                            "code": 0
                        }
                    }
                    response = {
                        "msgType": "agent_response",
                        "agentId": self.config.agent_id,
                        "sessionId": session_id,
                        "taskId": task_id,
                        "msgDetail": json.dumps(payload)
                    }
                    await self._safe_ws_send(url_key, response)
            return object_id
        except Exception as e:
            logger.error(f"XiaoyiChannel 发送文件响应失败: {e}")

    async def _send_file_response(self, session_id: str, task_id: str, file_info: dict, url_key: str) -> None:
        """发送文件响应到指定通道."""
        try:
            # If file is available locally, send as Base64
            if file_info.get("fullPath"):
                await self._send_file_response_base64(session_id, task_id, file_info, url_key)
                return
        except Exception as e:
            logger.error(f"XiaoyiChannel 发送文件响应失败: {e}")

    async def _safe_ws_send(self, url_key: str, payload: dict[str, Any]) -> None:
        ws = self._ws_connections.get(url_key)
        if not ws:
            raise RuntimeError(f"ws connection not available: {url_key}")
        lock = self._send_locks.get(url_key)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[url_key] = lock
        data = json.dumps(payload, ensure_ascii=False)
        async with lock:
            await ws.send(data)

    async def send_agent_response_to_all(
            self, session_id: str, task_id: str, response: dict[str, Any]
    ) -> None:
        """向所有活跃 WebSocket 连接发送预构建的 agent_response 消息.

        Args:
            session_id: 会话 ID
            task_id: 任务 ID
            response: 已包含 msgType、agentId 等字段的完整消息体
        """
        sent = False
        for url_key in list(self._ws_connections.keys()):
            try:
                await self._safe_ws_send(url_key, response)
                sent = True
            except Exception as e:
                logger.warning(
                    "XiaoyiChannel send_agent_response_to_all 失败 (%s): %s",
                    url_key,
                    e,
                )
        if not sent:
            raise RuntimeError("发送文件消息失败，WebSocket 未连接")

    def _clear_task_timeout(self, session_id: str, task_id: str | None = None) -> None:
        """Clear timeout task(s), preserving sibling session tasks."""
        keys = (
            [(session_id, task_id)]
            if task_id is not None
            else [key for key in self._task_timeout_tasks if key[0] == session_id]
        )
        for key in keys:
            task = self._task_timeout_tasks.get(key)
            if task and not task.done():
                task.cancel()
            self._task_timeout_tasks.pop(key, None)
            state = self._task_delivery_states.get(key)
            if state:
                state.task_timeout_task = None

    def _clear_session_timeout(self, session_id: str, task_id: str | None = None) -> None:
        """Clear periodic status task(s), preserving sibling session tasks."""
        keys = (
            [(session_id, task_id)]
            if task_id is not None
            else [key for key in self._session_timeout_tasks if key[0] == session_id]
        )
        for key in keys:
            task = self._session_timeout_tasks.get(key)
            if task and not task.done():
                task.cancel()
            self._session_timeout_tasks.pop(key, None)
            state = self._task_delivery_states.get(key)
            if state:
                state.session_timeout_task = None

    async def _send_push_notification(
        self,
        text: str,
        push_text: str,
        *,
        push_id: str | None = None,
        notification_id: str | None = None,
        state: XiaoyiTaskDeliveryState | None = None,
    ) -> PushDeliveryResult:
        """Send one task or cron Push with bounded retry for transient failures."""

        resolved_push_id = str(push_id if push_id is not None else self.config.push_id or "").strip()
        required_fields = ["api_id", "push_id"]
        if self.config.mode == "xiaoyi_claw":
            required_fields.extend(["uid", "api_key"])
        else:
            required_fields.extend(["ak", "sk"])
        values = {
            "api_id": self.config.api_id,
            "push_id": resolved_push_id,
            "uid": self.config.uid,
            "api_key": self.config.api_key,
            "ak": self.config.ak,
            "sk": self.config.sk,
        }
        missing = [name for name in required_fields if not str(values[name] or "").strip()]
        if missing:
            logger.warning("[PUSH_STATE] action=skipped_missing_config fields=%s", ",".join(missing))
            return PushDeliveryResult(
                accepted=False,
                http_status=None,
                error_code="missing_config",
                error_message=f"Missing Push configuration: {', '.join(missing)}",
                retryable=False,
            )

        push_service = XiaoYiPushService(
            PushConfig(
                mode=self.config.mode,
                api_id=self.config.api_id,
                push_id=resolved_push_id,
                push_url=self.config.push_url,
                ak=self.config.ak,
                sk=self.config.sk,
                uid=self.config.uid,
                api_key=self.config.api_key,
            )
        )
        request_id = notification_id or str(uuid.uuid4())
        result = PushDeliveryResult(
            accepted=False,
            http_status=None,
            error_code="not_attempted",
            error_message="Push was not attempted",
            retryable=False,
        )
        for attempt, delay_seconds in enumerate((0, 1, 5), start=1):
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            if state:
                state.push_attempts = attempt
            result = await push_service.send_push(
                text,
                push_text,
                notification_id=request_id,
            )
            logger.info(
                "[PUSH_STATE] session_id=%s task_id=%s attempt=%s accepted=%s "
                "retryable=%s http_status=%s trace_id=%s push_id=%s",
                state.session_id if state else "<cron>",
                state.task_id if state else "<cron>",
                attempt,
                result.accepted,
                result.retryable,
                result.http_status,
                result.trace_id,
                self._mask_push_id(resolved_push_id),
            )
            if result.accepted or not result.retryable:
                break
        return result

    async def send_xiaoyi_phone_tools_command(
            self,
            session_id: str,
            task_id: str,
            message_id: str,
            command: dict[str, Any],
            final: bool = False,
    ) -> bool:
        """发送 Command 指令到手机端（A2A artifact-update 格式）.

        Args:
            session_id: 会话 ID
            task_id: 任务 ID
            message_id: 消息 ID（用于 JSON-RPC id）
            command: Command 数据结构，包含 header 和 payload

        Returns:
            是否发送成功
        """
        response = {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "taskId": task_id,
                "kind": "artifact-update",
                "append": False,
                "lastChunk": True,
                "final": final,
                "artifact": {
                    "artifactId": str(uuid.uuid4()),
                    "parts": [{"kind": "data", "data": {"commands": [command]}}],
                },
            },
        }

        # OutboundWebSocketMessage：msgType/agentId/sessionId/taskId/msgDetail（msgDetail 为 JSON 字符串）
        wrapper = {
            "msgType": "agent_response",
            "agentId": self.config.agent_id,
            "sessionId": session_id,
            "taskId": task_id,
            "msgDetail": json.dumps(response, ensure_ascii=False),
        }

        # 发送到所有活跃连接
        privilege_intent = (
            command.get("payload", {})
            .get("executeParam", {})
            .get("intentName")
        )
        if privilege_intent == "CheckPlugInPrivilege":
            logger.info(
                "[CRON_DEVICE] phase=PRIVILEGE_WIRE_SEND "
                "agent_id=%s session_id=%s task_id=%s message_id=%s "
                "wrapper=%r",
                self.config.agent_id,
                session_id,
                task_id,
                message_id,
                wrapper,
            )

        sent = False
        is_gui_command = (
            command.get("header", {}).get("namespace") == "ClawAgent"
            and command.get("header", {}).get("name")
            == "InvokeJarvisGUIAgentRequest"
        )
        if is_gui_command:
            logger.info(
                "[GUI_RPC_TRACE] phase=CHANNEL_SEND_ENTER session_id=%s "
                "task_id=%s message_id=%s active_connection_count=%s",
                session_id,
                task_id,
                message_id,
                sum(bool(ws) for ws in self._ws_connections.values()),
            )
        for url_key, ws in self._ws_connections.items():
            if ws:
                try:
                    if is_gui_command:
                        logger.info(
                            "[GUI_RPC_TRACE] phase=CHANNEL_WS_SEND_BEGIN "
                            "session_id=%s task_id=%s message_id=%s "
                            "connection=%s",
                            session_id,
                            task_id,
                            message_id,
                            url_key,
                        )
                    await self._safe_ws_send(url_key, wrapper)
                    if is_gui_command:
                        logger.info(
                            "[GUI_RPC_TRACE] phase=CHANNEL_WS_SEND_DONE "
                            "session_id=%s task_id=%s message_id=%s "
                            "connection=%s success=true",
                            session_id,
                            task_id,
                            message_id,
                            url_key,
                        )
                    intent_name = command.get("payload", {}).get("executeParam", {}).get("intentName") or command.get(
                        "header", {}
                    ).get("name", "unknown")
                    logger.info(f"XiaoyiChannel 发送 command 成功 ({url_key}):intent={intent_name}")
                    sent = True
                except Exception as e:
                    if is_gui_command:
                        logger.warning(
                            "[GUI_RPC_TRACE] phase=CHANNEL_WS_SEND_DONE "
                            "session_id=%s task_id=%s message_id=%s "
                            "connection=%s success=false error_type=%s",
                            session_id,
                            task_id,
                            message_id,
                            url_key,
                            type(e).__name__,
                        )
                    logger.warning(f"XiaoyiChannel 发送 command 失败 ({url_key}): {e}")

        if is_gui_command:
            logger.info(
                "[GUI_RPC_TRACE] phase=CHANNEL_SEND_EXIT session_id=%s "
                "task_id=%s message_id=%s sent=%s",
                session_id,
                task_id,
                message_id,
                sent,
            )
        return sent

    async def execute_phone_tool_command(
        self,
        request: DeviceCommandRequest,
    ) -> dict[str, Any]:
        context = request.context
        session_id = (
            context.xiaoyi_root_session_id
            or context.xiaoyi_params_session_id
            or context.jiuwen_session_id
            or ""
        )
        if not session_id:
            raise RuntimeError("Xiaoyi session_id is missing")
        task_id = context.xiaoyi_task_id or session_id
        message_id = (
            context.xiaoyi_rpc_id
            if request.intent_name == "CheckPlugInPrivilege"
            and context.xiaoyi_rpc_id
            else f"cmd_{request.operation_id}"
        )
        if request.intent_name == "CheckPlugInPrivilege":
            lock = self._privilege_check_lock
        else:
            lock_key = (session_id, request.intent_name)
            lock = self._device_command_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            return await self._execute_phone_tool_command_locked(
                request=request,
                session_id=session_id,
                task_id=task_id,
                message_id=message_id,
            )

    async def _execute_phone_tool_command_locked(
        self,
        *,
        request: DeviceCommandRequest,
        session_id: str,
        task_id: str,
        message_id: str,
    ) -> dict[str, Any]:
        result_event = asyncio.Event()
        result_data: dict[str, Any] | None = None
        error: Exception | None = None
        started_at = time.monotonic()

        def on_data_event(event: DataEvent) -> None:
            nonlocal result_data, error
            if event.intent_name != request.intent_name:
                return
            if request.intent_name == "CheckPlugInPrivilege":
                result_data = {} if event.outputs is None else event.outputs
            elif _is_data_event_status_success(event.status):
                result_data = {} if event.outputs is None else event.outputs
            else:
                error = RuntimeError(f"Device execution failed: {event.status}")
            result_event.set()

        self.register_data_event_handler(request.intent_name, on_data_event)
        try:
            logger.info(
                "[XiaoyiChannel] device command send: rpc_id=%s operation_id=%s source_request_id=%s "
                "session_id=%s task_id=%s intent_name=%s pid=%s",
                request.rpc_id,
                request.operation_id,
                request.context.source_request_id,
                session_id,
                task_id,
                request.intent_name,
                os.getpid(),
            )
            sent = await self.send_xiaoyi_phone_tools_command(
                session_id=session_id,
                task_id=task_id,
                message_id=message_id,
                command=request.command,
            )
            if not sent:
                raise RuntimeError("Xiaoyi WebSocket is not connected")

            await asyncio.wait_for(
                result_event.wait(),
                timeout=request.timeout_seconds,
            )
            if error is not None:
                raise error
            return {} if result_data is None else result_data
        finally:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            logger.info(
                "[XiaoyiChannel] device command finished: rpc_id=%s operation_id=%s source_request_id=%s "
                "session_id=%s task_id=%s intent_name=%s pid=%s elapsed_ms=%s",
                request.rpc_id,
                request.operation_id,
                request.context.source_request_id,
                session_id,
                task_id,
                request.intent_name,
                os.getpid(),
                elapsed_ms,
            )
            self.unregister_data_event_handler(request.intent_name, on_data_event)

    async def execute_scheduled_phone_tool_command(
        self,
        request: DeviceCommandRequest,
    ) -> dict[str, Any]:
        scheduled_device = request.context.metadata.get("scheduled_device")
        if not isinstance(scheduled_device, dict):
            logger.warning(
                "[CRON_DEVICE] phase=SCHEDULED_INTENT_REJECTED rpc_id=%s "
                "operation_id=%s intent_name=%s reason=missing_scheduled_device",
                request.rpc_id,
                request.operation_id,
                request.intent_name,
            )
            raise RuntimeError(
                "Scheduled Xiaoyi device permissions are missing; "
                "recreate the cron job"
            )
        push_id = str(scheduled_device.get("push_id") or "").strip()
        required_intents = scheduled_device.get("required_intents")
        allowed_intents = {
            str(item or "").strip()
            for item in required_intents
            if str(item or "").strip()
        } if isinstance(required_intents, list) else set()
        if not push_id:
            raise RuntimeError("Scheduled Xiaoyi push_id is missing")
        if not allowed_intents:
            logger.warning(
                "[CRON_DEVICE] phase=SCHEDULED_INTENT_REJECTED rpc_id=%s "
                "operation_id=%s intent_name=%s reason=empty_required_intents",
                request.rpc_id,
                request.operation_id,
                request.intent_name,
            )
            raise RuntimeError(
                "Scheduled Xiaoyi device intents are missing; "
                "recreate the cron job"
            )
        if request.intent_name not in allowed_intents:
            logger.warning(
                "[CRON_DEVICE] phase=SCHEDULED_INTENT_REJECTED rpc_id=%s "
                "operation_id=%s intent_name=%s reason=intent_not_allowed",
                request.rpc_id,
                request.operation_id,
                request.intent_name,
            )
            raise RuntimeError(
                f"Intent {request.intent_name} is not allowed by the scheduled device context"
            )

        async with self._scheduled_device_command_lock:
            result_event = asyncio.Event()
            result_data: dict[str, Any] | None = None
            error: Exception | None = None
            started_at = time.monotonic()

            def on_data_event(event: DataEvent) -> None:
                nonlocal result_data, error
                if event.intent_name != request.intent_name:
                    return
                if _is_data_event_status_success(event.status):
                    result_data = {} if event.outputs is None else event.outputs
                else:
                    error = RuntimeError(
                        f"Device execution failed: {event.status}"
                    )
                result_event.set()

            self.register_data_event_handler(request.intent_name, on_data_event)
            try:
                push_config = PushConfig(
                    mode=self.config.mode,
                    api_id=self.config.api_id,
                    push_id=push_id,
                    push_url=self.config.push_url,
                    ak=self.config.ak,
                    sk=self.config.sk,
                    uid=self.config.uid,
                    api_key=self.config.api_key,
                )
                push_service = XiaoYiPushService(push_config)
                logger.info(
                    "[CRON_DEVICE] phase=DIRECTIVE_SEND_BEGIN rpc_id=%s "
                    "operation_id=%s intent_name=%s",
                    request.rpc_id,
                    request.operation_id,
                    request.intent_name,
                )
                sent = await push_service.send_push_with_directives(
                    push_id=push_id,
                    session_id=str(uuid.uuid4()),
                    directives=[request.command],
                )
                if not sent:
                    raise RuntimeError("Failed to send Xiaoyi directive push")
                await asyncio.wait_for(
                    result_event.wait(),
                    timeout=request.timeout_seconds,
                )
                if error is not None:
                    raise error
                return {} if result_data is None else result_data
            finally:
                self.unregister_data_event_handler(
                    request.intent_name,
                    on_data_event,
                )
                logger.info(
                    "[CRON_DEVICE] phase=DIRECTIVE_SEND_DONE rpc_id=%s "
                    "operation_id=%s intent_name=%s elapsed_ms=%s",
                    request.rpc_id,
                    request.operation_id,
                    request.intent_name,
                    int((time.monotonic() - started_at) * 1000),
                )

    def _get_a2a_parts(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """从直连或 Wrapped A2A 消息中取出 message.parts."""
        msg_type = message.get("msgType")
        if msg_type == "data":
            try:
                a2a_request = json.loads(message.get("msgDetail", "{}"))
            except json.JSONDecodeError:
                return []
            params = a2a_request.get("params", {})
        else:
            params = message.get("params", {})
        msg = params.get("message", {})
        parts = msg.get("parts", [])
        return parts if isinstance(parts, list) else []

    async def _dispatch_gui_agent_events(self, message: dict[str, Any]) -> None:
        """分发 InvokeJarvisGUIAgentResponse（data.events 内）.

        各 handler 独立 try/except，单个工具回调异常不影响同帧其他 handler 及后续 data-event。
        """
        if len(self._gui_agent_handlers) > 1:
            logger.warning(
                "XiaoyiChannel GUI handler 数量=%s，可能存在并发未串行化",
                len(self._gui_agent_handlers),
            )
        for part in self._get_a2a_parts(message):
            if part.get("kind") != "data":
                continue
            events = part.get("data", {}).get("events", [])
            if not isinstance(events, list):
                continue
            for item in events:
                if (
                        item.get("header", {}).get("namespace") == "ClawAgent"
                        and item.get("header", {}).get("name") == "InvokeJarvisGUIAgentResponse"
                ):
                    dispatch_item = dict(item)
                    dispatch_item["_xiaoyi_session_id"] = _gui_response_session_id(message)
                    payload = item.get("payload")
                    payload = payload if isinstance(payload, dict) else {}
                    stream_info = payload.get("streamInfo")
                    stream_info = stream_info if isinstance(stream_info, dict) else {}
                    content = stream_info.get("streamContent")
                    logger.info(
                        "[GUI_RPC_TRACE] phase=CHANNEL_GUI_FRAME_DISPATCH "
                        "session_id=%s interaction_id=%s is_final=%s "
                        "content_len=%s handler_count=%s",
                        dispatch_item["_xiaoyi_session_id"],
                        str(payload.get("interactionId") or ""),
                        payload.get("isFinal"),
                        len(str(content)) if content is not None else 0,
                        len(self._gui_agent_handlers),
                    )
                    logger.info(
                        "[GUI_AGENT_DIAG] phase=XIAOYI_GUI_EVENT_DISPATCH "
                        "session_id=%s interaction_id=%s raw_is_final=%r "
                        "stream_content=%r payload=%r event=%r",
                        dispatch_item["_xiaoyi_session_id"],
                        str(payload.get("interactionId") or ""),
                        payload.get("isFinal"),
                        content,
                        payload,
                        item,
                    )
                    for h in list(self._gui_agent_handlers):
                        try:
                            if asyncio.iscoroutinefunction(h):
                                await h(dispatch_item)
                            else:
                                h(dispatch_item)
                        except Exception as e:
                            logger.warning(
                                "XiaoyiChannel GUI agent 处理器异常（已隔离）: %s",
                                e,
                                exc_info=True,
                            )

    def register_gui_agent_handler(self, handler: Callable[[dict[str, Any]], Any]) -> None:
        """注册 InvokeJarvisGUIAgentResponse 处理器."""
        if handler not in self._gui_agent_handlers:
            self._gui_agent_handlers.append(handler)
            logger.info("XiaoyiChannel 注册 GUI agent 处理器")

    def unregister_gui_agent_handler(self, handler: Callable[[dict[str, Any]], Any]) -> None:
        """注销 GUI agent 处理器."""
        try:
            self._gui_agent_handlers.remove(handler)
            logger.info("XiaoyiChannel 注销 GUI agent 处理器")
        except ValueError:
            pass

    def register_data_event_handler(
            self, intent_name: str, handler: Callable[[DataEvent], Any]
    ) -> None:
        """注册 data-event 处理器.

        Args:
            intent_name: 要监听的 intent 名称（如 "GetCurrentLocation"）
            handler: 处理函数，接收 DataEvent 参数
        """
        if intent_name not in self._data_event_handlers:
            self._data_event_handlers[intent_name] = []
        if handler not in self._data_event_handlers[intent_name]:
            self._data_event_handlers[intent_name].append(handler)
            logger.info(f"XiaoyiChannel 注册 data-event 处理器: {intent_name}")

    def unregister_data_event_handler(
            self, intent_name: str, handler: Callable[[DataEvent], Any]
    ) -> None:
        """注销 data-event 处理器.

        Args:
            intent_name: intent 名称
            handler: 要移除的处理函数
        """
        if intent_name in self._data_event_handlers:
            try:
                self._data_event_handlers[intent_name].remove(handler)
                logger.info(f"XiaoyiChannel 注销 data-event 处理器: {intent_name}")
            except ValueError:
                pass

    async def _handle_data_event(self, event: DataEvent) -> None:
        """分发 data-event 到注册的处理器."""
        logger.info(f"[XiaoyiChannel] 分发 data-event: intent={event.intent_name}, status={event.status}")
        logger.info(f"[XiaoyiChannel] 已注册处理器: {list(self._data_event_handlers.keys())}")

        handlers = self._data_event_handlers.get(event.intent_name, [])
        if not handlers:
            logger.warning(f"[XiaoyiChannel] 无处理器处理 data-event: {event.intent_name}")
            return

        logger.info(f"[XiaoyiChannel] 找到 {len(handlers)} 个处理器 for {event.intent_name}")

        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as e:
                logger.warning(f"XiaoyiChannel data-event 处理器异常 ({event.intent_name}): {e}")

    def _extract_data_event(self, message: dict[str, Any]) -> DataEvent | None:
        """从 A2A 消息中提取 data-event（如果是 data-only 消息）.

        支持三种消息格式：
        1. Direct A2A format: 直接包含 params.message.parts
        2. Wrapped format (msgType="data"): A2A 内容在 msgDetail 中
        3. UploadExeResult 格式: header.name="UploadExeResult" + payload.intentName + payload.outputs

        Args:
            message: 解析后的 A2A 消息

        Returns:
            DataEvent 或 None（如果不是 data-only 消息）
        """
        # Wrapped format：msgType="data"，msgDetail 为嵌套的 A2A JSON-RPC 字符串
        msg_type = message.get("msgType")
        method = message.get("method")
        if msg_type == "data":
            try:
                # 从 msgDetail 解析 A2A JSON-RPC 请求
                a2a_request = json.loads(message.get("msgDetail", "{}"))
                params = a2a_request.get("params", {})
                msg = params.get("message", {})
                parts = msg.get("parts", [])
                session_id = message.get("sessionId", "")
            except json.JSONDecodeError as e:
                logger.info(
                    f"[XiaoyiChannel] _extract_data_event: msgDetail JSON 解析失败: {e}"
                )
                return None
            except KeyError as e:
                logger.info(
                    f"[XiaoyiChannel] _extract_data_event: Wrapped A2A 缺少字段: {e}"
                )
                return None
        else:
            # Direct A2A format
            params = message.get("params", {})
            msg = params.get("message", {})
            parts = msg.get("parts", [])
            session_id = message.get("sessionId", "")

        if not parts:
            return None

        # 检查是否所有 parts 都是 data 类型
        data_parts = [p for p in parts if p.get("kind") == "data"]
        if not data_parts or len(data_parts) != len(parts):
            return None

        # 提取 data 内容
        for part in data_parts:
            data = part.get("data", {})
            events = data.get("events", [])
            if not isinstance(events, list):
                continue

            for event in events:
                intent_name = ""
                outputs = {}
                status = "success"  # 未显式给出时与直接格式默认一致

                # 格式 1: 直接格式 (events[].intentName)
                if event.get("intentName"):
                    intent_name = event.get("intentName", "")
                    outputs = event.get("outputs", {})
                    status = event.get("status", "success")

                # 格式 2: UploadExeResult 包装格式 (header.name + payload)
                elif event.get("header", {}).get("name") == "UploadExeResult":
                    payload = event.get("payload", {})
                    intent_name = payload.get("intentName", "")
                    outputs = payload.get("outputs", {})
                    # UploadExeResult 格式默认 status 为 success
                    status = payload.get("status", "success") or "success"

                # 格式 3: InvokeJarvisGUIAgentResponse（GUI 工具响应，跳过）
                elif event.get("header", {}).get("namespace") == "ClawAgent" and \
                        event.get("header", {}).get("name") == "InvokeJarvisGUIAgentResponse":
                    # GUI 响应不处理，继续检查下一个 event
                    continue

                if intent_name:
                    outputs_keys = list(outputs.keys())
                    logger.info(f"[XiaoyiChannel] Extracted data-event: intent={intent_name}, "
                                f"status={status}, outputs_keys={outputs_keys}")
                    return DataEvent(
                        intent_name=intent_name,
                        outputs=outputs,
                        status=status,
                        session_id=message.get("sessionId", ""),
                        task_id=params.get("id", ""),
                    )

        return None

