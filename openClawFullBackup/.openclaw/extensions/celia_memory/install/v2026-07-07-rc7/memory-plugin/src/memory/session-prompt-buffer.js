/**
 * Session prompt buffer runtime store.
 *
 * This module keeps session-scoped prompt guidance in the OpenClaw gateway
 * process. It is intentionally not persisted to SQLite, markdown, or the MCP
 * server process. Restarting the gateway or ending a session drops the data.
 */
import { truncateUtf8Safe, utf8ByteLength } from "./markerProtocol.js";
const MAX_ITEMS_PER_SESSION = 16;
const MAX_ITEM_BYTES = 800;
const MAX_TOTAL_BYTES = 4_000;
const MAX_SESSIONS = 1_024;
let nextItemId = 1;
const sessionPromptBuffers = new Map();
export function buildSessionPromptBufferKey(tenantId, userId, openclawSessionId) {
    return `${tenantId || "default"}:${userId || "anon"}:`
        + `${openclawSessionId || "default"}`;
}
function enforceSessionLimit() {
    while (sessionPromptBuffers.size > MAX_SESSIONS) {
        const oldest = sessionPromptBuffers.keys().next().value;
        if (!oldest)
            return;
        sessionPromptBuffers.delete(oldest);
    }
}
function trimContent(content) {
    if (utf8ByteLength(content) <= MAX_ITEM_BYTES) {
        return content;
    }
    return truncateUtf8Safe(content, MAX_ITEM_BYTES);
}
function trimSessionItems(items) {
    while (items.length > MAX_ITEMS_PER_SESSION) {
        items.shift();
    }
    let totalBytes = 0;
    for (let i = items.length - 1; i >= 0; i--) {
        totalBytes += utf8ByteLength(items[i].content);
        if (totalBytes > MAX_TOTAL_BYTES) {
            items.splice(0, i + 1);
            return;
        }
    }
}
export function appendSessionPromptBuffer(key, content) {
    const trimmed = trimContent(content.trim());
    const item = {
        id: nextItemId++,
        content: trimmed,
        createdAtMs: Date.now(),
    };
    const items = sessionPromptBuffers.get(key) ?? [];
    items.push(item);
    trimSessionItems(items);
    sessionPromptBuffers.delete(key);
    sessionPromptBuffers.set(key, items);
    enforceSessionLimit();
    return item;
}
export function getSessionPromptBuffer(key) {
    return sessionPromptBuffers.get(key) ?? [];
}
export function clearSessionPromptBuffer(key) {
    sessionPromptBuffers.delete(key);
}
export function clearAllSessionPromptBuffers() {
    sessionPromptBuffers.clear();
}
export function _resetSessionPromptBufferForTesting() {
    sessionPromptBuffers.clear();
    nextItemId = 1;
}
