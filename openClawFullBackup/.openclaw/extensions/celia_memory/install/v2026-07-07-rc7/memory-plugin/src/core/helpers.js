/**
 * 共享辅助函数：转义、格式化、用户 ID 解析。
 */
// ============================================================================
// Prompt escape for memory injection
// ============================================================================
const PROMPT_ESCAPE_MAP = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
};
export function escapeMemoryForPrompt(text) {
    return text.replace(/[&<>"']/g, (c) => PROMPT_ESCAPE_MAP[c] ?? c);
}
// ============================================================================
// Format memories for context injection
// ============================================================================
export function formatContextSection(records) {
    const lines = records.map((r) => {
        const text = r.content ?? "(no content)";
        const score = Math.round(r.score * 100);
        return `- [score=${score}%] ${escapeMemoryForPrompt(text)}`;
    });
    return ["### Relevant Context", ...lines].join("\n");
}
// ============================================================================
// Derive per-user userId from openclaw session key
// ============================================================================
export function deriveUserId(ctx, fallback) {
    const sk = ctx?.sessionKey;
    if (sk) {
        const MARKER = "gradio:dm:";
        const idx = sk.indexOf(MARKER);
        if (idx !== -1)
            return sk.slice(idx + MARKER.length);
    }
    return fallback;
}
