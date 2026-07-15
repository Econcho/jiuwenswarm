"""Celia HTTP MCP 客户端。

协议参考 demo/http/example_simple.py 和 example_complex.py，
走 JSON-RPC 2.0 的 HTTP 调用。只依赖标准库（urllib + json）。

Celia 服务把 MCP 工具调用的结果包成这种形状：
    result.content[0].text      -> 业务 payload 的 JSON 串
    result.content[0].isError   -> 可选 bool，true 时 text 是错误字符串

所有工具包装方法会把 text 解析后的 dict 返回；协议/HTTP/服务端失败
会抛 McpError（按 kind 分级，调用方据此决定重试或放弃）。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class McpError(Exception):
    """MCP 调用失败时抛出。kind 取值：

    - "network"       : urllib 传输层错误，通常可重试
    - "http_5xx"      : 服务端 5xx，通常可重试
    - "http_4xx"      : 客户端 4xx，不可重试（参数问题）
    - "mcp_is_error"  : 工具返回 isError=true，业务级错误
    - "json_rpc"      : JSON-RPC 协议级错误
    - "decode"        : 服务端返回了非 JSON 内容
    """

    def __init__(self, kind: str, message: str, *, attempts: int = 1,
                 payload: Any = None) -> None:
        super().__init__(f"[{kind}] {message}")
        self.kind = kind
        self.message = message
        self.attempts = attempts
        self.payload = payload


@dataclass
class RetryPolicy:
    """重试策略：最大尝试次数 + 指数退避 + 单次超时。"""

    max_attempts: int = 3
    base_backoff_seconds: float = 1.0
    timeout_seconds: float = 120.0

    def backoff(self, attempt: int) -> float:
        """计算第 attempt 次尝试失败后应等待的秒数（attempt 1-indexed）。"""
        return self.base_backoff_seconds * (2 ** (attempt - 1))


class CeliaClient:
    """面向 Celia memory 服务的最小 HTTP MCP 客户端。

    `_id` 计数器用线程锁保护——这样多线程并行调同一个 client 时，
    每个请求拿到的 JSON-RPC id 仍然唯一，便于在服务端日志里对账。
    """

    def __init__(self, base_url: str, session_id: str,
                 policy: RetryPolicy | None = None,
                 client_name: str = "migrate-openclaw",
                 client_version: str = "0.1.0") -> None:
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id
        self.policy = policy or RetryPolicy()
        self.client_name = client_name
        self.client_version = client_version
        self._id = 0
        self._id_lock = threading.Lock()

    # ---- endpoints ----------------------------------------------------

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/health"

    # ---- low-level ----------------------------------------------------

    def health(self) -> dict:
        """调用 /health 做一次连通性探测。"""
        try:
            with urllib.request.urlopen(self.health_url, timeout=5) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.URLError as e:
            raise McpError("network", f"health check failed: {e}") from e
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"raw": body}

    def initialize(self) -> dict:
        """发送 MCP initialize 握手。"""
        return self._json_rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": self.client_name,
                           "version": self.client_version},
        })

    def _next_id(self) -> int:
        """原子地递增并返回新的 JSON-RPC id。"""
        with self._id_lock:
            self._id += 1
            return self._id

    def _json_rpc(self, method: str, params: dict) -> dict:
        """发送一次 JSON-RPC 请求，返回其中的 `result` 字段。

        只对 network 和 http_5xx 重试；4xx 和 decode 错误立即抛出
        （不会因为重试而自动好转）。
        """
        last_error: McpError | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            payload = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": method,
                "params": params,
            }
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                self.mcp_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(
                    req, timeout=self.policy.timeout_seconds
                ) as resp:
                    body = resp.read().decode("utf-8")
            except urllib.error.HTTPError as e:
                status = e.code
                try:
                    err_body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    err_body = ""
                if 500 <= status < 600:
                    last_error = McpError(
                        "http_5xx",
                        f"HTTP {status}: {err_body[:200]}",
                        attempts=attempt,
                    )
                    self._sleep_for_retry(attempt)
                    continue
                # 4xx：参数/鉴权/路径等问题，重试没用
                raise McpError(
                    "http_4xx",
                    f"HTTP {status}: {err_body[:200]}",
                    attempts=attempt,
                ) from e
            except urllib.error.URLError as e:
                last_error = McpError(
                    "network", str(e), attempts=attempt
                )
                self._sleep_for_retry(attempt)
                continue

            try:
                envelope = json.loads(body)
            except json.JSONDecodeError as e:
                # 服务端返回非 JSON——多半是走错了 url 或有中间代理在改
                raise McpError(
                    "decode",
                    f"non-JSON response: {body[:200]}",
                    attempts=attempt,
                ) from e

            if "error" in envelope and envelope["error"] is not None:
                raise McpError(
                    "json_rpc",
                    json.dumps(envelope["error"], ensure_ascii=False),
                    attempts=attempt,
                    payload=envelope["error"],
                )
            return envelope.get("result", {})

        assert last_error is not None
        raise last_error

    def _sleep_for_retry(self, attempt: int) -> None:
        """最后一次尝试后不再 sleep，避免浪费时间。"""
        if attempt < self.policy.max_attempts:
            time.sleep(self.policy.backoff(attempt))

    # ---- tool wrappers -------------------------------------------------

    def _tool(self, name: str, arguments: dict) -> dict:
        """调用一个 MCP 工具，把 content[0].text 当 JSON 解析后返回。

        isError=true 时抛 McpError("mcp_is_error")。
        text 是非 JSON 串时，把原文包成 {"raw": text} 返回，让调用方
        自己决定怎么处理（避免把错误吞掉）。
        """
        result = self._json_rpc("tools/call", {
            "name": name,
            "arguments": arguments,
        })
        content = result.get("content") or []
        if not content:
            return {}
        first = content[0] or {}
        text = first.get("text", "")
        if first.get("isError"):
            raise McpError("mcp_is_error", text or "(no message)",
                           payload={"tool": name, "arguments": arguments})
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    def open_session(self, user_id: str) -> dict:
        """打开一个 Celia memory session。"""
        return self._tool("memory_open", {
            "sessionId": self.session_id,
            "userId": user_id,
        })

    def memory_add(self, user_id: str, content: str,
                   scope: str = "user",
                   agent_id: str | None = None,
                   ingest_mode: str | None = None) -> dict:
        """写入一条记忆，返回服务端生成的 id 等元信息。

        `ingest_mode` 语义（与 mcp/tools/mcp_tools.c::ExtractAddOptionalFields
        约定一致）：
          - None / 省略 → 服务端缺省 `immediate`：同步单轮 LLM 提取。
            适合短小、单事实内容；对结构化 markdown 文档效果差。
          - "deferred" → 入队 `mem_conversation` 表（DB 持久化），
            由 InstanceWorker 的 Timer 按对话窗口管线批量提取出多条
            原子 L2 事实。**迁移场景应首选此模式**——服务重启后仍会
            被 worker 消费，USER.md 这种多事实文档会被正确原子化。
          - "deferred-urgent" → 同 deferred，但立刻 signal worker 尽快处理。
        """
        args: dict[str, Any] = {
            "sessionId": self.session_id,
            "userId": user_id,
            "scope": scope,
            "content": content,
        }
        if agent_id:
            args["agentId"] = agent_id
        if ingest_mode:
            args["ingestMode"] = ingest_mode
        return self._tool("memory_add", args)

    def memory_store_session(self, user_id: str, messages: list[dict],
                             timestamp: str,
                             scope: str = "user") -> dict:
        """整段会话写入（长期记忆迁移用不到，仅保留接口对齐 demo）。"""
        return self._tool("memory_store_session", {
            "sessionId": self.session_id,
            "userId": user_id,
            "scope": scope,
            "messages": messages,
            "timestamp": timestamp,
        })

    def memory_search_l2(self, user_id: str, query: str,
                         top_k: int = 5) -> dict:
        """语义检索——verify 阶段跑召回探针时用。"""
        args: dict[str, Any] = {
            "sessionId": self.session_id,
            "user_id": user_id,
            "query": query,
            "top_k": top_k,
        }
        return self._tool("memory_search_l2", args)

    def memory_list(self, user_id: str, limit: int = 100,
                      agent_id: str | None = None) -> dict:
        """按 userId 列出记忆——verify 阶段做对账用。"""
        args: dict[str, Any] = {
            "sessionId": self.session_id,
            "userId": user_id,
            "limit": limit,
            "layers": ["l2"],
        }
        if agent_id:
            args["agentId"] = agent_id
        return self._tool("memory_list", args)

    def memory_delete(self, user_id: str, memory_id: str) -> dict:
        """按 id 删除一条记忆。"""
        return self._tool("memory_delete", {
            "sessionId": self.session_id,
            "userId": user_id,
            "memoryId": memory_id,
        })
