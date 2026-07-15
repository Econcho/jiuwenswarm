# JiuwenSwarm 接入 Celia Memory：Coding Agent 实现说明

## 0. 文档用途与代码基线

本文用于给 coding agent 提供实现背景、代码阅读范围、架构约束和落地方向。目标不是复刻 OpenClaw 插件框架，而是复用 Celia Memory 二进制能力，并将其适配到 JiuwenSwarm 的 `MemoryProvider + ExternalMemoryRail` 机制中。

### 代码基线

- JiuwenSwarm：`openJiuwen-ai/jiuwenswarm`，分支 `copy_dev_0.2.3.beta1`
- JiuwenSwarm 版本：`0.2.3.beta1`
- JiuwenSwarm 声明依赖：`openjiuwen==0.1.15.post3`
- Celia Memory 参考包：同事提供的 `openClawFullBackup`
- Celia Memory 发行版本：`v2026-07-07-rc7`
- Celia Memory 核心二进制：`celia_memory_mcp_server`
- 二进制平台：Linux ARM64/aarch64，stripped ELF

> 重要：实现前必须以实际运行环境中 `openjiuwen==0.1.15.post3` 的源码为准检查 `MemoryProvider` 和 `ExternalMemoryRail` 接口。不要直接假定 agent-core `main` 分支与当前依赖版本完全一致。

---

# 1. 背景、已知信息与实现目标

## 1.1 背景

小艺 Claw 原先连接的 Agent 是修改版 OpenClaw。该 OpenClaw 环境安装了 Celia Memory 插件，并通过 OpenClaw Plugin SDK 将 Celia Memory 作为长期记忆后端。

当前业务要求是：当 JiuwenSwarm 作为小艺 Claw 后端 Agent 时，JiuwenSwarm 不再依赖自身内置长期记忆，而是实际使用 Celia Memory 完成跨会话记忆写入、召回、查询、删除和维护。

## 1.2 已知事实

### Celia Memory 的归属与形态

Celia Memory 是独立于 JiuwenSwarm 和 OpenClaw 的记忆引擎。OpenClaw 压缩包中的 TypeScript 代码是 OpenClaw 对 Celia Memory 的适配层，而不是 Celia Memory 核心算法本身。

当前 Celia Memory 的运行形态为：

```text
OpenClaw/Jiuwen Adapter
        ↓ stdio JSON-RPC / MCP
celia_memory_mcp_server
        ↓
本地 SQLite 数据库
        +
远程 Embedding / Chat / 可选 Rerank 服务
```

核心记忆算法、SQLite 存储、异步 ingest 和 L0/L1/L2/L3 处理主要位于已编译二进制中。

### Celia Memory 的四层记忆

| 层级 | 含义 | 典型底层 MCP 工具 |
|---|---|---|
| L0 | 用户全局概览、稳定画像和长期偏好 | `memory_get_l0_global_summary` |
| L1 | 场景或主题聚合摘要 | `memory_get_l1_index`、`memory_load_l1` |
| L2 | 原子事实、精确语义记忆 | `memory_search_l2` |
| L3 | 原始历史对话片段 | `memory_search_l3` |

### OpenClaw 的实际作用

OpenClaw 仍负责：

- Agent 当前会话和执行循环；
- Prompt 构造；
- Tool 注册；
- 生命周期 Hook；
- 对话轨迹捕获和清洗；
- Celia 子进程生命周期；
- 将 Celia L0/L1/L2/L3 结果注入模型上下文。

OpenClaw 只是把长期语义记忆 slot 切换为 Celia Memory，并没有把整个 Agent Runtime 替换掉。

### JiuwenSwarm 已有外部记忆框架

JiuwenSwarm 已有配置驱动的 External Memory 装配链：

```text
config.yaml
  ↓
external_memory_config.py
  ↓
external_memory_builder.py
  ↓
MemoryProvider
  ↓
ExternalMemoryRail
  ↓
Agent 生命周期
```

当前内置支持 `openjiuwen`、`mem0`、`openviking`、`lakebase`。Celia 尚未实现。

### “可插拔”的准确含义

本任务中的可插拔应理解为：

```yaml
memory:
  engine: external
  external:
    provider: celia
```

通过配置选择 `CeliaMemoryProvider`，由通用 `ExternalMemoryRail` 装配，而不是把 Celia 逻辑硬编码进 Agent、Prompt Builder 或小艺 Channel。

当前 `external_memory_builder.py` 中未知 Provider 会尝试导入 `plugin_discovery`，但该分支的插件发现能力可能尚未完整落地。因此第一版可将 Celia 作为仓内正式 Provider 实现，但必须遵守 `MemoryProvider` 抽象，保持配置可替换性。

## 1.3 实现目标

最终调用链应为：

```text
小艺用户请求
    ↓
Jiuwen Session Runtime
    ↓
ExternalMemoryRail
    ├─ init：注册 Celia 工具和静态使用说明
    ├─ before_invoke：初始化 Celia Provider/MCP Client
    ├─ before_model_call：从 Celia 召回记忆并注入 Prompt
    ├─ Tool Call：转发 Celia 记忆查询、删除、flush 等操作
    ├─ after_invoke：把本轮对话写入 Celia
    └─ uninit：释放 Provider 引用，必要时关闭子进程
    ↓
CeliaMemoryProvider
    ↓
CeliaMcpClientManager
    ↓ stdio MCP
celia_memory_mcp_server
    ↓
SQLite + Embedding/Chat 服务
```

### 首版目标

1. 配置 `memory.engine=external`、`memory.external.provider=celia` 时，只挂载 Celia External Memory。
2. Celia 二进制在 Jiuwen 进程内按进程级单例或引用计数方式管理，不能每个 Session 启动一个进程。
3. 支持 MCP 初始化、工具调用、并发请求关联、超时、异常退出和重启。
4. 回答前能召回 L0/L1/L2 相关内容并注入 Prompt。
5. 每轮结束后能调用 `memory_add` 写入对话。
6. 支持跨 Jiuwen Session 召回同一用户的长期记忆。
7. 接入小艺 `MEMORYSTATE`，用户关闭记忆时不主动召回或抽取。
8. Celia 故障不能阻塞 Jiuwen 主回答链路。

### 非目标

首版不要求：

- 重写 Celia 核心算法；
- 反编译 `celia_memory_mcp_server`；
- 在 Jiuwen 中重建 L0/L1/L2/L3 数据库；
- 启动 Node.js/OpenClaw 插件运行时；
- 将 Celia 当作普通 MCP Tool Server 直接挂到通用 MCPRail；
- 同时开启 Jiuwen builtin memory 与 Celia 后再判断结果来源。

---

# 2. JiuwenSwarm 中需要重点阅读的文件

## 2.1 配置和依赖基线

### `pyproject.toml`

重点确认：

- JiuwenSwarm 当前依赖 `openjiuwen==0.1.15.post3`；
- Python 版本范围；
- 是否已有 MCP/异步子进程依赖可复用。

Coding agent 必须检查实际安装包中的以下文件，而非只看 agent-core `main`：

```text
<site-packages>/openjiuwen/core/memory/external/provider.py
<site-packages>/openjiuwen/harness/rails/memory/external_memory_rail.py
```

### `jiuwenswarm/resources/config.yaml`

重点理解：

- `memory.engine`: `builtin | external | both | none`；
- `memory.external.provider` 单 Provider 策略；
- `user_id`、`scope_id` 默认值；
- 失败降级语义；
- External Memory 跨模式存活和热重载说明。

需要新增 `memory.external.celia` 配置段。

## 2.2 External Memory 装配链

### `jiuwenswarm/agents/harness/common/memory/external_memory_config.py`

当前职责：

- 解析 `memory.engine`；
- 判断 builtin/external 是否允许挂载；
- 读取 `memory.external`；
- 填充 Provider 配置默认值。

需要修改：

- 在 `get_external_memory_config()` 返回值中增加 `celia`；
- 必要时增加 Celia 配置校验和路径解析；
- 保持用户产品开关 `MEMORYSTATE` 与部署开关 `memory.engine` 分离。

### `jiuwenswarm/agents/harness/common/memory/external_memory_builder.py`

当前职责：

- 按 Provider 名称分发构造；
- 创建 `ExternalMemoryRail(provider, user_id, scope_id)`；
- Provider 构建失败时返回 `None`，不阻塞主流程。

需要修改：

```python
elif provider_name == "celia":
    provider = _build_celia_provider(ext_cfg)
```

重点检查：

- Rail 是何时构建和挂载的；
- Rail 是否在配置热重载时复用；
- 一个进程内可能存在多少 Rail 实例；
- 多个 Rail 是否可能共享同一数据库。

在仓库中全量搜索：

```text
build_external_memory_rail
is_external_memory_allowed
is_builtin_memory_allowed
```

不要在未确认调用位置前假设 Rail 生命周期。

### `jiuwenswarm/agents/harness/common/memory/config.py`

用于理解内置 Memory 配置，确保 `engine=external` 时不会误挂内置记忆。

### `jiuwenswarm/agents/harness/common/memory/manager.py`

用于理解 Jiuwen 内置记忆 Manager 生命周期、工具命名和潜在冲突。Celia 首版不应复用其存储实现，但要避免两套工具并存。

### `jiuwenswarm/agents/harness/common/memory/forbidden.py`

用于判断敏感信息禁止记忆规则是否仅作用于内置记忆。如果 External Memory 不经过该逻辑，Celia Provider 需要显式复用或实现等价过滤。

## 2.3 AgentCore Provider 与 Rail

以下路径需在实际 `openjiuwen==0.1.15.post3` 安装包中读取。

### `openjiuwen/core/memory/external/provider.py`

重点接口：

```python
name
is_available()
initialize()
get_tool_schemas()
handle_tool_call()
prefetch()
sync_turn()
system_prompt_block()
shutdown()
```

部分版本还可能包含：

```python
on_session_end()
is_initialized
```

Celia 必须通过该接口接入，不能修改通用 Rail 去硬编码 Celia 工具名。

### `openjiuwen/harness/rails/memory/external_memory_rail.py`

重点理解：

- `init()`：注册 Provider Tool，注入静态 Prompt；
- `before_invoke()`：初始化 Provider；
- `before_model_call()`：调用 `prefetch()` 并注入 Memory Attachment；
- `after_invoke()`：调用 `sync_turn()`；
- `uninit()`：注销工具和关闭 Provider；
- per-invoke prefetch cache；
- prefetch 超时；
- sync 串行化与熔断；
- Cron/Heartbeat 是否被排除；
- `user_id/scope_id/session_id` 当前是否为构造时静态字段。

已知边界：现有 Rail 的 `sync_turn()` 通常只传入用户输入和最终回答，无法天然获得 OpenClaw 版本保存的完整工具调用轨迹。MVP 可先接受，完整版需要扩展 Provider/Rail 或从 Callback Context 提取完整消息。

### 还需阅读的 AgentCore 文件

根据实际版本搜索：

```text
PromptAttachmentKind.MEMORY
build_external_memory_section
ToolCard
LocalFunction
ability_manager.add_ability
AgentCallbackContext
DeepAgentRail
before_invoke
before_model_call
after_invoke
```

目标是搞清 Prompt 注入、Tool 注册、Rail 回调和工具重名处理。

## 2.4 小艺请求上下文与 Session

### `jiuwenswarm/agents/harness/common/channel_runtime_context.py`

当前只保存：

```text
CURRENT_CHANNEL_ID
CURRENT_SESSION_ID
```

评估是否需要增加或从其他 Context 读取：

```text
user_id
conversation_id
memory_state
tenant_id
trace_id
```

### `jiuwenswarm/server/request_context.py`

当前保存：

- 当前 `AgentRequest`；
- 当前 `DeviceCommandContext`；
- 小艺 root session、params session、task id、rpc id 等 metadata。

Celia Provider 应优先从请求级 Context 获取动态 Session/Conversation 信息，而不是只使用配置中的 `__default__`。

### `jiuwenswarm/common/schema/agent.py`

阅读 `AgentRequest` 字段结构：

- `request_id`；
- `session_id`；
- `channel_id`；
- `chat_id`；
- `metadata`；
- `params`。

确定小艺 `MEMORYSTATE` 和稳定用户标识具体从哪个字段进入 Jiuwen。

### `jiuwenswarm/server/agent_ws_server.py`

搜索并追踪：

```text
set_current_agent_request
reset_current_agent_request
set_device_context
reset_device_context
xiaoyi_root_session_id
xiaoyi_task_id
metadata
```

目标是确认 ContextVar 的设置/清理范围，以及 Celia Provider 在 Rail 回调中能否读取到当前请求。

### 小艺 Channel 入口

在当前分支搜索以下关键词定位实际文件：

```text
source=xiaoyi
xiaoyi_session_id
MEMORYSTATE
memory_state
AgentRequest(
metadata=
```

需要整理从小艺 payload 到 `AgentRequest.metadata` 的字段映射。

## 2.5 测试相关

搜索现有 External Memory 测试：

```text
ExternalMemoryRail
MemoryProvider
mem0
openviking
lakebase
prefetch
sync_turn
```

优先复用现有 Fake Provider、Rail 生命周期测试和配置测试风格。

---

# 3. 需要的实现项

建议新增目录：

```text
jiuwenswarm/agents/harness/common/memory/celia/
├── __init__.py
├── config.py
├── mcp_client.py
├── client_manager.py
├── session_manager.py
├── provider.py
├── tools.py
├── sanitizer.py
├── formatter.py
├── runtime_context.py
└── errors.py
```

具体目录可按项目规范调整，但职责必须分离。

## 3.1 配置层

### 修改 `config.yaml`

建议配置形状：

```yaml
memory:
  engine: external
  external:
    provider: celia
    user_id: ${MEMORY_USER_ID:-__default__}
    scope_id: ${MEMORY_SCOPE_ID:-__default__}

    celia:
      binary_path: ${CELIA_MEMORY_BINARY_PATH:-}
      db_path: ${CELIA_MEMORY_DB_PATH:-}
      log_path: ${CELIA_MEMORY_LOG_PATH:-}
      tenant_id: ${CELIA_MEMORY_TENANT_ID:-default}
      startup_timeout: 20
      request_timeout: 10
      flush_timeout: 120
      fail_open: true
```

Chat/Embedding/Rerank 配置优先沿用二进制现有环境变量协议。不要在未检查 OpenClaw `client-env.ts`、`borrowed-config.ts` 前自行发明环境变量名称。

### 修改 `external_memory_config.py`

- 增加 `celia` 子配置；
- 解析路径和默认值；
- 不在此处启动进程；
- 不把每次请求的 Session ID 固化进全局配置。

### 修改 `external_memory_builder.py`

- 新增 `_build_celia_provider()`；
- 执行静态可用性检查；
- 创建 `CeliaMemoryProvider`；
- 仍由现有 `ExternalMemoryRail` 包装；
- 构建失败返回 `None`，记录可诊断日志。

## 3.2 Celia MCP Client

实现 Python 异步 stdio MCP Client，至少包含：

1. 检查二进制存在、是文件、可执行、平台匹配；
2. 创建 DB 和日志目录；
3. 使用 `asyncio.create_subprocess_exec` 启动二进制；
4. stdin/stdout 一行一个 JSON-RPC；
5. MCP `initialize` 和 `notifications/initialized`；
6. `request_id -> Future` 映射；
7. 支持多个并发在途请求；
8. 单请求超时和清理；
9. stdout reader task；
10. stderr 独立读取和日志；
11. 双层 JSON 响应解析；
12. 子进程异常退出时拒绝全部 pending 请求；
13. 指数退避重启；
14. 重启后清除 Celia logical session cache；
15. 正常关闭时 `SIGTERM -> 等待 -> SIGKILL`；
16. 进程启动和关闭幂等。

不要在每次 Tool Call 或每个 Jiuwen Session 中启动新二进制。

## 3.3 进程级 Client Manager

实现等价于 OpenClaw `globalThis + Symbol.for()` 的 Python 进程级管理器：

```text
一个 Jiuwen AgentServer 进程
    ↓
一个 CeliaMcpClientManager
    ↓
一个 celia_memory_mcp_server
```

可使用：

- 模块级 singleton；
- `asyncio.Lock`；
- 引用计数；
- `acquire()/release()`；
- 配置 fingerprint 防止不同 DB 配置误共享同一 Client。

如果同一进程可能加载不同用户的独立 DB，不能简单全局单例，需按 `(binary_path, db_path, tenant)` 建 Client Pool。

## 3.4 Celia logical session 管理

Celia MCP Server 要求先调用 `memory_open`。

参考 OpenClaw 实现：

```text
Celia tool session id = tools-${userId}
```

需要维护：

```text
open_sessions: set[str]
pending_opens: dict[str, Future]
```

同一个 user/session 并发首次调用时只能发送一次 `memory_open`。子进程重启后必须清空 session cache。

注意区分：

- Jiuwen/OpenClaw conversation ID：每个对话不同；
- Celia tool session ID：通常每个用户长期复用；
- user ID：长期记忆归属。

## 3.5 `CeliaMemoryProvider`

实现实际依赖版本中的 `MemoryProvider` 接口。

### `name`

返回 `celia`。

### `is_available()`

只做静态检查，不发网络请求：

- 二进制路径；
- 执行权限；
- Linux ARM64；
- DB 目录可写；
- 必要模型配置存在。

### `initialize()`

- 从 Client Manager 获取共享 Client；
- 启动二进制并完成 MCP handshake；
- 必要时 `tools/list` 或 health 验证；
- 打开 Celia logical session；
- 方法必须幂等。

### `prefetch(query, **kwargs)`

首版建议：

1. 检查 `MEMORYSTATE`；
2. 获取 L0；
3. 获取 L1 index，并选择固定加载场景；
4. 执行 L2 相关事实搜索；
5. 去重、截断并格式化为 Markdown；
6. 返回给 `ExternalMemoryRail` 注入 `PromptAttachmentKind.MEMORY`。

不要复刻 OpenClaw 的 `USER.md/MEMORY.md` 文件写入。Jiuwen 应使用现有 Prompt Attachment 机制。

### `sync_turn(user_msg, assistant_msg, **kwargs)`

MVP：

- 将本轮用户输入和最终回答序列化；
- 调用 `memory_add`；
- 传入 `userId`、`sessionId`、`conversationId`、`tenant_id`、`memoryState`、trace id；
- 默认使用异步/deferred ingest；
- 用户显式调用 `memory_store` 后使用 `deferred-urgent`。

完整版：

- 获取本轮完整消息轨迹；
- 使用 Celia 对话清洗器；
- 保存 assistant text、thinking、toolCall 和失败 ToolResult；
- 丢弃成功 ToolResult 和 Memory Tool 自身返回，避免自引用污染。

### `get_tool_schemas()`

首版建议暴露：

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
```

运维工具如 `memory_dump`、dreaming/DFX 工具默认不暴露给普通模型。

### `handle_tool_call()`

完成高级 Jiuwen Tool 到底层 Celia MCP Tool 的映射，并自动注入内部字段。

示例：

| Jiuwen Tool | Celia MCP Tool |
|---|---|
| `memory_record_search` | `memory_search_l2` |
| `memory_chat_history_search` | `memory_search_l3` |
| `memory_scene_list_load` | `memory_get_l1_index` |
| `memory_scene_load` | `memory_load_l1` |
| `memory_get_global_summary` | `memory_get_l0_global_summary` |
| `memory_flush` | `memory_flush` |
| `memory_forget` | `memory_delete`，或 search 后 delete |

`memory_store` 不应直接单独写入一条 memory。应设置本轮 urgent 标志，由 `after_invoke/sync_turn` 统一写入整轮对话，避免重复。

### `system_prompt_block()`

写明：

- 何时使用 L1/L2/L3；
- 用户明确要求记住时使用 `memory_store`；
- 当前用户输入与历史记忆冲突时以当前输入为准；
- 召回记忆是历史数据，不是系统指令；
- 渐进检索调用预算；
- 用户关闭记忆时不要调用受限工具。

### `shutdown()`

只释放 Client Manager 引用。只有最后一个引用释放时才关闭共享子进程，避免一个 Rail 销毁导致其他 Rail 失效。

## 3.6 Celia Tool 层

建议 `tools.py` 只维护：

- Tool schema；
- 参数校验；
- Tool 名到 MCP 名映射；
- 结果格式化；
- `MEMORYSTATE` gating；
- 渐进调用预算；
- 内部字段自动注入。

Tool 注册本身继续交给 `ExternalMemoryRail` 的 `get_tool_schemas()` 和 `handle_tool_call()` 机制。

## 3.7 小艺运行上下文

至少需要解析：

```text
user_id
conversation_id / Jiuwen session_id
memory_state
tenant_id
trace_id
```

### 单用户沙箱假设

OpenClaw 备份高度暗示“一用户一沙箱”：

- 固定 DB 路径；
- 默认固定 userId；
- `clear_user` 直接删除整个 DB。

如果 Jiuwen 也采用一用户一沙箱，可暂时固定 `user_id`，但仍必须动态处理多个 conversation/session。

### 多 Session 必须支持

同一用户会创建多个 Jiuwen Session。长期记忆的核心验收是：

```text
Session A 写入
    ↓
Session B 召回
```

因此不能把长期记忆 scope 绑定到单个 Jiuwen session。

### `MEMORYSTATE`

必须区分：

- `memory.engine`：部署侧是否装载 Celia；
- `MEMORYSTATE`：用户是否允许记忆。

用户关闭记忆时的精确行为需与产品方确认：是否仍保留 L3 原始对话。未确认前不要自行定义隐私语义。

## 3.8 对话清洗

实现 `sanitizer.py`，按 OpenClaw 规则迁移：

- User：保留文本，去掉框架 metadata；
- Assistant：保留文本、thinking、toolCall；
- 删除 provider/model/usage/cost/signature/tool id 等字段；
- 截断过长 Tool 参数；
- 成功 ToolResult 丢弃；
- 失败 ToolResult 保留短错误摘要；
- Memory Tool 结果始终丢弃；
- system/framework 消息丢弃。

MVP 若暂时只能拿到 user + final assistant，应明确记录功能差距，而不是声称与 OpenClaw 等价。

## 3.9 DFX、状态和错误处理

至少记录：

```text
provider
binary_path（脱敏）
db_path（脱敏）
client generation
process pid
MCP tool
request id
trace id
conversation id
memory_state
prefetch latency
sync latency
restart count
scope denied
```

错误策略：

- prefetch 超时：跳过记忆，继续回答；
- sync 失败：记录并重试/熔断，不影响本轮答案；
- 子进程崩溃：拒绝 pending 请求，清 session cache，按退避重启；
- DB 不可写/架构不匹配：Provider 构建或初始化失败，明确日志；
- 测试环境建议支持 fail-fast，生产默认 fail-open。

## 3.10 测试

至少实现：

1. Fake MCP Server 单元测试；
2. JSON-RPC 并发 request id 关联；
3. 超时清理；
4. 非法 JSON；
5. 子进程退出和重启；
6. 重启后重新 `memory_open`；
7. Provider 配置和 Builder；
8. Tool schema 注册；
9. `prefetch` Prompt 注入；
10. `sync_turn` 调用；
11. `memory_store` urgent 标志；
12. `MEMORYSTATE=false`；
13. 跨 Session 写入和召回；
14. Jiuwen builtin memory 确实未挂载；
15. Celia 故障不阻塞主流程。

---

# 4. OpenClaw 代码中可直接复用或略微修改后复用的部分

参考根目录：

```text
openClawFullBackup/.openclaw/extensions/celia_memory/
  install/v2026-07-07-rc7/
```

## 4.1 可直接复用

### `bin/celia_memory_mcp_server`

直接作为核心运行二进制复用。前提：

- 目标运行环境为 Linux ARM64；
- glibc 和动态库兼容；
- 有执行权限；
- DB/日志目录可写；
- 模型服务配置正确。

不要把内部闭源二进制、密钥或完整沙箱备份提交到公开仓库。

### `manifest.toml`、`build-info.toml`

可直接用于：

- 版本识别；
- 平台校验；
- 二进制发布目录；
- 升级和回滚；
- DFX 输出。

### MCP 协议和底层工具契约

以下内容可直接沿用：

- `initialize`；
- `notifications/initialized`；
- `tools/call`；
- 工具名称；
- 参数字段；
- 返回格式；
- `_trace_id` 和 `_meta` 约定；
- double JSON decode 行为。

### Tool 产品定义

`src/memory/tools.ts` 中的 Tool 名、描述、参数 schema、调用预算和错误提示可直接转写成 Python schema。框架注册 API 必须改写，但产品语义可以复用。

### `memory-chat-model-map.json`

如果二进制确实依赖该映射，可随发行包直接部署。先确认实际环境变量和查找路径，不要凭文件名假定它会自动生效。

## 4.2 可按逻辑近似逐行迁移为 Python

### `memory-plugin/src/core/client.ts`

迁移到 `mcp_client.py`：

- `buildCeliaArgs`；
- 兼容旧环境变量别名；
- executable check；
- stdio spawn；
- pending request map；
- initialize handshake；
- response parse；
- restart decision；
- 指数退避；
- stability window；
- graceful shutdown。

### `memory-plugin/src/core/session.ts`

迁移到 `session_manager.py`：

- `pendingOpens`；
- `openSessions`；
- 幂等 `memory_open`；
- 并发首次打开去重；
- server 重启后清 cache。

### `shared/celia-client-singleton.ts`

迁移到 `client_manager.py`：

- 进程级共享 Client；
- 引用计数；
- Lazy 获取；
- 最后一个引用释放时关闭。

Python 版需要额外考虑不同 `db_path`/租户的配置 fingerprint，不能无条件共享一个 Client。

### `memory-plugin/src/core/config.ts`

迁移配置数据结构、默认值、dedup policy、超时和 header 解析。去掉 OpenClaw Plugin SDK schema 写法，改为 Jiuwen/YAML 配置解析。

### `memory-plugin/src/core/client-env.ts`

迁移二进制要求的环境变量构造。该文件决定模型 endpoint、key、model、headers 如何传给二进制，是直接运行成功的关键。

### `memory-plugin/src/core/borrowed-config.ts`

参考 OpenClaw 如何复用 Agent 默认模型/Embedding 配置。Jiuwen 中应改为从 Jiuwen `models`、`embed` 或小艺环境变量读取。

### `memory-plugin/src/core/sandbox-headers.ts`

迁移小艺沙箱所需 header 透传和脱敏规则。

### `memory-plugin/src/memory/conversation-sanitizer.ts`

将纯数据清洗逻辑迁移为 Python。需要适配 Jiuwen 的消息对象结构。

### `memory-plugin/src/memory/runtime-state.ts`

迁移：

- `.xiaoyiruntime` 路径解析；
- `MEMORYSTATE=true/false/1/0` 解析；
- 缺失或非法时的默认行为。

若 Jiuwen 已从请求 metadata 收到 memory state，应优先使用请求级值，文件读取只做兼容回退。

### `memory-plugin/src/memory/store-signal.ts`

迁移为 request/session 级 urgent ingest 标志。优先使用 `ContextVar` 或按 conversation key 的有界缓存，避免全局状态泄漏。

### `memory-plugin/src/memory/session-prompt-buffer.ts`

迁移当前 Session 的即时记忆缓冲，使 `memory_store` 后本轮/下一次模型调用即可看到内容，不必等待异步 L2 抽取完成。

### `memory-plugin/src/memory/fixedContext.ts`

迁移 L0/L1 响应解析和格式化函数。

### `memory-plugin/src/memory/l0Sync.ts`

复用 L0 拉取、超时和正文提取逻辑。Jiuwen 中不写 `USER.md`，而是返回给 `prefetch()`。

### `memory-plugin/src/memory/l1ScenesSync.ts`

复用 L1 index 拉取、preset/scene 分类和固定场景选择算法。Jiuwen 中格式化后注入 Memory Attachment。

### `memory-plugin/src/memory/text-utils.ts`

可迁移通用截断、文本清洗和字节限制函数。

## 4.3 只能复用行为，不能复用框架代码

### `memory-plugin/src/memory/hooks.ts`

OpenClaw Hook 映射为 Jiuwen Rail：

| OpenClaw Hook | Jiuwen 对应点 |
|---|---|
| `session_start` | Session/Agent 初始化或首个 `before_invoke` |
| `before_prompt_build` | `ExternalMemoryRail.before_model_call` / Provider `prefetch` |
| `after_compaction` | 若 Jiuwen 有压缩回调则刷新，否则由 prefetch cache/dirty 管理 |
| `agent_end` | `ExternalMemoryRail.after_invoke` / Provider `sync_turn` |
| `session_end` | Provider `on_session_end` 或 Session teardown |

不能复制 `api.on(...)`，但 TTL、dirty、对话捕获和状态清理逻辑应迁移。

### `memory-plugin/src/memory/tools.ts`

不能复制 `api.registerTool()`，但可复用 Tool schema、参数、底层 MCP 映射和结果格式。Jiuwen 由 `ExternalMemoryRail` 自动注册 Provider Tool。

### `memory-plugin/index.ts`

不能复制 `definePluginEntry()`、`registerService()`、`registerHttpRoute()`。只能作为整体装配时序参考。

---

# 5. OpenClaw 代码中可提供参考或提示的文件

以下文件不建议直接移植，但对理解产品行为、生命周期和边界有价值。

## 5.1 总入口与注册

### `memory-plugin/index.ts`

用于确认：

- full 与 tool-discovery 注册模式；
- Client 何时创建和预热；
- Tools、Hooks、Service 的装配顺序；
- 哪些功能属于 OpenClaw 框架，哪些属于 Celia。

### `memory-plugin/openclaw.plugin.json`

用于理解 OpenClaw memory slot 和插件配置 schema。Jiuwen 不应复制该格式。

### `memory-plugin/package.json`

用于确认插件名称、描述、依赖和版本。

## 5.2 生命周期与并发

### `shared/celia-client-singleton.ts`

关键提示：一个 OpenClaw Gateway 进程共享一个 Celia MCP Server，而不是每个 Session 启一个。

### `src/core/service.ts`

关键提示：OpenClaw 避免其他模块自行 spawn 第二个 Celia 进程并竞争同一 DB。Jiuwen 内部也应统一经过 Client Manager。

其中 `/celia/clear_user` 通过停止进程并删除整个 DB，进一步说明当前部署大概率是一用户一沙箱。

### `src/core/session.ts`

关键提示：Celia logical tool session 与 OpenClaw conversation 不是同一个概念。

## 5.3 Prompt 与记忆使用规则

### `celiaclaw/config/AGENTS.md`

用于理解 Celia Memory 的模型使用策略和 L0/L1/L2/L3 渐进查询规则。

### `src/memory/agentsTemplate.generated.ts`

用于生成 Provider `system_prompt_block()` 的基础文案。需要删掉 OpenClaw 专属路径和命令。

### `src/memory/agentsGuideSync.ts`

用于理解 OpenClaw 如何保证 AGENTS 指南同步。Jiuwen 可直接通过 system prompt section 注入，不需要文件同步。

### `src/memory/agentsShapeEnforcer.ts`

用于理解对记忆指南文件形态的约束。Jiuwen 通常无需实现。

## 5.4 固定上下文与文件写入

### `src/memory/safeWriteMarker.ts`

参考其原子写入和 marker 更新方式。如果 Jiuwen 不写 Markdown 文件，则不需要迁移。

### `src/memory/markerProtocol.ts`

参考 L0/L1 marker 的边界和去重，主要服务于 OpenClaw `USER.md/MEMORY.md`。

### `src/memory/l0Sync.ts`

重点参考 MCP 请求、超时和正文提取，不复制文件写入。

### `src/memory/l1ScenesSync.ts`

重点参考固定加载场景选择策略和 L1 分类。

## 5.5 DFX 与错误处理

### `src/core/dfx.ts`

参考：

- trace id；
- MCP echo；
- scope denied；
- latency；
- token/recall 指标；
- 结构化日志字段。

### `src/core/log-path.ts`

参考二进制日志路径和目录创建。

### `src/core/helpers.ts`

参考通用解析和错误包装。

## 5.6 安装、升级和状态检查

### `scripts/install.sh`

参考版本目录、`current` 软链接、checksum、执行权限和配置备份。路径和 OpenClaw 配置修改不可直接使用。

### `scripts/status.sh`

参考健康检查、版本和进程诊断。

### `scripts/upgrade.sh`

参考原子升级和回滚。

### `scripts/uninstall.sh`

参考停止进程、清配置和保留/删除 DB 的策略。

## 5.7 数据迁移工具

### `tools/migrate_openclaw/`

参考：

- OpenClaw 原有记忆导出；
- IR 数据结构；
- 批量写入；
- 校验；
- E2E 测试。

其中 `mcp_client.py` 是迁移工具使用的 HTTP 客户端，不适合作为 Jiuwen Runtime 的 stdio Client，但其错误分类和响应解析可以参考。

## 5.8 测试

### `memory-plugin/scripts/run-tests.mjs`

查看已有单元测试入口。

### `tools/migrate_openclaw/scripts/test-unit.sh`
### `tools/migrate_openclaw/scripts/test-e2e.sh`
### `tools/migrate_openclaw/scripts/test-multi-agent.sh`

参考测试环境准备、数据清理和验证方式。

---

# 7. 核心验收标准

1. `engine=external + provider=celia` 时，Jiuwen 只暴露 Celia 记忆能力。
2. 同一 Jiuwen 进程中不会因多个 Session 启动多个相同 DB 的 Celia Server。
3. Session A 写入的偏好可在 Session B 召回。
4. `MEMORYSTATE=false` 时不主动注入 L0/L1/L2。
5. 写入后 `memory_flush` 可使后续查询及时可见。
6. `memory_store` 不造成同一轮重复写入。
7. Celia 进程崩溃后能重启，并重新执行 `memory_open`。
8. prefetch 超时或 Celia 不可用时，Jiuwen 仍能正常回答。
9. 不将 Memory Tool 的返回再次写入记忆。
10. 不把二进制、密钥、用户 DB、会话 trajectory 提交到公开仓库。

---

# 8. Coding Agent 执行约束

1. 所有结论以 `copy_dev_0.2.3.beta1` 和实际安装的 `openjiuwen==0.1.15.post3` 为准。
2. 修改前先搜索现有 External Memory 装配点，不新增第二套平行 Rail 体系。
3. 首版优先复用 `ExternalMemoryRail`；只有确认其 Callback Context 无法提供所需数据时才扩展或增加 Celia 专用子类。
4. Celia 专属逻辑必须位于 Provider/Client 内，通用 Rail 不应出现 Celia MCP 工具名。
5. Tool 内部字段必须由 Provider 注入，禁止让 LLM 提供 `userId/sessionId/tenant_id/trace_id`。
6. 所有全局缓存必须有上限、清理机制和并发锁。
7. 子进程重启后必须清理 Celia session handle 缓存。
8. 数据库路径相同的情况下，禁止无验证地启动多个 Celia 进程。
9. 不要声称 MVP 的 `user + final answer` 写入与 OpenClaw 完整轨迹写入等价。
10. 对未知产品语义，例如 `MEMORYSTATE=false` 是否仍保存 L3，直接标记待确认，不自行推断。



