/**
 * TS 插件层 DFX helpers：跨层 trace_id 生成 + 日志前缀规范。
 *
 * 配合 C 层 `src/utils/utl_dfx.c` 的 `UtlDfxGenTraceId` / `UtlDfxInheritOrGenTraceId`
 * 形成 OpenClaw → TS plugin → MCP → C 层四层贯穿的 trace_id 机制。
 *
 * 设计参考：docs/design/specs/2026-04-24-retrieval-dfx.md
 */
import { createHash, randomBytes } from "node:crypto";
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
/**
 * 生成 12 字符 hex trace_id（6 字节随机数）。
 *
 * 与 C 层 `UtlDfxGenTraceId` 输出格式一致，便于跨层 grep。空间足够覆盖
 * 单机请求量 10K/min 场景（48 bit 熵）。
 */
export function genTraceId() {
    return randomBytes(6).toString("hex");
}
/**
 * 构造规范化的 TS 层日志前缀。
 *
 * 约定：`[TS] trace_id=xxx mod=yyy`。各 event 字段（event/dur_ms/...）
 * 由调用方拼接在后。mod 例：`fixedContext` / `mdFileSync` /
 * `tokenStats` / `memory_search_l2` 等。
 */
export function logPrefix(mod, traceId) {
    return `[TS] trace_id=${traceId} mod=${mod}`;
}
/**
 * PII 哈希：把 tenant/user/session 等 ID 脱敏成 "<prefix>_<8hex>"。
 *
 * 与 C 层 `UtlDfxFormatScopedId` 算法一致（FNV-1a 32-bit），保证
 * 同一 rawId 在两层日志里输出相同的脱敏串，便于关联。
 *
 * 空输入回落为 `"<prefix>_anon"`。
 */
export function formatScopedId(prefix, rawId) {
    const p = prefix || "x";
    if (!rawId)
        return `${p}_anon`;
    /* FNV-1a 32-bit，保持和 C 实现一致 */
    let h = 2166136261;
    for (let i = 0; i < rawId.length; i++) {
        h ^= rawId.charCodeAt(i) & 0xff;
        h = Math.imul(h, 16777619) >>> 0;
    }
    return `${p}_${h.toString(16).padStart(8, "0")}`;
}
/** 含 query 字段的事件默认对该字段做 sha256 脱敏。 */
const PII_FIELDS = new Set(["query", "q"]);
/** 是否允许 PII 原文落盘（默认否）。 */
function piiEnabled() {
    return process.env.DFX_PII === "1";
}
/** NDJSON 文件 sink 路径（默认空 = 不落盘）。 */
function dfxFileSink() {
    return process.env.CELIA_DFX_FILE ?? "";
}
/**
 * 把 query/q 字段脱敏：原文 → `<k>_hash` (sha256:8hex) + `<k>_chars` (length)。
 *
 * 其他字段透传。DFX_PII=1 时原文透传不脱敏。
 */
function sanitizePIIFields(fields) {
    if (piiEnabled())
        return fields;
    const out = {};
    for (const [k, v] of Object.entries(fields)) {
        if (PII_FIELDS.has(k) && typeof v === "string") {
            const hash = createHash("sha256")
                .update(v, "utf8")
                .digest("hex")
                .slice(0, 8);
            out[`${k}_hash`] = `sha256:${hash}`;
            out[`${k}_chars`] = v.length;
        }
        else {
            out[k] = v;
        }
    }
    return out;
}
/**
 * 把字段值序列化为 `key=value` 单元；含空格的字符串加引号。
 *
 * 用于人类可读单行 `[DFX] evt k=v k=v ...`。
 */
function formatFieldValue(v) {
    if (v === null || v === undefined)
        return "null";
    if (typeof v === "string") {
        return /[\s"]/.test(v) ? `"${v.replace(/"/g, '\\"')}"` : v;
    }
    if (typeof v === "number" || typeof v === "boolean")
        return String(v);
    return JSON.stringify(v);
}
/**
 * 发射一条结构化 DFX 事件。
 *
 * 人类可读单行通过 ctx.logger.info 输出（始终启用，便于实时 grep）；
 * NDJSON 文件落盘按 CELIA_DFX_FILE 环境变量决定是否启用。
 *
 * 失败语义：sink IO 失败被 swallow，永不抛出，避免污染调用链。
 *
 * @param ctx     trace_id + 可选 logger
 * @param evt     事件名（约定 `<subsystem>.<action>`，如 `fixedLoad.write`）
 * @param fields  结构化字段对象；query/q 默认脱敏（除非 DFX_PII=1）
 */
export function emitDFX(ctx, evt, fields = {}) {
    const sanitized = sanitizePIIFields(fields);
    /* 人类可读单行 */
    const kv = Object.entries(sanitized)
        .map(([k, v]) => `${k}=${formatFieldValue(v)}`)
        .join(" ");
    const humanLine = `[DFX] ${evt} trace=${ctx.traceId}${kv ? " " + kv : ""}`;
    ctx.logger?.info?.(humanLine);
    /* NDJSON 文件落盘（opt-in） */
    const sinkPath = dfxFileSink();
    if (sinkPath) {
        const event = {
            ts: new Date().toISOString(),
            trace: ctx.traceId,
            layer: "ts",
            evt,
            fields: sanitized,
        };
        try {
            mkdirSync(dirname(sinkPath), { recursive: true });
            appendFileSync(sinkPath, JSON.stringify(event) + "\n", "utf8");
        }
        catch {
            /* sink 失败 swallow，不污染调用链 */
        }
    }
}
