# celia_memory 自研记忆插件工具、Hook 点与配置说明

## 1. 文档目的

本文说明 CeliacLaw 适配的 celia_memory 记忆插件对外提供的工具、使用的 OpenClaw Hook、各 Hook 承担的职责，以及 `openclaw.json` 需要完成的配置适配。

本文仅描述公开能力和配置契约，不包含源码路径、内部调用链、存储结构、算法、完整 Prompt、真实凭据或生产环境路径。

## 2. 总体结论

| 项目               | 说明                                                 |
| ------------------ | ---------------------------------------------------- |
| Agent 可调用工具   | 13 个                                                |
| OpenClaw 事件 Hook | 7 个不同的 Hook 名                                   |
| 记忆方式           | 普通对话自动处理，明确记忆或纠正可主动触发           |
| 检索方式           | 全局概览、场景索引、场景详情、事实和原始对话逐层检索 |
| 配置方式           | 公共记忆插件注册配置 + CeliacLaw 平台配置            |
| 运行原则           | 记忆能力异常时应允许主 Agent 降级运行                |

CeliacLaw 使用统一的 `memory-celia` 插件能力，不另外定义一套同名工具或 Hook。

## 3. 对 Agent 提供的工具

### 3.1 工具分类

13 个工具按用途可分为 4 类。使用时可先根据任务目标选择工具类别，再查看下一节中的具体参数。

| 工具类别       | 类别说明                                                    | 包含工具                                                     | 典型使用场景                                                 | 是否影响记忆数据                               |
| -------------- | ----------------------------------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------ | ---------------------------------------------- |
| 主动记忆管理   | 对长期记忆进行新增、删除，或立即处理等待中的记忆更新        | `memory_store`<br>`memory_forget`<br>`memory_flush`          | 用户明确要求记住、纠正或遗忘信息；写入后需要立即完成处理     | 会新增、删除或处理记忆数据                     |
| 记忆查看与导出 | 查看已有记忆，或将指定范围的记忆导出为文件                  | `memory_list`<br>`memory_dump`                               | 用户查看已保存的信息；经授权进行数据导出、审计或问题定位     | 查看操作只读；导出会生成文件，但不修改已有记忆 |
| 分层记忆检索   | 按“全局概览 → 场景 → 事实 → 原始对话”的层次逐步获取所需信息 | `memory_get_global_summary`<br>`memory_scene_list_load`<br>`memory_scene_load`<br>`memory_record_search`<br>`memory_chat_history_search` | 回答问题或执行任务前，需要获取用户背景、相关场景、具体事实或历史原话 | 只读，不修改记忆数据                           |
| 记忆整理查询   | 查询记忆整理任务的运行状态、单次结果和近期历史              | `dream_status`<br>`dream_run_summary`<br>`dream_recent_runs` | 用户关注记忆整理是否运行、运行进度、整理结果或近期记录       | 只读，不触发新的整理任务                       |

其中，“主动记忆管理”用于用户明确表达的记忆操作；普通对话的记忆沉淀由自动流程完成。“分层记忆检索”应遵循由概览到细节的顺序，只有需要核对原话或完整上下文时才检索历史对话。

### 3.2 工具与参数总表

| 类别           | 工具                         | 工具描述与调用场景                                           | 参数说明                                                     | 返回与数据影响                                               |
| -------------- | ---------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------ | ------------------------------------------------------------ |
| 主动记忆管理   | `memory_store`               | 主动保存需要长期生效的信息。仅在用户明确要求“记住”、明确纠正长期信息，或给出以后可复用的反馈时调用；不用于普通聊天、一次性任务内容、临时计划或单纯感谢。 | `text`：string，必填；建议非空且语义完整，用于填写需要长期记住的信息。 | 返回是否已接收本次记忆请求；记忆能力关闭时返回未写入说明。提交记忆更新。 |
| 主动记忆管理   | `memory_forget`              | 删除指定记忆。仅在用户明确要求删除或遗忘某条长期记忆时调用。 | `query`：string，条件必填，用自然语言查找待删除记忆。<br>`memoryId`：number，条件必填，精确指定记忆 ID。<br>两者至少提供一个；同时提供时优先按 `memoryId` 处理。 | 精确匹配时返回删除结果；匹配不唯一时返回候选列表，不直接批量删除。删除记忆数据。 |
| 主动记忆管理   | `memory_flush`               | 立即处理等待中的记忆更新。适合连续调用 `memory_store` 后，需要后续查询尽快看到新内容的场景。 | `timeoutMs`：number，可选，默认 `60000` 毫秒，表示最大等待时间。<br>`tenant_id`：string，可选，通常自动补齐。<br>`user_id`：string，可选，通常自动补齐。 | 返回处理状态和结果摘要。触发同步处理。                       |
| 记忆查看与导出 | `memory_list`                | 按语义类别查看已有记忆。仅在用户明确要求查看已保存的个人信息、场景或事实时调用。 | `categories`：string[]，必填，至少一项；可选值为 `global_overview`、`scene_memory`、`atomic_facts`。<br>`limit`：number，可选，默认 `20`、最大 `100`，表示原子事实每页条数。<br>`offset`：number，可选，默认 `0`，表示原子事实分页偏移量。 | 返回所选类别的全局概览、场景记忆或分页原子事实。只读。       |
| 记忆查看与导出 | `memory_dump`                | 将指定会话范围的记忆导出为文件。适合用户明确要求导出，或经过授权的审计和问题定位。 | `sessionId`：string，可选，默认当前工具会话。<br>`outputPath`：string，可选，默认自动命名；仅允许导出目录下的相对路径，不接受绝对路径或 `..`。<br>`category`：number，可选，小于 `0` 表示不限分类。<br>`sinceTimestampMs`：number，可选，`0` 表示不设时间下界。<br>`untilTimestampMs`：number，可选，`0` 表示不设上界，且上界不包含。<br>`timeField`：string，可选，默认 `updated_at_ms`，也可使用 `created_at_ms`。<br>`includeSceneMemory`：boolean，可选，是否包含场景记忆摘要。<br>`includeGlobalOverview`：boolean，可选，是否包含全局概览。 | 返回导出文件位置、各类记忆数量和文件大小，不直接返回完整记忆内容。生成导出文件。 |
| 分层记忆检索   | `memory_get_global_summary`  | 按需获取用户长期信息总体概览。适合当前上下文没有全局记忆，或子任务需要用户画像、偏好、程序索引和长期事项。 | `tier`：string/number，可选，默认 `edge`；`edge`/`0` 为精简摘要，`cloud_s`/`1` 为中等摘要，`cloud_l`/`2` 为较完整摘要。<br>`tenant_id`：string，可选，通常自动补齐。<br>`user_id`：string，可选，通常自动补齐。 | 返回与 `tier` 对应长度的全局摘要。只读。                     |
| 分层记忆检索   | `memory_scene_list_load`     | 获取当前用户的场景记忆索引。用于先判断有哪些相关场景，再调用 `memory_scene_load` 加载详情。 | `tenant_id`：string，可选，通常自动补齐。<br>`user_id`：string，可选，通常自动补齐。 | 返回场景路径、摘要及版本等公开索引信息。只读。               |
| 分层记忆检索   | `memory_scene_load`          | 按场景路径批量加载场景记忆详情。适合全局概览或场景索引不足以回答，需要具体项目、主题或经历详细摘要的场景。 | `paths`：string[]，必填，非空且最多 `5` 条；使用 `memory_scene_list_load` 返回的场景路径。<br>`tenant_id`：string，可选，通常自动补齐。<br>`user_id`：string，可选，通常自动补齐。 | 返回每条路径对应的场景内容和批量加载结果摘要。一次批量按一次工具调用计算。只读。 |
| 分层记忆检索   | `memory_record_search`       | 按语义查询具体事实、经验和程序记忆。适合查询精确事实；执行、继续、修复、调试、构建或发布任务前，可用于检索历史流程和注意事项。 | `query`：string，必填，建议使用 `2` 至 `8` 个关键词。<br>`top_k`：number，可选，默认 `5`。<br>`is_procedural`：boolean，可选，默认 `false`；设为 `true` 时只查询程序记忆。<br>`dedup_policy`：object，可选，默认使用系统策略。<br>`dedup_policy.enable_lineage_dedup`：boolean，可选，默认 `true`。<br>`dedup_policy.served_l1_decay`：number，可选，默认 `0.5`，范围 `0` 至 `1`。<br>`time_hint`：boolean，可选，默认按查询内容判断；涉及时间表达时可设为 `true`。<br>`tenant_id`：string，可选，通常自动补齐。<br>`user_id`：string，可选，通常自动补齐。 | 返回相关事实列表及可用的结构化元数据；过长内容可能被截短。只读。 |
| 分层记忆检索   | `memory_chat_history_search` | 检索历史对话原文和完整上下文片段。适合核对用户原话、事实来源、时间顺序或上下文细节。 | `query`：string，必填，建议非空且语义明确。<br>`top_k`：number，可选，默认 `5`。<br>`sessionIdFilter`：string，可选，默认不限会话。<br>`tenant_id`：string，可选，通常自动补齐。<br>`user_id`：string，可选，通常自动补齐。 | 返回相关历史对话片段及发生时间；过长内容可能被截短。只读。   |
| 记忆整理查询   | `dream_status`               | 查询记忆整理任务的阶段和进度。适合用户询问整理是否正在运行、运行到哪个阶段。 | `sessionId`：string，可选，默认当前工具会话。<br>`runId`：string，可选，默认最近一次任务。 | 返回指定或最近一次整理任务的状态和进度。只读。               |
| 记忆整理查询   | `dream_run_summary`          | 查看一次已完成记忆整理任务的结果摘要。适合用户询问本次整理形成了哪些结果或变化。 | `sessionId`：string，可选，默认当前工具会话。<br>`runId`：string，可选，默认最近一次已完成任务。 | 返回指定或最近一次已完成任务的结果概览。只读。               |
| 记忆整理查询   | `dream_recent_runs`          | 查看最近若干次记忆整理任务。适合用户询问近期进行了哪些记忆整理。 | `sessionId`：string，可选，默认当前工具会话。<br>`limit`：number，可选，默认 `5`，表示最多返回多少次近期任务。 | 返回最近若干次任务的标识、状态和简要结果。只读。             |

推荐检索顺序是：全局概览或场景索引 → 场景详情或事实 → 必要时查询原始对话。普通对话由自动记忆流程处理，不应在每轮对话中调用 `memory_store`。`tenant_id` 和 `user_id` 等身份参数通常由插件补齐，常规 Agent 调用无需手工传入。

## 4. 使用的 OpenClaw Hook

以下按实际启用的 Hook 名统计。`agent_end` 虽然承担两类职责，但只算一个 Hook 点。

| Hook                  | 触发时机                  | 插件在该点做什么                                             | 对 Agent 的效果                                    | 重要性                                  |
| --------------------- | ------------------------- | ------------------------------------------------------------ | -------------------------------------------------- | --------------------------------------- |
| `session_start`       | 新会话开始                | 准备当前用户的基础长期记忆上下文                             | 第一轮即可获得必要的长期背景                       | 完整适配必需，可由首次 Prompt Hook 替代 |
| `after_compaction`    | OpenClaw 完成上下文压缩后 | 重新补充必要的长期记忆上下文                                 | 避免压缩后丢失用户画像和重要场景                   | 使用上下文压缩时必需                    |
| `before_prompt_build` | 每次构造模型 Prompt 之前  | 检查记忆上下文是否需要刷新，并追加记忆使用指引及当前会话内已明确纠正的信息 | 模型在回答前看到最新、必要的记忆上下文             | 核心必需                                |
| `before_tool_call`    | 调用记忆工具之前          | 为记忆工具补充当前用户和会话上下文                           | Agent 无需手工维护身份参数，降低串用户和串会话风险 | 语义必需，可由 Tool Wrapper 替代        |
| `agent_end`           | 一轮 Agent 执行结束       | 收集本轮有效对话交给自动记忆流程，同时记录本轮模型和记忆召回用量 | 普通对话可以自动沉淀为长期记忆                     | 自动记忆核心必需                        |
| `session_end`         | 会话结束、关闭或重置      | 清理当前会话的已加载场景状态和会话级临时记忆上下文           | 防止状态长期累积或影响其他会话                     | 推荐，缺失时需有过期清理机制            |
| `llm_input`           | 最终输入即将发送给模型    | 只读检查长期记忆和主动检索结果是否进入最终模型输入           | 用于诊断记忆可见性，不改变模型输入                 | 可选                                    |

插件还使用 OpenClaw 的服务启动和停止生命周期管理运行状态，但这不是 `api.on(...)` 事件 Hook，因此未计入上述 7 个 Hook。

## 5. Hook 与记忆流程的关系

| 阶段         | 主要 Hook             | 对应能力                   |
| ------------ | --------------------- | -------------------------- |
| 会话建立     | `session_start`       | 准备基础长期记忆           |
| 模型回答前   | `before_prompt_build` | 刷新并注入必要记忆上下文   |
| 主动检索前   | `before_tool_call`    | 补充当前用户和会话信息     |
| 最终模型输入 | `llm_input`           | 诊断记忆是否真正可见       |
| 一轮回答完成 | `agent_end`           | 自动捕获有效对话并记录用量 |
| 上下文压缩后 | `after_compaction`    | 恢复被压缩的长期记忆上下文 |
| 会话结束     | `session_end`         | 清理会话级临时状态         |

## 6. `openclaw.json` 必需适配项

### 6.1 插件注册与权限

| 配置项                                                       | 建议值                            | 作用                                           | 对应能力                 |
| ------------------------------------------------------------ | --------------------------------- | ---------------------------------------------- | ------------------------ |
| `agents.defaults.compaction.memoryFlush.enabled`             | `false`                           | 关闭 OpenClaw 自带的同类刷新路径，避免重复处理 | 自动记忆、压缩后恢复     |
| `plugins.slots.memory`                                       | `"memory-celia"`                  | 指定当前启用的记忆插件                         | 所有记忆工具和 Hook      |
| `plugins.load.paths`                                         | 包含记忆插件目录                  | 让 OpenClaw 能发现并加载插件                   | 插件启动                 |
| `plugins.allow`                                              | 使用白名单时包含 `"memory-celia"` | 允许插件加载                                   | 插件启动                 |
| `plugins.entries.memory-celia.enabled`                       | `true`                            | 启用插件                                       | 所有记忆能力             |
| `plugins.entries.memory-celia.hooks.allowConversationAccess` | `true`                            | 允许插件访问本轮有效对话                       | `agent_end` 自动记忆     |
| `plugins.entries.memory-celia.config.serverBinaryPath`       | 由安装流程填写                    | 指定记忆运行组件                               | 工具执行、自动记忆和检索 |
| `plugins.entries.memory-celia.config.dbPath`                 | 由安装流程填写                    | 指定持久化数据位置                             | 记忆保存和重启恢复       |
| `plugins.entries.memory-celia.config.chat.model`             | CeliacLaw 指定的记忆模型          | 指定适合记忆抽取与整理的模型                   | 自动记忆和记忆整理       |

`plugins.allow` 仅在 OpenClaw 已启用插件白名单时必须配置。插件目录、运行组件路径、数据路径和模型值应由部署流程生成，不应在通用文档中写入真实环境值。

### 6.2 插件公开可选配置

| 配置项                 | 用途                             | 建议                                   |
| ---------------------- | -------------------------------- | -------------------------------------- |
| `userId`               | 无法从会话获得用户身份时的兜底值 | 多用户环境优先使用框架传入的用户标识   |
| `vectorDim`            | 指定 Embedding 向量维度          | 应与实际 Embedding 模型一致            |
| `embed.baseUrl`        | Embedding 服务地址               | 可直接配置，也可复用 OpenClaw 全局配置 |
| `embed.apiKey`         | Embedding 服务凭据               | 使用 Secret 或环境变量引用             |
| `embed.model`          | Embedding 模型                   | 与向量维度保持一致                     |
| `embed.headers`        | Embedding 附加请求头             | 仅配置业务需要的附加头                 |
| `chat.baseUrl`         | 记忆抽取模型服务地址             | 可直接配置，也可复用全局 Provider      |
| `chat.apiKey`          | Chat 服务凭据                    | 使用 Secret 或环境变量引用             |
| `chat.model`           | 记忆抽取与整理模型               | CeliacLaw 建议显式指定适配模型         |
| `chat.headers`         | Chat 附加请求头                  | 不重复填写认证信息                     |
| `rerank.baseUrl`       | Rerank 服务地址                  | 可选；不用 Rerank 时省略               |
| `rerank.apiKey`        | Rerank 服务凭据                  | 与另外两个 Rerank 字段成套配置         |
| `rerank.model`         | Rerank 模型                      | 与另外两个 Rerank 字段成套配置         |
| `proceduralDir`        | 程序记忆或技能文件目录           | 仅在需要指定独立目录时配置             |
| `proceduralLearnDebug` | 程序记忆学习诊断开关             | 默认关闭，仅排障时开启                 |

公开配置采用严格字段校验，不应增加未列出的字段。`embed.headers` 和 `chat.headers` 是整体覆盖语义，不与复用的全局 Header 逐项合并。

## 7. 可复用的 OpenClaw 全局模型配置

如果 OpenClaw 已配置模型服务，记忆插件可以复用，减少重复配置。

### 7.1 Embedding 配置来源

| OpenClaw 配置项                                     | 记忆插件用途                    |
| --------------------------------------------------- | ------------------------------- |
| `agents.defaults.memorySearch.enabled`              | 控制是否复用全局 Embedding 配置 |
| `agents.defaults.memorySearch.model`                | Embedding 模型                  |
| `agents.defaults.memorySearch.outputDimensionality` | 向量维度                        |
| `agents.defaults.memorySearch.remote.baseUrl`       | Embedding 服务地址              |
| `agents.defaults.memorySearch.remote.apiKey`        | Embedding 服务凭据              |
| `agents.defaults.memorySearch.remote.headers`       | Embedding 附加请求头            |

### 7.2 Chat 配置来源

| OpenClaw 配置项                       | 记忆插件用途                |
| ------------------------------------- | --------------------------- |
| `agents.defaults.model.primary`       | 确定默认模型及对应 Provider |
| `models.providers.<provider>.baseUrl` | Chat 服务地址               |
| `models.providers.<provider>.apiKey`  | Chat 服务凭据               |
| `models.providers.<provider>.headers` | Chat 附加请求头             |
| `models.providers.<provider>.models`  | 可用模型清单                |

配置优先级是：插件专属 `embed` / `chat` 配置优先，其次复用 OpenClaw 全局配置，最后才使用部署环境提供的兜底值。

## 8. 脱敏后的最小配置示例

以下示例只展示配置结构，尖括号内容必须由安装或部署流程替换：

```json
{
  "agents": {
    "defaults": {
      "compaction": {
        "memoryFlush": {
          "enabled": false
        }
      }
    }
  },
  "plugins": {
    "allow": [
      "memory-celia"
    ],
    "slots": {
      "memory": "memory-celia"
    },
    "load": {
      "paths": [
        "<memory-plugin-directory>"
      ]
    },
    "entries": {
      "memory-celia": {
        "enabled": true,
        "hooks": {
          "allowConversationAccess": true
        },
        "config": {
          "serverBinaryPath": "<memory-runtime>",
          "dbPath": "<memory-data-path>",
          "chat": {
            "model": "<memory-chat-model>"
          }
        }
      }
    }
  }
}
```

## 9. 配置与功能对应关系

| 目标功能               | 关键配置                                                     |
| ---------------------- | ------------------------------------------------------------ |
| 插件被发现并启用       | `plugins.load.paths`、`plugins.allow`、`plugins.entries.memory-celia.enabled` |
| Celia 成为默认记忆插件 | `plugins.slots.memory`                                       |
| 自动捕获普通对话       | `hooks.allowConversationAccess`                              |
| 模型调用和记忆整理     | `chat.*` 或全局 `models.providers.*`                         |
| 语义检索               | `embed.*` 或全局 `agents.defaults.memorySearch.*`            |
| 可选结果重排           | `rerank.*`                                                   |
| 记忆持久化与重启恢复   | `dbPath`                                                     |
| 上下文压缩后的记忆恢复 | `compaction.memoryFlush.enabled=false`，由记忆插件接管对应流程 |

## 10. 适配判断要点

其他 Agent 框架如果能够提供工具注册、模型调用前系统上下文注入、回合结束消息获取、稳定的用户与会话标识，以及插件服务和持久化能力，就可以实现完整适配。

如果只有 Tool Calling 而缺少 Prompt 和回合结束 Hook，则只能实现主动保存、删除和查询，不能完整保留自动记忆、固定加载、压缩后恢复和会话内纠正能力。