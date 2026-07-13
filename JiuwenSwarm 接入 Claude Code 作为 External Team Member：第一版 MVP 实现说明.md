# JiuwenSwarm 接入 Claude Code 作为 External Team Member：第一版 MVP 实现说明

## 1. 任务概述

### 1.1 任务目标

在 JiuwenSwarm 的 Team 模式中接入 Claude Code，使 Claude Code 作为一个独立的外部 Member Agent 参与团队协作。

MVP 需要证明以下完整链路成立：

```text
用户提交 Coding 任务
        ↓
Jiuwen Leader 识别任务类型
        ↓
Jiuwen Leader 动态启动 Claude Code
        ↓
Claude Code 以 external_cli Member 身份加入 Team
        ↓
Leader 创建并派发 Coding 子任务
        ↓
Claude Code 操作指定代码仓库、运行测试
        ↓
Claude Code 通过 Team MCP 汇报并完成任务
        ↓
Jiuwen Leader 汇总结果并回复用户
```

本任务验证的是：

> JiuwenSwarm 能够将第三方完整 Agent 纳入 Jiuwen Team，并以 Team Member 的方式组织和使用其专业能力。

不是简单执行一次 Claude Code 命令，也不是把 Claude Code 包装成普通 Tool。

------

## 2. MVP 成功标准

实现完成后，必须同时满足以下条件：

1. JiuwenSwarm 配置可以声明一个 Claude Code External CLI Agent。
2. Team 构建完成后，`TeamAgentSpec.external_cli_agents` 中存在 Claude Code 配置。
3. Leader 可以调用 `spawn_external_cli`。
4. Coding 任务能够触发 Claude Code 动态启动。
5. Claude Code 以独立 Member 身份出现在 Team Roster。
6. Claude Code 能收到 Leader 派发的任务。
7. Claude Code 能通过 Team MCP：
   - 查看任务；
   - 认领任务；
   - 向 Leader 发送消息；
   - 完成任务。
8. Claude Code 能在指定项目目录中读取和修改代码。
9. Claude Code 能运行测试并返回测试结果。
10. Leader 能获得 Claude Code 的结果并向用户输出统一回复。
11. 同一个 Team Session 中的后续 Coding 请求复用已有 Claude Code Member，不重复启动同名成员。
12. 非 Coding 请求不启动 Claude Code。
13. Claude Code 启动失败时，Leader 获得明确错误，而不是无限等待。
14. Team 关闭后，Claude Code 子进程能够正常退出。

------

## 3. 非目标

第一版不得扩展到以下范围：

- 不接入 OpenClaw、Codex、Gemini 或其他外部 Agent；
- 不实现 A2A；
- 不设计通用 Agent Marketplace；
- 不实现完整 `Agent as Harness` 安全体系；
- 不实现 Capability Token；
- 不实现 MCP 动态裁剪；
- 不实现 Credential Broker；
- 不实现容器级安全沙箱；
- 不实现复杂的 Agent 能力评分；
- 不实现多 Agent 自动回退；
- 不实现跨机器远程 Agent；
- 不修改 Claude Code 源码；
- 不重新实现 AgentCore 已经存在的 External CLI Runtime；
- 不把 Claude Code 封装为一次性 MCP Tool。

安全治理属于后续阶段。当前 MVP 只验证外部 Agent 的 Team 接入能力。

但由于当前 AgentCore 内置 Claude Adapter 使用 `--dangerously-skip-permissions` 自动批准 Claude Code 的 Tool Call，MVP 只能在隔离的测试仓库和非生产环境中运行。

------

## 4. 当前源码基础

### 4.1 JiuwenSwarm 当前装配入口

重点阅读：

```text
jiuwenswarm/agents/swarm/assembly.py
```

`enrich_team_spec_for_swarm()` 是 JiuwenSwarm 和 AgentCore Team Spec 之间的主要装配入口。

当前逻辑只遍历：

```python
_MEMBER_ROLES = ("leader", "teammate")
```

并为原生 Leader 和 Teammate 装配：

- DeepAgent Spec；
- Tool；
- MCP；
- Rail；
- Workspace；
- Build Context。

当前代码没有从 JiuwenSwarm 配置中构造并写入：

```python
spec.external_cli_agents
```

这是本 MVP 在 JiuwenSwarm 中需要补齐的主要能力。

### 4.2 AgentCore 已有 External CLI 配置模型

重点阅读：

```text
openjiuwen/agent_teams/schema/team.py
openjiuwen/agent_teams/schema/blueprint.py
```

AgentCore 已定义：

```python
ExternalCliAgentSpec
```

其主要字段包括：

```python
cli_agent
command
cwd
inject_mcp
mcp_server_command
env
```

其中：

- `cli_agent="claude"` 选择 Claude Code 内置 Adapter；
- `cwd` 指定 Claude Code 工作目录；
- `inject_mcp=True` 将 Jiuwen Team MCP 注入 Claude Code；
- `mcp_server_command` 默认执行 `openjiuwen-team-mcp`；
- `env` 用于追加子进程环境变量。

`TeamAgentSpec` 已包含：

```python
external_cli_agents: list[ExternalCliAgentSpec]
```

并要求不同配置的 `cli_agent` 名称唯一。

### 4.3 AgentCore 已有动态启动工具

重点阅读：

```text
openjiuwen/agent_teams/tools/tool_member.py
```

AgentCore 已实现：

```text
spawn_external_cli
```

调用参数包括：

```text
member_name
display_name
desc
cli_agent
```

其中 `cli_agent` 必须预先声明在：

```python
TeamAgentSpec.external_cli_agents
```

启动成功后，成员的：

```text
role_type = external_cli
```

因此本任务不需要重新实现动态成员启动机制。

### 4.4 AgentCore 已有 Claude Code Adapter

重点阅读：

```text
openjiuwen/agent_teams/external/cli_agent/adapters.py
```

当前 Claude Adapter 使用：

```text
claude
--print
--input-format stream-json
--output-format stream-json
--verbose
--dangerously-skip-permissions
```

并具有：

- 流式 JSON 输入；
- 流式 JSON 输出；
- `result` 事件完成检测；
- MCP 启动参数注入；
- System Prompt 注入；
- Claude Code 父会话环境标记清理。

### 4.5 AgentCore 已有 External CLI Runtime

重点阅读：

```text
openjiuwen/agent_teams/external/cli_agent/spawn.py
```

`build_cli_runtime()` 已负责：

- 选择 CLI Adapter；
- 构造 Team Join Descriptor；
- 注入成员身份；
- 注入 Team MCP；
- 注入 System Prompt；
- 设置 `cwd`；
- 启动子进程；
- 建立 stdin/stdout 管道；
- 返回 `ExternalCliRuntime`。

外部 CLI Member 运行在独立进程中，因此 Team 必须使用跨进程通信和共享数据库。AgentCore 注释明确指出，应使用 `pyzmq` 和文件型 SQLite，不能使用仅限单进程的内存数据库和 `inprocess` transport。

### 4.6 AgentCore 已有 Team MCP

重点阅读：

```text
openjiuwen/agent_teams/mcp/server.py
```

对于 `scope="member"` 的外部 CLI Agent，Team MCP 会暴露真实的 Teammate 协作工具，包括：

```text
view_task
claim_task
complete_task
send_message
read_inbox
list_members
```

外部 CLI Agent 因此可以成为一等 Team Member，而不是只能返回子进程 stdout。

------

## 5. 实现前必须完成的源码和环境核查

不要立即开始修改代码。先完成以下核查，并将结果记录在实施报告中。

### 5.1 核查实际 OpenJiuwen 版本

JiuwenSwarm 当前 `develop` 的 `pyproject.toml` 固定依赖：

```text
openjiuwen==0.1.15.post3
```

而本说明引用的 External CLI 实现位于 AgentCore 当前 `develop` 分支。不能假定已安装的 `openjiuwen==0.1.15.post3` 一定包含完全相同的 API。

在实际开发环境执行等价检查：

```bash
python - <<'PY'
from openjiuwen.agent_teams.schema.team import ExternalCliAgentSpec
from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
from openjiuwen.agent_teams.external.cli_agent.adapters import build_adapter

print(ExternalCliAgentSpec)
print(TeamAgentSpec.model_fields.get("external_cli_agents"))
print(build_adapter("claude"))
PY
```

同时检查：

```bash
python -c "import openjiuwen; print(openjiuwen.__file__)"
python -c "import importlib.metadata; print(importlib.metadata.version('openjiuwen'))"
```

如果当前安装版本缺少相关实现：

- 优先让 JiuwenSwarm 使用兼容的 AgentCore 开发版本或包含该能力的发布版本；
- 不要将 AgentCore External CLI 源码复制进 JiuwenSwarm；
- 不要在 JiuwenSwarm 中重新实现一套重复 Runtime。

### 5.2 核查 Claude Code

执行：

```bash
which claude
claude --version
claude --help
```

确认实际安装版本支持：

```text
--print
--input-format stream-json
--output-format stream-json
--verbose
--mcp-config
--append-system-prompt
```

若实际 Claude Code 参数与 AgentCore Adapter 不兼容，应优先修正或覆盖 Adapter 启动命令，不得绕过问题伪造测试通过。

### 5.3 核查 Team MCP

执行：

```bash
which openjiuwen-team-mcp
openjiuwen-team-mcp --help
```

并确认该命令来自当前实际使用的 OpenJiuwen 环境。

### 5.4 核查跨进程依赖

执行：

```bash
python -c "import zmq; print(zmq.__version__)"
```

确认当前安装方式包含 ZMQ 支持。

JiuwenSwarm 当前定义了：

```text
distribute = ["openjiuwen[postgres,zmq]"]
```

如果环境缺少相关依赖，应补齐对应可选依赖。

------

## 6. 推荐实现原则

### 6.1 最小改动

尽量只修改 JiuwenSwarm：

1. 配置读取；
2. External CLI Spec 构造；
3. Team Spec 装配；
4. Leader 路由 Prompt；
5. 日志；
6. 测试。

除非发现明确缺陷，否则不要修改 AgentCore。

### 6.2 复用 AgentCore 抽象

必须复用：

```text
ExternalCliAgentSpec
TeamAgentSpec.external_cli_agents
spawn_external_cli
ExternalCliRuntime
openjiuwen-team-mcp
```

### 6.3 配置与机制分离

JiuwenSwarm 负责：

- 是否启用 Claude Code；
- Claude Code 工作目录；
- 环境变量；
- Leader 在什么情况下使用 Claude Code；
- Claude Code Member Persona。

AgentCore 负责：

- 子进程启动；
- CLI 输入输出；
- Team 身份；
- MCP 注入；
- Runtime 生命周期。

### 6.4 不提前通用化

第一版只支持：

```text
cli_agent = claude
```

可以保留未来扩展空间，但不要为了支持未来所有 Agent 提前设计复杂 Registry、Protocol 或 Plugin Framework。

------

## 7. 推荐配置模型

在遵守 JiuwenSwarm 当前配置组织习惯的前提下，增加 Claude Code 配置。

具体配置节点需要根据当前 `config.yaml` 的 Team Mode 结构确定，不要机械新增一个与现有规范冲突的顶级节点。

推荐语义如下：

```yaml
modes:
  team:
    external_cli_agents:
      - cli_agent: claude
        enabled: true
        command: null
        cwd: null
        inject_mcp: true
        mcp_server_command:
          - openjiuwen-team-mcp
        env: {}
```

字段含义：

- `cli_agent`：AgentCore Adapter 名称，MVP 固定为 `claude`；
- `enabled`：是否启用；
- `command`：可选的完整启动命令覆盖；
- `cwd`：可选固定目录；
- `inject_mcp`：是否注入 Team MCP，MVP 必须为 `true`；
- `mcp_server_command`：Team MCP 命令；
- `env`：传给 Claude Code 子进程的附加环境变量。

`cwd` 解析优先级建议为：

```text
配置中显式 cwd
    ↓
当前请求 project_dir
    ↓
Team Workspace
```

MVP 必须确保 Claude Code 实际工作目录是用户指定的演示项目，而不是 JiuwenSwarm 服务进程目录。

------

## 8. 推荐代码改动

### 8.1 新增配置转换模块

建议新增一个职责单一的模块，例如：

```text
jiuwenswarm/agents/swarm/external_cli_specs.py
```

模块职责：

```text
JiuwenSwarm Config
        ↓
list[ExternalCliAgentSpec]
```

推荐接口语义：

```python
def build_external_cli_agent_specs(
    config,
    *,
    mode: str,
    project_dir: str | None,
    team_workspace: str | None,
) -> list[ExternalCliAgentSpec]:
    ...
```

函数需要：

1. 获取当前 Team Mode 下的外部 CLI 配置；
2. 过滤 `enabled=false` 的配置；
3. 当前 MVP 只接受 `cli_agent="claude"`；
4. 校验 `command` 必须是字符串列表或空；
5. 校验 `env` 必须是字符串到字符串的映射；
6. 解析最终 `cwd`；
7. 验证 `cwd` 存在且为目录；
8. 构造 `ExternalCliAgentSpec`；
9. 返回列表；
10. 不修改传入配置对象。

错误配置应在 Team 构建阶段明确失败，不要静默忽略。

### 8.2 修改 Team 装配入口

修改：

```text
jiuwenswarm/agents/swarm/assembly.py
```

在已有原生成员装配之后，为 Team Spec 写入：

```python
spec.external_cli_agents
```

需要保持以下原则：

- 不要把 `external_cli` 加入 `_MEMBER_ROLES`；
- `_MEMBER_ROLES` 表示需要构造 `DeepAgentSpec` 的 Jiuwen 原生成员；
- External CLI Agent 使用不同 Runtime，不应调用 `build_member_deep_agent_spec()`；
- 外部 Agent 配置应通过独立转换函数构造。

概念流程：

```text
注册 Swarm Provider
        ↓
构造 SwarmBuildContext
        ↓
装配 Leader / Teammate
        ↓
构造 ExternalCliAgentSpec
        ↓
写入 spec.external_cli_agents
        ↓
设置 build_context
```

写入时不得覆盖调用方已经显式设置的外部配置，需根据项目既有装配习惯决定：

- 合并；
- 或配置优先；
- 或显式冲突报错。

第一版推荐：如果调用方已有同名 `cli_agent`，明确报重复配置错误，避免静默覆盖。

### 8.3 确保 Team 使用跨进程配置

检查 Team Spec 的：

```text
spawn_mode
transport
storage
```

MVP 需要：

```text
spawn_mode = process
transport = pyzmq
storage = file-backed sqlite
```

不要在不确认当前 Team 默认配置的情况下硬编码覆盖所有 Team。

推荐行为：

- 如果启用了 External CLI Agent；
- 且当前 transport 是 `inprocess`；
- 则在构建阶段明确报错，说明 External CLI 需要跨进程 transport；
- 如果 storage 是内存数据库，也明确报错；
- 只有在项目已有明确默认策略时，才自动补全 `pyzmq` 和文件 SQLite。

不要通过静默改变全局 Team 行为掩盖配置问题。

### 8.4 确保 Leader 获得 `spawn_external_cli`

AgentCore 的 `spawn_external_cli` 只有在 `external_cli_agents` 非空时才应被装配到 Team Tool 集。

实现完成后必须验证：

```text
Leader Tool 列表包含 spawn_external_cli
```

如果没有，沿以下链路排查：

```text
spec.external_cli_agents
→ Team build
→ create_team_tools
→ Leader DeepAgent tools
```

不要另写一个 JiuwenSwarm 自定义 `spawn_claude` Tool 绕过现有机制。

### 8.5 增加 Leader 路由规则

在 JiuwenSwarm Leader 的 Team Prompt 或 Rail 中增加最小路由规则。

建议语义：

```text
团队中可以按需启动名为 claude 的外部 Coding Agent。

当任务包含以下内容时，应使用 Claude Code：
- 阅读或分析代码仓库；
- 实现、修改或重构代码；
- 定位和修复 Bug；
- 运行测试；
- 分析构建或 CI 错误；
- 执行与软件工程直接相关的 Shell 或 Git 操作。

执行流程：
1. 先检查 Team 中是否已有 member_name=claude-coder。
2. 如果不存在，调用 spawn_external_cli：
   - member_name: claude-coder
   - display_name: Claude Code
   - cli_agent: claude
   - desc: 明确的 Coding Agent Persona
3. 创建具体且可验收的 Team Task。
4. 将任务目标、项目目录、限制条件和验收标准发给 claude-coder。
5. 等待其反馈和任务完成状态。
6. 验证其报告后向用户汇总。

普通问答、简单写作、翻译和非 Coding 任务不得启动 Claude Code。
```

不要第一版引入额外分类模型或独立 Router Agent。

### 8.6 Claude Code Member Persona

`spawn_external_cli.desc` 同时承担 Member Persona 的作用。

建议内容：

```text
你是 Jiuwen Team 中的软件工程专家 Claude Code。

职责：
- 理解被分配的代码仓库和 Coding 任务；
- 阅读相关文件并定位根因；
- 在指定工作目录中实施最小必要修改；
- 运行相关测试；
- 使用 Team MCP 查看、认领和完成任务；
- 使用 send_message 向 Leader 汇报进展、结果和阻塞项。

完成任务时必须报告：
- 根因；
- 修改文件；
- 核心修改；
- 执行的测试；
- 测试结果；
- 未解决问题；
- 潜在风险。

不要处理与当前 Coding 任务无关的工作。
```

### 8.7 Member 复用

同一 Team Session 中不得重复启动：

```text
member_name = claude-coder
```

Leader 路由 Prompt 应先要求检查成员列表。

同时在代码层确认：

- 重复成员名时 AgentCore 会返回明确失败；
- 失败不会导致 Leader无限重试；
- 后续消息可以发送给已有成员。

第一版不需要实现 Agent Pool。

### 8.8 生命周期

验证：

- Team 启动后 Claude Code 按需启动；
- Team 运行期间 Claude Code 进程保持可用；
- 后续 Coding 任务可复用；
- `shutdown_member` 可以终止 Claude Code；
- Team 整体关闭时不会残留孤儿进程；
- 子进程异常退出时 Member 状态会变为失败或不可用。

如果 AgentCore 已提供对应生命周期，不得在 JiuwenSwarm 再实现一套独立进程管理器。

------

## 9. 完整期望执行链路

### 9.1 用户请求

```text
检查当前项目的除法实现，修复除零处理，
补充测试并运行测试。
```

### 9.2 Leader 路由

Leader 判断：

```text
任务类型 = Coding
Claude Code Member = 不存在
```

调用：

```json
{
  "member_name": "claude-coder",
  "display_name": "Claude Code",
  "cli_agent": "claude",
  "desc": "负责代码分析、修改、测试和结果汇报的软件工程专家"
}
```

### 9.3 External CLI 启动

AgentCore：

1. 根据 `cli_agent="claude"` 找到预声明配置；
2. 构造 Claude Adapter；
3. 构造 Team Join Descriptor；
4. 设置 `scope="member"`；
5. 注入 Team 身份环境；
6. 注入 `openjiuwen-team-mcp`；
7. 注入 Member Persona；
8. 使用项目目录作为 `cwd`；
9. 启动 Claude Code 子进程。

### 9.4 Leader 创建任务

任务示例：

```text
task_id: fix-zero-division
title: 修复除零处理并补充测试

content:
- 检查当前项目中的除法实现；
- 明确当前行为和根因；
- 实施最小修改；
- 对除数为零增加明确处理；
- 增加对应测试；
- 运行相关测试；
- 返回修改文件和测试结果；
- 不执行 Git Push。
```

### 9.5 Claude Code 执行

Claude Code 应：

1. 使用 Team MCP 查看任务；
2. 认领任务；
3. 阅读代码；
4. 修改代码；
5. 修改或新增测试；
6. 运行测试；
7. 使用 `send_message` 汇报结果；
8. 使用 `complete_task` 完成任务。

### 9.6 Leader 返回

Leader 应向用户返回：

```text
Claude Code 已完成修复。

根因：
...

修改：
- ...
- ...

验证：
- 执行 ...
- 结果 ...

剩余风险：
...
```

不能将 Claude Code 的原始 JSONL 日志直接返回给用户。

------

## 10. 日志要求

增加或确认以下结构化日志：

```text
[EXTERNAL_AGENT_CONFIG_LOADED]
[EXTERNAL_AGENT_ROUTE]
[EXTERNAL_AGENT_SPAWN_REQUEST]
[EXTERNAL_AGENT_SPAWNED]
[EXTERNAL_AGENT_TASK_ASSIGNED]
[EXTERNAL_AGENT_MESSAGE]
[EXTERNAL_AGENT_TASK_COMPLETED]
[EXTERNAL_AGENT_SHUTDOWN]
[EXTERNAL_AGENT_ERROR]
```

每条日志至少包含：

```text
session_id
team_name
member_name
cli_agent
task_id
project_dir
pid（适用时）
```

启动日志可以记录命令名称和非敏感参数，但禁止打印：

- API Key；
- Token；
  -完整环境变量；
- Team Join Descriptor 中的敏感字段；
- 用户私密数据。

------

## 11. 异常处理要求

### 11.1 Claude Code 不存在

检测到 `claude` 可执行文件不存在时：

- External Agent 启动失败；
- 返回明确错误；
- Leader 向用户说明 Coding Agent 当前不可用；
- 不进入无限重试；
- 不假装任务已经派发成功。

### 11.2 Claude Code 参数不兼容

如果实际版本不支持 Adapter 参数：

- 日志记录 stderr 尾部；
- 返回结构化启动错误；
- 不通过删除必要参数临时掩盖问题；
- 根据实际版本调整 `command` 覆盖或 Adapter。

### 11.3 Team MCP 不可用

如果 Claude Code 能修改代码，但不能调用 Team MCP：

- MVP 判定为失败；
- 不得仅凭文件被修改就认为接入成功。

因为本任务验证的是：

```text
Claude Code 成为 Jiuwen Team Member
```

而不是：

```text
Jiuwen 启动了一个 Claude Code 进程
```

### 11.4 Claude Code 异常退出

需要：

- 捕获退出码；
- 记录 stderr 尾部；
- 更新 Member 或 Task 状态；
- 通知 Leader；
- Leader向用户返回明确失败原因。

### 11.5 超时

第一版至少配置：

```text
单任务总超时
无输出超时
```

具体数值遵循项目现有超时配置；没有现有配置时，可为集成测试设置合理的固定值。

超时后：

1. 终止成员或当前任务；
2. 将任务标记为失败；
3. 通知 Leader；
4. 清理子进程。

------

## 12. 测试要求

### 12.1 单元测试

至少覆盖：

#### External CLI 配置解析

- 正确构造 Claude Code Spec；
- `enabled=false` 时不构造；
- 非法 `command` 报错；
- 非法 `env` 报错；
- 不存在的 `cwd` 报错；
- `project_dir` 能作为默认 `cwd`；
- 重复 `cli_agent` 不被静默接受。

#### Team Spec 装配

- 启用 Claude 后 `spec.external_cli_agents` 非空；
- 未启用时保持原行为；
- 原生 Leader/Teammate 装配不受影响；
- 已存在冲突配置时行为明确；
- External CLI 没有被错误当作 `DeepAgentSpec` 构建。

#### 跨进程配置校验

- External CLI + `inprocess` transport 明确失败；
- External CLI + 内存数据库明确失败；
- 合法 ZMQ + 文件 SQLite 能通过。

### 12.2 集成测试

允许使用 Mock CLI 完成稳定 CI 测试，不要求 CI 环境真实安装 Claude Code。

Mock CLI 应模拟：

- 读取 stdin；
- 输出 Claude Adapter 所需的 JSONL；
- 输出完成事件；
- 接收注入的 System Prompt；
- 检查 MCP 启动参数；
- 正常退出；
- 异常退出；
- 超时。

集成测试至少验证：

```text
配置
→ Team 构建
→ spawn_external_cli
→ external_cli Member 注册
→ 消息发送
→ 结果接收
→ shutdown
```

### 12.3 真实 Claude Code 手工测试

真实 Claude Code 测试在隔离 Demo 仓库中进行。

准备一个简单 Python 项目：

```text
demo_project/
├── app/
│   └── calculator.py
├── tests/
│   └── test_calculator.py
└── README.md
```

预置明确 Bug，例如除零行为不符合要求。

------

## 13. 验收场景

### 场景一：非 Coding 请求

输入：

```text
解释多 Agent 系统是什么。
```

预期：

- Jiuwen Leader 自行回答；
- 不调用 `spawn_external_cli`；
- Team Roster 中没有 `claude-coder`。

### 场景二：首次 Coding 请求

输入：

```text
检查当前项目中的除法实现，
修复除零问题，补充测试并运行测试。
```

预期：

1. Leader 调用 `spawn_external_cli`；
2. Team Roster 出现 `claude-coder`；
3. Member 类型为 `external_cli`；
4. Claude Code 收到并认领任务；
5. Demo 项目产生真实代码修改；
6. 测试执行成功；
7. Claude Code 完成任务；
8. Leader向用户返回结果。

### 场景三：同会话后续修改

输入：

```text
再把异常信息修改得更明确。
```

预期：

- 不重复启动 `claude-coder`；
- 使用已有 Member；
- Claude Code 保留当前项目上下文；
- 完成第二轮修改和测试。

### 场景四：Claude Code 不可用

模拟删除或更改 Claude Code 路径。

预期：

- 启动失败；
- Leader收到明确错误；
- 用户得到明确说明；
- Team 不会无限等待；
- 不残留异常 Member。

### 场景五：Team MCP 失败

模拟 `openjiuwen-team-mcp` 不可用。

预期：

- Claude Code 无法完成 Team 协作闭环；
- 测试明确失败；
- 日志指出 MCP 注册或启动问题。

------

## 14. 禁止事项

Coding Agent 实施时禁止：

1. 不得把 Claude Code实现为普通 Jiuwen Tool。
2. 不得使用 `subprocess.run(["claude", prompt])` 代替 External CLI Runtime。
3. 不得复制 AgentCore 的 External CLI 实现到 JiuwenSwarm。
4. 不得新增与 `spawn_external_cli` 重复的 `spawn_claude` Tool。
5. 不得为了演示成功跳过 Team MCP。
6. 不得只读取 Claude Code stdout，而不注册 Team Member。
7. 不得把一次性 CLI 调用伪装成长期 Team Member。
8. 不得硬编码用户本地项目路径。
9. 不得把 API Key 写入仓库配置模板。
10. 不得修改与本 MVP 无关的 Channel、Cron、端插件或 GUI Agent 代码。
11. 不得提前实现 A2A。
12. 不得大规模重构 Team Runtime。
13. 不得静默吞掉 Claude Code 启动失败。
14. 不得使用生产仓库验证具有写权限的 Claude Code。
15. 不得声称已经解决第三方 Agent 安全治理问题。

------

## 15. 推荐实施顺序

### 阶段一：AgentCore 独立验证

先在最小脚本或现有 AgentCore 测试中验证：

```text
TeamAgentSpec
+ ExternalCliAgentSpec(cli_agent="claude")
+ spawn_external_cli
+ Team MCP
```

确认 AgentCore 链路本身可运行。

### 阶段二：JiuwenSwarm 配置接入

实现：

```text
JiuwenSwarm YAML
→ ExternalCliAgentSpec
→ TeamAgentSpec.external_cli_agents
```

暂时不增加自动路由，先验证手动或固定 Prompt 可以调用 `spawn_external_cli`。

### 阶段三：Leader 自动路由

加入 Coding 任务路由规则，验证：

```text
Coding 请求
→ 自动启动 Claude Code
```

### 阶段四：Member 复用和生命周期

验证：

- 第二轮请求复用；
- 正常关闭；
- 异常退出；
- 超时；
- 无孤儿进程。

### 阶段五：完整 Demo

通过 JiuwenSwarm Web 或当前最稳定的 Channel 执行完整演示。

第一版不要直接使用小艺 Channel 作为主要调试入口，以免同时引入：

- Xiaoyi Channel；
- Gateway；
- WebSocket；
- Device Session；
- Claude Adapter；
- Team MCP；

多个故障变量。

先在 Web 或 CLI 打通，再验证小艺入口是否能够透明复用同一 Team 能力。

------

## 16. 最终交付物

Coding Agent 完成任务后必须提交：

### 16.1 代码

- External CLI 配置解析；
- Team Spec 装配；
- Leader 路由 Prompt；
- 必要日志；
- 单元测试；
- 集成测试。

### 16.2 配置示例

提供不包含真实凭据的 Claude Code 配置示例。

### 16.3 测试结果

列出：

- 执行的测试命令；
- 通过和失败数量；
- 真实 Claude Code 演示结果；
- 未验证项。

### 16.4 源码改动说明

逐文件说明：

```text
文件
修改内容
修改原因
是否影响原有行为
```

### 16.5 完整调用链

给出实际实现后的调用链，必须包含真实类名和函数名：

```text
用户请求
→ Team 构建入口
→ enrich_team_spec_for_swarm
→ external_cli_agents 装配
→ Leader Tool
→ spawn_external_cli
→ spawn_external_cli_agent
→ build_cli_runtime
→ Claude Code subprocess
→ Team MCP
→ Team Task / Message
→ Leader
```

不得只给概念流程。

### 16.6 已知问题

至少说明：

- 当前 Claude Adapter 的危险权限参数；
- 当前安全机制不覆盖外部 Agent 自身 Tool；
- 实际 OpenJiuwen 版本兼容情况；
- Claude Code 版本兼容情况；
- Team MCP 稳定性；
- 未来接入其他 Agent 时需要抽象的部分。

------

## 17. 完成定义

只有同时展示以下五项证据，MVP 才算完成：

```text
1. Leader 识别 Coding 任务；
2. Leader 动态调用 spawn_external_cli；
3. Team Roster 出现 claude-coder；
4. Claude Code 通过 Team MCP 认领并完成任务；
5. Leader 获得结果并统一回复用户。
```

仅满足以下任一情况均不算完成：

```text
Claude Code 进程启动成功；
Claude Code 输出了一段文本；
Claude Code 修改了代码；
Leader 调用了一个 Claude Tool；
MCP Server 能单独启动。
```

最终需要证明的是：

> Claude Code 已经从一个独立外部 Coding Agent，转变为能够被 Jiuwen Leader 创建、派发、协作和管理的 Jiuwen Team Member。