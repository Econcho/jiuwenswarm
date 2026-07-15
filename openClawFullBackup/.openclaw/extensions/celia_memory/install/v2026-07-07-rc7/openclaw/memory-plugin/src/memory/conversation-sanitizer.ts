/**
 * conversation-sanitizer.ts
 *
 * 对话清洗层：在投递 LTM 提取之前，将原始平台消息数组过滤为
 * 纯粹的 user ↔ assistant 直接交流文本。
 *
 * 清洗规则：
 *   - toolResult 消息整条丢弃
 *   - assistant 消息只保留 text block；thinking / toolCall block 剔除。
 *     若 assistant 消息无 text block（纯工具调用），整条丢弃。
 *   - user 消息保留，但经过平台元数据清洗（委托给 sanitizeUserTextForCapture）。
 *   - 其他 role（system 等）整条丢弃。
 *
 * 该模块不依赖任何外部 IO，纯函数，易于单独测试和替换。
 */

import { sanitizeUserTextForCapture } from "./text-utils.js";

// ============================================================================
// Types
// ============================================================================

type MessageLike = Record<string, unknown>;

export type CleanedMessage = {
  role: "user" | "assistant";
  text: string;
};

// ============================================================================
// Internal helpers
// ============================================================================

/**
 * 从消息的 content 字段中提取第一个 text block 的内容。
 * content 可能是字符串或 block 数组；只取 type==="text" 的 block。
 * thinking / toolCall / tool_use / tool_result 等 block 均被忽略。
 */
function extractTextBlock(msg: MessageLike): string | null {
  const content = msg.content;
  if (typeof content === "string") return content || null;

  if (Array.isArray(content)) {
    for (const block of content as Array<Record<string, unknown>>) {
      if (
        block &&
        typeof block === "object" &&
        block.type === "text" &&
        typeof block.text === "string"
      ) {
        return block.text || null;
      }
    }
  }

  return null;
}

// ============================================================================
// Public API
// ============================================================================

/**
 * 清洗原始对话轮次，输出适合 LTM 提取的纯净消息列表。
 *
 * @param messages  原始消息数组（来自 event.messages）
 * @param startIdx  起始索引（含），通常为最后一条 user 消息的位置
 * @returns         清洗后的消息列表（role + 纯文本）
 */
export function sanitizeConversationRound(
  messages: MessageLike[],
  startIdx: number,
): CleanedMessage[] {
  const result: CleanedMessage[] = [];

  for (let i = startIdx; i < messages.length; i++) {
    const m = messages[i];
    if (!m || typeof m !== "object") continue;

    const role = m.role as string;

    /* toolResult 整条丢弃 */
    if (role === "toolResult") continue;

    /* 只处理 user / assistant */
    if (role !== "user" && role !== "assistant") continue;

    /* 提取 text block（thinking / toolCall 被隐式跳过） */
    const raw = extractTextBlock(m);
    if (!raw) continue; /* 纯 tool call 的 assistant 消息，整条丢弃 */

    if (role === "user") {
      const cleaned = sanitizeUserTextForCapture(raw);
      if (!cleaned) continue;
      result.push({ role: "user", text: cleaned });
    } else {
      result.push({ role: "assistant", text: raw });
    }
  }

  return result;
}

/**
 * 将清洗后的消息列表格式化为供 LTM 提取的对话窗口字符串。
 *
 * 格式：每行 "User: ..." 或 "Assistant: ..."，行间以换行分隔。
 */
export function formatCleanedRound(messages: CleanedMessage[]): string {
  return messages
    .map((m) => `${m.role === "user" ? "User" : "Assistant"}: ${m.text}`)
    .join("\n");
}
