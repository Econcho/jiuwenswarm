/**
 * L0 全局摘要 → USER.md 同步器（progressive-loading v1）。
 *
 * 在 `session_start` / `after_compaction` 钩子中调用：
 *   1. 通过 MCP `memory_get_l0_global_summary` 拉取 L0 markdown。
 *   2. 调 `safeWriteMarker(USER.md, MEMORY_OVERVIEW, l0Body)`
 *      写入 marker 区。
 *
 * 失败语义：MCP 拉取失败 → 仅 WARN，不阻塞；safeWriteMarker 内部已具备
 * IO 失败降级。
 *
 * 设计参考：spec §5.3 / §6.4.5。
 */

import * as path from "node:path";
import {
    safeWriteMarker,
    type MarkerWriteResult,
    type SafeWriteCtx,
} from "./safeWriteMarker.js";
import {
    extractL0Body,
    type McpClientLike,
} from "./fixedContext.js";

/** L0 fetch 的 MCP 调用 deadline（毫秒）。 */
const L0_FETCH_DEADLINE_MS = 3000;

/**
 * 通过 MCP 拉取 L0 global_summary markdown 字符串。
 *
 * 返回空串表示用户尚无 L0 内容（合法场景）。
 * 抛错表示 MCP 调用本身失败（caller 应 WARN）。
 */
export async function fetchL0(
    client: McpClientLike,
    tenantId: string,
    userId: string,
    ctx: SafeWriteCtx & { deadlineMs?: number },
): Promise<string> {
    const ms = ctx.deadlineMs ?? L0_FETCH_DEADLINE_MS;
    const traceArgs: Record<string, unknown> = ctx.traceId
        ? { _trace_id: ctx.traceId }
        : {};
    const promise = client.callTool("memory_get_l0_global_summary", {
        userId,
        tenantId,
        ...traceArgs,
    });
    const raw = await Promise.race([
        promise,
        new Promise((_, reject) =>
            setTimeout(
                () => reject(new Error("L0 fetch deadline exceeded")),
                ms,
            ),
        ),
    ]);
    return extractL0Body(raw, ctx.logger);
}

/**
 * 同步 L0 内容到 `<workspaceDir>/USER.md` 顶部 MEMORY_OVERVIEW marker 区。
 *
 * 失败时返回 null（但仍记 WARN 日志）。
 */
export async function syncL0ToUserMd(
    client: McpClientLike,
    workspaceDir: string,
    tenantId: string,
    userId: string,
    ctx: SafeWriteCtx,
): Promise<MarkerWriteResult | null> {
    /* ========== 阶段一：拉取 L0 ========== */
    let l0Body: string;
    try {
        l0Body = await fetchL0(client, tenantId, userId, ctx);
    } catch (err) {
        ctx.logger?.warn?.(
            `memory-celia: [l0Sync] trace=${ctx.traceId} ` +
                `evt=fetch_failed err=${String(err)}`,
        );
        return null;
    }

    /* L0 无正文语义时，safeWriteMarker 会跳过首次 marker 创建。 */
    const targetPath = path.join(workspaceDir, "USER.md");
    return await safeWriteMarker(targetPath, "MEMORY_OVERVIEW", l0Body, ctx);
}
