/**
 * Session 管理：确保 memory tools/hooks 调用前有活跃的 MCP session。
 *
 * MCP server 要求先 memory_open 创建 session handle，后续操作传 sessionId。
 * 此模块为 tools 和 hooks 提供统一的 session 管理，按 userId 维护一个长期 session。
 */
/**
 * Tracks in-flight memory_open promises per sessionId.  Prevents concurrent
 * duplicate memory_open calls when multiple tools fire simultaneously for
 * the same userId.
 */
const pendingOpens = new Map();
const openSessions = new Set();
/**
 * 确保指定 userId 有一个活跃的 MCP session。
 * 幂等：同一 userId 多次调用只触发一次 memory_open。
 * 并发安全：并发调用共享同一个 in-flight promise。
 *
 * @returns sessionId（格式 "tools-{userId}"）
 */
export async function ensureToolSession(client, userId) {
    const sessionId = `tools-${userId}`;
    if (openSessions.has(sessionId))
        return sessionId;
    // Reuse in-flight promise to prevent concurrent duplicate memory_open
    const existing = pendingOpens.get(sessionId);
    if (existing)
        return existing;
    const promise = (async () => {
        await client.callTool("memory_open", { sessionId, userId });
        // Only add if still tracked — clearSessionCache() may have reset.
        if (pendingOpens.has(sessionId)) {
            openSessions.add(sessionId);
        }
        return sessionId;
    })();
    pendingOpens.set(sessionId, promise);
    try {
        return await promise;
    }
    finally {
        pendingOpens.delete(sessionId);
    }
}
/**
 * 清理 session 缓存。server 重启后旧 session 失效，需要重新 memory_open。
 */
export function clearSessionCache() {
    openSessions.clear();
    pendingOpens.clear();
}
