/**
 * 钩子实现：MD 文件同步 / 对话捕获 / token DFX / workspace 隔离。
 *
 * - md-file sync (session_start / after_compaction / before_prompt_build):
 *     fetch L0 + L1 index 后通过 safeWriteMarkers 批量原子写入
 *     USER.md / MEMORY.md marker 区；AGENTS.md 由 Docker/celiaclaw 预置，
 *     不再写入
 * - conversation capture (agent_end): 每轮对话清洗后写入 mem_conversation
 *     原始表，由 C 端 worker 异步消费 ingest pipeline。ingestMode 由
 *     StoreUrgentIngest 决定 deferred / deferred-urgent
 * - token stats DFX (agent_end): 上报每轮 agent token usage 与 recall 统计
 * - workspace isolation (session_start/end): 按用户隔离 workspace 文件
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { deriveUserId } from "../core/helpers.js";
import { ensureToolSession } from "../core/session.js";
import { formatScopedId, genTraceId, logPrefix } from "../core/dfx.js";
import { ensureUppercaseMd, hasMarkerSemanticContent, MARKER_BUDGETS, renderContent, safeWriteMarkers, trimByPolicy, } from "./safeWriteMarker.js";
import { fetchL0 } from "./l0Sync.js";
import { fetchL1Index, filterL1ByType, selectFixedLoadScenes, } from "./l1ScenesSync.js";
import { STATIC_GUIDE_TEMPLATE } from "./agentsGuideSync.js";
import { buildStoreUrgentIngestKey, consumeStoreUrgentIngest, } from "./store-signal.js";
import { sanitizeUserTextForCapture } from "./text-utils.js";
import { clearServedL1ForSession } from "./tools.js";
import { utf8ByteLength } from "./markerProtocol.js";
import { buildSessionPromptBufferKey, clearSessionPromptBuffer, getSessionPromptBuffer, } from "./session-prompt-buffer.js";
import { readXiaoyiMemoryState } from "./runtime-state.js";
// ============================================================================
// 跨 hook 共享状态（before_prompt_build → agent_end）
// ============================================================================
/**
 * 固定加载 token 数（runAllSyncs 写入后保持，TTL skip / 跨多个 agent_end 沿用）。
 * key = `${tenantId}|${userId}|${workspaceDir}`，另保留 `${tenantId}|${userId}|*`
 * 兜底，兼容 agent_end 缺 workspaceDir 的旧运行时。
 *
 * 语义：sticky——registerTokenStatsCapture 读取后**不**删除，下次 token
 * stats 上报继续读到同一值；只有 runAllSyncs 重新跑（触发 full sync 或
 * 强制 dirty 刷新）才会被覆盖。这样 TTL skip 路径直接跳过 sync 时仍能
 * 准确报告"本轮 prompt 中可见 marker tokens"，而不是 0。
 *
 * 换算规则：Math.ceil(utf8Bytes / 2)，与 recallTokenCount 一致。
 */
const lastFixedLoadTokens = new Map();
/**
 * 上次成功执行 runAllSyncs 的时间戳（ms epoch）。
 * key = `${tenantId}|${userId}`。
 *
 * 用于 before_prompt_build TTL skip：连续多次 prompt 触发不必每次都
 * 重新拉 L0/L1 + 渲染 + parse marker。session_start / after_compaction
 * 永远走 full sync 不读这个 Map。
 */
const lastFullSyncMs = new Map();
/**
 * before_prompt_build 的 TTL 窗口（毫秒）。
 *
 * 30 秒：单轮多次 LLM 重试 / 工具循环不会重复 MCP 拉取；session_start
 * 之后 30s 内的连续 prompt 也直接复用首次 sync 结果。超过 TTL 任意
 * 一次 before_prompt_build 都会刷一遍。
 */
const BEFORE_PROMPT_TTL_MS = 30_000;
/**
 * 标脏的 (tenantId, userId) key 集合（dirty flag）。
 *
 * 用途：memory_store / memory_flush / agent_end 等"内存内容可能变化"
 * 的事件后，把 key 加入此集合；下次 before_prompt_build 即便在 TTL
 * 窗口内也会强制 full sync 一次然后清除标记，确保 LLM 看到最新的
 * L0 / L1 而不是 30s 前的 stale 状态。
 */
const dirtyKeys = new Set();
function normalizeStateWorkspace(workspaceDir) {
    return workspaceDir && workspaceDir.trim() ? workspaceDir.trim() : "*";
}
function syncStateKey(tenantId, userId, workspaceDir) {
    return `${tenantId || "default"}|${userId}|${normalizeStateWorkspace(workspaceDir)}`;
}
function globalSyncStateKey(tenantId, userId) {
    return syncStateKey(tenantId, userId);
}
/**
 * 标记某 (tenantId, userId) 的固定加载内容为 dirty，下次 before_prompt
 * _build 强制刷新（绕过 TTL skip）。
 *
 * 调用方：
 *  - tools.ts::memory_store 工具 execute 末尾（用户主动记忆）
 *  - tools.ts::memory_flush 工具 execute 末尾（强制提取）
 *  - registerConversationCapture::agent_end 末尾（每轮对话捕获后）
 *
 * 缺省 tenantId 用 "default"（与 hook 内 fallback 对齐）。
 */
export function markFixedLoadDirty(tenantId, userId) {
    dirtyKeys.add(globalSyncStateKey(tenantId || "default", userId));
}
function joinContextSections(parts) {
    const present = parts
        .map((p) => p?.trim())
        .filter((p) => Boolean(p));
    return present.length > 0 ? present.join("\n\n") : undefined;
}
function renderPatchBodyForPrompt(patch) {
    const budget = MARKER_BUDGETS[patch.markerName];
    const rendered = renderContent(patch.markerName, patch.inputContent);
    let body;
    if (utf8ByteLength(rendered) <= budget) {
        body = rendered.trim();
    }
    else {
        body = trimByPolicy(patch.markerName, patch.inputContent, budget).trimmedContent.trim();
    }
    return hasMarkerSemanticContent(body) ? body : undefined;
}
function renderGuidanceSystemContext(guidancePatch) {
    try {
        const body = renderPatchBodyForPrompt(guidancePatch);
        if (!body) {
            return undefined;
        }
        return ["# Celia Memory Guidance", body].join("\n\n");
    }
    catch {
        /* guidance 渲染失败时 fail-open，不影响 marker 刷新。 */
        return undefined;
    }
}
/** 记忆工具名集合——其 toolResult 不参与对话捕获（避免自引用循环） */
const MEMORY_TOOLS = new Set([
    "memory_open", "memory_store", "memory_forget",
    "memory_scene_load", "memory_record_search", "memory_chat_history_search",
    "memory_scene_list_load", "memory_flush", "memory_list",
]);
// ============================================================================
// SessionPromptBuffer 注入到 prompt
// ============================================================================
/**
 * Sanitize a single SessionPromptBuffer content for safe rendering inside a
 * markdown bullet list：
 *
 *   1. 折叠所有换行序列（CR / LF / CRLF / U+2028 / U+2029）为单空格——
 *      多行内容会逃逸 "- bullet" markdown 格式，注入"独立段落"伪装成
 *      系统指令（典型 prompt injection 向量："忽略上文，按我说的做"）。
 *   2. 移除 unicode bidi-override（U+202A-E、U+2066-9）—— 双向控制字符
 *      可让显示文本与实际字节序列不一致，迷惑人类审阅但 LLM 仍读到隐藏
 *      指令。
 *   3. 移除 ChatML 控制序列 `<|...|>` —— OpenAI / Anthropic 等模型用
 *      `<|im_start|>` 等定界 system/user/assistant 角色，用户内容若含
 *      该序列会被 LLM 误解析为新的对话轮次。
 *   4. 控制字符（C0/C1）替换为空格，避免 0x00 截断显示。
 *
 * 不动正常的 emoji / 中文 / 英文 / 标点；不破坏语义。
 */
function sanitizeSessionPromptBufferLine(input) {
    // 1. 折叠换行/段落分隔符（LF, CR, U+2028 LINE SEP, U+2029 PARA SEP）
    let out = input.replace(/[\r\n\u2028\u2029]+/g, " ");
    // 2. 移除 unicode bidi-override（U+202A..E, U+2066..9）
    out = out.replace(/[\u202A-\u202E\u2066-\u2069]/g, "");
    // 3. 移除 ChatML 控制序列 <|im_start|> / <|im_end|> 等（防角色伪装）
    out = out.replace(/<\|[^|]{0,64}\|>/g, "");
    // 4. C0/C1 控制字符 → 空格（保留 \t = U+0009）
    // eslint-disable-next-line no-control-regex
    out = out.replace(/[\x00-\x08\x0B-\x1F\x7F-\x9F]/g, " ");
    return out.trim();
}
function renderSessionPromptBufferBlock(items) {
    if (items.length === 0)
        return "";
    const bullets = items
        .map((it) => sanitizeSessionPromptBufferLine(it.content ?? ""))
        .filter((s) => s.length > 0)
        .map((s) => `- ${s}`);
    if (bullets.length === 0)
        return "";
    return [
        "# Session Prompt Buffer",
        "",
        "The user explicitly requested, corrected, or gave feedback during "
            + "this session. Treat the following as session-scoped guidance for "
            + "the current conversation:",
        "",
        ...bullets,
    ].join("\n");
}
/**
 * 测试钩子：暴露 sanitizeSessionPromptBufferLine 供单测验证
 * prompt-injection 防御。生产代码不调用此函数。
 */
export function _sanitizeSessionPromptBufferLineForTesting(input) {
    return sanitizeSessionPromptBufferLine(input);
}
/**
 * 从 plugin 进程内 SessionPromptBuffer 读取当前 session 的 prompt
 * guidance，渲染并返回 `appendSystemContext`。
 */
export async function fetchAndRenderSessionPromptBuffer(api, cfg, ctx) {
    const openclawSessionId = ctx.sessionId ?? "";
    if (!openclawSessionId)
        return undefined;
    const userId = deriveUserId(ctx, cfg.userId);
    const tenantId = cfg.tenantId ?? "default";
    const traceId = genTraceId();
    const pfx = logPrefix("sessionPromptBuffer", traceId);
    const key = buildSessionPromptBufferKey(tenantId, userId, openclawSessionId);
    const items = getSessionPromptBuffer(key);
    const renderedText = renderSessionPromptBufferBlock(items);
    api.logger.info?.(`${pfx} event=done count=${items.length} `
        + `bytes=${renderedText.length} session=${openclawSessionId} `
        + `user=${formatScopedId("u", userId)}`);
    return renderedText
        ? { appendSystemContext: renderedText }
        : undefined;
}
// ============================================================================
// MD-file sync hook（progressive-loading v1：固定加载下沉）
// ============================================================================
/**
 * 注册 MD 文件同步钩子：把 celia 固定加载内容下沉到 OpenClaw 原生
 * USER.md / MEMORY.md marker 区。
 *
 * 触发：
 *   - `session_start`：首次保底（USER.md / MEMORY.md marker 全写）
 *   - `after_compaction`：L0 / L1 可能变化时刷新
 *   - `before_prompt_build`：每次 LLM call 前同步一次（覆盖 resume）
 *
 * Marker 分布（详见 spec §6.4.3）：
 *   - USER.md：MEMORY_OVERVIEW（4KB）
 *   - MEMORY.md：MEMORY_SCENES（6KB，per-summary 等比缩短）
 *   - MEMORY_GUIDE：仅随 before_prompt_build 的 guidance system context
 *     返回，不再写入环境 AGENTS.md
 *
 * memory_type 六类摘要不再固定加载：保留 L1_memtype_*.md 生成链路，
 * 走 memory_scene_load 渐进访问（详见 docs codex md_files-only 计划 §4）。
 *
 * 当前固定走 md_files 加载路径。
 */
export function registerMdFileSync(api, client, cfg) {
    /** 在指定 hook 阶段执行固定加载同步。 */
    async function runAllSyncs(stage, ctx) {
        /* 入口诊断日志：记录触发事件 + 关键 ctx 字段，便于排查
         * "hook 没跑/早期 return" 类问题。 */
        api.logger.info?.(`memory-celia: [mdFileSync] stage=${stage} fired ` +
            `ws=${ctx.workspaceDir ?? "<undef>"} ` +
            `client_ready=${client.isConnected()}`);
        if (!ctx.workspaceDir) {
            api.logger.warn?.(`memory-celia: [mdFileSync] stage=${stage} skip ` +
                `reason=no_workspace_dir`);
            return {};
        }
        if (!client.isConnected() && !(await client.waitReady())) {
            api.logger.warn?.(`memory-celia: [mdFileSync] stage=${stage} skip ` +
                `reason=client_not_ready`);
            return {};
        }
        const traceId = genTraceId();
        const pfx = logPrefix("mdFileSync", traceId);
        const userId = deriveUserId(ctx, cfg.userId);
        const tenantId = cfg.tenantId ?? "default";
        const startedAt = Date.now();
        /* ---- before_prompt_build TTL skip + dirty bypass ----
         * session_start / after_compaction 是显式刷新点，必须 full sync。
         * before_prompt_build 在工具循环 / 多次 LLM 重试中可能短时间内连续
         * 触发多次，TTL 内直接跳过 fetch+render+IO，省 ~80% 的浪费调用。
         *
         * dirty bypass：memory_store / memory_flush / agent_end 等事件标
         * dirtyKeys 后，下次 before_prompt_build 即便在 TTL 内也强制走 full
         * sync 一次（同时清 dirty），保证 LLM 看到最新的 L0 / L1 而不是
         * 30s 前的 stale 状态。 */
        const stateKey = syncStateKey(tenantId, userId, ctx.workspaceDir);
        const dirtyFallbackKey = globalSyncStateKey(tenantId, userId);
        if (stage === "before_prompt_build") {
            const isDirty = dirtyKeys.has(stateKey) || dirtyKeys.has(dirtyFallbackKey);
            const last = lastFullSyncMs.get(stateKey) ?? 0;
            const ageMs = startedAt - last;
            if (!isDirty && last > 0 && ageMs < BEFORE_PROMPT_TTL_MS) {
                api.logger.info?.(`${pfx} stage=${stage} event=skip reason=ttl ` +
                    `age_ms=${ageMs} ttl_ms=${BEFORE_PROMPT_TTL_MS} ` +
                    `user=${formatScopedId("u", userId)}`);
                return {};
            }
            if (isDirty) {
                api.logger.info?.(`${pfx} stage=${stage} event=dirty_refresh ` +
                    `age_ms=${ageMs} user=${formatScopedId("u", userId)}`);
                dirtyKeys.delete(stateKey);
                dirtyKeys.delete(dirtyFallbackKey);
            }
        }
        /* ---- ensure 大写 USER.md / MEMORY.md 文件存在 ---- */
        try {
            await Promise.all([
                ensureUppercaseMd(ctx.workspaceDir, "USER.md", {
                    traceId,
                    logger: api.logger,
                }),
                ensureUppercaseMd(ctx.workspaceDir, "MEMORY.md", {
                    traceId,
                    logger: api.logger,
                }),
            ]);
        }
        catch (err) {
            api.logger.warn?.(`${pfx} stage=${stage} event=ensure_md_failed ` +
                `err=${String(err)}`);
            return {};
        }
        /* ---- L1 预取：一次 MCP 调用拿全 L1 index，过滤出 SCENE 用于固定加载 ----
         * memory_type 类（MEMTYPE）不再固定加载，仅靠 memory_scene_load 渐进访问。
         * L0 与 L1 并行 fetch；fetch 失败仅本路 patch 缺席，不阻塞兄弟。 */
        const syncCtx = { traceId, logger: api.logger };
        const fetched = await Promise.allSettled([
            fetchL0(client, tenantId, userId, syncCtx),
            fetchL1Index(client, tenantId, userId, syncCtx),
        ]);
        const fetchOk = (idx, label) => {
            const r = fetched[idx];
            if (r.status === "fulfilled")
                return r.value;
            api.logger.warn?.(`${pfx} stage=${stage} event=fetch_failed source=${label} ` +
                `err=${String(r.reason)}`);
            return null;
        };
        const l0Body = fetchOk(0, "l0");
        const allL1 = fetchOk(1, "l1_index");
        const scenes = allL1
            ? selectFixedLoadScenes(filterL1ByType(allL1, "SCENE"))
            : null;
        /* ---- 按文件分组组装 patch 数组 ----
         * USER.md 写 MEMORY_OVERVIEW，MEMORY.md 写 MEMORY_SCENES；
         * 每个文件各通过 safeWriteMarkers 做单次 read-modify-write，
         * 避免同文件并发读改互相覆盖。 */
        const userMdPath = join(ctx.workspaceDir, "USER.md");
        const memoryMdPath = join(ctx.workspaceDir, "MEMORY.md");
        const userPatches = [];
        const memoryPatches = [];
        if (l0Body !== null) {
            userPatches.push({
                markerName: "MEMORY_OVERVIEW",
                inputContent: l0Body,
            });
        }
        if (scenes !== null) {
            memoryPatches.push({
                markerName: "MEMORY_SCENES",
                inputContent: scenes,
            });
        }
        /* GUIDE 仅注入本轮 prompt，不再写入环境 AGENTS.md。 */
        const guidancePatch = {
            markerName: "MEMORY_GUIDE",
            inputContent: STATIC_GUIDE_TEMPLATE,
        };
        /* ---- 两个文件各一次原子写 ---- */
        const writeJobs = [];
        if (userPatches.length > 0) {
            writeJobs.push({
                patches: userPatches,
                promise: safeWriteMarkers(userMdPath, userPatches, syncCtx),
            });
        }
        if (memoryPatches.length > 0) {
            writeJobs.push({
                patches: memoryPatches,
                promise: safeWriteMarkers(memoryMdPath, memoryPatches, syncCtx),
            });
        }
        const guidanceSystemContext = stage === "before_prompt_build"
            ? renderGuidanceSystemContext(guidancePatch)
            : undefined;
        const writeRes = await Promise.allSettled(writeJobs.map((j) => j.promise));
        /* ---- 汇总 marker token 数，写入跨 hook 共享状态 ---- */
        let totalFixedTokens = 0;
        const perMarker = [];
        for (let i = 0; i < writeRes.length; i++) {
            const wr = writeRes[i];
            if (wr.status !== "fulfilled")
                continue;
            const patches = writeJobs[i].patches;
            for (let j = 0; j < wr.value.length; j++) {
                const r = wr.value[j];
                const name = patches[j]?.markerName ?? "?";
                if (r.action !== "noop") {
                    totalFixedTokens += Math.ceil(r.bytes / 2);
                }
                perMarker.push(`${name}=${r.bytes}B`);
            }
        }
        /* 仅当所有文件 marker（L0 + L1_SCENES）都参与统计时才覆写；
         * fetch 失败导致 patch 缺席时 perMarker.length < 2，保留上次
         * 完整值，避免 MEMORY.md 中旧内容仍在但统计降级到部分 marker。 */
        const EXPECTED_MARKER_COUNT = 2;
        if (perMarker.length >= EXPECTED_MARKER_COUNT) {
            lastFixedLoadTokens.set(stateKey, totalFixedTokens);
            lastFixedLoadTokens.set(dirtyFallbackKey, totalFixedTokens);
        }
        const elapsedMs = Date.now() - startedAt;
        const ok = writeRes.filter((r) => r.status === "fulfilled").length;
        const failed = writeRes.length - ok;
        /* 仅文件级写入全部成功才更新 TTL 时间戳——失败时下次 before_prompt
         * _build 仍尝试重试，避免错过新鲜数据。 */
        if (failed === 0) {
            lastFullSyncMs.set(stateKey, Date.now());
        }
        api.logger.info?.(`${pfx} stage=${stage} event=done dur_ms=${elapsedMs} ` +
            `files_ok=${ok} files_failed=${failed} ` +
            `fixed_load_tokens=${totalFixedTokens} ` +
            `marker_bytes=[${perMarker.join(",")}] ` +
            `user=${formatScopedId("u", userId)}`);
        return { guidanceSystemContext };
    }
    api.on("session_start", async (_event, ctx) => {
        await runAllSyncs("session_start", ctx);
    });
    api.on("after_compaction", async (_event, ctx) => {
        await runAllSyncs("after_compaction", ctx);
    });
    /* before_prompt_build：每次 LLM call 前可能触发同步。
     * 覆盖"resume 已有 session"等 session_start 不发的场景。
     * runAllSyncs 内部走 BEFORE_PROMPT_TTL_MS（30s）skip：连续多次 prompt
     * 触发不重复 MCP 拉取 + render + IO；过 TTL 才会 full sync。
     *
     * SessionPromptBuffer 闭环：md sync 完成后读取当前 session 的运行态
     * prompt buffer，随 guidance context 一起注入 appendSystemContext。 */
    api.on("before_prompt_build", async (_event, ctx) => {
        const syncResult = await runAllSyncs("before_prompt_build", ctx);
        const promptBuffer = await fetchAndRenderSessionPromptBuffer(api, cfg, ctx);
        const appendSystemContext = joinContextSections([
            syncResult.guidanceSystemContext,
            promptBuffer?.appendSystemContext,
        ]);
        return appendSystemContext ? { appendSystemContext } : undefined;
    });
    api.logger.info?.("memory-celia: [mdFileSync] enabled " +
        "(triggers=session_start+after_compaction+before_prompt_build); " +
        "session prompt buffer injection enabled on before_prompt_build");
}
// ============================================================================
// Conversation capture hook
// ============================================================================
/**
 * agent_end 对话捕获钩子。
 *
 * 始终捕获每一轮对话清洗后写入 mem_conversation 原始表，由 C 端
 * worker 异步消费 ingest pipeline。
 *
 * ingestMode 决策：
 *  - StoreUrgentIngest 标记 → deferred-urgent（立即唤醒 Worker）
 *  - 无标记 → deferred（worker 5min / 10 条阈值定时处理）
 */
export function registerConversationCapture(api, client, cfg) {
    api.on("agent_end", async (event, ctx) => {
        /* heartbeat run 不含真实用户对话，跳过 capture */
        if (ctx.trigger === "heartbeat")
            return;
        const userId = deriveUserId(ctx, cfg.userId);
        if (!event.success || !event.messages || event.messages.length === 0) {
            api.logger.info?.(`memory-celia: [capture] skip — success=${event.success}, msgs=${event.messages?.length ?? 0}`);
            return;
        }
        try {
            /* 找到最后一个 user 消息的索引，作为本轮对话的起点 */
            const msgs = event.messages;
            let lastUserIdx = -1;
            for (let i = msgs.length - 1; i >= 0; i--) {
                if (msgs[i]?.role === "user") {
                    lastUserIdx = i;
                    break;
                }
            }
            if (lastUserIdx === -1)
                return;
            /* /new 或 /reset 触发的 Session Startup 序列是框架元操作，
             * 不含用户真实意图，整轮跳过。 */
            const lastUserMsg = msgs[lastUserIdx];
            const lastUserRaw = (() => {
                const c = lastUserMsg.content;
                if (typeof c === "string")
                    return c;
                if (Array.isArray(c)) {
                    for (const b of c) {
                        if (b?.type === "text" && typeof b.text === "string") {
                            return b.text;
                        }
                    }
                }
                return "";
            })();
            if (/session.*(started|reset).*\/(new|reset)/i
                .test(lastUserRaw.slice(0, 200))
                || /Run your Session Startup sequence/i
                    .test(lastUserRaw.slice(0, 200))) {
                api.logger.info?.("memory-celia: [capture] skip — session startup sequence");
                return;
            }
            /* 清洗策略（2026-04-17 调整）：
             *   - user: 只保留 text，过 sanitizeUserTextForCapture。
             *   - assistant: 保留 content blocks 里的 text / thinking / toolCall
             *     三类（thinking 对后续 TSE/Fb/TP 抽取有上下文价值）。
             *     但 thinking block 的 thinkingSignature（Anthropic 加密签名，
             *     base64 几 KB，对学习无用）一律剥离。丢弃所有 assistant 顶层
             *     元数据：api / provider / model / usage / cost / stopReason /
             *     responseId / timestamp 等——它们单条能占几十 KB。
             *   - toolResult: 整条丢弃。text 太大（单条动辄几 KB 的日志），
             *     且语义上只是对话上下文的副产物，不进 mem_conversation。
             *     记忆工具（MEMORY_TOOLS）本来就要丢以避免自引用。
             *   - 其他 role（system 等）丢弃。
             */
            const KEEP_ASSISTANT_BLOCK_TYPES = new Set([
                "text",
                "thinking",
                "toolCall",
            ]);
            const sanitizeAssistantBlocks = (content) => {
                if (!Array.isArray(content))
                    return null;
                const out = [];
                for (const block of content) {
                    if (!block || typeof block !== "object")
                        continue;
                    const t = String(block.type ?? "");
                    if (!KEEP_ASSISTANT_BLOCK_TYPES.has(t))
                        continue;
                    /* 去掉 arguments 为空的 toolCall（agent 传参失败的废调用） */
                    if (t === "toolCall") {
                        const args = block.arguments;
                        if (args == null
                            || (typeof args === "object"
                                && Object.keys(args).length === 0)) {
                            continue;
                        }
                    }
                    const copy = { ...block };
                    /* 去掉 Anthropic thinking signature（不可读、不可学习）*/
                    if (t === "thinking")
                        delete copy.thinkingSignature;
                    /* toolCall 的 id 仅用于与 toolResult 配对，
                     * toolResult 已整条丢弃，id 无用且浪费 token */
                    if (t === "toolCall")
                        delete copy.id;
                    /* toolCall.arguments 大字段截断（与 toolResult 截 300 对称）：
                     * write 整份文件 / edit 大 hunk / curl POST body 等单字段
                     * 容易 50KB+，下游 procedural pipeline 仅需"前 200 字 + 长度"
                     * 元数据，扔给 LLM 看完整 body 是浪费 token + 撑爆 buffer。
                     * 改用浅拷贝 args 避免污染上游对象。 */
                    if (t === "toolCall") {
                        const rawArgs = copy.arguments;
                        if (rawArgs && typeof rawArgs === "object"
                            && !Array.isArray(rawArgs)) {
                            const MAX_ARG_FIELD = 1024;
                            const trimmedArgs = {};
                            for (const [k, v] of Object.entries(rawArgs)) {
                                if (typeof v === "string" && v.length > MAX_ARG_FIELD) {
                                    trimmedArgs[k] = v.slice(0, 200)
                                        + `…[truncated, original_len=${v.length}]`;
                                }
                                else {
                                    trimmedArgs[k] = v;
                                }
                            }
                            copy.arguments = trimmedArgs;
                        }
                    }
                    out.push(copy);
                }
                return out.length > 0 ? out : null;
            };
            const cleaned = [];
            for (let i = lastUserIdx; i < msgs.length; i++) {
                const m = msgs[i];
                if (!m || typeof m !== "object")
                    continue;
                const role = String(m.role ?? "unknown");
                if (role === "user") {
                    /* 只保留 text block，过 sanitize */
                    const content = m.content;
                    const parts = [];
                    if (typeof content === "string") {
                        parts.push(content);
                    }
                    else if (Array.isArray(content)) {
                        for (const block of content) {
                            if (block?.type === "text" && typeof block.text === "string") {
                                parts.push(block.text);
                            }
                        }
                    }
                    const s = sanitizeUserTextForCapture(parts.join("\n"));
                    if (!s)
                        continue;
                    cleaned.push({ role: "user", content: s });
                }
                else if (role === "assistant") {
                    const blocks = sanitizeAssistantBlocks(m.content);
                    if (!blocks)
                        continue;
                    cleaned.push({ role: "assistant", content: blocks });
                }
                else if (role === "toolResult") {
                    /* 成功的 toolResult 整条丢弃（text 太大，语义贡献低）。
                     * 但报错的 toolResult 保留摘要——记录 agent 的失败尝试，
                     * 对后续 feedback/error-pattern 提取有学习价值。
                     * MEMORY_TOOLS 的结果无论成败都丢弃（避免自引用循环）。 */
                    const mr = m;
                    const tn = String(mr.toolName ?? "");
                    if (MEMORY_TOOLS.has(tn))
                        continue;
                    if (!mr.isError)
                        continue;
                    /* 只保留报错摘要，截断避免过长 */
                    const errContent = mr.content;
                    let errText = "";
                    if (typeof errContent === "string") {
                        errText = errContent;
                    }
                    else if (Array.isArray(errContent)) {
                        for (const b of errContent) {
                            if (b?.type === "text" && typeof b.text === "string") {
                                errText += b.text + "\n";
                            }
                        }
                    }
                    errText = errText.trim();
                    if (!errText)
                        continue;
                    const MAX_ERR_LEN = 300;
                    if (errText.length > MAX_ERR_LEN) {
                        errText = errText.slice(0, MAX_ERR_LEN) + "…(truncated)";
                    }
                    cleaned.push({
                        role: "toolResult",
                        toolName: tn,
                        isError: true,
                        content: errText,
                    });
                }
                /* 其他 role（system 等）一律丢弃 */
            }
            if (cleaned.length === 0)
                return;
            const roundText = JSON.stringify(cleaned);
            /* 始终入队 mem_conversation。LLM 提取由 C 端 worker 在
             * ingest pipeline 阶段处理；不在 TS 层做 gate。 */
            if (!client.isConnected() && !(await client.waitReady())) {
                api.logger.warn?.("memory-celia: capture skipped — server not ready");
                return;
            }
            let toolSessionId;
            try {
                toolSessionId = await ensureToolSession(client, userId);
            }
            catch (err) {
                api.logger.warn?.(`memory-celia: ensureToolSession failed: ${String(err)}`);
                return;
            }
            const tenantId = cfg.tenantId ?? "default";
            const conversationId = ctx.sessionId ?? userId;
            const hasStoreUrgentIngestValue = consumeStoreUrgentIngest(buildStoreUrgentIngestKey(tenantId, userId, conversationId))
                || consumeStoreUrgentIngest(buildStoreUrgentIngestKey(tenantId, userId, userId))
                || consumeStoreUrgentIngest(`${userId}`);
            const ingestMode = hasStoreUrgentIngestValue
                ? "deferred-urgent"
                : "deferred";
            const memoryState = await readXiaoyiMemoryState();
            api.logger.info?.(`memory-celia: [capture] last round ${cleaned.length} messages, ` +
                `ingestMode=${ingestMode} `
                + `memoryState=${memoryState} `
                + `storeUrgentIngest=${hasStoreUrgentIngestValue} ` +
                `text="${roundText.slice(0, 120)}"`);
            /* B6'：本次 capture 的 trace_id；MCP/C 层落 mem_conversation.trace_id
             * 列，跨 TS/MCP/C 三层日志可 grep 关联。 */
            const captureTraceId = genTraceId();
            const addParams = {
                tenant_id: tenantId,
                content: roundText,
                userId,
                scope: "user",
                sessionId: toolSessionId,
                conversationId: ctx.sessionId ?? "",
                ingestMode,
                memoryState,
                _trace_id: captureTraceId,
            };
            api.logger.info?.(`${logPrefix("capture", captureTraceId)} memory_add params: ${JSON.stringify(addParams)}`);
            const result = await client.callTool("memory_add", addParams);
            api.logger.info(`memory-celia: [capture] → ${JSON.stringify(result)}`);
            /* 标 dirty：本轮对话已捕获，下次 before_prompt_build 强制刷
             * L0/L1（即便 TTL 未到期）。这是用户感知"刚说的话立刻反映在
             * 固定加载里"的关键路径。 */
            markFixedLoadDirty(tenantId, userId);
            /* 清洗后消息日志，与实际存入内容一致 */
            for (let j = 0; j < cleaned.length; j++) {
                api.logger.info?.(`memory-celia: [capture-cleaned] [${j}] ${JSON.stringify(cleaned[j])}`);
            }
        }
        catch (err) {
            api.logger.warn(`memory-celia: capture failed: ${String(err)}`);
        }
    });
}
// ============================================================================
// Token stats DFX — per-round usage reporting
// ============================================================================
/** 每个 session 的 round 计数器。 */
const sessionRounds = new Map();
/** 检索类 memory tool 名称集合（用于统计 toolResult token）。 */
const RECALL_TOOLS = new Set([
    "memory_load", "memory_list",
    "memory_scene_load", "memory_record_search", "memory_chat_history_search",
    "memory_scene_list_load", "memory_get_global_summary",
]);
/**
 * 注册 token 统计钩子：在 agent_end 时直接从 event.messages 提取
 * agent token usage 和 recall 统计，上报到 MCP server。
 */
export function registerTokenStatsCapture(api, client, cfg) {
    api.on("agent_end", async (event, ctx) => {
        if (ctx.trigger === "heartbeat")
            return;
        if (!event.success || !event.messages)
            return;
        if (!client.isConnected() && !(await client.waitReady()))
            return;
        const msgs = event.messages;
        /* 找到最后一个 user 消息，只统计本轮（与 conversation capture 一致） */
        let lastUserIdx = -1;
        for (let i = msgs.length - 1; i >= 0; i--) {
            if (msgs[i]?.role === "user") {
                lastUserIdx = i;
                break;
            }
        }
        if (lastUserIdx === -1)
            return;
        /* ---- 打印本轮完整对话 + token 消耗 ---- */
        for (let i = lastUserIdx; i < msgs.length; i++) {
            const m = msgs[i];
            const role = String(m?.role ?? "unknown");
            const usage = m?.usage;
            const usageStr = usage
                ? ` usage={input:${usage.input ?? 0},` +
                    `output:${usage.output ?? 0},` +
                    `cacheRead:${usage.cacheRead ?? 0},` +
                    `total:${usage.totalTokens ?? 0}}`
                : "";
            const toolName = m?.toolName ? ` tool=${m.toolName}` : "";
            const content = m?.content;
            let preview = "";
            if (typeof content === "string") {
                preview = content.slice(0, 120);
            }
            else if (Array.isArray(content)) {
                preview = content
                    .map((b) => {
                    const t = String(b?.type ?? "?");
                    const txt = String(b?.text ?? b?.thinking ?? "");
                    return `${t}:"${txt.slice(0, 40)}"`;
                })
                    .join(", ");
            }
            api.logger.info?.(`memory-celia: [token-stats] [${i - lastUserIdx}] ` +
                `role=${role}${toolName}${usageStr} ` +
                `${preview.replace(/\n/g, " ")}`);
        }
        /* ---- 阶段一：累加本轮 assistant 消息的 usage ---- */
        let promptTokens = 0;
        let completionTokens = 0;
        let cacheReadTokens = 0;
        let llmTurns = 0;
        let isEstimated = false;
        for (let i = lastUserIdx + 1; i < msgs.length; i++) {
            const m = msgs[i];
            if (m?.role !== "assistant")
                continue;
            const usage = m.usage;
            if (!usage)
                continue;
            llmTurns++;
            promptTokens += usage.input ?? usage.prompt_tokens ?? 0;
            completionTokens += usage.output ?? usage.completion_tokens ?? 0;
            cacheReadTokens += usage.cacheRead ?? usage.cache_read_input_tokens ?? 0;
        }
        /* 兜底：usage 全零时按字符数估算（chars / 1.5 ≈ tokens） */
        if (promptTokens === 0 && completionTokens === 0) {
            isEstimated = true;
            for (let i = lastUserIdx; i < msgs.length; i++) {
                const m = msgs[i];
                const role = String(m?.role ?? "");
                const content = m?.content;
                const text = typeof content === "string"
                    ? content
                    : Array.isArray(content)
                        ? content
                            .filter((b) => b?.type === "text" || b?.type === "thinking")
                            .map((b) => String(b?.text ?? b?.thinking ?? ""))
                            .join("")
                        : "";
                if (!text)
                    continue;
                const tokens = Math.ceil(Buffer.byteLength(text, "utf8") / 2);
                if (role === "user" || role === "toolResult") {
                    promptTokens += tokens;
                }
                else if (role === "assistant") {
                    completionTokens += tokens;
                }
            }
        }
        /* ---- 阶段二：agent 主动检索的 token ---- */
        let recallTokenCount = 0;
        for (let i = lastUserIdx + 1; i < msgs.length; i++) {
            const m = msgs[i];
            if (m?.role !== "toolResult")
                continue;
            const tn = String(m.toolName ?? "");
            if (!RECALL_TOOLS.has(tn))
                continue;
            const content = m.content;
            const text = typeof content === "string"
                ? content
                : Array.isArray(content)
                    ? content
                        .filter((b) => b?.type === "text")
                        .map((b) => String(b?.text ?? ""))
                        .join("")
                    : "";
            recallTokenCount += Math.ceil(Buffer.byteLength(text, "utf8") / 2);
        }
        /* ---- 阶段三：无数据则跳过 ---- */
        if (promptTokens === 0 && completionTokens === 0
            && recallTokenCount === 0) {
            return;
        }
        /* ---- 阶段四：roundIndex + 上报 ---- */
        const userId = deriveUserId(ctx, cfg.userId);
        const sessionId = ctx.sessionId ?? "unknown";
        const prev = sessionRounds.get(sessionId) ?? 0;
        sessionRounds.set(sessionId, prev + 1);
        const roundIndex = prev + 1;
        /* ---- 读取跨 hook 共享的固定加载 token 数（sticky：不 delete） ----
         * runAllSyncs 写入后保持值，下次 agent_end 仍读到同一数；TTL skip
         * 路径不更新值时，本字段继续报告上一次成功 sync 的可见 tokens，
         * 而不是 0。runAllSyncs 重跑（full sync 或 dirty 刷新）会覆盖。 */
        const tenantId = cfg.tenantId ?? "default";
        const exactFixedKey = syncStateKey(tenantId, userId, ctx.workspaceDir);
        const fixedLoadTokens = lastFixedLoadTokens.get(exactFixedKey)
            ?? lastFixedLoadTokens.get(globalSyncStateKey(tenantId, userId))
            ?? 0;
        try {
            const toolSessionId = await ensureToolSession(client, userId);
            await client.callTool("memory_report_round_usage", {
                sessionId: toolSessionId,
                userId,
                roundIndex,
                agentPromptTokens: promptTokens,
                agentCacheReadTokens: cacheReadTokens,
                agentCompletionTokens: completionTokens,
                llmTurns,
                recallTokenCount,
                isEstimated,
                fixedLoadTokens,
            });
            api.logger.info?.(`memory-celia: [token-stats] round=${roundIndex} ` +
                `prompt=${promptTokens} cacheRead=${cacheReadTokens} ` +
                `completion=${completionTokens} llmTurns=${llmTurns} ` +
                `estimated=${isEstimated} ` +
                `recallTokens=${recallTokenCount} ` +
                `fixedLoadTokens=${fixedLoadTokens}`);
        }
        catch (err) {
            api.logger.warn?.(`memory-celia: token stats report failed: ${String(err)}`);
        }
    });
}
// ============================================================================
// Per-user workspace isolation hooks
// ============================================================================
const WORKSPACE_FILES = ["USER.md", "IDENTITY.md"];
const WORKSPACE_STORE = "/app/data/celia_openclaw/workspace";
export function registerServedL1Cleanup(api, cfg) {
    api.on("session_end", (_event, ctx) => {
        const tenant = cfg.tenantId ?? "default";
        const user = deriveUserId(ctx, cfg.userId);
        const conv = ctx?.sessionId ?? "";
        if (!conv)
            return;
        try {
            clearServedL1ForSession(tenant, user, conv);
            clearSessionPromptBuffer(buildSessionPromptBufferKey(tenant, user, conv));
        }
        catch (err) {
            api.logger.warn(`session cleanup failed: ${String(err)}`);
        }
    });
}
export function registerWorkspaceHooks(api) {
    api.on("session_start", (_event, ctx) => {
        const sk = ctx?.sessionKey;
        const marker = "gradio:dm:";
        const idx = sk ? sk.indexOf(marker) : -1;
        if (idx === -1)
            return;
        const userId = sk.slice(idx + marker.length);
        const workspaceDir = ctx.workspaceDir;
        if (!workspaceDir)
            return;
        const userDir = join(WORKSPACE_STORE, userId);
        for (const file of WORKSPACE_FILES) {
            const snapshot = join(userDir, file);
            const live = join(workspaceDir, file);
            try {
                if (existsSync(snapshot)) {
                    writeFileSync(live, readFileSync(snapshot));
                }
                else {
                    writeFileSync(live, "");
                }
            }
            catch (err) {
                api.logger.warn(`workspace: restore ${file} failed: ${String(err)}`);
            }
        }
    });
    /**
     * Cache P1.1 follow-up（deep review FIX-5）：session_end 时立即释放
     * servedL1Paths 中对应 (tenant, user, conversationId) 的 entry——避免
     * 长跑 plugin 进程下 entries 单调累积。LRU 上限是兜底，session_end
     * 显式清理是首选。
     *
     * 兼容性：ctx 字段命名跨 OpenClaw 版本可能漂移；用 unknown + 运行时
     * 取值守护，缺字段时 silent skip 不阻塞主清理路径。
     */
    api.on("session_end", (_event, ctx) => {
        const c = ctx;
        if (!c)
            return;
        const tenant = c.tenantId ?? "";
        const user = c.userId ?? "";
        const conv = c.sessionId ?? "";
        if (tenant || user || conv) {
            try {
                clearServedL1ForSession(tenant, user, conv);
            }
            catch (err) {
                api.logger.warn(`servedL1: cleanup failed: ${String(err)}`);
            }
        }
    });
    api.on("session_end", (_event, ctx) => {
        const sk = ctx?.sessionKey;
        const marker = "gradio:dm:";
        const idx = sk ? sk.indexOf(marker) : -1;
        if (idx === -1)
            return;
        const userId = sk.slice(idx + marker.length);
        const workspaceDir = ctx.workspaceDir;
        if (!workspaceDir)
            return;
        try {
            mkdirSync(join(WORKSPACE_STORE, userId), { recursive: true });
        }
        catch { /* ignore */ }
        for (const file of WORKSPACE_FILES) {
            const live = join(workspaceDir, file);
            const snapshot = join(WORKSPACE_STORE, userId, file);
            try {
                if (existsSync(live)) {
                    const content = readFileSync(live, "utf8");
                    if (content.trim()) {
                        writeFileSync(snapshot, content);
                    }
                }
            }
            catch (err) {
                api.logger.warn(`workspace: save ${file} failed: ${String(err)}`);
            }
        }
    });
}
// ============================================================================
// OpenClaw gateway observer (llm_input)
// ============================================================================
/** progressive-loading 工具名集合（用于 LLM_IN 观察器匹配最近 toolResult） */
const PROGRESSIVE_TOOLS = new Set([
    "memory_scene_load",
    "memory_record_search",
    "memory_chat_history_search",
    "memory_scene_list_load",
]);
/**
 * 监听 llm_input 事件,在最终 prompt 被送往 LLM provider 前观察:
 *  1. system prompt 是否包含 <user-profile> 标签及 L0 长度
 *  2. 最近一次 progressive-loading 工具（memory_scene_load /
 *     memory_record_search / memory_chat_history_search / memory_scene_list_load）
 *     的 toolResult 是否进了消息流
 *  3. 两者的头部预览,便于与 C 层/插件层日志对照
 *
 * 注册此观察器等价于在 openclaw gateway 层加日志,但不改 openclaw 源码。
 */
export function registerGatewayObserver(api) {
    api.on("llm_input", (event, ctx) => {
        const msgs = (event.historyMessages ?? []);
        /* ---- 解析 system prompt ---- */
        const sys = msgs.find((m) => m?.role === "system");
        const sysText = typeof sys?.content === "string"
            ? sys.content
            : Array.isArray(sys?.content)
                ? (sys.content
                    .map((b) => (typeof b?.text === "string" ? b.text : ""))
                    .join("\n"))
                : "";
        const profileMatch = sysText.match(/<user-profile>([\s\S]*?)<\/user-profile>/);
        const hasProfile = !!profileMatch;
        const profileLen = profileMatch ? profileMatch[1].length : 0;
        /* ---- 找最近一条 progressive-loading toolResult ---- */
        let lastProgResult;
        let lastProgTool;
        for (let i = msgs.length - 1; i >= 0; i--) {
            const m = msgs[i];
            const tn = m?.toolName;
            if (m?.role === "toolResult" && typeof tn === "string"
                && PROGRESSIVE_TOOLS.has(tn)) {
                const c = m.content;
                lastProgResult = typeof c === "string"
                    ? c
                    : Array.isArray(c)
                        ? (c
                            .map((b) => (typeof b?.text === "string" ? b.text : ""))
                            .join("\n"))
                        : undefined;
                lastProgTool = tn;
                break;
            }
        }
        /* ---- 汇总日志 ---- */
        api.logger.info(`memory-celia: [LLM_IN] sid=${ctx.sessionId} msgs=${msgs.length} ` +
            `sysLen=${sysText.length} hasUserProfile=${hasProfile} ` +
            `profileLen=${profileLen} ` +
            `lastProgTool=${lastProgTool ?? "none"} ` +
            `progResultLen=${lastProgResult?.length ?? 0}`);
        if (hasProfile && profileLen > 0) {
            api.logger.info(`memory-celia: [LLM_IN] user-profile head="` +
                `${profileMatch[1].slice(0, 300).replace(/\n/g, " ")}"`);
        }
        if (lastProgResult) {
            api.logger.info(`memory-celia: [LLM_IN] ${lastProgTool} tool_result head="` +
                `${lastProgResult.slice(0, 300).replace(/\n/g, " ")}"`);
        }
    });
}
