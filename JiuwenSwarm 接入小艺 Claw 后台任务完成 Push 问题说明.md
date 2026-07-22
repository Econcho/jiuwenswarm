# JiuwenSwarm 接入小艺 Claw 后台任务完成 Push 问题说明

## 一、问题背景

分析基线为：

```text
openJiuwen-ai/jiuwenswarm
branch: copy_dev_0.2.3.beta1
```

本文仅以该分支代码为准。

小艺 Claw 接入 JiuwenSwarm 后，用户可能发起耗时较长的任务，例如：

```text
写一个俄罗斯方块小游戏
```

用户提交任务后退出小艺 Claw、返回桌面或切换到其他 App。JiuwenSwarm 可以继续在后台执行任务，但任务完成后，小艺 Claw不会在手机状态栏或锁屏展示“任务已完成”通知。

当前表现为：

```text
用户发起任务
    ↓
JiuwenSwarm 后台继续执行
    ↓
任务执行完成
    ↓
结果可能仍保存在会话中
    ↓
手机无系统通知
```

该问题并不等价于 Agent 输出没有返回，也不等价于 Gateway 无法发送消息。其核心是：**任务完成事件没有稳定触发小艺系统 Push 通道。**

------

# 二、JiuwenSwarm Push 链路简要分析

## 2.1 两类不同的 Push

JiuwenSwarm 中存在两类名称相近、用途不同的 Push。

### 第一类：Gateway Push

相关目录：

```text
jiuwenswarm/server/gateway_push/
```

重点文件：

```text
jiuwenswarm/server/gateway_push/transport.py
```

这部分 Push 用于解决 JiuwenSwarm 多进程架构中的消息传递问题，其职责是：

```text
AgentServer
    ↓
Gateway Push Transport
    ↓
Gateway
    ↓
Channel.send()
```

它本质上是：

```text
AgentServer → Gateway 的进程间下行消息通道
```

不是 Android 系统通知服务。即使 Gateway Push 工作正常，也只能说明 Agent 输出能够到达 Gateway 或 XiaoyiChannel，不能说明手机状态栏会出现通知。

------

### 第二类：小艺系统 Push

相关目录：

```text
jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi/
```

重点文件：

```text
jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi/xiaoyi_connect.py

jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi/
    xiaoyi_utils/push.py
```

这部分负责调用小艺侧的 HTTP Webhook Push 接口，才是可能最终形成：

```text
状态栏通知
锁屏通知
任务完成提醒
```

的实际链路。

整体链路为：

```text
用户通过小艺 Claw 发起任务
    ↓
XiaoyiChannel 接收 message/stream 请求
    ↓
转换为 JiuwenSwarm 内部 Message
    ↓
Agent / Coding Agent 执行任务
    ↓
Agent 输出通过 Gateway 路由回 XiaoyiChannel.send()
    ↓
XiaoyiChannel 判断任务是否需要 Push
    ↓
XiaoYiPushService 构造 HTTP Push 请求
    ↓
HAG Agent Webhook
    ↓
小艺 Claw / 手机系统通知
```

------

## 2.2 任务接收与状态管理

主要文件：

```text
jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi/
    xiaoyi_connect.py
```

该文件中的 `XiaoyiChannel` 同时负责：

- 小艺 WebSocket 连接；
- 用户请求解析；
- Agent 输出回传；
- 流式文本处理；
- 任务超时管理；
- 会话状态管理；
- 任务完成 Push 判断；
- 调用 `XiaoYiPushService`。

`XiaoyiChannelConfig` 中定义了：

```python
task_timeout_ms: int = 3600000
```

默认值为 3,600,000 毫秒，即一小时。

Channel 内部维护：

```python
self._task_timeout_tasks
self._sessions_waiting_for_push
self._accumulated_texts
self._session_active
```

其中：

```python
self._sessions_waiting_for_push: dict[str, str]
```

保存：

```text
session_id → task_id
```

用于标记某个任务完成后是否需要发送系统 Push。

------

## 2.3 当前 Push 触发方式

当前普通任务并不是在任务完成时直接发送 Push。

实际逻辑为：

```text
收到任务
    ↓
启动 task_timeout_ms 计时器
    ↓
任务运行超过一小时
    ↓
将 session 标记为 waiting_for_push
    ↓
任务之后完成
    ↓
如果正文非空，则调用 Push
```

任务完成阶段的核心判断相当于：

```python
if (
    self._is_session_waiting_for_push(session_id, task_id)
    and accumulated_text
):
    await self._send_push_notification(...)
```

因此，普通任务要发送 Push，必须同时满足：

```text
1. 任务此前已经进入 waiting_for_push；
2. 任务完成时 accumulated_text 非空。
```

如果任务在一小时内完成，超时处理器尚未触发，任务不会进入 `waiting_for_push`，最终不会调用 Push。

------

## 2.4 Push HTTP 请求

主要文件：

```text
jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi/
    xiaoyi_utils/push.py
```

该文件中的 `XiaoYiPushService` 负责：

- 检查 Push 配置；
- 构造 Push 请求 Header；
- 构造 JSON-RPC 请求体；
- 调用 HAG Agent Webhook；
- 根据 HTTP 状态判断是否发送成功。

Push 所需的主要配置包括：

```text
uid
api_key
api_id
push_id
push_url
agent_id
```

在 `xiaoyi_claw` 模式下，HTTP 请求会携带小艺相关鉴权 Header，并使用 `push_id` 指定通知目标。

------

# 三、当前 Push 实现存在的问题

## 3.1 核心问题：Push 被错误绑定到一小时任务超时

问题文件：

```text
jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi/
    xiaoyi_connect.py
```

当前代码把以下两个概念混为一体：

```text
任务是否需要系统通知
```

和：

```text
任务是否执行超过一小时
```

但这两个概念没有必然关系。

实际场景可能是：

```text
00:00 用户发起任务
00:10 用户退出小艺 Claw
05:00 任务完成
```

此时任务只运行五分钟，没有触发一小时超时：

```text
waiting_for_push 未设置
    ↓
任务完成
    ↓
Push 判断为 False
    ↓
不发送通知
```

因此，任务完成得越快，反而越不可能触发当前 Push。

这是用户退出 App 后任务完成却没有通知的首要原因。

------

## 3.2 JiuwenSwarm 不知道 App 是否处于后台

问题文件：

```text
xiaoyi_connect.py
```

当前代码主要维护：

```text
WebSocket 是否连接
任务是否活跃
任务是否超时
会话是否完成
```

但没有维护：

```text
小艺 Claw 是否在前台
用户是否仍停留在当前会话
任务结果是否已在端侧展示
用户是否已经消费最终结果
```

因此 JiuwenSwarm 无法根据真实前后台状态决定是否需要通知。

需要明确：

```text
WebSocket 在线 ≠ App 在前台
```

用户退出到桌面后，小艺 Claw 与云端之间的连接仍可能存在，因此不能通过 WebSocket 是否断开来判断用户是否正在查看任务结果。

------

## 3.3 Terminal Status 分支绕过 Push

问题文件：

```text
xiaoyi_connect.py
```

`XiaoyiChannel.send()` 对状态事件存在独立处理分支。

当状态为：

```text
completed
failed
canceled
```

时，代码会：

```text
停止心跳
清理超时任务
标记会话完成
清理 accumulated_text
return
```

该分支没有检查：

```python
_is_session_waiting_for_push(...)
```

也没有调用：

```python
_send_push_notification(...)
```

因此，如果任务最终完成主要通过 terminal status 表达，而不是通过文本 final 表达，Push 会被直接绕过。

如果 terminal status 先于最终文本到达，还可能提前清理任务状态和累计正文，使后续文本无法完成正确的 Push 判断。

------

## 3.4 文本完成和状态完成由两套逻辑分别处理

问题文件：

```text
xiaoyi_connect.py
```

当前存在两套任务完成处理入口：

```text
CHAT_FINAL / 文本 final
```

以及：

```text
completed / failed / canceled status
```

两套入口分别执行：

```text
停止心跳
清理超时
标记完成
清理正文
```

但只有文本 final 分支包含 Push 判断。

这会导致任务最终行为依赖事件到达顺序：

```text
先收到 CHAT_FINAL
可能触发 Push

先收到 completed status
可能直接清理并跳过 Push
```

任务完成、资源清理和消息投递不应由多个分支各自实现，而应统一进入一个终态处理函数。

------

## 3.5 系统 Push 与 WebSocket 发送逻辑耦合

问题文件：

```text
xiaoyi_connect.py
```

`XiaoyiChannel.send()` 开头存在对 `_ws_connections` 的判断。

在某些连接状态下，方法可能在进入任务完成 Push 判断前直接返回。

但从语义上：

```text
WebSocket
```

主要用于前台实时消息；

```text
HTTP Push
```

主要用于后台或离线通知。

系统 Push 不应依赖 WebSocket 当前是否可发送。恰恰是在实时通道不可用时，Push 才更有价值。

正确关系应当是：

```text
实时消息投递失败或未确认
    ↓
使用系统 Push 作为补充通知
```

而不是：

```text
实时连接不可用
    ↓
整个 send 提前返回
    ↓
系统 Push 也无法执行
```

------

## 3.6 `push_id` 是 Channel 级全局状态，没有绑定任务

问题文件：

```text
xiaoyi_connect.py
xiaoyi_utils/push.py
```

当前 Push ID 主要保存在：

```python
self.config.push_id
self.push_id
```

这意味着一个 `XiaoyiChannel` 实例主要保存一个当前 Push ID。

但正确的数据关系应为：

```text
用户 / 设备 / session / task
    ↓
对应自己的 push_id
```

例如：

```text
(session_id, task_id) → push_id
```

如果同一个 JiuwenSwarm 服务同时处理多个用户或多个设备，后到达请求可能覆盖前一个请求的 Push ID，从而产生：

```text
任务 A 由设备 A 发起
任务 B 由设备 B 发起
任务 A 最后完成
读取到的却是设备 B 的 push_id
```

这会导致通知串用户、串设备或投递失败。

------

## 3.7 `waiting_for_push` 的键粒度不足

问题文件：

```text
xiaoyi_connect.py
```

当前结构为：

```python
dict[session_id, task_id]
```

同一 session 同时只能保存一个等待 Push 的 task。

在以下场景中可能发生覆盖：

```text
同一会话发起多个并发任务
Coding Agent 启动子任务
任务取消后立即重试
多 Agent 并行执行
```

更合理的主键应为：

```python
(session_id, task_id)
```

而不是只用 `session_id`。

------

## 3.8 `_accumulated_texts` 的实现依赖上游消息格式

问题文件：

```text
xiaoyi_connect.py
```

代码名称表示其保存的是累计文本：

```python
self._accumulated_texts
```

但实际处理主要是：

```python
self._accumulated_texts[session_id] = content
```

即覆盖，而不是明确追加。

该实现隐含假设：

```text
每一个 CHAT_DELTA 的 content 都是从开头到当前的完整累计正文
```

如果上游发送的是纯增量 chunk，则最终缓存的可能只是最后一段内容。

此外，最终 Push 还要求：

```python
and accumulated_text
```

如果最终事件的正文为空，或者正文已经被 terminal status 分支清理，也不会发送 Push。

------

## 3.9 Push 成功判断过于简单

问题文件：

```text
xiaoyi_utils/push.py
```

当前代码主要根据 HTTP 状态码判断：

```text
HTTP 200 → Push 成功
非 200 → Push 失败
```

但 HTTP 200 只能证明服务端成功返回 HTTP 响应，不能必然证明：

```text
push_id 有效
业务鉴权成功
HAG 已接受通知
设备在线
通知已到达手机
系统成功展示通知
```

当前实现对以下信息记录不足：

```text
响应正文
JSON-RPC error
业务错误码
错误描述
Push 平台请求 ID
设备投递状态
```

因此，即使日志显示发送成功，也无法确认通知是否真正到达设备。

HAG 接口的完整业务响应语义无法仅根据 JiuwenSwarm 仓库确定，需要结合小艺 Push 接口文档或实际响应报文验证。

------

## 3.10 Push 失败后没有重试和补偿

问题文件：

```text
xiaoyi_connect.py
xiaoyi_utils/push.py
```

普通任务完成后，无论 Push 是否真正成功，代码都会继续清理：

```text
waiting_for_push
accumulated_text
任务状态
```

当前没有：

```text
重试
指数退避
持久化 Outbox
死信记录
任务完成通知补偿
幂等投递
```

因此只要发生一次临时网络错误，任务完成通知就会永久丢失。

------

# 四、可行解决方案

## 4.1 方案一：最小改动方案，不修改小艺 Claw

适用目标：

```text
先解决“任务完成后完全没有通知”
```

实现原则：

> 对需要后台执行的任务，在终态到达时直接尝试 Push，不再依赖一小时 timeout。

可以在 `xiaoyi_connect.py` 中调整为：

```text
任务进入终态
    ↓
获得最终正文或任务摘要
    ↓
存在有效 push_id
    ↓
调用 _send_push_notification()
```

可以增加配置：

```yaml
push_on_task_complete: true
```

在 `xiaoyi_claw` 模式下启用。

任务完成处理统一为：

```python
async def _complete_task(
    session_id,
    task_id,
    terminal_state,
    final_text,
):
    ...
```

由以下事件统一调用：

```text
CHAT_FINAL
completed status
failed status
canceled status
```

优点：

- 修改范围小；
- 不依赖小艺端新增协议；
- 能直接解决用户退出 App 后无通知的问题；
- 不再依赖一小时 timeout。

缺点：

- 用户仍停留在前台时，也可能收到一条系统通知；
- 需要增加幂等控制，避免文本 final 和 status 都触发通知。

该方案适合作为第一阶段修复。

------

## 4.2 方案二：推荐方案，增加端侧前后台或结果 ACK

最合理的判断依据不是任务运行时长，而是：

```text
最终结果是否已被端侧展示和消费
```

建议小艺 Claw 或中间服务增加以下任一种信号：

### 前后台状态事件

```text
app_foreground
app_background
conversation_visible
conversation_hidden
```

任务状态中记录：

```python
state.app_foreground
state.conversation_visible
```

任务完成时：

```text
当前会话可见
    → 只发送 WebSocket 最终结果

当前会话不可见
    → 发送 WebSocket 结果并发送系统 Push
```

### 最终结果 ACK

更推荐由端侧在成功展示最终结果后返回：

```text
final_result_displayed
```

或：

```text
task_result_ack
```

服务端逻辑：

```text
任务完成
    ↓
通过 WebSocket 发送最终结果
    ↓
等待端侧 ACK，例如 3～10 秒
    ├─ 收到 ACK：不发送系统 Push
    └─ 未收到 ACK：发送系统 Push
```

这种方式比检查 WebSocket 是否连接更可靠，因为它验证的是结果是否真正展示，而不是连接是否存在。

------

## 4.3 方案三：引入任务级投递状态

建议在 `xiaoyi_connect.py` 中引入任务级状态对象：

```python
@dataclass
class XiaoyiTaskDeliveryState:
    session_id: str
    task_id: str
    push_id: str

    final_text: str = ""
    terminal_state: str = ""

    app_foreground: bool | None = None
    websocket_delivered: bool = False
    final_ack_received: bool = False

    push_required: bool = False
    push_sent: bool = False
    push_attempts: int = 0
```

保存结构：

```python
self._task_delivery_states: dict[
    tuple[str, str],
    XiaoyiTaskDeliveryState,
]
```

这样可以解决：

```text
push_id 串任务
同 session 多任务覆盖
文本状态分散
重复 Push
任务完成事件顺序不一致
```

任务创建时保存：

```text
session_id
task_id
push_id
用户或设备标识
```

任务结束后使用该任务自己的 `push_id`，禁止从 Channel 全局配置中读取最后一个 Push ID。

------

## 4.4 方案四：统一终态处理

在 `xiaoyi_connect.py` 中增加唯一终态入口：

```python
async def _finalize_task(
    self,
    session_id: str,
    task_id: str,
    state: str,
    final_text: str | None = None,
) -> None:
    ...
```

统一负责：

```text
1. 合并最终正文；
2. 更新任务终态；
3. 停止心跳；
4. 清理 timeout；
5. 发送实时最终结果；
6. 判断是否需要系统 Push；
7. 执行 Push；
8. 记录投递结果；
9. 最后清理任务状态。
```

以下分支只负责提取事件信息，不再独立清理：

```text
CHAT_FINAL
completed
failed
canceled
```

还应增加幂等字段：

```python
state.finalized
state.push_sent
```

避免同时收到文本 final 和 completed status 时发送两次通知。

------

## 4.5 方案五：系统 Push 与 WebSocket 解耦

应将任务完成后的两种投递视为独立动作：

```text
实时结果投递
系统通知投递
```

伪代码：

```python
ws_result = await send_result_over_websocket(...)

if should_push(state, ws_result):
    push_result = await send_system_push(...)
```

即使：

```text
WebSocket 不存在
WebSocket 已断开
WebSocket 发送异常
```

也必须继续执行系统 Push 判断。

不能在 `XiaoyiChannel.send()` 开头因为没有 WebSocket 就直接跳过全部处理。

------

## 4.6 方案六：增加可靠投递机制

至少增加有限重试：

```text
第一次立即发送
失败后 1 秒重试
再次失败后 5 秒重试
最多重试 3 次
```

更完整的实现可引入 Push Outbox：

```python
@dataclass
class PushOutboxItem:
    task_id: str
    push_id: str
    title: str
    content: str
    status: str
    retry_count: int
    next_retry_at: float
```

Push 成功后才将 Outbox 项标记为完成。

同时使用稳定幂等键：

```text
task_id + notification_type
```

防止网络重试造成重复通知。

------

# 五、推荐修改文件

## 必须修改

### 1. `xiaoyi_connect.py`

主要修改内容：

- 去除 Push 对一小时 timeout 的强依赖；
- 新增任务级投递状态；
- 将 `push_id` 与 `(session_id, task_id)` 绑定；
- 合并文本 final 和 terminal status 的完成逻辑；
- Push 与 WebSocket 解耦；
- 增加 Push 幂等控制；
- 正确累计最终文本；
- 根据 Push 返回值决定是否清理状态。

### 2. `xiaoyi_utils/push.py`

主要修改内容：

- 返回结构化 Push 结果，而不是只返回布尔值；
- 读取和记录 HTTP 响应正文；
- 解析 JSON-RPC error 或业务错误码；
- 对日志中的 uid、api_key、push_id 做脱敏；
- 增加明确的超时配置；
- 支持有限重试或由上层统一重试；
- 记录 trace ID、HTTP 状态和业务状态。

------

## 可能需要修改

### 3. 小艺 Channel 配置加载位置

需要新增配置项，例如：

```yaml
push_on_task_complete: true
push_ack_timeout_ms: 5000
push_max_retries: 3
push_retry_interval_ms: 1000
```

具体配置加载文件需要根据 JiuwenSwarm 当前 Channel 配置构造链路确定。

### 4. 小艺 Claw 或小艺中间服务

如果采用推荐方案，需要新增：

```text
App 前后台状态
当前会话可见状态
最终结果展示 ACK
```

JiuwenSwarm 仓库本身无法确定小艺端当前支持哪些事件，需要结合小艺 Claw 协议或服务端代码确认。

------

# 六、建议实施顺序

## 第一阶段：快速验证根因

临时将：

```yaml
task_timeout_ms: 3600000
```

改为：

```yaml
task_timeout_ms: 10000
```

让任务执行超过十秒。

如果十秒超时后任务完成能够收到通知，则可以确认：

```text
HAG Push 基本可用
push_id 基本有效
主要问题是 waiting_for_push 的触发条件错误
```

也可以临时在请求接收阶段直接执行：

```python
self._mark_session_waiting_for_push(session_id, task_id)
```

该方式仅用于验证，不应作为最终修复，因为它会导致前台任务也发送通知。

------

## 第二阶段：完成最小修复

实现：

```text
任务终态统一处理
任务完成直接 Push
Push 与 WebSocket 解耦
任务级 push_id
Push 幂等
```

这一阶段不依赖小艺端修改，即可解决当前主要问题。

------

## 第三阶段：增加端侧 ACK

由小艺端明确反馈最终结果是否已展示。

根据 ACK 决定：

```text
已展示 → 不发送系统通知
未展示 → 发送系统通知
```

解决前台重复通知问题。

------

## 第四阶段：增加可靠性机制

实现：

```text
Push 重试
Outbox
结果持久化
点击通知回到任务
完整结果关联
投递监控
```

------

# 七、验证与日志要求

建议在 `xiaoyi_connect.py` 中增加统一日志：

```text
[PUSH_STATE]
session_id
task_id
terminal_state
push_id_masked
waiting_for_push
app_foreground
final_ack_received
final_text_length
push_required
```

在 `push.py` 中增加：

```text
[PUSH_REQUEST]
endpoint
api_id
push_id_masked
trace_id
attempt
```

以及：

```text
[PUSH_RESPONSE]
http_status
response_body
business_code
business_message
elapsed_ms
```

禁止输出完整：

```text
api_key
uid
push_id
```

------

# 八、验收标准

修复后至少应满足以下场景。

## 场景一：后台任务正常完成

```text
用户发起长任务
用户退出小艺 Claw
任务完成
```

预期：

```text
状态栏或锁屏收到任务完成通知
点击后能够回到对应任务或会话
```

## 场景二：用户保持前台

采用最小方案时允许收到通知。

采用 ACK 方案后预期：

```text
结果已经在前台展示
不再发送重复系统通知
```

## 场景三：WebSocket 断开

```text
任务执行中 WebSocket 断开
任务最终完成
```

预期：

```text
仍然尝试发送 HTTP Push
```

## 场景四：同用户并发任务

预期：

```text
每个 task 使用自己的 push_id 和结果摘要
任务状态互不覆盖
```

## 场景五：多用户并发

预期：

```text
设备 A 的任务只通知设备 A
设备 B 的任务只通知设备 B
```

## 场景六：Push 临时失败

预期：

```text
进行有限重试
日志能够定位失败原因
任务通知不会在第一次网络失败后永久丢失
```

## 场景七：文本 final 和 completed status 同时到达

预期：

```text
任务只完成一次
系统通知最多发送一次
状态只清理一次
```

------

# 九、最终结论

当前无通知问题的核心不是 HAG Push 类完全不存在，而是 JiuwenSwarm 对 Push 触发条件的建模错误。

当前逻辑是：

```text
任务运行超过一小时
    → 标记 waiting_for_push
    → 任务完成后 Push
```

正确逻辑应为：

```text
任务完成
    ↓
判断最终结果是否已经被用户看到
    ├─ 已看到：不 Push
    └─ 未看到：Push
```

在无法修改小艺端、无法获得前后台状态或结果 ACK 的情况下，最现实的修复方式是：

```text
对小艺 Claw 的后台型任务，在任务终态到达时直接 Push
```

同时必须完成：

```text
统一终态处理
Push 与 WebSocket 解耦
push_id 任务级绑定
幂等控制
失败重试
完整日志
```

其中，优先修改文件为：

```text
jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi/
    xiaoyi_connect.py

jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi/
    xiaoyi_utils/push.py
```

`server/gateway_push` 不是本问题的主要修复位置，它只负责 AgentServer 到 Gateway 的进程间消息转发。