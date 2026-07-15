/**
 * 跨模块信号：memory_store → agent_end hook 通信。
 *
 * memory_store 被调用时设置信号，agent_end hook 读取后决定 ingestMode：
 *  - 有信号 → deferred-urgent（立即唤醒 Worker）
 *  - 无信号 → deferred（worker 5min / 10 条阈值定时处理）
 *
 * StoreUrgentIngest 是会话级内存标记，不保存文本内容，只表示当前
 * session 在 agent_end 时应使用 deferred-urgent 进入长期记忆抽取链路。
 */

const storeUrgentIngest = new Set<string>();

/** 构造 StoreUrgentIngest key（tenant:user:conversation 三元组）。 */
export function buildStoreUrgentIngestKey(
  tenantId: string,
  userId: string,
  conversationId: string,
): string {
  return `${tenantId || "default"}:${userId || "anon"}:${conversationId || "default"}`;
}

/** memory_store 调用时标记本轮需要加急长期抽取。 */
export function markStoreUrgentIngest(sessionKey: string): void {
  storeUrgentIngest.add(sessionKey);
}

/** agent_end 只检查 urgent ingest 标记，不消费。 */
export function hasStoreUrgentIngest(sessionKey: string): boolean {
  return storeUrgentIngest.has(sessionKey);
}

/** agent_end 检查并消费 urgent ingest 标记（一次性）。 */
export function consumeStoreUrgentIngest(sessionKey: string): boolean {
  return storeUrgentIngest.delete(sessionKey);
}

export function _resetStoreUrgentIngestForTesting(): void {
  storeUrgentIngest.clear();
}
