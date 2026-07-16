# memory_add tool/list 不存在问题

## 场景

### 1. 部署场景

- Celia MCP：`v2026-07-07-rc7\bin\celia_memory_mcp_server`
- Celia 数据库：

  ```text
  /home/sandbox/.jiuwenswarm/agent/workspace/memory/celia_memory/celia_memory.db
  ```

- Celia 日志：

  ```text
  /home/sandbox/.openclaw/logs/Celia_memory.log
  ```

- Jiuwen 日志：当前工作目录下的 `jiuwenswarm.log`
- 运行时状态文件：

  ```text
  /home/sandbox/.openclaw/.xiaoyiruntime
  ```

### 2. `memory_add` 不存在于 `tools/list` 的场景

Jiuwen 启动 Celia MCP 后调用 `tools/list`。最初 Jiuwen 将 `memory_add` 当作公开必需工具校验，因 Celia `tools/list` 中没有 `memory_add`，导致 provider 初始化失败，后续对话结束时没有执行 `memory_add`。

需要确认：

1. `memory_add` 是否设计为内部 Hook 工具，因此不应该出现在公开 `tools/list` 中；
2. OpenClaw 是否通过 `tools/call` 直接调用未出现在 `tools/list` 中的 `memory_add`；
3. Jiuwen 当前采用“`tools/list` 能力探测 + 直接调用内部 `memory_add`”的方式是否符合 Celia MCP 的正式协议。

## 日志

```text
2026-07-15 16:27:12.366 WARNING jiuwenswarm.agents.harness.common.memory.celia.provider: [CeliaMemoryProvider] initialization failed: stage=tools/list exception=CeliaError message=Celia MCP is missing required tools: memory_add db=/home/sandbox/.jiuwenswarm/agent/workspace/memory/celia_memory/celia_memory.db log=/home/sandbox/.openclaw/logs/Celia_memory.log
2026-07-15 16:27:12.368 WARNING jiuwenswarm.agents.harness.common.memory.celia.rail: [CeliaMemoryRail] prewarm failed; provider diagnostics contain the cause
2026-07-15 16:27:13.683 WARNING jiuwenswarm.agents.harness.common.memory.celia.provider: [CeliaMemoryProvider] initialization failed: stage=tools/list exception=CeliaError message=Celia MCP is missing required tools: memory_add db=/home/sandbox/.jiuwenswarm/agent/workspace/memory/celia_memory/celia_memory.db log=/home/sandbox/.openclaw/logs/Celia_memory.log
2026-07-15 16:27:13.685 WARNING jiuwenswarm.agents.harness.common.memory.celia.rail: [CeliaMemoryRail] provider initialize failed; provider diagnostics contain the cause
2026-07-15 16:27:40.619 WARNING jiuwenswarm.agents.harness.common.memory.celia.rail: [CeliaMemoryRail] turn sync skipped: provider is not initialized
```



## Celia memory 提供的所有 tool 的用法和使用时机。

### 1. 对话记忆与管理工具

| Tool | 当前使用场景 | 使用时机/触发点 | 需要 Celia 确认的内容 |
|---|---|---|---|
| `memory_open` | 创建或恢复逻辑 session，当前 sessionId 为 `tools-{userId}` | session start、provider 初始化、Celia 重启后 | 是否公开出现在 `tools/list`；参数和返回值；是否必须先调用 |
| `memory_add` | 写入本轮原始对话并进入异步抽取队列 | `agent_end`/`after_invoke`；普通对话结束后调用 | 是否为内部工具；完整参数；`memoryState`、`ingestMode` 语义；成功是否仅表示入队 |
| `memory_store` | 用户明确要求“记住/存入记忆” | 模型处理用户的显式记忆请求时 | 是否只写 PromptBuffer；是否必须由后续 `memory_add` 完成持久化 |
| `memory_forget` | 删除指定记忆，或根据查询返回待删除候选 | 用户明确要求删除记忆时 | `memoryId`、查询候选、删除返回值和幂等行为 |
| `memory_flush` | 排空异步写入/抽取队列 | 批量 `memory_store` 后需要立即验证时 | 完成条件、超时参数、是否触发 LLM/Embedding 重试 |
| `memory_report_round_usage` | 上报本轮 token、召回和固定加载统计 | 每轮对话结束后 | 是否仅用于 DFX；参数和是否涉及外部权益扣费 |

### 2. L0/L1/L2/L3 检索工具

| Tool | 当前使用场景 | 使用时机/触发点 | 需要 Celia 确认的内容 |
|---|---|---|---|
| `memory_get_l0_global_summary` | 获取 L0 全局摘要 | session start、compaction 后、每次模型调用前固定加载 | `tier`/用户参数；返回字段；MEMORYSTATE=false 时的行为 |
| `memory_get_l1_index` | 获取 L1 scene/index 列表 | 固定上下文加载、`memory_scene_list_load`、Markdown 同步 | `hasIndex` 含义；scene 生成时机；返回字段 |
| `memory_load_l1` | 按 scene path 加载 L1 全文 | 需要进一步读取相关 scene 时 | path 参数、最多加载数量、返回结构 |
| `memory_scene_list_load` | 面向 Agent 查看 scene 列表 | 模型需要了解可用 scene 时 | 是否映射到 `memory_get_l1_index`；关闭状态行为 |
| `memory_scene_load` | 面向 Agent 加载指定 scene | 模型需要读取指定 L1 scene 时 | 是否映射到 `memory_load_l1`；path 和返回结构 |
| `memory_search_l2` | 查询 L2 原子事实/长期记忆 | 用户问题需要精确事实、偏好或属性时 | `time_hint`、`dedup_policy`、`is_procedural` 参数；MEMORYSTATE=false 行为 |
| `memory_record_search` | 面向 Agent 查询 L2 记录 | 需要语义事实检索时 | 与 `memory_search_l2` 的映射、参数和关闭状态返回 |
| `memory_search_l3` | 查询 L3 原始对话 | 需要原始对话、MEMORYSTATE=false 或 L2 不可用时 | session 过滤、排序和返回结构 |
| `memory_chat_history_search` | 面向 Agent 查询历史对话 | 需要原始对话召回时 | 与 `memory_search_l3` 的映射和参数 |
| `memory_list` | 按分类列出 L0/L1/L2 记忆 | 用户或模型要求查看指定分类时 | `layers/categories` 参数和返回结构 |
| `memory_delete` | 删除指定底层记忆记录 | `memory_forget` 确认删除后 | `memoryId` 参数、删除范围和返回值 |
| `memory_get_global_summary` | 面向 Agent 获取 L0 摘要 | 用户或模型明确请求全局摘要时 | 与 `memory_get_l0_global_summary` 的映射和 `tier` 语义 |

### 3. 导出和梦境工具

| Tool | 当前使用场景 | 使用时机/触发点 | 需要 Celia 确认的内容 |
|---|---|---|---|
| `memory_dump` | 导出记忆数据用于诊断或备份 | 用户或测试明确要求导出时 | category、时间范围、L0/L1 开关、输出路径 |
| `dream_status` | 查询梦境任务状态 | 测试或诊断梦境任务时 | sessionId、runId 和返回结构 |
| `dream_run_summary` | 查询指定梦境运行摘要 | 已知 runId，需要查看运行结果时 | runId、状态和返回结构 |
| `dream_recent_runs` | 查询最近梦境运行记录 | 测试凌晨 cron 执行情况时 | limit、时间范围和返回结构 |



# .xiaoyiruntime 中 MEMORYSTATE=true/false 对应的 L0-L3 行为

* L0
* L1
* L2
* L3



# celia_memory.db中mem_conversation和mem_record

| 表                 | 作用                                                         | 写入时机                                    | 对应记忆层                 |
| ------------------ | ------------------------------------------------------------ | ------------------------------------------- | -------------------------- |
| `mem_conversation` | 保存清洗后的原始对话轮次，作为异步抽取队列/原始数据源        | `memory_add` 成功后立即写入                 | 原始历史，主要用于 L3 检索 |
| `mem_record`       | 保存 Celia 从原始对话中抽取出的结构化长期记忆，例如偏好、事实、属性等 | 异步 Worker 调用 LLM 抽取、去重、索引后写入 | L2 原子记忆                |
