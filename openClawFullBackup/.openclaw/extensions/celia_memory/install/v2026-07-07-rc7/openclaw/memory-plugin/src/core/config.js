import { homedir } from "node:os";
import { join } from "node:path";
import { loadCeliaEnv } from "../../../celiaclaw/shared/env-loader.js";
import { resolveWithEnvFallback, resolveJsonOnly, assertAllowedKeys, } from "../../../shared/env-utils.js";
const DEFAULT_DB_PATH = join(homedir(), ".openclaw", "workspace", "memory", "celia_memory.db");
/* embed/chat model 都不在 TS 层兜底——schema 返回空串时让 index.ts 后续
 * 的优先级链有机会填值：
 *   1. plugin config (memory-celia.config.chat)
 *   2. borrow (models.providers.<primary>)
 *   3. env 兜底 (celia SERVICE_URL / openclaw celia_env.json)
 * 最终若仍为空：embed 端由 C mcp_main.c:174 兜底为
 * "text-embedding-3-small"，chat 端由 llm_openai.c:233 fail loud。
 *
 * 历史：早先这里有 const DEFAULT_CHAT_MODEL = "gpt-4o-mini"，schema
 * 在 chat.model 缺省时直接兜底成它——结果 sandbox 上 agents.defaults.
 * model.primary 派生的 xiaoyiprovider/<model> 永远进不来 borrow 链
 * （cfg.chat.model 永远非空，`||` 短路）。跟历史上 embed 那条 BUG 同
 * pattern，已修掉。
 *
 * 2026-06 进一步修复：chat baseUrl/apiKey/model 不再走
 * resolveWithEnvFallback（env 回退会导致 alias 通过 env 短路
 * borrow 链）。改用 resolveJsonOnly，env 兜底由 index.ts 显式
 * 计算为第三档 fallback。 */
/**
 * 解析 sandbox 额外 headers map（embed.headers / chat.headers）。
 *
 * - undefined / null / 非对象 → 返回 undefined（调用方 fall back 借
 *   openclaw.json 的对应 headers）。
 * - object 内 value 必须是字符串；非字符串/空字符串值静默丢弃。
 * - 显式过滤 x-api-key / x-uid（大小写不敏感）—— 它们走 apiKey/uid
 *   专用通道，重复写会被 C 端当作 sandbox 额外槽，污染请求。
 *
 * 返回空对象 `{}` 表示"用户显式给了空 headers"，与 undefined 语义
 * 不同（前者覆盖借用值为空，后者不覆盖）。
 */
function parseHeaderMap(raw, label) {
    if (raw === undefined || raw === null) {
        return undefined;
    }
    if (typeof raw !== "object" || Array.isArray(raw)) {
        throw new Error(`${label} must be an object`);
    }
    const src = raw;
    const out = {};
    for (const [k, v] of Object.entries(src)) {
        const kl = k.toLowerCase();
        if (kl === "x-api-key" || kl === "x-uid") {
            const isEmbed = label.startsWith("embed");
            const sourceHint = kl === "x-api-key"
                ? (isEmbed ? "embed.apiKey" : "chat.apiKey")
                    + ` or ${isEmbed
                        ? 'agents.defaults.memorySearch.remote.headers["x-api-key"]'
                        : 'models.providers.<primary>.headers["x-api-key"]'}`
                : (isEmbed
                    ? 'agents.defaults.memorySearch.remote.headers["x-uid"]'
                    : 'models.providers.<primary>.headers["x-uid"]')
                    + " (auto-borrowed by plugin)";
            throw new Error(`${label}: do not include "${k}" — `
                + `it goes through a dedicated channel. Use ${sourceHint} instead.`);
        }
        if (typeof v === "string" && v.length > 0) {
            out[k] = v;
        }
    }
    return out;
}
/**
 * 解析 dedupPolicy 子对象，做字段级校验：
 *   - enableLineageDedup：必须是 boolean，否则丢弃
 *   - servedL1Decay：必须是 [0, 1] 范围内的有限数，否则丢弃
 *   - 未知子字段：通过 assertAllowedKeys 抛错（由调用方捕获）
 *
 * 返回 undefined 的条件：整个 dedupPolicy 字段缺省 / 非对象。
 * 所有子字段都不合法时仍返回空对象 `{}`，让 tools.ts 的
 * `buildDedupPolicyArg` 走兜底（相当于 undefined 的语义但类型明确）。
 */
function parseDedupPolicy(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
        return undefined;
    }
    const src = raw;
    assertAllowedKeys(src, ["enableLineageDedup", "servedL1Decay"], "dedupPolicy");
    const out = {};
    if (typeof src.enableLineageDedup === "boolean") {
        out.enableLineageDedup = src.enableLineageDedup;
    }
    if (typeof src.servedL1Decay === "number"
        && Number.isFinite(src.servedL1Decay)
        && src.servedL1Decay >= 0
        && src.servedL1Decay <= 1) {
        out.servedL1Decay = src.servedL1Decay;
    }
    return out;
}
export const celiaMemoryConfigSchema = {
    parse(value) {
        loadCeliaEnv();
        if (!value || typeof value !== "object" || Array.isArray(value)) {
            throw new Error("memory-celia config required");
        }
        const cfg = value;
        assertAllowedKeys(cfg, [
            "serverBinaryPath",
            "dbPath",
            "userId",
            "tenantId",
            "vectorDim",
            "embed",
            "chat",
            "rerank",
            "proceduralDir",
            "proceduralLearnDebug",
            "dedupPolicy",
        ], "memory-celia config");
        if (typeof cfg.serverBinaryPath !== "string" || !cfg.serverBinaryPath) {
            throw new Error("serverBinaryPath is required");
        }
        /* ========== embed: 仅 JSON 来源解析 ==========
         * 与 chat/rerank 不同，embed schema 不在此做 process.env 回退——
         * env 兜底由 index.ts 在 memorySearch 之后再处理，确保
         * "agents.defaults.memorySearch.remote" 能在 env 之上插队成为
         * 第二优先级源（与用户"想直接从 openclaw.json 取"的诉求对齐）。
         *
         * - ${FOO} 占位符：严格解析（取不到即抛）。
         * - 非空字面量：原样返回。
         * - undefined / 空串：返回空串（不读 env）。
         *
         * chat 仍保留三层 env fallback（chat 历来从 .xiaoyienv 注入 env，
         * 改链路风险更大；不在本次改动范围）。
         */
        const embed = cfg.embed ?? {};
        const chat = cfg.chat ?? {};
        const rerank = cfg.rerank ?? {};
        assertAllowedKeys(embed, ["baseUrl", "apiKey", "model", "headers"], "embed config");
        assertAllowedKeys(chat, ["baseUrl", "apiKey", "model", "headers"], "chat config");
        assertAllowedKeys(rerank, ["baseUrl", "apiKey", "model"], "rerank config");
        const embedHeaders = parseHeaderMap(embed.headers, "embed.headers");
        const chatHeaders = parseHeaderMap(chat.headers, "chat.headers");
        const embedBaseUrl = resolveJsonOnly(embed.baseUrl);
        const embedApiKey = resolveJsonOnly(embed.apiKey);
        const embedModel = resolveJsonOnly(embed.model);
        const chatBaseUrl = resolveJsonOnly(chat.baseUrl);
        const chatApiKey = resolveJsonOnly(chat.apiKey);
        const chatModel = resolveJsonOnly(chat.model);
        /* rerank 三元组均为可选：任一为空即视为关闭 cross-encoder 重排。
         * 无默认模型名——不同 provider（Jina、Fireworks、Cohere）格式各异，
         * 由用户显式指定或走 provider 默认。 */
        const rerankBaseUrl = resolveWithEnvFallback(rerank.baseUrl, "OPENAI_RERANK_BASE_URL");
        const rerankApiKey = resolveWithEnvFallback(rerank.apiKey, "OPENAI_RERANK_API_KEY");
        const rerankModel = resolveWithEnvFallback(rerank.model, "OPENAI_RERANK_MODEL");
        return {
            serverBinaryPath: cfg.serverBinaryPath,
            dbPath: typeof cfg.dbPath === "string" ? cfg.dbPath : DEFAULT_DB_PATH,
            userId: typeof cfg.userId === "string" ? cfg.userId : "openclaw-user",
            tenantId: typeof cfg.tenantId === "string" && cfg.tenantId
                ? cfg.tenantId
                : undefined,
            vectorDim: typeof cfg.vectorDim === "number" ? cfg.vectorDim : undefined,
            proceduralDir: typeof cfg.proceduralDir === "string" ? cfg.proceduralDir : undefined,
            proceduralLearnDebug: typeof cfg.proceduralLearnDebug === "boolean" ? cfg.proceduralLearnDebug : false,
            embed: {
                baseUrl: embedBaseUrl,
                apiKey: embedApiKey,
                model: embedModel, /* 空串保留——让 index.ts memorySearch 兜底 */
                headers: embedHeaders,
            },
            chat: {
                baseUrl: chatBaseUrl,
                apiKey: chatApiKey,
                model: chatModel, /* 空串保留——让 index.ts borrowed.chat.model 兜底 */
                headers: chatHeaders,
            },
            rerank: {
                baseUrl: rerankBaseUrl,
                apiKey: rerankApiKey,
                model: rerankModel,
            },
            dedupPolicy: parseDedupPolicy(cfg.dedupPolicy),
        };
    },
};
