/**
 * Text sanitization utilities for Celia memory conversation capture.
 *
 * "Strip-down" approach: iteratively remove known metadata envelopes
 * from platform-wrapped user messages, leaving only the original user text.
 *
 * Inspired by OpenViking memory plugin text-utils.
 */
// ============================================================================
// Regex constants — known metadata patterns injected by the platform
// ============================================================================
/** Injected <relevant-memories>…</relevant-memories> blocks from recall */
const RELEVANT_MEMORIES_BLOCK_RE = /<relevant-memories>[\s\S]*?<\/relevant-memories>/gi;
/**
 * OpenViking-style profile/memory context injected by chat.py into user messages.
 * Marker phrase: "以下核心用户信息已固定加载在上下文中" (always present in profile header).
 * These blocks must not be re-captured as new memories.
 */
const OV_PROFILE_CONTEXT_RE = /##\s*\S[^\n]*\n以下核心用户信息已固定加载在上下文中[\s\S]*/i;
/**
 * OpenClaw skill-推荐 prompt wrapper: 上游把 skill 推荐说明 + 检测到的
 * skill 列表整段塞在 user message 头部，再用 "---以下是用户原始XX---"
 * 分隔出真实用户文本。原文样例：
 *
 *   ## 用户查询相关skill列表如下：
 *   ### foo
 *   name: foo-skill
 *   ...
 *   以上是检索到的、与当前查询相关但用户尚未安装的skill...
 *   ---以下是用户原始请求---
 *
 *   {真实用户消息}
 *
 * 入到 mem_conversation 后这块会被误当作"用户说的话"提取记忆——例如
 * "skill 列表 / 推荐规则 1-4" 进 LTM。剥离掉前缀，只保留分隔符之后的
 * 真实用户内容。"原始" 后允许任意中文（请求/查询/消息/...），分隔符
 * 末尾 dashes 数量不固定也兼容。
 */
const OPENCLAW_SKILL_LIST_PREFIX_RE = /##\s*用户查询相关skill列表如下：[\s\S]*?---\s*以下是用户原始[^\n-]*-+\s*\n*/;
/** "Conversation info (untrusted metadata): ```json … ```" envelope */
const CONVERSATION_METADATA_BLOCK_RE = /(?:^|\n)\s*(?:Conversation info|Conversation metadata|会话信息|对话信息)\s*(?:\([^)]+\))?\s*:\s*```[\s\S]*?```/gi;
/** "Sender (untrusted metadata): ```json … ```" envelope */
const SENDER_METADATA_BLOCK_RE = /Sender\s*\([^)]*\)\s*:\s*```[\s\S]*?```/gi;
/** Fenced JSON blocks with ≥3 known metadata keys → probably platform envelope */
const FENCED_JSON_BLOCK_RE = /```json\s*([\s\S]*?)```/gi;
const METADATA_JSON_KEY_RE = /"(session|sessionid|sessionkey|conversationid|channel|sender|userid|agentid|timestamp|timezone|message_id|sender_id)"\s*:/gi;
/** "[message_id: om_xxx]" line prefix */
const LEADING_MESSAGE_ID_RE = /\[message_id:\s*[^\]]+\]\s*/g;
/** OpenClaw channel timestamp prefix, e.g. "[Fri 2026-04-24 14:33 GMT+8] hi" */
const LEADING_OPENCLAW_TIMESTAMP_RE = /^\s*\[[A-Z][a-z]{2}\s+\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}\s+GMT[+-]\d{1,2}\]\s*/;
/** "ou_84b090637e001185dcc1421f4e723cd7: " speaker prefix (multiline: each line) */
const SPEAKER_PREFIX_RE = /^[a-z0-9_]{10,}:\s*/gim;
// ============================================================================
// Core sanitization
// ============================================================================
/**
 * Strip platform metadata envelopes from a raw user message.
 *
 * Works by iteratively removing known metadata patterns (relevant-memories
 * blocks, conversation/sender metadata envelopes, fenced JSON with metadata
 * keys, message-id line prefixes, speaker prefixes) and returning whatever
 * remains — the actual user text.
 */
export function sanitizeUserTextForCapture(raw) {
    let text = raw;
    // 1. Remove null characters
    text = text.replace(/\0/g, "");
    // 2. Remove <relevant-memories> injection blocks
    text = text.replace(RELEVANT_MEMORIES_BLOCK_RE, "");
    // 2b. Remove OpenViking-style profile context blocks injected by chat.py
    text = text.replace(OV_PROFILE_CONTEXT_RE, "");
    // 2c. Strip OpenClaw skill-recommendation wrapper that prefixes user
    //     messages with a long skill catalog + recommendation rules.
    //     Without this, the rules and catalog text leak into LTM as if
    //     the user had typed them.
    text = text.replace(OPENCLAW_SKILL_LIST_PREFIX_RE, "");
    // 3. Remove "Conversation info …: ```json … ```" envelope
    text = text.replace(CONVERSATION_METADATA_BLOCK_RE, "");
    // 4. Remove "Sender (…): ```json … ```" envelope
    text = text.replace(SENDER_METADATA_BLOCK_RE, "");
    // 5. Remove fenced JSON blocks that contain ≥3 metadata keys
    text = text.replace(FENCED_JSON_BLOCK_RE, (match, jsonBody) => {
        const keyMatches = jsonBody.match(METADATA_JSON_KEY_RE);
        return keyMatches && keyMatches.length >= 3 ? "" : match;
    });
    // 6. Remove [message_id: …] line prefixes
    text = text.replace(LEADING_MESSAGE_ID_RE, "");
    // 7. Remove OpenClaw channel timestamp prefix
    text = text.replace(LEADING_OPENCLAW_TIMESTAMP_RE, "");
    // 8. Remove speaker id prefix (e.g. "ou_84b090637e001185: ")
    text = text.replace(SPEAKER_PREFIX_RE, "");
    // 9. Normalize whitespace
    text = text.replace(/\n{3,}/g, "\n\n").trim();
    return text;
}
