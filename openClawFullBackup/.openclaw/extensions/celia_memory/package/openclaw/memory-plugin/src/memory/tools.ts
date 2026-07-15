/**
 * 工具注册：memory_store / memory_forget /
 * memory_scene_load / memory_record_search / memory_chat_history_search /
 * memory_scene_list_load / memory_flush / memory_list / memory_dump
 * + CLI 命令。
 */

import type { OpenClawPluginApi } from "openclaw/plugin-sdk/plugin-entry";
import type { CeliaMcpClient } from "../core/client.js";
import { parseSearchResponse } from "../core/client.js";
import type {
  DedupPolicyConfig,
  CeliaMemoryConfig,
} from "../core/config.js";
import { formatScopedId, genTraceId, logPrefix } from "../core/dfx.js";
import { deriveUserId } from "../core/helpers.js";
import { ensureToolSession } from "../core/session.js";
import {
  buildStoreUrgentIngestKey,
  markStoreUrgentIngest,
} from "./store-signal.js";
import { markFixedLoadDirty } from "./hooks.js";
import { truncateUtf8Safe, utf8ByteLength } from "./markerProtocol.js";
import {
  appendSessionPromptBuffer,
  buildSessionPromptBufferKey,
} from "./session-prompt-buffer.js";
import { readXiaoyiMemoryState } from "./runtime-state.js";

/**
 * 渐进加载工具响应的单条 result 截断上限（字节，UTF-8）。
 *
 * search_l2 原子事实平均 200-500 字节，长尾 1-3 KB；800 B 卡住外溢。
 * search_l3 原始对话片段长度更分散，600 B 兼顾常见单轮对话 + 简短截断。
 *
 * 截断后 content 末尾追加 `…` 提示，并在响应顶层加 `_trim` 字段告诉
 * LLM 有 N 条被截断、可显式 `query` 改细化以拿全文。
 */
const SEARCH_L2_CONTENT_MAX_BYTES = 800;
const SEARCH_L3_CONTENT_MAX_BYTES = 600;
const MEMORY_CLOSED_RECALL_ALT_TOOL = "memory_chat_history_search";
const MEMORY_CLOSED_REASON = "memory_disabled";
const MEMORY_CLOSED_RECALL_TEXT =
  "由于梦境记忆开关关闭，本工具不产生召回结果，是正常现象，"
  + "不用以错误的形式向用户报告；如果有需要，请使用 "
  + MEMORY_CLOSED_RECALL_ALT_TOOL
  + " 工具检索原始对话记录。";

const OPTIONAL_SCHEMA: unique symbol = Symbol("optionalJsonSchema");

type JsonSchema = Record<string, unknown> & {
  [OPTIONAL_SCHEMA]?: boolean;
};

type JsonSchemaOptions = Record<string, unknown>;
type JsonSchemaProperties = Record<string, JsonSchema>;

const Type = {
  String(options: JsonSchemaOptions = {}): JsonSchema {
    return { type: "string", ...options };
  },

  Number(options: JsonSchemaOptions = {}): JsonSchema {
    return { type: "number", ...options };
  },

  Boolean(options: JsonSchemaOptions = {}): JsonSchema {
    return { type: "boolean", ...options };
  },

  Literal(value: string | number | boolean | null): JsonSchema {
    const type = value === null ? "null" : typeof value;
    return { const: value, type };
  },

  Array(items: JsonSchema, options: JsonSchemaOptions = {}): JsonSchema {
    return { type: "array", items, ...options };
  },

  Union(items: JsonSchema[], options: JsonSchemaOptions = {}): JsonSchema {
    return { anyOf: items, ...options };
  },

  Optional(schema: JsonSchema): JsonSchema {
    const optionalSchema = { ...schema };
    Object.defineProperty(optionalSchema, OPTIONAL_SCHEMA, {
      value: true,
      enumerable: false,
    });
    return optionalSchema;
  },

  Object(
    properties: JsonSchemaProperties,
    options: JsonSchemaOptions = {},
  ): JsonSchema {
    const required = Object.entries(properties)
      .filter(([, schema]) => !schema[OPTIONAL_SCHEMA])
      .map(([name]) => name);
    const schema: JsonSchema = {
      type: "object",
      properties,
      ...options,
    };
    if (required.length > 0) {
      schema.required = required;
    }
    return schema;
  },
};

const TIME_HINT_PATTERNS = [
  /\b\d{4}-\d{1,2}-\d{1,2}\b/,
  /\d{4}年\d{1,2}月\d{1,2}[日号]/,
  /\b\d{1,2}:\d{2}\b/,
  /今天|昨天|前天|明天|后天|今晚|昨晚|今早|明早/,
  /上周|这周|本周|下周|上个月|这个月|本月|下个月/,
  /周[一二三四五六日天末]/,
  /星期[一二三四五六日天]|礼拜[一二三四五六日天]/,
  /早上|上午|中午|下午|晚上|凌晨/,
  /\b(today|yesterday|tomorrow|tonight)\b/i,
  /\b(last|next)\s+(week|month|monday|tuesday|wednesday)\b/i,
  /\b(thursday|friday|saturday|sunday)\b/i,
];

interface RawSearchHit {
  content?: string;
  [k: string]: unknown;
}

interface TrimReport {
  trimmed_count: number;
  per_item_cap_bytes: number;
}

/**
 * 把 search_l2 / search_l3 响应里 results[].content 字段按 maxBytes 截断。
 *
 * 返回 `{ value, report }`：value 是修改后的 payload（深拷贝顶层 + results
 * 数组，content 按需截断），report 给调用方拼 _trim 元信息。raw 不是
 * 对象 / results 不是数组时直接透传，无副作用。
 */
function trimSearchContent(raw: unknown, maxBytes: number): {
  value: unknown;
  report: TrimReport;
} {
  const report: TrimReport = {
    trimmed_count: 0,
    per_item_cap_bytes: maxBytes,
  };
  if (!raw || typeof raw !== "object") {
    return { value: raw, report };
  }
  const obj = raw as Record<string, unknown>;
  const list = obj.results;
  if (!Array.isArray(list)) {
    return { value: raw, report };
  }
  const trimmed = list.map((it) => {
    if (!it || typeof it !== "object") return it;
    const hit = it as RawSearchHit;
    if (typeof hit.content !== "string") return it;
    if (utf8ByteLength(hit.content) <= maxBytes) return it;
    report.trimmed_count++;
    return { ...hit, content: truncateUtf8Safe(hit.content, maxBytes) };
  });
  return { value: { ...obj, results: trimmed, _trim: report }, report };
}

/**
 * 已加载的 L1 路径追踪（per-conversation 级）。
 *
 * key = `${tenantId}:${userId}:${conversationId}`
 * value = 该 conversation 内所有 memory_scene_load 成功的 path 集合。
 * 生命周期：随 plugin 进程；重启时自然清零。
 *
 * Cache P1.1：原 key 仅 tenant:user，跨 conversation 污染——A 会话 served
 * 的 L1 path 在 B 会话被 `served_l1_decay` 错误降权。升级为三元组 key 后
 * 各 conversation 独立 Set，互不影响。
 *
 * conversationId 缺失时（极少数 hook race / 老调用方）兜底用 userId，
 * 与历史 memory_store 路径的 fallback 语义对齐
 * （tools.ts:253 `const conversationId = _conversationId ?? userId`）。
 */
const servedL1Paths = new Map<string, Set<string>>();

/**
 * Cache P1.1 follow-up（deep review FIX-5）：servedL1Paths 上限。
 *
 * 长跑 OpenClaw plugin 进程下，每个 (tenant, user, conversationId) 三元组
 * 会创建一条 entry；conversation 是无界增长的（每次会话一条）。无 LRU
 * 上限会导致月级累积——上限 = 1024 在 entries 数到达时按"最早访问者"
 * 淘汰（Map 的迭代序即插入序，删第一项即 LRU）。访问时
 * delete+set 重新挂到尾部更新 recency。
 */
const SERVED_L1_PATHS_MAX_ENTRIES = 1024;

/** 内部 helper：构造 servedL1 key（与读写双方共享） */
function buildServedL1Key(
  tenantId: string,
  userId: string,
  conversationId: string,
): string {
  return `${tenantId || "default"}:${userId || "anon"}:`
    + `${conversationId || userId || "default"}`;
}

/** 把 key 提到 Map 末尾（LRU recency bump）；不存在则保持。 */
function touchServedL1Recency(key: string): void {
  const set = servedL1Paths.get(key);
  if (set !== undefined) {
    servedL1Paths.delete(key);
    servedL1Paths.set(key, set);
  }
}

/** 记录一个成功加载的 L1 path（per-conversation）。 */
function trackServedL1(
  tenantId: string,
  userId: string,
  conversationId: string,
  path: string,
): void {
  const key = buildServedL1Key(tenantId, userId, conversationId);
  if (!servedL1Paths.has(key)) {
    /* 到达上限时淘汰最早 entry（Map 迭代序 = 插入序）。 */
    if (servedL1Paths.size >= SERVED_L1_PATHS_MAX_ENTRIES) {
      const firstKey = servedL1Paths.keys().next().value;
      if (firstKey !== undefined) {
        servedL1Paths.delete(firstKey);
      }
    }
    servedL1Paths.set(key, new Set());
  } else {
    touchServedL1Recency(key);
  }
  servedL1Paths.get(key)!.add(path);
}

/**
 * 显式清理某 conversation 的 servedL1 entry（session_end 钩子调用）。
 *
 * Cache P1.1 follow-up：长跑 plugin 进程的内存泄漏防御——session 结束
 * 时立即释放对应 (tenant, user, conversationId) 的 Set，避免依赖 LRU
 * 淘汰兜底。
 */
export function clearServedL1ForSession(
  tenantId: string,
  userId: string,
  conversationId: string,
): void {
  servedL1Paths.delete(buildServedL1Key(tenantId, userId, conversationId));
}

/** 获取已加载的 L1 paths（per-conversation；空集返回 undefined）。 */
function getServedL1Paths(
  tenantId: string,
  userId: string,
  conversationId: string,
): string[] | undefined {
  const set = servedL1Paths.get(
    buildServedL1Key(tenantId, userId, conversationId),
  );
  return set && set.size > 0 ? Array.from(set) : undefined;
}

/** 测试钩子：清空 servedL1Paths（仅供 vitest fixture）。 */
export function _resetServedL1PathsForTesting(): void {
  servedL1Paths.clear();
}

/** 测试钩子：track 接口直暴（仅供 vitest，不在生产路径调用）。 */
export function _trackServedL1ForTesting(
  tenantId: string,
  userId: string,
  conversationId: string,
  path: string,
): void {
  trackServedL1(tenantId, userId, conversationId, path);
}

/** 测试钩子：query 接口直暴（仅供 vitest，不在生产路径调用）。 */
export function _getServedL1PathsForTesting(
  tenantId: string,
  userId: string,
  conversationId: string,
): string[] | undefined {
  return getServedL1Paths(tenantId, userId, conversationId);
}

/**
 * 合并 tool call 传入的 `dedup_policy` 与 `cfg.dedupPolicy` 基线。
 *
 * 优先级（字段级 override）：`userArg[field] > cfgPolicy[field] > C 默认`
 *
 * 返回值：
 *   - 两者都无值 → `undefined`（callTool 时不带 dedup_policy 字段，C 端走默认）
 *   - 两者任一有值 → 合并后的 snake_case 对象（与 C 端 ParseDedup 对齐）
 */
export function buildDedupPolicyArg(
  userArg: Record<string, unknown> | undefined,
  cfgPolicy: DedupPolicyConfig | undefined,
): Record<string, unknown> | undefined {
  if (!userArg && !cfgPolicy) return undefined;

  const ua = userArg ?? {};
  const cp = cfgPolicy ?? {};

  const out: Record<string, unknown> = {};

  /* enable_lineage_dedup */
  if (typeof ua.enable_lineage_dedup === "boolean") {
    out.enable_lineage_dedup = ua.enable_lineage_dedup;
  } else if (typeof cp.enableLineageDedup === "boolean") {
    out.enable_lineage_dedup = cp.enableLineageDedup;
  }

  /* served_l1_decay */
  if (
    typeof ua.served_l1_decay === "number"
    && Number.isFinite(ua.served_l1_decay)
    && ua.served_l1_decay >= 0
    && ua.served_l1_decay <= 1
  ) {
    out.served_l1_decay = ua.served_l1_decay;
  } else if (
    typeof cp.servedL1Decay === "number"
    && Number.isFinite(cp.servedL1Decay)
    && cp.servedL1Decay >= 0
    && cp.servedL1Decay <= 1
  ) {
    out.served_l1_decay = cp.servedL1Decay;
  }

  return Object.keys(out).length > 0 ? out : undefined;
}

/** 判断 search_l2 是否应启用 C 端时间意图抽取。 */
export function shouldEnableTimeHint(
  query: string,
  explicit?: boolean,
): boolean {
  if (explicit === true) return true;
  if (explicit === false) return false;
  return TIME_HINT_PATTERNS.some((pattern) => pattern.test(query));
}

function buildMemoryClosedPayload(
  memoryState: number,
  base: Record<string, unknown>,
): Record<string, unknown> {
  return {
    ...base,
    message: MEMORY_CLOSED_RECALL_TEXT,
    _memory_state: {
      memoryState,
      skipped: true,
      reason: MEMORY_CLOSED_REASON,
      alternative_tool: MEMORY_CLOSED_RECALL_ALT_TOOL,
    },
  };
}

function buildMemoryClosedDetails(
  memoryState: number,
  base: Record<string, unknown>,
): Record<string, unknown> {
  return {
    ...base,
    memory_state: memoryState,
    skipped: true,
    reason: MEMORY_CLOSED_REASON,
    alternative_tool: MEMORY_CLOSED_RECALL_ALT_TOOL,
    action: `call_${MEMORY_CLOSED_RECALL_ALT_TOOL}`,
  };
}

// ============================================================================
// Tool registration
// ============================================================================

export function registerTools(
  api: OpenClawPluginApi,
  client: CeliaMcpClient,
  cfg: CeliaMemoryConfig,
): void {
  api.logger.info("[celia-debug] registerTools() called");

  // ------------------------------------------------------------------
  // Inject per-session userId into memory tool params before execution
  // ------------------------------------------------------------------

  const MEMORY_TOOLS = new Set([
    "memory_open",
    "memory_store",
    "memory_forget",
    "memory_scene_load",
    "memory_record_search",
    "memory_chat_history_search",
    "memory_scene_list_load",
    "memory_get_global_summary",
    "memory_flush",
    "memory_list",
    "memory_dump",
  ]);

  /* 标记是否已 warn 过 sessionId 缺失，避免每个 tool call 刷屏。 */
  let sessionIdMissingWarned = false;
  api.on("before_tool_call", (event, ctx) => {
    if (!MEMORY_TOOLS.has(event.toolName)) return;
    const userId = deriveUserId(ctx, cfg.userId);
    /* Cache P1.1 follow-up（deep review M11）：sessionId 缺失时 fallback
     * 链是 conversationId ?? userId（per-conversation dedup 退化为 per-user）。
     * 早期 OpenClaw SDK / 极端 hook race 可能不传 sessionId——首次发生时
     * warn 一次让 SRE 知晓并联系 OpenClaw 升级。 */
    if (!ctx.sessionId && !sessionIdMissingWarned) {
      api.logger.warn?.(
        "[celia] before_tool_call: ctx.sessionId is undefined; "
          + "servedL1 dedup will fall back to per-user. "
          + "Consider upgrading OpenClaw runtime to ensure sessionId injection.",
      );
      sessionIdMissingWarned = true;
    }
    return { params: { ...event.params, _userId: userId, _conversationId: ctx.sessionId } };
  });

  /**
   * 解析 tenant_id / user_id，优先级：
   *   params.tenant_id → cfg.tenantId → "default"
   *   params.user_id → params._userId（由 before_tool_call 注入）→ cfg.userId
   *
   * 目的：让模型可以在 tool call 里省略 tenant_id/user_id，插件自动
   * 注入默认值。对应 `celia_openclaw_检索问题汇总_2026-04-24.md` 的
   * #2/#14 自动注入方案。
   */
  function resolveTenantUser(
    params: unknown,
  ): { tenant_id: string; user_id: string } {
    const p = (params ?? {}) as Record<string, unknown>;
    const explicitUser =
      typeof p.user_id === "string" && p.user_id ? p.user_id : undefined;
    const injectedUser =
      typeof p._userId === "string" && p._userId ? p._userId : undefined;
    const user_id = explicitUser ?? injectedUser ?? cfg.userId;

    const explicitTenant =
      typeof p.tenant_id === "string" && p.tenant_id ? p.tenant_id : undefined;
    const tenant_id = explicitTenant ?? cfg.tenantId ?? "default";

    return { tenant_id, user_id };
  }

  // ------------------------------------------------------------------
  // memory_store —— agent 显式记忆、反馈、纠错触发。当前 session
  // 立即写 SessionPromptBuffer，同时把当轮 ingestMode 升级为
  // deferred-urgent，立即唤醒 C 端 worker。
  // ------------------------------------------------------------------

  api.registerTool(
    {
      name: "memory_store",
      label: "Memory Store",
      description:
        "Call only when: (1) the user explicitly asks you to remember durable information " +
        "(e.g. '记一下', '帮我记住', 'remember this', 'save this'); or " +
        "(2) the user gives explicit reusable feedback about the agent's completed task, " +
        "answer style, behavior, or future responses; or " +
        "(3) the user explicitly corrects something you previously remembered " +
        "or assumed about them, their long-term preferences, or your future behavior " +
        "(e.g. '不对，我喜欢咖啡不是茶', '我其实是做后端的', " +
        "'下次别用这种格式', 'Actually I prefer Python, not Java'). " +
        "NEVER call for normal conversation, standalone user facts or preferences, " +
        "generic praise or thanks, task content, plans, or one-off/transient context.",
      parameters: Type.Object({
        text: Type.String(),
      }),
      async execute(_toolCallId, params) {
        const { text, _userId, _conversationId } = params as {
          text: string;
          _userId?: string;
          _conversationId?: string;
        };
        const userId = _userId ?? cfg.userId;
        const openclawSessionId = _conversationId ?? "";
        const tenantId = cfg.tenantId ?? "default";

        if (openclawSessionId) {
          appendSessionPromptBuffer(
            buildSessionPromptBufferKey(
              tenantId,
              userId,
              openclawSessionId,
            ),
            text,
          );
        }

        markStoreUrgentIngest(
          buildStoreUrgentIngestKey(
            tenantId,
            userId,
            openclawSessionId || userId,
          ),
        );

        api.logger.info(
          `[memory_store] session-prompt-buffer text="${text.slice(0, 120)}" ` +
            `session=${openclawSessionId || "(none)"} userId=${userId}`,
        );

        return {
          content: [{
            type: "text",
            text: `Noted: "${text.slice(0, 100)}${text.length > 100 ? "..." : ""}"`,
          }],
          details: {
            action: openclawSessionId
              ? "session_prompt_buffer_write"
              : "urgent_ingest_only",
            ingestMode: "deferred-urgent",
          },
        };
      },
    },
    { name: "memory_store" },
  );

  // ------------------------------------------------------------------
  // memory_forget
  // ------------------------------------------------------------------

  api.registerTool(
    {
      name: "memory_forget",
      label: "Memory Forget",
      description: "Delete specific memories by ID or search query.",
      parameters: Type.Object({
        query: Type.Optional(
          Type.String({ description: "Search to find memory to delete" }),
        ),
        memoryId: Type.Optional(
          Type.Number({ description: "Specific memory ID to delete" }),
        ),
      }),
      async execute(_toolCallId, params) {
        if (!client.isConnected() && !(await client.waitReady())) {
          return {
            content: [{ type: "text", text: "Memory server is not available. Please try again shortly." }],
            isError: true,
          };
        }

        const { query, memoryId, _userId } = params as {
          query?: string;
          memoryId?: number;
          _userId?: string;
        };
        const userId = _userId ?? cfg.userId;
        const sessionId = await ensureToolSession(client, userId);

        if (memoryId != null) {
          await client.callTool("memory_delete", { memoryId, sessionId });
          return {
            content: [
              { type: "text", text: `Memory ${memoryId} forgotten.` },
            ],
            details: { action: "deleted", id: memoryId },
          };
        }

        if (query) {
          const tenantId = cfg.tenantId ?? "default";
          const raw = await client.callTool("memory_search_l2", {
            tenant_id: tenantId,
            user_id: userId,
            query,
            top_k: 5,
            sessionId,
          });
          const data = parseSearchResponse(raw);

          if (data.results.length === 0) {
            return {
              content: [
                { type: "text", text: "No matching memories found." },
              ],
              details: { found: 0 },
            };
          }

          if (data.results.length === 1 && data.results[0].score > 0.9) {
            const target = data.results[0];
            await client.callTool("memory_delete", {
              memoryId: target.id,
              sessionId,
            });
            const desc = target.content ?? "(memory)";
            return {
              content: [
                { type: "text", text: `Forgotten: "${desc}"` },
              ],
              details: { action: "deleted", id: target.id },
            };
          }

          const list = data.results
            .map(
              (r) =>
                `- [id:${r.id}] ${(r.content ?? "").slice(0, 60)}...`,
            )
            .join("\n");

          return {
            content: [
              {
                type: "text",
                text: `Found ${data.results.length} candidates. Specify memoryId:\n${list}`,
              },
            ],
            details: {
              action: "candidates",
              candidates: data.results.map((r) => ({
                id: r.id,
                score: r.score,
                content: r.content,
              })),
            },
          };
        }

        return {
          content: [
            { type: "text", text: "Provide query or memoryId." },
          ],
          details: { error: "missing_param" },
        };
      },
    },
    { name: "memory_forget" },
  );

  // ------------------------------------------------------------------
  // memory_scene_load — 按 paths 批量拉取分类/场景摘要原文
  // ------------------------------------------------------------------

  api.registerTool(
    {
      name: "memory_scene_load",
      label: "Memory Scene Load",
      description:
        "加载场景记忆摘要原文（按 memory_scene_list_load 返回的 path）。" +
        "使用 paths[] 批量加载（≤5），一次批量算 1 次调用预算。" +
        "用于在已加载全局概览或场景记忆不足时拉取具体场景全文。" +
        "tenant_id / user_id 由插件自动注入，通常只需提供 paths。" +
        "提示：本轮对话总调用 memory_scene_load + memory_record_search + memory_chat_history_search 不超过 3 次。",
      parameters: Type.Object({
        paths: Type.Array(Type.String(), {
          description: "场景记忆条目路径数组，最多 5 条。",
          maxItems: 5,
        }),
        tenant_id: Type.Optional(
          Type.String({ description: "租户 ID（可选；插件自动注入 cfg.tenantId 或 \"default\"）" }),
        ),
        user_id: Type.Optional(
          Type.String({ description: "用户 ID（可选；插件自动注入 ctx._userId 或 cfg.userId）" }),
        ),
      }),
      async execute(_toolCallId, params) {
        const memoryState = await readXiaoyiMemoryState();
        if (memoryState === 0) {
          const p = params as { paths?: string[] };
          const pathsArr = Array.isArray(p.paths) ? p.paths : [];
          const traceId = genTraceId();
          const pfx     = logPrefix("memory_scene_load", traceId);
          const text = JSON.stringify(buildMemoryClosedPayload(memoryState, {
            results: [],
            totalBytes: 0,
          }));
          api.logger.info(
            `${pfx} event=skip reason=memory_disabled ` +
            `paths_count=${pathsArr.length} ` +
            "dfx_metric=load_l1_batch_skipped memory_state=0",
          );
          return {
            content: [{ type: "text", text }],
            details: buildMemoryClosedDetails(memoryState, {
              tool: "memory_scene_load",
              mode: "batch",
              count: 0,
              totalBytes: 0,
            }),
          };
        }

        if (!client.isConnected() && !(await client.waitReady())) {
          return {
            content: [{ type: "text", text: "Memory server is not available. Please try again shortly." }],
            isError: true,
          };
        }

        const p = params as {
          paths?: string[];
          _conversationId?: string;
        };
        const { tenant_id, user_id } = resolveTenantUser(params);
        /* Cache P1.1: per-conversation servedL1 dedup. _conversationId 由
         * hooks.ts before_tool_call 注入；缺省 fallback userId（与历史
         * memory_store 路径语义一致）。 */
        const conversationId = p._conversationId ?? user_id;
        const traceId = genTraceId();
        const pfx     = logPrefix("memory_scene_load", traceId);

        const pathsArr =
          Array.isArray(p.paths) && p.paths.length > 0 ? p.paths : null;

        if (pathsArr === null) {
          return {
            content: [{
              type: "text",
              text: "Provide 'paths' as a non-empty array of strings.",
            }],
            isError: true,
            details: { error: "missing_paths" },
          };
        }

        const t0 = Date.now();

        api.logger.info(
          `${pfx} event=start mode=batch n=${pathsArr.length} ` +
            `user=${formatScopedId("u", user_id)}`,
        );
        const batch = await client.loadL1Batch(pathsArr, {
          traceId,
          tenantId: tenant_id,
          userId: user_id,
        });
        const durMs = Date.now() - t0;
        const text = JSON.stringify(batch);
        /* DFX:memtype_paths 用于 P3 数据驱动决策 (cc-execution-plan §5.2);
         * 占比连续 1-2 周 < 5% 才考虑退役 L1 memtype 聚合。 */
        const memtypePaths = pathsArr.filter(
          (pp) => typeof pp === "string" && pp.startsWith("L1_memtype_"),
        ).length;
        for (const pp of pathsArr) {
          if (typeof pp === "string") {
            trackServedL1(tenant_id, user_id, conversationId, pp);
          }
        }
        api.logger.info(
          `${pfx} event=done mode=batch dur_ms=${durMs} ` +
            `n=${batch.results.length} total_bytes=${batch.totalBytes} ` +
            `dfx_metric=load_l1_batch paths_count=${pathsArr.length} ` +
            `memtype_paths=${memtypePaths}`,
        );
        return {
          content: [{ type: "text", text }],
          details: {
            mode: "batch",
            count: batch.results.length,
            totalBytes: batch.totalBytes,
            memtype_paths: memtypePaths,
          },
        };
      },
    },
    { name: "memory_scene_load" },
  );

  // ------------------------------------------------------------------
  // memory_record_search — 原子事实向量检索
  // ------------------------------------------------------------------

  api.registerTool(
    {
      name: "memory_record_search",
      label: "Memory Record Search",
      description:
        "按向量语义检索原子事实。用于获取与 query 相关的事实细节。" +
        "当用户要求执行/继续/修复/调试/构建/发布任务时，先用 " +
        "is_procedural=true + 任务关键词检索程序记忆，召回已学到的流程、" +
        "工具偏好、注意事项和历史踩坑，再开始执行。" +
        "tenant_id / user_id 由插件自动注入，通常只需提供 query 和 top_k。" +
        "若命中事实带有分类结构化字段，会在 extractMeta 中返回。" +
        "默认 top_k=5；返回单条 content 超过 800 B 会被截短并末尾加 `…`，" +
        "整体响应附 `_trim` 报告（trimmed_count / per_item_cap_bytes），" +
        "如需全文请用 memory_scene_load 或更精细的 query。" +
        "提示：本轮对话总调用 memory_scene_load + memory_record_search + memory_chat_history_search 不超过 3 次。",
      parameters: Type.Object({
        query: Type.String({ description: "检索查询（建议 2-8 个关键词）" }),
        top_k: Type.Optional(
          Type.Number({ description: "返回条数，默认 5（之前 10）" }),
        ),
        is_procedural: Type.Optional(
          Type.Boolean({
            description:
              "仅查程序记忆。执行/继续/修复/调试/构建/发布任务前设 true，" +
              "用任务/工具关键词召回流程、偏好和历史踩坑。",
          }),
        ),
        dedup_policy: Type.Optional(
          Type.Object(
            {
              enable_lineage_dedup: Type.Optional(
                Type.Boolean({ description: "启用血缘去重（默认 true）" }),
              ),
              served_l1_decay: Type.Optional(
                Type.Number({
                  description: "已送达摘要来源事实降权系数（0-1，默认 0.5）",
                }),
              ),
            },
            { description: "血缘去重策略" },
          ),
        ),
        tenant_id: Type.Optional(
          Type.String({ description: "租户 ID（可选；插件自动注入 cfg.tenantId 或 \"default\"）" }),
        ),
        user_id: Type.Optional(
          Type.String({ description: "用户 ID（可选；插件自动注入 ctx._userId 或 cfg.userId）" }),
        ),
        time_hint: Type.Optional(
          Type.Boolean({
            description:
              "查询到时间范围/时间点时设 true；" +
              "自动识别节假日/季度/上次等时间表达可主动设 true；",
          }),
        ),
      }),
      async execute(_toolCallId, params) {
        const memoryState = await readXiaoyiMemoryState();
        if (memoryState === 0) {
          const { query, top_k } = params as {
            query?: string;
            top_k?: number;
          };
          const queryText = typeof query === "string" ? query : "";
          const traceId = genTraceId();
          const pfx     = logPrefix("memory_record_search", traceId);
          const text = JSON.stringify(buildMemoryClosedPayload(memoryState, {
            results: [],
          }));
          api.logger.info(
            `${pfx} event=skip reason=memory_disabled ` +
            `query_len=${queryText.length} top_k=${top_k ?? "default"} ` +
            "dfx_metric=search_l2_skipped memory_state=0",
          );
          return {
            content: [{ type: "text", text }],
            details: buildMemoryClosedDetails(memoryState, {
              tool: "memory_record_search",
              query: queryText,
              top_k: top_k ?? 5,
            }),
          };
        }

        const legacy = params as Record<string, unknown>;
        if ("category" in legacy || "memory_type" in legacy) {
          return {
            content: [{
              type: "text",
              text:
                "category/memory_type filters were removed; use " +
                "is_procedural=true for procedural recall.",
            }],
            isError: true,
            details: { error: "deprecated_filter" },
          };
        }

        if (!client.isConnected() && !(await client.waitReady())) {
          return {
            content: [{ type: "text", text: "Memory server is not available. Please try again shortly." }],
            isError: true,
          };
        }

        const { query, top_k, is_procedural, dedup_policy, time_hint,
          _conversationId } = params as {
            query: string;
            top_k?: number;
            is_procedural?: boolean;
            dedup_policy?: Record<string, unknown>;
            time_hint?: boolean;
            _conversationId?: string;
          };
        const { tenant_id, user_id } = resolveTenantUser(params);
        const timeHint = shouldEnableTimeHint(query, time_hint);
        /* Cache P1.1: per-conversation servedL1 dedup（同 memory_scene_load）。 */
        const conversationId = _conversationId ?? user_id;
        const sessionId = await ensureToolSession(client, user_id);
        const traceId = genTraceId();
        const pfx     = logPrefix("memory_record_search", traceId);

        api.logger.info(
          `${pfx} event=start user=${formatScopedId("u", user_id)} ` +
          `query_len=${query.length} top_k=${top_k ?? "default"} ` +
          `procedural=${is_procedural === true} time_hint=${timeHint}`,
        );
        const t0 = Date.now();

        const args: Record<string, unknown> = {
          tenant_id, user_id, query, sessionId,
        };
        /* P2.3：默认 top_k 收紧到 5（之前 10），减少长上下文占用；
         * 用户显式传值仍优先。 */
        args.top_k = top_k != null ? top_k : 5;
        if (is_procedural === true) args.is_procedural = true;
        if (timeHint) args.time_hint = true;
        /* 合并 userArg 与 cfg.dedupPolicy（字段级 override）：
         * 任一有值都走合并路径；两者都空则不带字段，C 端走默认。 */
        const mergedDedup = buildDedupPolicyArg(dedup_policy, cfg.dedupPolicy);
        if (mergedDedup != null) args.dedup_policy = mergedDedup;

        /* 注入已送达 L1 路径（per-conversation；Cache P1.1） */
        const served = getServedL1Paths(tenant_id, user_id, conversationId);
        if (served) args.served_l1_paths = served;

        const raw   = await client.callTool(
          "memory_search_l2", args, undefined, { traceId });
        const trimmed = trimSearchContent(raw, SEARCH_L2_CONTENT_MAX_BYTES);
        const durMs   = Date.now() - t0;
        const text    = typeof trimmed.value === "string"
          ? trimmed.value
          : JSON.stringify(trimmed.value);
        /* DFX: procedural_only / returned_bytes / trimmed 用于数据驱动：
         * - 验证 procedural 专用召回是否被 agent 使用
         * - 看 token trim 实际收益 (returned_bytes vs trim 前)
         * 字段命名 dfx_metric=search_l2 便于 grep/分析。 */
        const proceduralOnly = is_procedural === true;
        api.logger.info(
          `${pfx} event=done dur_ms=${durMs} body_len=${text.length} ` +
          `trimmed=${trimmed.report.trimmed_count} ` +
          `dfx_metric=search_l2 returned_bytes=${text.length} ` +
          `procedural_only=${proceduralOnly}`,
        );
        return {
          content: [{ type: "text", text }],
          details: {
            query,
            top_k: args.top_k as number,
            trimmed_count: trimmed.report.trimmed_count,
            procedural_only: proceduralOnly,
            time_hint: timeHint,
          },
        };
      },
    },
    { name: "memory_record_search" },
  );

  // ------------------------------------------------------------------
  // memory_chat_history_search — 原始会话 chunk 检索
  // ------------------------------------------------------------------

  api.registerTool(
    {
      name: "memory_chat_history_search",
      label: "Memory Chat History Search",
      description:
        "检索原始会话中的完整上下文片段。用于回溯具体对话原文。" +
        "由 Celia 记忆系统提供原始会话检索能力，适合查来源和上下文。" +
        "tenant_id / user_id 由插件自动注入，通常只需提供 query 和 top_k。" +
        "每条结果的 createdAtMs/createdAtIso 表示该段原始对话发生时间；" +
        "涉及最近、上次、之前、时间顺序或事实冲突时应优先参考。" +
        "默认 top_k=5；返回单条 content 超过 600 B 会被截短并末尾加 `…`，" +
        "整体响应附 `_trim` 报告，需要全文请缩窄 query 或 conv_id。" +
        "提示：本轮对话总调用 memory_scene_load + memory_record_search + memory_chat_history_search 不超过 3 次。",
      parameters: Type.Object({
        query: Type.String({ description: "检索查询" }),
        top_k: Type.Optional(
          Type.Number({ description: "返回条数，默认 5" }),
        ),
        tenant_id: Type.Optional(
          Type.String({ description: "租户 ID（可选；插件自动注入 cfg.tenantId 或 \"default\"）" }),
        ),
        user_id: Type.Optional(
          Type.String({ description: "用户 ID（可选；插件自动注入 ctx._userId 或 cfg.userId）" }),
        ),
        sessionIdFilter: Type.Optional(
          Type.String({ description: "数据 session 过滤条件（可选）" }),
        ),
      }),
      async execute(_toolCallId, params) {
        if (!client.isConnected() && !(await client.waitReady())) {
          return {
            content: [{ type: "text", text: "Memory server is not available. Please try again shortly." }],
            isError: true,
          };
        }

        const { query, top_k, sessionIdFilter } = params as {
          query: string;
          top_k?: number;
          sessionIdFilter?: string;
        };
        const { tenant_id, user_id } = resolveTenantUser(params);
        const sessionId = await ensureToolSession(client, user_id);
        const traceId = genTraceId();
        const pfx     = logPrefix("memory_chat_history_search", traceId);

        api.logger.info(
          `${pfx} event=start user=${formatScopedId("u", user_id)} ` +
          `query_len=${query.length} top_k=${top_k ?? "default"}`,
        );
        const t0 = Date.now();

        const args: Record<string, unknown> = {
          tenant_id, user_id, query, sessionId,
        };
        if (top_k != null) args.top_k = top_k;
        if (sessionIdFilter != null && sessionIdFilter !== "") {
          args.sessionIdFilter = sessionIdFilter;
        }

        const raw   = await client.callTool(
          "memory_search_l3", args, undefined, { traceId });
        const trimmed = trimSearchContent(raw, SEARCH_L3_CONTENT_MAX_BYTES);
        const durMs   = Date.now() - t0;
        const text    = typeof trimmed.value === "string"
          ? trimmed.value
          : JSON.stringify(trimmed.value);
        /* DFX:returned_bytes 用于评估 L3 token 成本 (cc-execution-plan §5.2)。 */
        api.logger.info(
          `${pfx} event=done dur_ms=${durMs} body_len=${text.length} ` +
          `trimmed=${trimmed.report.trimmed_count} ` +
          `dfx_metric=search_l3 returned_bytes=${text.length}`,
        );
        return {
          content: [{ type: "text", text }],
          details: {
            query,
            top_k: top_k ?? null,
            trimmed_count: trimmed.report.trimmed_count,
          },
        };
      },
    },
    { name: "memory_chat_history_search" },
  );

  // ------------------------------------------------------------------
  // memory_scene_list_load — 获取摘要索引 JSON 原文
  // ------------------------------------------------------------------

  api.registerTool(
    {
      name: "memory_scene_list_load",
      label: "Memory Scene List Load",
      description:
        "获取当前用户的场景记忆索引 JSON 原文（含 path、summary、版本等元数据），" +
        "用于 agent 决定下一步 memory_scene_load 拉取哪条 path。返回 raw JSON 字符串。" +
        "tenant_id / user_id 由插件自动注入，通常无需参数。",
      parameters: Type.Object({
        tenant_id: Type.Optional(
          Type.String({ description: "租户 ID（可选；插件自动注入 cfg.tenantId 或 \"default\"）" }),
        ),
        user_id: Type.Optional(
          Type.String({ description: "用户 ID（可选；插件自动注入 ctx._userId 或 cfg.userId）" }),
        ),
      }),
      async execute(_toolCallId, params) {
        const memoryState = await readXiaoyiMemoryState();
        if (memoryState === 0) {
          const { tenant_id, user_id } = resolveTenantUser(params);
          const traceId = genTraceId();
          const pfx     = logPrefix("memory_scene_list_load", traceId);
          const text = JSON.stringify(buildMemoryClosedPayload(memoryState, {
            entries: [],
          }));
          api.logger.info(
            `${pfx} event=skip reason=memory_disabled ` +
            `user=${formatScopedId("u", user_id)} ` +
            "dfx_metric=get_l1_index_skipped memory_state=0",
          );
          return {
            content: [{ type: "text", text }],
            details: buildMemoryClosedDetails(memoryState, {
              tool: "memory_scene_list_load",
              tenant_id,
              user_id,
              entries_count: 0,
            }),
          };
        }

        if (!client.isConnected() && !(await client.waitReady())) {
          return {
            content: [{ type: "text", text: "Memory server is not available. Please try again shortly." }],
            isError: true,
          };
        }

        const { tenant_id, user_id } = resolveTenantUser(params);
        const sessionId = await ensureToolSession(client, user_id);
        const traceId = genTraceId();
        const pfx     = logPrefix("memory_scene_list_load", traceId);

        api.logger.info(
          `${pfx} event=start user=${formatScopedId("u", user_id)}`,
        );
        const t0 = Date.now();

        const raw = await client.callTool("memory_get_l1_index", {
          tenant_id,
          user_id,
          sessionId,
        }, undefined, { traceId });
        const durMs = Date.now() - t0;
        const text  = typeof raw === "string" ? raw : JSON.stringify(raw);
        /* DFX:entries_count 用于跟踪 L1 索引规模 (索引膨胀触发预警)。 */
        let entriesCount = -1;
        try {
          const parsed = typeof raw === "string" ? JSON.parse(raw) : raw;
          if (parsed && typeof parsed === "object"
              && Array.isArray((parsed as Record<string, unknown>).entries)) {
            entriesCount =
              ((parsed as Record<string, unknown>).entries as unknown[]).length;
          }
        } catch { /* 解析失败保持 -1 */ }
        api.logger.info(
          `${pfx} event=done dur_ms=${durMs} body_len=${text.length} ` +
            `dfx_metric=get_l1_index entries_count=${entriesCount}`,
        );
        return {
          content: [{ type: "text", text }],
          details: { tenant_id, user_id, entries_count: entriesCount },
        };
      },
    },
    { name: "memory_scene_list_load" },
  );

  // ------------------------------------------------------------------
  // memory_get_global_summary — 按需获取全局概览
  // 主 agent 已在 MEMORY.md 固定加载全局概览；本工具主要给 subagent /
  // cron（不加载 MEMORY.md）按需取概览。零 LLM,纯规则拼接。
  // ------------------------------------------------------------------

  /** 把字符串/数字 tier 映射为 C 端期望的整数 (0=edge / 1=cloud_s / 2=cloud_l)。
   *  agent-facing 默认 0(edge),避免一次塞 4KB 给 subagent。 */
  function parseL0Tier(tier: unknown): number {
    if (tier === 0 || tier === "edge") return 0;
    if (tier === 1 || tier === "cloud_s") return 1;
    if (tier === 2 || tier === "cloud_l") return 2;
    return 0;
  }

  api.registerTool(
    {
      name: "memory_get_global_summary",
      label: "Memory Get Global Summary",
      description:
        "按需获取 Celia 全局概览（用户画像 / 偏好 / 程序索引 / 前瞻）。" +
        "主 agent 通常已在 MEMORY.md 固定加载,无需调用;subagent / cron 不加载 " +
        "MEMORY.md,需要概览类信息时显式调用本工具。tier 控制摘要长度:" +
        "edge(~400 token,默认) / cloud_s(~1200 token) / cloud_l(~2500 token)。" +
        "不计入 memory_scene_load + memory_record_search + memory_chat_history_search 的 3 次渐进预算。",
      parameters: Type.Object({
        tier: Type.Optional(
          Type.Union(
            [
              Type.Literal("edge"),
              Type.Literal("cloud_s"),
              Type.Literal("cloud_l"),
              Type.Number({ description: "0=edge / 1=cloud_s / 2=cloud_l" }),
            ],
            { description: "摘要长度层级,默认 edge" },
          ),
        ),
        tenant_id: Type.Optional(
          Type.String({ description: "租户 ID(可选;插件自动注入 cfg.tenantId 或 \"default\")" }),
        ),
        user_id: Type.Optional(
          Type.String({ description: "用户 ID(可选;插件自动注入 ctx._userId 或 cfg.userId)" }),
        ),
      }),
      async execute(_toolCallId, params) {
        if (!client.isConnected() && !(await client.waitReady())) {
          return {
            content: [{ type: "text", text: "Memory server is not available. Please try again shortly." }],
            isError: true,
          };
        }

        const { user_id } = resolveTenantUser(params);
        const tier        = parseL0Tier((params as { tier?: unknown }).tier);
        const traceId     = genTraceId();
        const pfx         = logPrefix("memory_get_l0_global_summary", traceId);

        api.logger.info(
          `${pfx} event=start user=${formatScopedId("u", user_id)} tier=${tier}`,
        );
        const t0 = Date.now();

        /* C 端 ParseL0GlobalSummaryArgs 只识别 camelCase userId / tier,
         * 不接受 snake_case;TS wrapper 必须显式 mapping。 */
        const raw = await client.callTool(
          "memory_get_l0_global_summary",
          { userId: user_id, tier, _trace_id: traceId },
          undefined,
          { traceId },
        );
        const durMs = Date.now() - t0;
        const text  = typeof raw === "string" ? raw : JSON.stringify(raw);
        /* DFX:size_bytes 用于评估 subagent L0 token 成本是否随 tier 合理收紧。 */
        api.logger.info(
          `${pfx} event=done dur_ms=${durMs} body_len=${text.length} tier=${tier} ` +
            `dfx_metric=get_l0_summary size_bytes=${text.length}`,
        );
        return {
          content: [{ type: "text", text }],
          details: { tier, size_bytes: text.length },
        };
      },
    },
    { name: "memory_get_global_summary" },
  );

  // ------------------------------------------------------------------
  // memory_flush — drain 异步队列 + 强制触发所有 timer（同步屏障）
  // ------------------------------------------------------------------

  api.registerTool(
    {
      name: "memory_flush",
      label: "Memory Flush",
      description:
        "Synchronously drain the async ingest queue and force-run periodic memory maintenance once. Call after a batch of memory_store writes when you need subsequent memory_record_search to immediately reflect them.",
      parameters: Type.Object({
        timeoutMs: Type.Optional(
          Type.Number({ description: "Timeout in milliseconds (default: 60000)" }),
        ),
        tenant_id: Type.Optional(
          Type.String({ description: "租户 ID(可选;插件自动注入 cfg.tenantId 或 \"default\")" }),
        ),
        user_id: Type.Optional(
          Type.String({ description: "用户 ID(可选;插件自动注入 ctx._userId 或 cfg.userId)" }),
        ),
      }),
      async execute(_toolCallId, params) {
        if (!client.isConnected() && !(await client.waitReady())) {
          return {
            content: [{ type: "text", text: "Memory server is not available. Please try again shortly." }],
            isError: true,
          };
        }

        const { timeoutMs } = params as { timeoutMs?: number };
        const { tenant_id, user_id } = resolveTenantUser(params);

        api.logger.info(
          `[memory_flush] timeoutMs=${timeoutMs ?? 60000} ` +
            `user=${formatScopedId("u", user_id)}`,
        );

        const args: Record<string, unknown> = { userId: user_id };
        if (timeoutMs != null) args.timeoutMs = timeoutMs;

        const raw = (await client.callTool("memory_flush", args, 120_000)) as {
          status?: number;
          drained?: number;
          l1Targeted?: number;
          l1Status?: number;
        };

        const ok = raw.status === 0;
        api.logger.info(
          `[memory_flush] → status=${raw.status} drained=${raw.drained} ` +
            `l1Targeted=${raw.l1Targeted} l1Status=${raw.l1Status}`,
        );

        /* flush 强制 drain 异步队列 + 跑所有 timer，L0/L1 极可能变化；
         * 标 dirty，下次 before_prompt_build 立即刷新，避免 30s TTL 内
         * 用户感知不到 flush 后的最新状态。 */
        if (ok) {
          markFixedLoadDirty(tenant_id, user_id);
        }

        return {
          content: [
            {
              type: "text",
              text: ok
                ? "Memory flushed: async queue drained and all timers force-triggered."
                : `Memory flush failed (status=${raw.status}).`,
            },
          ],
          isError: !ok,
          details: {
            status: raw.status,
            drained: raw.drained,
            l1Targeted: raw.l1Targeted,
            l1Status: raw.l1Status,
          },
        };
      },
    },
    { name: "memory_flush" },
  );

  // ------------------------------------------------------------------
  // memory_list — 语义化列出全局概览 / 场景记忆 / 原子事实
  // ------------------------------------------------------------------

  api.registerTool(
    {
      name: "memory_list",
      label: "Memory List",
      description:
        "List stored memories by semantic category: global overview, " +
        "scene memory, and atomic facts. " +
        "Only call when the user explicitly requests to " +
        "see or list their memories.",
      parameters: Type.Object({
        categories: Type.Array(
          Type.String({
            enum: ["global_overview", "scene_memory", "atomic_facts"],
          }),
          { description: "Which memory categories to include" },
        ),
        limit: Type.Optional(
          Type.Number({
            description: "Atomic fact page size (default 20, max 100)",
          }),
        ),
        offset: Type.Optional(
          Type.Number({
            description: "Atomic fact page offset (default 0)",
          }),
        ),
      }),
      async execute(_toolCallId, params) {
        if (!client.isConnected() && !(await client.waitReady())) {
          return {
            content: [{ type: "text", text: "Memory server is not available. Please try again shortly." }],
            isError: true,
          };
        }

        const { categories, layers, limit, offset, _userId } = params as {
          categories?: string[];
          layers?: string[];
          limit?: number;
          offset?: number;
          _userId?: string;
        };
        const userId = _userId ?? cfg.userId;
        const layerMap: Record<string, string> = {
          global_overview: "l0",
          scene_memory: "l1",
          atomic_facts: "l2",
          l0: "l0",
          l1: "l1",
          l2: "l2",
        };
        const requestedCategories = categories ?? layers ?? [];
        const internalLayers = Array.from(
          new Set(
            requestedCategories
              .map((layer) => layerMap[layer])
              .filter((layer): layer is string => typeof layer === "string"),
          ),
        );
        if (internalLayers.length === 0) {
          return {
            content: [{
              type: "text",
              text: "Choose at least one memory category.",
            }],
            isError: true,
            details: { error: "missing_memory_category" },
          };
        }

        api.logger.info(
          `[memory_list] categories=${JSON.stringify(requestedCategories)} ` +
            `layers=${JSON.stringify(internalLayers)} limit=${limit} ` +
            `offset=${offset} userId=${userId}`,
        );

        const sessionId = await ensureToolSession(client, userId);
        const args: Record<string, unknown> = {
          layers: internalLayers,
          sessionId,
          userId,
        };
        if (limit != null) args.limit = limit;
        if (offset != null) args.offset = offset;

        const raw = (await client.callTool("memory_list", args)) as Record<string, unknown>;

        const parts: string[] = [];

        if (raw.l0) {
          const l0 = raw.l0 as { global_summary?: string };
          parts.push(
            "## 全局概览\n" + (l0.global_summary || "(empty)"),
          );
        }
        if (raw.l1) {
          const l1 = raw.l1 as {
            memtype_count?: number;
            scene_count?: number;
            memtype_summaries?: unknown[];
            scene_summaries?: unknown[];
          };
          parts.push(
            `## 场景记忆\n` +
            `MemTypes: ${l1.memtype_count ?? 0}, ` +
            `Scenes: ${l1.scene_count ?? 0}\n\n` +
            JSON.stringify(l1, null, 2),
          );
        }
        if (raw.l2) {
          const l2 = raw.l2 as {
            count?: number;
            totalCount?: number;
            limit?: number;
            offset?: number;
            records?: unknown[];
          };
          parts.push(
            `## 原子事实\n` +
            `Showing ${l2.count ?? 0} of ${l2.totalCount ?? 0} ` +
            `(offset=${l2.offset ?? 0}, limit=${l2.limit ?? 20})\n\n` +
            JSON.stringify(l2.records ?? [], null, 2),
          );
        }

        if (parts.length === 0) {
          parts.push("No memory categories returned.");
        }

        return {
          content: [{ type: "text", text: parts.join("\n\n") }],
        };
      },
    },
    { name: "memory_list" },
  );

  // ------------------------------------------------------------------
  // memory_dump — 把当前 session 记忆落盘为 JSON 文件
  // 与 memory_list 一样，Debug/Release 构建下均由 MCP 常驻暴露。
  // ------------------------------------------------------------------

  api.registerTool(
    {
      name: "memory_dump",
      label: "Memory Dump",
      description:
        "Export this session's memories as a JSON file under dumpDir. " +
        "Returns only the file path + record counts, never the payload itself.",
      parameters: Type.Object({
        sessionId: Type.Optional(
          Type.String({
            description:
              "Session ID. Omit to auto-derive from current user's tool session.",
          }),
        ),
        outputPath: Type.Optional(
          Type.String({
            description:
              "Relative path under dumpDir. Empty/omit = auto-name. " +
              "Absolute paths or '..' are rejected.",
          }),
        ),
        category: Type.Optional(
          Type.Number({ description: "Filter by category (<0 = any)" }),
        ),
        sinceTimestampMs: Type.Optional(
          Type.Number({ description: "Lower bound timestamp in ms (0 = unbounded)" }),
        ),
        untilTimestampMs: Type.Optional(
          Type.Number({ description: "Upper bound timestamp in ms, exclusive (0 = unbounded)" }),
        ),
        timeField: Type.Optional(
          Type.String({
            description: "'updated_at_ms' (default) or 'created_at_ms'",
          }),
        ),
        includeSceneMemory: Type.Optional(
          Type.Boolean({ description: "Include scene memory summaries" }),
        ),
        includeGlobalOverview: Type.Optional(
          Type.Boolean({ description: "Include global overview" }),
        ),
      }),
      async execute(_toolCallId, params) {
        if (!client.isConnected() && !(await client.waitReady())) {
          return {
            content: [{ type: "text", text: "Memory server is not available. Please try again shortly." }],
            isError: true,
          };
        }

        const p = params as {
          sessionId?: string;
          outputPath?: string;
          category?: number;
          sinceTimestampMs?: number;
          untilTimestampMs?: number;
          timeField?: string;
          includeSceneMemory?: boolean;
          includeGlobalOverview?: boolean;
          includeL1?: boolean;
          includeL0?: boolean;
          _userId?: string;
        };
        const userId = p._userId ?? cfg.userId;
        const sessionId = p.sessionId ?? (await ensureToolSession(client, userId));

        const args: Record<string, unknown> = { sessionId };
        if (p.outputPath != null) args.outputPath = p.outputPath;
        if (p.category != null) args.category = p.category;
        if (p.sinceTimestampMs != null) args.sinceTimestampMs = p.sinceTimestampMs;
        if (p.untilTimestampMs != null) args.untilTimestampMs = p.untilTimestampMs;
        if (p.timeField != null) args.timeField = p.timeField;
        const includeSceneMemory = p.includeSceneMemory ?? p.includeL1;
        const includeGlobalOverview = p.includeGlobalOverview ?? p.includeL0;
        if (includeSceneMemory != null) args.includeL1 = includeSceneMemory;
        if (includeGlobalOverview != null) args.includeL0 = includeGlobalOverview;

        api.logger.info(
          `[memory_dump] sessionId=${sessionId} userId=${userId} ` +
          `outputPath=${p.outputPath ?? "(auto)"} ` +
          `includeGlobalOverview=${includeGlobalOverview} ` +
          `includeSceneMemory=${includeSceneMemory}`,
        );

        const raw = (await client.callTool("memory_dump", args, 120_000)) as {
          status?: number;
          file_path?: string;
          record_count?: { l0?: number; l1?: number; l2?: number };
          file_size_bytes?: number;
          generated_at_ms?: number;
        };

        const ok = raw.status === 0;
        const counts = raw.record_count ?? {};
        api.logger.info(
          `[memory_dump] → status=${raw.status} file=${raw.file_path} ` +
          `L0=${counts.l0} L1=${counts.l1} L2=${counts.l2} size=${raw.file_size_bytes}B`,
        );

        const summary = ok
          ? `Memory dumped to: ${raw.file_path}\n` +
            `Records: global_overview=${counts.l0 ?? 0}, ` +
            `scene_memory=${counts.l1 ?? 0}, ` +
            `atomic_facts=${counts.l2 ?? 0}\n` +
            `Size: ${raw.file_size_bytes ?? 0} bytes`
          : `Memory dump failed (status=${raw.status}).`;

        return {
          content: [{ type: "text", text: summary }],
          isError: !ok,
          details: {
            status: raw.status,
            filePath: raw.file_path,
            recordCount: {
              global_overview: counts.l0 ?? 0,
              scene_memory: counts.l1 ?? 0,
              atomic_facts: counts.l2 ?? 0,
            },
            fileSizeBytes: raw.file_size_bytes,
            generatedAtMs: raw.generated_at_ms,
          },
        };
      },
    },
    { name: "memory_dump" },
  );

  // ========================================================================
  // Dream MCP toolkit —— OpenClaw chat 查询入口
  //
  //   - dream_status        实时进度（phase + progress）
  //   - dream_run_summary   单次 run 结果汇总（scene/命名/冲突/token）
  //   - dream_recent_runs   最近 N 次 run 列表
  // 这些工具是薄透传：args 直接转发给 C 端 mcp_tools_dream.c。
  // 输出已是 prompt-friendly 中文，LLM 直接转述给用户。
  // ========================================================================

  api.registerTool(
    {
      name: "dream_status",
      label: "Dream Status",
      description:
        "Query current/specified dream run progress (phase + percent). " +
        "Use when the user asks 'dream 跑得咋样' / 'how is dreaming going'.",
      parameters: Type.Object({
        sessionId: Type.Optional(Type.String({
          description: "Session ID. Omit to auto-derive from current session.",
        })),
        runId: Type.Optional(Type.String({
          description: "Specific run id; omit for the latest run.",
        })),
      }),
      async execute(_toolCallId, params) {
        if (!client.isConnected() && !(await client.waitReady())) {
          return { content: [{ type: "text", text: "Memory server unavailable" }], isError: true };
        }
        const p = params as { sessionId?: string; runId?: string };
        const userId = cfg.userId;
        const sessionId = p.sessionId ?? (await ensureToolSession(client, userId));
        const args: Record<string, unknown> = { sessionId };
        if (p.runId) args.runId = p.runId;
        const raw = (await client.callTool("dream_status", args)) as { content?: { type: string; text: string }[] };
        return raw as unknown as { content: { type: string; text: string }[] };
      },
    },
    { name: "dream_status" },
  );

  api.registerTool(
    {
      name: "dream_run_summary",
      label: "Dream Run Summary",
      description:
        "Query the result summary of a finished dream run (emergent scenes, " +
        "naming, conflicts, token usage). " +
        "Use when the user asks '刚才跑完整理了什么' / 'what did dream do'.",
      parameters: Type.Object({
        sessionId: Type.Optional(Type.String()),
        runId: Type.Optional(Type.String({
          description: "Specific run id; omit for the latest finished run.",
        })),
      }),
      async execute(_toolCallId, params) {
        if (!client.isConnected() && !(await client.waitReady())) {
          return { content: [{ type: "text", text: "Memory server unavailable" }], isError: true };
        }
        const p = params as { sessionId?: string; runId?: string };
        const sessionId = p.sessionId ?? (await ensureToolSession(client, cfg.userId));
        const args: Record<string, unknown> = { sessionId };
        if (p.runId) args.runId = p.runId;
        const raw = (await client.callTool("dream_run_summary", args)) as { content?: { type: string; text: string }[] };
        return raw as unknown as { content: { type: string; text: string }[] };
      },
    },
    { name: "dream_run_summary" },
  );

  api.registerTool(
    {
      name: "dream_recent_runs",
      label: "Dream Recent Runs",
      description:
        "List the most recent N dream runs with brief summaries. " +
        "Use when the user asks '最近几次 dream 跑了啥'.",
      parameters: Type.Object({
        sessionId: Type.Optional(Type.String()),
        limit: Type.Optional(Type.Number({
          description: "Max number of runs (default 5).",
        })),
      }),
      async execute(_toolCallId, params) {
        if (!client.isConnected() && !(await client.waitReady())) {
          return { content: [{ type: "text", text: "Memory server unavailable" }], isError: true };
        }
        const p = params as { sessionId?: string; limit?: number };
        const sessionId = p.sessionId ?? (await ensureToolSession(client, cfg.userId));
        const args: Record<string, unknown> = { sessionId };
        if (p.limit != null) args.limit = p.limit;
        const raw = (await client.callTool("dream_recent_runs", args)) as { content?: { type: string; text: string }[] };
        return raw as unknown as { content: { type: string; text: string }[] };
      },
    },
    { name: "dream_recent_runs" },
  );

}

// ============================================================================
// CLI commands
// ============================================================================

export function registerCli(
  api: OpenClawPluginApi,
  client: CeliaMcpClient,
  cfg: CeliaMemoryConfig,
): void {
  api.registerCli(
    ({ program }) => {
      const mem = program
        .command("celia-mem")
        .description("Celia memory plugin commands");

      mem
        .command("search")
        .description("Search memories")
        .argument("<query>", "Search query")
        .option("--limit <n>", "Max results", "5")
        .action(async (query: string, opts: { limit: string }) => {
          const sessionId = await ensureToolSession(client, cfg.userId);
          const raw = await client.callTool("memory_search_l2", {
            tenant_id: cfg.tenantId ?? "default",
            user_id: cfg.userId,
            query,
            top_k: parseInt(opts.limit),
            sessionId,
          });
          const data = parseSearchResponse(raw);
          const output = data.results.map((r) => ({
            id: r.id,
            score: r.score,
            confidence: r.confidence,
            content: r.content,
          }));
          console.log(JSON.stringify(output, null, 2));
        });

      mem
        .command("store")
        .description("Store a memory")
        .argument("<text>", "Text to store")
        .action(async (text: string) => {
          const sessionId = await ensureToolSession(client, cfg.userId);
          const raw = await client.callTool("memory_add", {
            content: text,
            userId: cfg.userId,
            scope: "user",
            sessionId,
          });
          console.log(JSON.stringify(raw, null, 2));
        });

      /* `celia-mem list` command removed: old hierarchical browse support was
       * dropped in PR A1. Use
       * `celia-mem search <query>` instead. */
    },
    { commands: ["celia-mem"] },
  );
}
