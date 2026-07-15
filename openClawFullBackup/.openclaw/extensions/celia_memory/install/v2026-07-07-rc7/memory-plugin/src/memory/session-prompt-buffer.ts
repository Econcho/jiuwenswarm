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

export interface SessionPromptBufferItem {
  id: number;
  content: string;
  createdAtMs: number;
}

let nextItemId = 1;
const sessionPromptBuffers =
  new Map<string, SessionPromptBufferItem[]>();

export function buildSessionPromptBufferKey(
  tenantId: string,
  userId: string,
  openclawSessionId: string,
): string {
  return `${tenantId || "default"}:${userId || "anon"}:`
    + `${openclawSessionId || "default"}`;
}

function enforceSessionLimit(): void {
  while (sessionPromptBuffers.size > MAX_SESSIONS) {
    const oldest = sessionPromptBuffers.keys().next().value as
      | string
      | undefined;
    if (!oldest) return;
    sessionPromptBuffers.delete(oldest);
  }
}

function trimContent(content: string): string {
  if (utf8ByteLength(content) <= MAX_ITEM_BYTES) {
    return content;
  }
  return truncateUtf8Safe(content, MAX_ITEM_BYTES);
}

function trimSessionItems(items: SessionPromptBufferItem[]): void {
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

export function appendSessionPromptBuffer(
  key: string,
  content: string,
): SessionPromptBufferItem {
  const trimmed = trimContent(content.trim());
  const item: SessionPromptBufferItem = {
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

export function getSessionPromptBuffer(
  key: string,
): ReadonlyArray<SessionPromptBufferItem> {
  return sessionPromptBuffers.get(key) ?? [];
}

export function clearSessionPromptBuffer(key: string): void {
  sessionPromptBuffers.delete(key);
}

export function clearAllSessionPromptBuffers(): void {
  sessionPromptBuffers.clear();
}

export function _resetSessionPromptBufferForTesting(): void {
  sessionPromptBuffers.clear();
  nextItemId = 1;
}
