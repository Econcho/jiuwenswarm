/**
 * MCP 响应解析工具（L0 / L1 index）。
 *
 * 当前仅保留 `extractL0Body` / `extractL1Entries` 两个解析工具，
 * 由 `l0Sync.ts` / `l1ScenesSync.ts` 复用——避免每个 sync 各自重写
 * "raw → typed" 的容错逻辑。
 *
 * 仅保留 `extractL0Body` / `extractL1Entries` 两个解析工具。
 */
// ---- 解析工具 ---------------------------------------------------------------
/**
 * 从 MCP 响应提取 L0 global_summary。
 *
 * 区分四种场景，前三种 warn：
 *   - raw == null              → 调用方应检查 MCP 是否返回空
 *   - global_summary 字段缺失  → 响应结构变了（可能是 C 层改了字段名）
 *   - global_summary 非字符串  → 类型错（可能是 C 层改了返回格式）
 *   - global_summary 真的空串  → 静默返回 ""（用户确实没 L0）
 */
export function extractL0Body(raw, logger) {
    if (raw == null) {
        logger?.warn?.("memory-celia: [fixedContext] L0 response is null/undefined");
        return "";
    }
    if (typeof raw !== "object") {
        logger?.warn?.(`memory-celia: [fixedContext] L0 response is not object; type=${typeof raw}`);
        return "";
    }
    const r = raw;
    if (!("global_summary" in r)) {
        logger?.warn?.(`memory-celia: [fixedContext] L0 missing 'global_summary' field; ` +
            `keys=${Object.keys(r).join(",")}`);
        return "";
    }
    const body = r.global_summary;
    if (typeof body !== "string") {
        logger?.warn?.(`memory-celia: [fixedContext] L0 'global_summary' is not string; ` +
            `type=${typeof body}`);
        return "";
    }
    /* global_summary 真的空串不 warn——这是合法场景（用户无 L0 摘要） */
    return body;
}
/**
 * 从 MCP 响应提取 L1 索引条目数组。
 *
 * 区分四种场景，前三种 warn：
 *   - raw == null         → MCP 返回空
 *   - entries 字段缺失    → 响应结构变了
 *   - entries 非数组      → 类型错
 *   - entries 真的 []     → 静默返回 []（用户确实没 L1）
 */
export function extractL1Entries(raw, logger) {
    if (raw == null) {
        logger?.warn?.("memory-celia: [fixedContext] L1 index response is null/undefined");
        return [];
    }
    if (typeof raw !== "object") {
        logger?.warn?.(`memory-celia: [fixedContext] L1 index response is not object; ` +
            `type=${typeof raw}`);
        return [];
    }
    const r = raw;
    if (!("entries" in r)) {
        logger?.warn?.(`memory-celia: [fixedContext] L1 missing 'entries' field; ` +
            `keys=${Object.keys(r).join(",")}`);
        return [];
    }
    const arr = r.entries;
    if (!Array.isArray(arr)) {
        logger?.warn?.(`memory-celia: [fixedContext] L1 'entries' is not array; ` +
            `type=${typeof arr}`);
        return [];
    }
    return arr;
}
