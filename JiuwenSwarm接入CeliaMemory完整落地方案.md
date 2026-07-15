# JiuwenSwarm 接入 Celia Memory 完整落地方案

## 1. 文档目的与代码基线

本文用于指导 coding agent 在 JiuwenSwarm 中完成 Celia Memory 接入。阅读者只需要具备以下两套代码：

1. JiuwenSwarm：`openJiuwen-ai/jiuwenswarm` 的 `copy_dev_0.2.3.beta1` 分支。
2. 小艺版 OpenClaw 完整备份，其中 Celia 适配代码位于：

```text
.openclaw/extensions/celia_memory/install/v2026-07-07-rc7/
```

JiuwenSwarm `copy_dev_0.2.3.beta1` 固定依赖 `openjiuwen==0.1.15.post3`。实现必须以该版本的 `MemoryProvider` 和 Rail 生命周期为准，不能以 AgentCore 其他版本替代。

本文的行为基准不是重新设计一套记忆系统，而是：

> 尽可能复刻 OpenClaw 对 Celia Memory 的实际接入行为，只把 OpenClaw Plugin SDK、Hook、Service 和 Workspace 文件机制适配成 JiuwenSwarm 的 Provider、Rail、Tool 和 Prompt Attachment 机制。

---

## 2. 背景与实现目标

### 2.1 Celia Memory 是什么

Celia Memory 是独立于 JiuwenSwarm 和 OpenClaw 的长期记忆引擎。当前发行包由两部分组成：

```text
OpenClaw TypeScript 适配层
        ↓ stdio MCP / JSON-RPC
celia_memory_mcp_server
        ↓
本地 SQLite + 远程 Chat / Embedding / Rerank 服务
```

核心实现位于 Linux ARM64 的闭源二进制：

```text
bin/celia_memory_mcp_server
```

OpenClaw 代码负责：

- 启动和管理二进制进程；
- 通过 stdio 发送 MCP JSON-RPC；
- 注册记忆工具；
- 在模型调用前加载记忆；
- 在一轮对话结束后写入对话；
- 读取 `.xiaoyiruntime` 中的 `MEMORYSTATE`；
- 管理 logical session、缓存和运行态数据。

Celia 对 Agent 展示四层语义记忆：

| 语义层 | Celia 接口 | 作用 |
|---|---|---|
| 全局概览 | `memory_get_l0_global_summary` | 用户画像、长期偏好和全局状态 |
| 场景记忆 | `memory_get_l1_index`、`memory_load_l1` | 按主题/场景聚合的摘要 |
| 原子事实 | `memory_search_l2` | 精确事实、偏好、流程和可复用经验 |
| 原始会话 | `memory_search_l3` | 历史对话原文和上下文 |

### 2.2 JiuwenSwarm 已有能力

JiuwenSwarm 已提供通用 External Memory 装配链：

```text
memory.engine + memory.external.provider
        ↓
external_memory_config.py
        ↓
external_memory_builder.py
        ↓
MemoryProvider
        ↓
External Memory Rail
```

`openjiuwen==0.1.15.post3` 的 `MemoryProvider` 提供以下接口：

```python
name
is_available()
initialize()
get_tool_schemas()
handle_tool_call()
prefetch()
sync_turn()
system_prompt_block()
on_session_end()
shutdown()
is_initialized
```

### 2.3 最终目标

配置：

```yaml
memory:
  engine: external
  external:
    provider: celia
```

启用后，JiuwenSwarm 全局 External Memory 后端使用 Celia，与 channel 和 mode 无关。

实现结果必须满足：

1. Celia 是可插拔 External Memory Provider。
2. Jiuwen 核心代码不感知 Celia 的 L0/L1/L2/L3、SQLite、MCP 或二进制细节。
3. 多个 Jiuwen session 共享一个 Celia MCP 子进程，而不是每个 session 启动一个进程。
4. Tool、`MEMORYSTATE`、logical session、固定加载、对话写入、重启和缓存行为尽可能与 OpenClaw 一致。
5. Celia 故障不能阻断 Jiuwen 主回答流程。

---

## 3. OpenClaw 行为基准

以下 OpenClaw 文件是实现事实来源：

```text
memory-plugin/index.ts
memory-plugin/src/core/client.ts
memory-plugin/src/core/session.ts
memory-plugin/src/core/service.ts
memory-plugin/src/core/config.ts
memory-plugin/src/core/client-env.ts
memory-plugin/src/core/borrowed-config.ts
memory-plugin/src/memory/hooks.ts
memory-plugin/src/memory/tools.ts
memory-plugin/src/memory/runtime-state.ts
memory-plugin/src/memory/l0Sync.ts
memory-plugin/src/memory/l1ScenesSync.ts
memory-plugin/src/memory/fixedContext.ts
memory-plugin/src/memory/agentsGuideSync.ts
memory-plugin/src/memory/store-signal.ts
memory-plugin/src/memory/session-prompt-buffer.ts
memory-plugin/src/memory/conversation-sanitizer.ts
shared/celia-client-singleton.ts
```

必须复刻的核心行为：

- 二进制启动命令和 stdio MCP 协议；
- Client 进程级单例和引用计数；
- `tools-{user_id}` logical session；
- MCP 进程崩溃自动重启；
- `MEMORYSTATE` 的读取路径和现有分支行为；
- L0 + L1 固定加载和 30 秒 TTL；
- preset 场景全量 + 动态场景按 `factCount` Top 10；
- `memory_store` 只写 SessionPromptBuffer 和 urgent 标记；
- `agent_end` 统一调用 `memory_add`；
- `deferred` / `deferred-urgent`；
- L2/L3 返回内容截断；
- 已加载 L1 path 的 per-conversation 追踪；
- session 结束时清理运行态缓存。

---

## 4. 总体架构

建议采用以下结构：

```text
config.yaml
   ↓
external_memory_config.py
   ↓
external_memory_builder.py
   ↓
CeliaMemoryRail
   ↓
CeliaMemoryProvider
   ├── Celia tool adapter
   ├── fixed-context loader
   ├── conversation capture
   └── runtime state
   ↓
CeliaClientManager（进程级共享）
   ↓
CeliaMcpClient
   ↓ stdio JSON-RPC
celia_memory_mcp_server
   ↓
celia_memory.db
```

### 为什么需要 `CeliaMemoryRail`

Jiuwen 自带 `ExternalMemoryRail` 可完成基础 Provider 初始化、工具注册、prefetch 和 sync，但无法完整复刻 OpenClaw：

1. 通用 Rail 对同一次 invoke 的 prefetch 做缓存；OpenClaw 的 `before_prompt_build` 在每次模型调用前都会注入 SessionPromptBuffer。
2. OpenClaw 在 `agent_end` 保存当前轮的 user、assistant thinking/toolCall 和失败 toolResult；通用 Rail 默认只向 Provider 传用户输入和最终回答。
3. OpenClaw 的运行态清理和 per-conversation L1 去重需要更明确的 session 生命周期。

因此新增一个 Celia 专用适配 Rail，但将其严格限制在 `memory/celia/rail.py`。它仍然通过标准 `MemoryProvider` 操作 Celia，不修改 AgentCore，不影响其他 External Memory Provider。

---

## 5. 文件改造清单

### 5.1 新增目录

```text
jiuwenswarm/agents/harness/common/memory/celia/
├── __init__.py
├── config.py
├── client_env.py
├── protocol.py
├── client.py
├── client_manager.py
├── session.py
├── runtime_state.py
├── runtime_store.py
├── fixed_context.py
├── formatter.py
├── sanitizer.py
├── tools.py
├── provider.py
├── rail.py
└── errors.py
```

### 5.2 修改现有文件

```text
jiuwenswarm/resources/config.yaml

jiuwenswarm/agents/harness/common/memory/
├── external_memory_config.py
└── external_memory_builder.py

jiuwenswarm/server/runtime/agent_adapter/
└── interface_deep.py
```

### 5.3 测试目录

```text
tests/unit/memory/celia/
tests/integration/memory/celia/
```

---

## 6. 配置设计

在 `memory.external` 中新增 Celia 配置：

```yaml
memory:
  engine: external

  external:
    provider: celia
    user_id: ${MEMORY_USER_ID:-openclaw-user}
    scope_id: user

    celia:
      server_binary_path: ${CELIA_MEMORY_BINARY_PATH:-}
      db_path: ${CELIA_MEMORY_DB_PATH:-}
      log_path: ${CELIA_MEMORY_LOG_PATH:-}
      tenant_id: ${CELIA_TENANT_ID:-default}
      vector_dim: ${CELIA_VECTOR_DIM:-}

      embed:
        base_url: ${OPENAI_EMBED_BASE_URL:-}
        api_key: ${OPENAI_EMBED_API_KEY:-}
        model: ${OPENAI_EMBED_MODEL:-}
        uid: ${CELIA_EMBED_UID:-}
        headers: {}

      chat:
        base_url: ${OPENAI_CHAT_BASE_URL:-}
        api_key: ${OPENAI_CHAT_API_KEY:-}
        model: ${OPENAI_CHAT_MODEL:-}
        uid: ${CELIA_CHAT_UID:-}
        headers: {}

      rerank:
        base_url: ${OPENAI_RERANK_BASE_URL:-}
        api_key: ${OPENAI_RERANK_API_KEY:-}
        model: ${OPENAI_RERANK_MODEL:-}

      procedural_dir: ${CELIA_PROCEDURAL_DIR:-}
      procedural_learn_debug: false
      dreaming_enabled: false
```

### 6.1 配置优先级

按照 OpenClaw 的三档逻辑实现：

```text
Celia 显式子配置
    ↓
从 Jiuwen 全局配置借用
    ↓
环境变量兜底
```

Jiuwen 借用映射：

- Embedding：`get_embed_config()` 返回的 `embed_api_key / embed_base_url / embed_model`。
- Chat：`models.defaults` 中 `is_default=true` 的模型；找不到时取第一个。
- Chat 字段映射：
  - `api_base` → `OPENAI_CHAT_BASE_URL`
  - `api_key` → `OPENAI_CHAT_API_KEY`
  - `model_name` → `OPENAI_CHAT_MODEL`
  - `custom_headers` → `OPENAI_CHAT_HEADERS_JSON`
- 对 OpenClaw 中 `SERVICE_URL + /celia-claw/v1/sse-api`、`PERSONAL_UID`、`CELIA_CHAT_UID` 的 fallback 逻辑进行等价移植。

### 6.2 生成子进程环境变量

`client_env.py` 输出与 OpenClaw 一致的变量：

```text
OPENAI_EMBED_BASE_URL
OPENAI_EMBED_API_KEY
OPENAI_EMBED_MODEL
OPENAI_EMBED_HEADERS_JSON
CELIA_EMBED_UID

OPENAI_CHAT_BASE_URL
OPENAI_CHAT_API_KEY
OPENAI_CHAT_MODEL
OPENAI_CHAT_HEADERS_JSON
CELIA_CHAT_UID

OPENAI_RERANK_BASE_URL
OPENAI_RERANK_API_KEY
OPENAI_RERANK_MODEL

CELIA_TENANT_ID
CELIA_VECTOR_DIM
CELIA_PROCEDURAL_DIR
CELIA_PROCEDURAL_LEARN_DEBUG
CELIA_DREAMING_ENABLED
```

不得把 `x-api-key` 和 `x-uid` 同时放进 extra headers；它们走专用字段，校验规则按 OpenClaw `config.ts` 复刻。

---

## 7. MCP Client 实现

### 7.1 启动命令

```text
celia_memory_mcp_server <db_path> --log-file <log_path>
```

使用：

```python
asyncio.create_subprocess_exec(
    binary_path,
    db_path,
    "--log-file",
    log_path,
    stdin=PIPE,
    stdout=PIPE,
    stderr=PIPE,
    env=child_env,
)
```

### 7.2 MCP 初始化

启动成功后发送：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-03-26",
    "clientInfo": {
      "name": "jiuwenswarm-memory-celia",
      "version": "0.1.0"
    }
  }
}
```

随后发送：

```json
{
  "jsonrpc": "2.0",
  "method": "notifications/initialized"
}
```

工具调用统一为：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "memory_search_l2",
    "arguments": {}
  }
}
```

### 7.3 `CeliaMcpClient` 必须实现

```python
class CeliaMcpClient:
    async def start(self) -> None: ...
    async def wait_ready(self, timeout_ms: int | None = None) -> bool: ...
    async def call_tool(
        self,
        name: str,
        args: dict,
        timeout_ms: int | None = None,
        trace_id: str | None = None,
    ) -> object: ...
    async def load_l1_batch(...): ...
    async def close(self, grace_ms: int = 5000) -> None: ...
    def is_connected(self) -> bool: ...
```

内部必须包含：

- 自增 JSON-RPC request ID；
- `pending[id] -> Future`；
- stdout 按行缓冲；
- stdin 写锁；
- 并发请求；
- 单请求 timeout；
- MCP error 转异常；
- `content[].text` 双层 JSON 解码；
- stderr 独立读取；
- 子进程 generation；
- 老进程退出回调不得误杀新进程；
- 子进程退出时 reject 所有 pending Future；
- 幂等 `start()`；
- `SIGTERM → 5 秒 → SIGKILL`。

### 7.4 自动重启

与 OpenClaw 保持一致：

```text
1s → 2s → 4s → 8s → ... → 30s
最多连续 10 次
稳定运行 30s 后重置失败计数
```

每次重新 spawn 后：

- 清空 logical session cache；
- 清空固定加载 TTL；
- 将 L0/L1 标记为 dirty。

### 7.5 运行时协议校验

初始化完成后调用 `tools/list`，至少校验 OpenClaw 使用的 MCP 工具是否存在。`tools/list` 用于版本兼容检查，但具体调用参数以 OpenClaw 代码为默认协议。

---

## 8. 进程级共享 Manager

OpenClaw 通过 `globalThis + Symbol.for()` 保证一个进程只启动一个 Celia MCP Server。Jiuwen 中实现模块级单例：

```python
class CeliaClientManager:
    async def acquire(spec: CeliaProcessSpec) -> CeliaClientLease: ...
    async def release(lease: CeliaClientLease) -> None: ...
    async def close_all() -> None: ...
```

### 8.1 规则

1. 相同进程配置复用同一个 Client。
2. 每个 Provider `initialize()` acquire 一次。
3. 每个 Provider `shutdown()` release 一次。
4. 引用计数归零后关闭子进程。
5. acquire/release 使用 `asyncio.Lock`。
6. 同一个规范化 `db_path` 不允许启动两个不同 Celia 进程。
7. 配置不一致但 DB 相同时抛出明确冲突错误。
8. API Key 不得出现在日志、registry key 或异常文本中。

由于配置是全局单 Provider，第一份有效配置创建 Client；后续 Provider 必须复用相同配置。

---

## 9. Logical Session

OpenClaw 要求先调用 `memory_open`：

```text
logical session ID = tools-{user_id}
```

实现：

```python
class CeliaSessionManager:
    async def ensure_tool_session(client, user_id: str) -> str: ...
    def clear(self) -> None: ...
```

状态：

```text
open_sessions: set[str]
pending_opens: dict[str, Task]
```

行为：

- 同一 user 只调用一次 `memory_open`；
- 并发调用共享一个 in-flight Task；
- 二进制重启后清空；
- 下一次请求重新执行 `memory_open`。

请求：

```json
{
  "sessionId": "tools-openclaw-user",
  "userId": "openclaw-user"
}
```

Jiuwen session ID 不得替代 Celia user ID；Jiuwen session 作为 `conversationId`。

---

## 10. `MEMORYSTATE` 热切换

### 10.1 状态来源

按 OpenClaw `runtime-state.ts` 原样移植路径优先级：

```text
CELIA_XIAOYI_RUNTIME_PATH
→ CELIA_CONFIG_DIR/.xiaoyiruntime
→ ~/.openclaw/.xiaoyiruntime
```

文件格式：

```text
MEMORYSTATE=true
```

解析：

```text
true / 1  → 1
false / 0 → 0
文件缺失、字段缺失、读取失败、非法值 → 0
```

该状态由小艺端的 `MemoryStateSet` 事件写入 `.xiaoyiruntime`。Jiuwen 只负责读取，不负责重新定义该协议。

### 10.2 必须复刻的现有行为

`memoryState=0` 时：

- `memory_scene_load` 返回 `memory_disabled`；
- `memory_record_search` 返回 `memory_disabled`；
- `memory_scene_list_load` 返回 `memory_disabled`；
- `memory_chat_history_search` 仍然执行；
- `memory_get_global_summary` 仍然执行；
- `memory_store` 仍写 SessionPromptBuffer 和 urgent 标记；
- `memory_forget`、`memory_flush`、`memory_list` 仍然执行；
- 固定 L0/L1 加载不增加额外状态门禁；
- 每轮仍调用 `memory_add`，只把 `memoryState=0` 传给二进制；
- Provider、Tool 和 MCP Server 均保持注册和运行。

因此热切换是运行时软门禁，不是动态卸载 Provider。

---

## 11. 运行态 Store

新增 `runtime_store.py`，等价移植 OpenClaw 的三个状态容器。

### 11.1 SessionPromptBuffer

Key：

```text
tenant_id:user_id:conversation_id
```

限制与 OpenClaw 一致：

```text
最多 1024 个 session
每个 session 最多 16 条
单条最多 800 UTF-8 bytes
每个 session 总计最多 4000 bytes
```

用于 `memory_store` 后立即在当前会话 Prompt 中可见，不持久化到 SQLite。

注入前必须复刻 OpenClaw 的防注入清洗：

- 换行折叠为空格；
- 删除 bidi override；
- 删除 `<|...|>` 控制序列；
- 清除 C0/C1 控制字符。

### 11.2 StoreUrgentIngest

Key 同上，保存为一次性 Set 标记：

```text
memory_store → mark
对话写入 → consume
```

有标记：

```text
ingestMode=deferred-urgent
```

无标记：

```text
ingestMode=deferred
```

### 11.3 Served L1 Paths

Key：

```text
tenant_id:user_id:conversation_id
```

`memory_scene_load` 成功后记录 path；`memory_record_search` 将这些 path 作为 `served_l1_paths` 传入 Celia，用于血缘去重/降权。

限制：最多 1024 个 conversation entry，Map/LRU 淘汰；session 结束时主动清理。

---

## 12. 固定上下文和 Prompt 注入

### 12.1 OpenClaw 行为

每次 `before_prompt_build`：

1. 获取 L0 全局概览；
2. 获取 L1 index；
3. 选择全部 preset scene；
4. 动态 scene 按 `factCount` 降序取 Top 10；
5. 注入静态 Celia 使用指南；
6. 注入当前 SessionPromptBuffer。

L0/L1 使用 30 秒 TTL；`memory_add` 和成功的 `memory_flush` 会标记 dirty，使下一次调用绕过 TTL。

### 12.2 Jiuwen 映射

`CeliaMemoryRail.before_model_call()` 每次模型调用都执行：

```text
provider.build_prompt_context()
    ├── fixed context（内部 30s TTL）
    └── session prompt buffer（每次读取，不走 TTL）
```

然后通过 Jiuwen `PromptAttachmentKind.MEMORY` 注入。

静态工具使用指南由 `provider.system_prompt_block()` 返回，内容直接移植 OpenClaw `STATIC_GUIDE_TEMPLATE`。

### 12.3 容量控制

复刻 OpenClaw marker 预算，即使 Jiuwen 不写 Markdown 文件：

```text
L0 MEMORY_OVERVIEW：4 KB
L1 MEMORY_SCENES：6 KB
GUIDE：1.5 KB
```

将 `safeWriteMarker.ts` 中与内容渲染和截断相关的纯函数移植到 `formatter.py`；不移植文件 marker 读写部分。

### 12.4 不自动检索 L2

OpenClaw 的固定加载只有 L0 和 L1。Jiuwen 不应在 `prefetch()` 中自动调用 L2。L2 由模型根据 GUIDE 显式调用 `memory_record_search`，否则会偏离 OpenClaw 行为。

---

## 13. Tool 实现

Tool schema、描述和返回结构优先从 `tools.ts` 等价移植。

### 13.1 高级工具

```text
memory_store
memory_forget
memory_scene_load
memory_record_search
memory_chat_history_search
memory_scene_list_load
memory_get_global_summary
memory_flush
memory_list
memory_dump
dream_status
dream_run_summary
dream_recent_runs
```

底层 MCP 工具 `memory_open` 和 `memory_add` 不暴露给模型。

若当前二进制 `tools/list` 不包含 dream 或 dump 工具，则对应高级工具不注册，并输出兼容性警告。

### 13.2 `memory_store`

参数：

```json
{"text": "..."}
```

行为：

```text
写 SessionPromptBuffer
→ 标记 StoreUrgentIngest
→ 返回 Noted
```

不得直接调用 `memory_add`。

### 13.3 `memory_forget`

- 有 `memoryId`：调用 `memory_delete`。
- 只有 query：先 `memory_search_l2(top_k=5)`。
- 无结果：返回未找到。
- 唯一结果且 `score > 0.9`：自动删除。
- 否则返回候选 ID，由模型/用户指定。

### 13.4 `memory_scene_load`

- `paths` 非空且最多 5 条；
- `memoryState=0` 时返回 OpenClaw 同构的 disabled payload；
- 调用 batch `memory_load_l1`；
- 成功后记录 served L1 path。

### 13.5 `memory_record_search`

- `memoryState=0` 时返回 disabled；
- 默认 `top_k=5`；
- 支持 `is_procedural`；
- 支持 `time_hint` 和自动时间词识别；
- 支持 `dedup_policy`；
- 注入 `served_l1_paths`；
- 每条 `results[].content` 最大 800 bytes，超出截断并附 `_trim`。

### 13.6 `memory_chat_history_search`

- 不受 `memoryState` 门禁；
- 调用 `memory_search_l3`；
- 支持 `sessionIdFilter`；
- 每条 content 最大 600 bytes。

### 13.7 `memory_scene_list_load`

- `memoryState=0` 时返回 disabled；
- 调用 `memory_get_l1_index`。

### 13.8 `memory_get_global_summary`

- 不受 `memoryState` 门禁；
- `tier` 映射为 `edge/cloud_s/cloud_l` 或二进制接受的 0/1/2；
- MCP 参数使用 `userId` camelCase。

### 13.9 `memory_flush`

- 不受 `memoryState` 门禁；
- 调用超时 120 秒；
- 成功后标记固定上下文 dirty。

### 13.10 `memory_list`

将语义分类映射为内部层：

```text
global_overview → l0
scene_memory    → l1
atomic_facts    → l2
```

---

## 14. 对话捕获与写入

### 14.1 `CeliaMemoryRail` 的 turn recorder

为了复刻 OpenClaw `agent_end`，Rail 在一次 invoke 内使用 `ctx.extra` 维护 turn events：

- `before_invoke`：记录用户 query；
- `after_model_call`：记录 assistant text/thinking/toolCall；
- `before_tool_call`：记录 toolCall 参数；
- `after_tool_call`：仅记录失败 toolResult 摘要；
- `after_invoke`：补充最终输出并完成写入。

如果当前 AgentCore 返回结构无法提供完整 block，则按以下顺序降级：

1. 从模型 response 提取完整 block；
2. 从 `ctx.context.get_messages()` 提取最后一轮；
3. 从 `ctx.inputs` 提取；
4. 最终降级为 user + final assistant。

### 14.2 清洗规则

按 OpenClaw `hooks.ts` 当前实现：

- user：只保留 text，并清除平台元数据；
- assistant：保留 `text`、`thinking`、`toolCall`；
- thinking：删除 `thinkingSignature`；
- toolCall：删除 id；
- 空 arguments 的 toolCall 丢弃；
- toolCall 中超长字符串字段保留前 200 字，并标注原长度；
- 成功 toolResult 丢弃；
- 失败 toolResult 保留最多 300 字；
- 所有 Celia memory toolResult 丢弃，避免自引用；
- system 等其他 role 丢弃；
- `/new`、`/reset` Session Startup 序列不写入。

### 14.3 写入参数

```json
{
  "tenant_id": "default",
  "content": "<cleaned round JSON>",
  "userId": "openclaw-user",
  "scope": "user",
  "sessionId": "tools-openclaw-user",
  "conversationId": "<jiuwen session id>",
  "ingestMode": "deferred | deferred-urgent",
  "memoryState": 0,
  "_trace_id": "..."
}
```

行为：

- heartbeat 跳过；
- 普通轮次始终调用 `memory_add`；
- `memoryState` 每次写入前重新读取；
- 成功后标记 L0/L1 dirty；
- 写入失败只记录日志，不影响 Agent 回答；
- 写超时不自动重试，因为服务端可能已经执行。

---

## 15. `CeliaMemoryProvider`

```python
class CeliaMemoryProvider(MemoryProvider):
    @property
    def name(self) -> str: ...
    def is_available(self) -> bool: ...
    async def initialize(self, **kwargs) -> None: ...
    def get_tool_schemas(self) -> list[dict]: ...
    async def handle_tool_call(self, tool_name, args) -> str: ...
    async def prefetch(self, query, **kwargs) -> str: ...
    async def sync_turn(self, user_msg, assistant_msg, **kwargs) -> None: ...
    def system_prompt_block(self) -> str: ...
    async def on_session_end(self, messages=None) -> None: ...
    async def shutdown(self) -> None: ...
```

职责：

- 持有配置和 Manager lease；
- 初始化共享 Client；
- 维护当前 `user_id / tenant_id / conversation_id`；
- 将 Tool 调用转成 Celia MCP 调用；
- 提供固定上下文；
- 写入清洗后的 turn；
- session end 时清理 prompt buffer 和 served L1；
- shutdown 时释放 Client 引用。

`is_available()` 只做本地静态检查，不启动进程、不发网络请求：

- Linux；
- ARM64/aarch64；
- binary 存在且可执行；
- DB 目录可写；
-必要路径合法。

---

## 16. `CeliaMemoryRail`

实现为 Jiuwen 内部 Celia 专用 Rail，职责只做框架生命周期适配：

```python
class CeliaMemoryRail(DeepAgentRail):
    def init(self, agent): ...
    def uninit(self, agent): ...
    async def before_invoke(self, ctx): ...
    async def before_model_call(self, ctx): ...
    async def after_model_call(self, ctx): ...
    async def before_tool_call(self, ctx): ...
    async def after_tool_call(self, ctx): ...
    async def after_invoke(self, ctx): ...
```

### `init`

- 注册 Provider tools；
- 注入 Celia GUIDE；
- 尝试异步 prewarm Provider；
- 不阻塞 Agent 构建。

### `before_invoke`

- await Provider 初始化；
- 创建本轮 capture buffer。

### `before_model_call`

- 每次模型调用都获取固定上下文；
- fixed L0/L1 由 Provider 内部 TTL 限流；
- SessionPromptBuffer 每次读取；
- 清除上一轮 Memory Attachment 后重新注入。

### `after_invoke`

- 复刻 OpenClaw `agent_end`；
- 异步串行写入；
- 连续失败断路器可沿用 ExternalMemoryRail：5 次失败，冷却 120 秒。

### `uninit`

- 同步注销工具和 Prompt section；
- 使用 `asyncio.create_task(provider.shutdown())`，不得在当前事件循环内 `run_coroutine_threadsafe(...).result()` 阻塞。

---

## 17. Builder 和 Adapter 修改

### 17.1 `external_memory_config.py`

`get_external_memory_config()` 增加：

```python
"celia": ext.get("celia") or {}
```

新增 `build_celia_provider_config()` 或在 `celia/config.py` 中处理。

### 17.2 `external_memory_builder.py`

扩展签名：

```python
def build_external_memory_rail(
    config=None,
    workspace_dir=".",
    session_id="__default__",
):
```

分支：

```python
if provider_name == "celia":
    provider = CeliaMemoryProvider(...)
    return CeliaMemoryRail(
        provider,
        user_id=ext_cfg["user_id"],
        scope_id=ext_cfg["scope_id"],
        session_id=session_id,
    )
```

其他 Provider 继续使用 AgentCore 的 `ExternalMemoryRail`。

### 17.3 `interface_deep.py`

修改 `_build_external_memory_rail()`：

```python
return build_external_memory_rail(
    config=get_config(),
    workspace_dir=self._workspace_dir,
    session_id=self._parent_session_id or "__default__",
)
```

这样每个 session-scoped Adapter 的 Celia Rail 都持有正确的 `conversationId`，多个 Provider 通过 Manager 共享同一个二进制。

无需增加 channel 或 mode 判断；当前 External Memory 本身就是 mode-independent。

---

## 18. 生命周期时序

### 18.1 首次请求

```text
读取 config
→ Builder 创建 Provider + CeliaMemoryRail
→ Rail 注册 Tool 和 GUIDE
→ Provider prewarm
→ before_invoke 等待 MCP ready
→ memory_open(tools-{user_id})
→ before_model_call 加载 L0/L1 + PromptBuffer
→ 模型执行
→ after_invoke memory_add
```

### 18.2 `memory_store`

```text
模型调用 memory_store
→ 写 SessionPromptBuffer
→ 设置 urgent 标记
→ 下一个 before_model_call 立即注入 buffer
→ after_invoke consume urgent
→ memory_add(deferred-urgent)
```

### 18.3 `memoryState=false`

```text
Provider 和二进制保持运行
→ L1 scene list/load 与 L2 search 返回 disabled
→ L3、L0、store、forget、flush、list 保持原行为
→ after_invoke 继续 memory_add(memoryState=0)
```

### 18.4 二进制崩溃

```text
exit
→ reject pending RPC
→ 清 logical session
→ 指数退避重启
→ initialize
→ 下一次调用重新 memory_open
```

### 18.5 session 结束

```text
clear SessionPromptBuffer
clear StoreUrgentIngest
clear served L1 paths
release Provider lease
引用计数归零时关闭 MCP Server
```

---

## 19. 失败处理和安全要求

### 19.1 Fail-soft

- binary 缺失：Builder 不挂载 Celia Rail；
- initialize 失败：Agent 仍可回答；
- fixed context 失败：返回空记忆上下文；
- Tool 失败：返回明确 error payload；
- memory_add 失败：仅日志告警；
- 进程崩溃：自动恢复。

### 19.2 日志

不得输出：

- API Key；
- 完整认证 Header；
- 完整记忆正文；
- 完整用户对话；
- 完整 DB 内容。

OpenClaw 当前存在将 `memory_add params` 和 cleaned message 写入日志的行为；Jiuwen 应保留 trace 和长度信息，但对正文做脱敏/截断，以满足生产安全要求。该差异只影响日志，不改变 Celia 功能行为。

### 19.3 DB 所有权

- 同一个 DB 只允许一个 Celia MCP Server；
- OpenClaw 和 Jiuwen 不得同时操作同一 DB；
- 切换前先关闭旧 Agent 和 Celia 进程；
- 正常 flush/关闭后再启动 Jiuwen。

---

## 20. 测试方案

### 20.1 Fake MCP Server

实现可控子进程，覆盖：

- initialize；
- tools/list；
- tools/call；
- 并发乱序响应；
- 半行 JSON；
- 多行一次返回；
- 非法 JSON；
- 未知 ID；
- MCP error；
- 超时；
- EOF；
- 进程崩溃；
- SIGTERM 不退出；
- 自动重启；
- 重启后重新 memory_open。

### 20.2 Client/Manager

- 多 Provider 只启动一个 PID；
- 相同 DB 复用；
- 相同 DB 不同配置拒绝；
- 引用计数；
- 并发 acquire/release；
- pending 请求在 close 前 drain。

### 20.3 Runtime State

- `true/1/false/0`；
- export 前缀；
- 单双引号；
- 文件缺失；
- 非法值；
- 自定义 runtime path。

### 20.4 Tool 对照测试

对每个高级工具建立 OpenClaw golden fixture：

- 输入 schema；
- 发给 MCP 的工具名和参数；
- 输出格式；
- `memoryState=0/1` 分支；
- 截断和候选删除逻辑。

### 20.5 固定上下文

- L0 4KB；
- L1 6KB；
- preset 全量；
- dynamic Top 10；
- 30 秒 TTL；
- memory_add / flush dirty；
- PromptBuffer 每次模型调用可见。

### 20.6 对话捕获

- user text；
- assistant text/thinking/toolCall；
- thinkingSignature 删除；
- toolCall 长参数截断；
- 成功 toolResult 丢弃；
- 失败 toolResult 300 字；
- memory tool result 丢弃；
- session startup 跳过；
- urgent ingest 一次性消费。

### 20.7 ARM64 真实联调

1. 启动 binary；
2. `tools/list`；
3. `memory_open`；
4. `memory_add → memory_flush → L3`；
5. 等待/flush 后验证 L2；
6. 验证 L1 index 和 scene load；
7. 验证 L0；
8. `MEMORYSTATE=true/false`；
9. kill 二进制后自动恢复；
10. 多 Jiuwen session 共享一个 PID；
11. 同一用户跨 session 召回。

### 20.8 OpenClaw/Jiuwen 行为对照

对相同 DB、相同 user、相同输入分别运行 OpenClaw 和 Jiuwen，比较：

- MCP tool 名；
- 参数；
- memoryState；
- logical session；
- ingestMode；
- Tool 返回；
- L0/L1 Prompt 内容；
- L2/L3 截断；
- 崩溃恢复。

---

## 21. 实施顺序

### 阶段 1：协议和 golden fixture

- 从 OpenClaw 提取所有 Tool schema、参数和响应样例；
- 在 ARM64 执行 `tools/list`；
- 固化测试 fixture。

### 阶段 2：MCP 基础设施

实现：

```text
config.py
client_env.py
protocol.py
errors.py
client.py
client_manager.py
session.py
```

### 阶段 3：运行态和 Tool

实现：

```text
runtime_state.py
runtime_store.py
tools.py
```

### 阶段 4：固定上下文

实现：

```text
fixed_context.py
formatter.py
STATIC_GUIDE_TEMPLATE
```

### 阶段 5：Provider 和 Rail

实现：

```text
provider.py
rail.py
sanitizer.py
```

### 阶段 6：Jiuwen 装配

修改：

```text
external_memory_config.py
external_memory_builder.py
interface_deep.py
config.yaml
```

### 阶段 7：真实环境对照验收

- OpenClaw/Jiuwen A/B；
- 小艺 UI 热切换；
- 跨 session；
- 崩溃恢复；
- 数据连续性。

---

## 22. 最终验收标准

1. 配置 `engine=external, provider=celia` 后只挂 Celia External Memory。
2. Celia 不与 channel 和 mode 耦合。
3. 多个 Jiuwen session 共享一个 MCP Server。
4. logical session 固定为 `tools-{user_id}`。
5. Tool 名称、参数、返回和 `MEMORYSTATE` 行为与 OpenClaw 一致。
6. L0/L1 固定加载内容和容量策略与 OpenClaw 等价。
7. L2 不被自动 prefetch，只按 GUIDE 显式查询。
8. `memory_store` 不直接写数据库，并在同一 invoke 的后续模型调用中可见。
9. `memory_add` 的参数、`deferred/deferred-urgent` 与 OpenClaw 一致。
10. 对话清洗尽可能保留 OpenClaw 的 text/thinking/toolCall/失败结果语义。
11. `memoryState` 切换无需重启 Provider 或二进制。
12. 二进制崩溃后自动重启并重新 `memory_open`。
13. Celia 故障不阻断 Jiuwen 回答。
14. 同一 DB 不会被两个 Celia 进程同时持有。
15. OpenClaw 与 Jiuwen 对相同输入产生等价 MCP 请求和记忆上下文。

