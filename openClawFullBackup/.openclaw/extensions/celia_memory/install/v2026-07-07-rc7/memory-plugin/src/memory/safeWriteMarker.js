/**
 * safeWriteMarker —— 记忆插件 marker 区原子写入工具。
 *
 * 把"内容"写入指定 MD 文件的 <NAME>_BEGIN/END marker 区，
 * 严格遵守字符预算合约，除迁移清理旧 marker 外不修改用户内容。
 *
 * 设计要点（详见 spec §6.4.3 / §6.4.4）：
 *   1. 预算自校验：超 budget 时由插件自截断 / 等比缩短，绝不让
 *      OpenClaw 的 head 75% / tail 25% 截断介入（否则会破坏 marker 边界）。
 *   2. Lockfile 串行化：基于 mkdir 的目录锁，跨进程互斥。
 *   3. 原子写：tmp + rename，POSIX rename 原子语义。
 *   4. Hash 幂等：hash 一致直接 skip，不写盘。
 *   5. 错误降级：任何 IO/解析异常仅 WARN，返回带 action 的结果，
 *      不抛出，不阻塞调用方（典型在 plugin hook 中调用，必须不阻塞 LLM）。
 *
 * 路径白名单：调用方必须保证 filePath 在 workspaceDir 内（外部约束）。
 */
import * as fsp from "node:fs/promises";
import * as path from "node:path";
import { hasLegacyMarkerBlocks, parseMarker, removeMarkerBlocks, replaceOrInjectMarker, sha256Short, truncateUtf8Readable, truncateUtf8Safe, utf8ByteLength, } from "./markerProtocol.js";
import { emitDFX } from "../core/dfx.js";
// ---- 字符预算常量 -----------------------------------------------------------
/** 各 marker 的硬上限字节数（详见 spec §6.4.3）。 */
export const MARKER_BUDGETS = {
    /* MEMORY.md：插件总占用 ≤ 10 KB，留 ≥ 2 KB 给用户 */
    MEMORY_OVERVIEW: 4 * 1024,
    MEMORY_SCENES: 6 * 1024,
    /* AGENTS.md：仅 GUIDE，插件总占用 ≤ 1.5 KB，留 ≥ 10 KB 给用户 */
    MEMORY_GUIDE: Math.floor(1.5 * 1024),
};
/** OpenClaw 单 bootstrap 文件字符上限（见 system-prompt 的 12k 截断阈值）。 */
export const FILE_TOTAL_LIMIT = 12 * 1000;
/**
 * 单条 summary 渲染的字节上限（UTF-8）。
 *
 * 仅在 MEMORY_SCENES 总预算超限后，由 trim 策略作为每条 summary 的
 * 缩短上限。默认渲染直接使用 C 层 L1_index.json 给出的 brief summary，
 * 避免把正常摘要提前截断成半句。
 *
 * Cache P1.2 (rollout-plan §3): 400 → 250。trim 触发时单条更紧凑，能在
 * 同 6KB MEMORY_SCENES budget 容纳更多 scenes（典型场景 20 → 30+）。
 * Soft floor 100B 不变；典型场景下 trim 不触发，硬 cap 仅在等比缩短分母
 * 较大时生效。
 *
 * 历史值 400 是 C 层 L1 doc summary 字段设计上限（spec §6.4.3）；本常量
 * 仅约束渲染到 MEMORY_SCENES marker 的截断行为，不影响 C 层 L1 文件
 * 内容存储。
 */
export const PER_SUMMARY_HARD_CAP = 250;
/** 等比缩短的 per-summary 下限；低于此值触发 starvation warn。 */
export const PER_SUMMARY_SOFT_FLOOR = 100;
/** marker 框架（BEGIN/END 行）保守预留字节数。 */
const MARKER_FRAME_RESERVE = 200;
/** L0 列表因预算删尾时追加的可见截断标记。 */
const L0_LIST_TRUNCATION_LINE = "- ...";
const MEMORY_CHANGE_LOG_PATH = "/home/sandbox/.openclaw/.memory.log";
const MEMORY_ACTION_LOG_PATH = "/home/sandbox/.openclaw/logs/celia_memory/celia_memory.log";
const MEMORY_LOG_ROOT = "/home/sandbox/.openclaw";
const MEMORY_CHANGE_LOG_FILES = new Set(["memory.md", "user.md"]);
const MEMORY_CONSTRAINT_CHANGE_DESC = "更新了规避约束信息";
const MEMORY_PROFILE_CHANGE_DESC = "更新了用户画像";
const MEMORY_PREFERENCE_CHANGE_DESC = "更新了偏好信息";
const MEMORY_INTENT_CHANGE_DESC = "更新了待办/意图";
const MEMORY_SCENES_CHANGE_DESC = "更新了高频场景信息";
function pad2(n) {
    return String(n).padStart(2, "0");
}
function formatLocalTimestamp(now = new Date()) {
    return `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-` +
        `${pad2(now.getDate())}T${pad2(now.getHours())}:` +
        `${pad2(now.getMinutes())}:${pad2(now.getSeconds())}`;
}
function memoryChangeDesc(markerName) {
    if (markerName === "MEMORY_SCENES") {
        return MEMORY_SCENES_CHANGE_DESC;
    }
    return null;
}
function shouldCleanMigratedOverview(filePath, markerNames) {
    const markerSet = new Set(markerNames);
    return path.basename(filePath).toLowerCase() === "memory.md" &&
        markerSet.has("MEMORY_SCENES") &&
        !markerSet.has("MEMORY_OVERVIEW");
}
function cleanupMigratedOverview(filePath, content, markerNames) {
    if (!shouldCleanMigratedOverview(filePath, markerNames)) {
        return {
            content,
            changed: false,
        };
    }
    const cleaned = removeMarkerBlocks(content, "MEMORY_OVERVIEW");
    return {
        content: cleaned,
        changed: cleaned !== content,
    };
}
function prepareMdFileForWrite(filePath, content, markerNames) {
    const cleanup = cleanupMigratedOverview(filePath, content, markerNames);
    const normalized = normalizeManagedMarkerTails(filePath, cleanup.content, markerNames);
    return {
        content: normalized.content,
        changed: cleanup.changed || normalized.changed,
    };
}
function shouldKeepMarkerAtFileTail(filePath, markerName) {
    const baseName = path.basename(filePath).toLowerCase();
    return (baseName === "user.md" &&
        markerName === "MEMORY_OVERVIEW") || (baseName === "memory.md" &&
        markerName === "MEMORY_SCENES");
}
function trimMovedTailContent(content) {
    return content
        .replace(/^(?:[ \t]*\r?\n)+/, "")
        .replace(/[ \t\r\n]+$/, "");
}
function assembleMarkerTailNormalization(before, movedTail, markerBlock) {
    const sections = [
        before.replace(/[ \t\r\n]+$/, ""),
        movedTail,
        markerBlock.replace(/[ \t\r\n]+$/, ""),
    ].filter((section) => section.length > 0);
    return `${sections.join("\n\n")}\n`;
}
function normalizeManagedMarkerTail(filePath, content, markerName) {
    if (!shouldKeepMarkerAtFileTail(filePath, markerName)) {
        return { content, changed: false };
    }
    const parsed = parseMarker(content, markerName);
    if (parsed.primary === null ||
        parsed.broken ||
        parsed.orphans.length > 0) {
        return { content, changed: false };
    }
    const marker = parsed.primary;
    const tail = content.slice(marker.range.end);
    if (tail.trim().length === 0) {
        return { content, changed: false };
    }
    const movedTail = trimMovedTailContent(tail);
    if (movedTail.trim().length === 0) {
        return { content, changed: false };
    }
    const before = content.slice(0, marker.range.start);
    const markerBlock = content.slice(marker.range.start, marker.range.end);
    return {
        content: assembleMarkerTailNormalization(before, movedTail, markerBlock),
        changed: true,
    };
}
function normalizeManagedMarkerTails(filePath, content, markerNames) {
    let current = content;
    let changed = false;
    for (const markerName of markerNames) {
        const normalized = normalizeManagedMarkerTail(filePath, current, markerName);
        current = normalized.content;
        changed = changed || normalized.changed;
    }
    return { content: current, changed };
}
async function appendMemoryChangeDescriptions(filePath, descriptions, ctx) {
    const changeFile = path.basename(filePath).toLowerCase();
    if (!MEMORY_CHANGE_LOG_FILES.has(changeFile)) {
        return;
    }
    const logPath = resolveMemoryLogPath(filePath, ctx.memoryChangeLogPath, MEMORY_CHANGE_LOG_PATH);
    if (logPath === null) {
        return;
    }
    const ts = formatLocalTimestamp();
    const lines = descriptions
        .filter((desc) => desc.length > 0)
        .map((desc) => `${ts}|${changeFile}|${desc}\n`);
    if (lines.length === 0) {
        return;
    }
    await appendLogLines(logPath, lines, "change_log", ctx);
}
function resolveMemoryLogPath(filePath, overridePath, defaultPath) {
    if (overridePath === null) {
        return null;
    }
    if (overridePath !== undefined) {
        return overridePath;
    }
    const resolvedFile = path.resolve(filePath);
    const resolvedRoot = path.resolve(MEMORY_LOG_ROOT) + path.sep;
    if (!resolvedFile.startsWith(resolvedRoot)) {
        return null;
    }
    return defaultPath;
}
async function appendLogLines(logPath, lines, logKind, ctx) {
    try {
        await fsp.mkdir(path.dirname(logPath), { recursive: true });
        await fsp.appendFile(logPath, lines.join(""), "utf8");
    }
    catch (err) {
        ctx.logger?.warn?.(`memory-celia: [safeWriteMarker] WARN ${logKind}_failed ` +
            `file=${logPath} err=${String(err)}`);
        emitDFX(ctx, `fixedLoad.${logKind}_failed`, {
            file: path.basename(logPath),
            err: String(err),
        });
    }
}
function appendContentChangeDescriptions(filePath, descriptions, reason, ctx) {
    if (reason !== "semantic_change") {
        return Promise.resolve();
    }
    return appendMemoryChangeDescriptions(filePath, descriptions, ctx);
}
function markerActionName(action, reason) {
    if (reason === "remove_empty") {
        return "marker.delete";
    }
    if (reason === "repair") {
        return "marker.repair";
    }
    if (action === "trim+create") {
        return "marker.content.create.trim";
    }
    if (action === "trim+update") {
        return "marker.content.update.trim";
    }
    return action === "create" ? "marker.content.create" :
        "marker.content.update";
}
function markerActionDescription(markerName, action, reason, bytes, hash) {
    const actionName = markerActionName(action, reason);
    const reasonPart = reason === "remove_empty" ? " reason=empty" :
        reason === "repair" ? " reason=repair" : "";
    const hashPart = hash.length > 0 ? ` hash=${hash.slice(0, 19)}` : "";
    return `action=${actionName} marker=${markerName}${reasonPart} ` +
        `bytes=${bytes}${hashPart}`;
}
function fileActionDescription(action, reason) {
    return `action=file.${action} reason=${reason}`;
}
async function appendMemoryActionDescriptions(filePath, descriptions, ctx) {
    const changeFile = path.basename(filePath).toLowerCase();
    if (!MEMORY_CHANGE_LOG_FILES.has(changeFile)) {
        return;
    }
    const logPath = resolveMemoryLogPath(filePath, ctx.memoryActionLogPath, MEMORY_ACTION_LOG_PATH);
    if (logPath === null) {
        return;
    }
    const ts = formatLocalTimestamp();
    const lines = descriptions
        .filter((desc) => desc.length > 0)
        .map((desc) => `${ts}|INFO|${changeFile}|${desc}\n`);
    if (lines.length === 0) {
        return;
    }
    await appendLogLines(logPath, lines, "action_log", ctx);
}
function markerActionReason(hasSemanticContent, removeMarker, semanticChanged, changeDescriptions) {
    if (!hasSemanticContent || removeMarker) {
        return "remove_empty";
    }
    if (semanticChanged || changeDescriptions.length > 0) {
        return "semantic_change";
    }
    return "repair";
}
function isPlaceholderSemanticLine(line) {
    let text = line.trim().replace(/^[-*]\s*/, "");
    text = text.replace(/^\[[^\]]+\]\s*/, "").trim();
    const lowered = text.toLowerCase();
    return lowered === "" ||
        lowered === "(empty)" ||
        lowered === "empty" ||
        lowered === "none" ||
        lowered === "n/a" ||
        lowered === "no memory" ||
        lowered === "no memories" ||
        lowered === "no memory yet" ||
        text === "占位" ||
        text === "无" ||
        text === "暂无" ||
        text === "暂无内容" ||
        text === "暂无记忆";
}
function normalizeMarkerSemanticBody(body) {
    return body
        .replace(/\r\n/g, "\n")
        .split("\n")
        .map((line) => line.trim())
        .filter((line) => line.length > 0)
        .filter((line) => !line.startsWith("#"))
        .filter((line) => !isPlaceholderSemanticLine(line))
        .join("\n")
        .trim();
}
export function hasMarkerSemanticContent(body) {
    return normalizeMarkerSemanticBody(body).length > 0;
}
function markerSemanticChanged(oldBody, newBody) {
    const newSemantic = normalizeMarkerSemanticBody(newBody);
    if (newSemantic.length === 0) {
        return false;
    }
    return normalizeMarkerSemanticBody(oldBody) !== newSemantic;
}
const OVERVIEW_LOG_SECTION_ORDER = [
    "constraints",
    "profile",
    "preferences",
    "intents",
];
const OVERVIEW_LOG_SECTION_DESCS = {
    constraints: MEMORY_CONSTRAINT_CHANGE_DESC,
    profile: MEMORY_PROFILE_CHANGE_DESC,
    preferences: MEMORY_PREFERENCE_CHANGE_DESC,
    intents: MEMORY_INTENT_CHANGE_DESC,
};
const OVERVIEW_LOG_SECTION_TITLES = {
    constraints: [
        "important constraints",
        "重要硬约束",
        "需要优先避开的事项",
        "安全与禁忌",
    ],
    profile: ["user profile", "用户画像"],
    preferences: ["preferences", "用户偏好"],
    intents: ["pending tasks & intents", "待办和意图", "待办/意图"],
};
function overviewHeadingSection(line) {
    const match = /^##\s+(.+?)\s*$/.exec(line.trim());
    if (!match) {
        return null;
    }
    const title = match[1].trim().toLowerCase();
    for (const section of OVERVIEW_LOG_SECTION_ORDER) {
        if (OVERVIEW_LOG_SECTION_TITLES[section].includes(title)) {
            return section;
        }
    }
    return null;
}
function overviewLineSection(line) {
    const text = line.trim();
    if (/^current mood\s*:/i.test(text) || /^当前情绪\s*[:：]/.test(text)) {
        return "profile";
    }
    return null;
}
function extractOverviewLogSections(body) {
    const sections = {
        constraints: "",
        profile: "",
        preferences: "",
        intents: "",
    };
    let currentSection = null;
    for (const rawLine of body.replace(/\r\n/g, "\n").split("\n")) {
        const headingSection = overviewHeadingSection(rawLine);
        if (headingSection !== null) {
            currentSection = headingSection;
            continue;
        }
        if (/^##\s+/.test(rawLine.trim())) {
            currentSection = null;
            continue;
        }
        const lineSection = overviewLineSection(rawLine);
        const targetSection = lineSection ?? currentSection;
        if (targetSection !== null) {
            sections[targetSection] += `${rawLine}\n`;
        }
    }
    return sections;
}
function overviewChangeDescriptions(oldBody, newBody) {
    const oldSections = extractOverviewLogSections(oldBody);
    const newSections = extractOverviewLogSections(newBody);
    const descriptions = [];
    for (const section of OVERVIEW_LOG_SECTION_ORDER) {
        const oldSemantic = normalizeMarkerSemanticBody(oldSections[section]);
        const newSemantic = normalizeMarkerSemanticBody(newSections[section]);
        if (oldSemantic !== newSemantic &&
            (oldSemantic.length > 0 || newSemantic.length > 0)) {
            descriptions.push(OVERVIEW_LOG_SECTION_DESCS[section]);
        }
    }
    return descriptions;
}
function markerChangeDescriptions(markerName, oldBody, newBody) {
    if (markerName === "MEMORY_OVERVIEW") {
        return overviewChangeDescriptions(oldBody, newBody);
    }
    if (!markerSemanticChanged(oldBody, newBody)) {
        return [];
    }
    const desc = memoryChangeDesc(markerName);
    return desc ? [desc] : [];
}
// ---- 渲染：把内容（string 或 LtmSummary[]）转为 marker 区 markdown -----------
/**
 * 把任意输入内容渲染为 marker 区的 markdown 文本。
 *
 * - MEMORY_OVERVIEW、MEMORY_GUIDE：输入即字符串，原样返回（caller 已构造好 markdown）。
 * - MEMORY_SCENES：输入 LtmSummary[]，按 updatedAtMs 倒序渲染为
 *   `- [<id>] <summary>` markdown 列表。
 *
 * Cache P1.2 (rollout-plan §3) 渲染轻量化：
 *   - 去掉每条的 ` `<path>` ` 反引号块（path 在 fixed-load prompt 里没用
 *     到，agent 通过 memory_load_l1 拉取时会拿全 path；但每条 ~25B 占用
 *     marker budget）
 *   - 标题简化为 `# Celia Scenario Memory Summaries`
 *     （原 `## L1 场景摘要（共 N 条…）`，
 *     去掉冗余 metadata，少一行字节）
 *   - 正常路径完整渲染 C 层 brief summary；超预算时按完整句子 /
 *     自然分隔符边界缩短，避免半句话摘要进入 fixed-load。
 */
export function renderContent(name, content, 
/* 可选：每条 summary 字节上限（等比缩短时使用） */
perSummaryCap) {
    if (typeof content === "string")
        return content;
    if (name !== "MEMORY_SCENES") {
        throw new Error(`renderContent: 仅 MEMORY_SCENES 接受 LtmSummary[]（实际 ${name}）`);
    }
    const cap = perSummaryCap ?? PER_SUMMARY_HARD_CAP;
    const sorted = [...content].sort((a, b) => b.updatedAtMs - a.updatedAtMs);
    const lines = ["# Celia Scenario Memory Summaries", ""];
    for (const item of sorted) {
        const source = item.summary ?? "";
        let summary = source;
        if (perSummaryCap !== undefined) {
            summary = cap <= 0 ? "" : truncateUtf8Readable(source, cap);
        }
        lines.push(`- [${item.id}] ${summary}`);
    }
    return lines.join("\n");
}
/**
 * 对超 budget 的内容按 marker 类型应用裁剪策略。
 *
 * - **MEMORY_OVERVIEW**：识别 markdown 区块（## A / ## B / ## C / ## D），
 *   按优先级 A>D>B>C 尾截。**A、D 必保**。
 * - **MEMORY_SCENES**：等比缩短，每条 summary cap = min(PER_SUMMARY_HARD_CAP,
 *   eb / N)（P1.2 起 PER_SUMMARY_HARD_CAP=250；frame 估算 24B/条），
 *   永不丢条目。
 * - **MEMORY_GUIDE**：完全静态，超限抛 Error（CI 单测拦截）。
 */
export function trimByPolicy(name, content, budget) {
    const effective = budget - MARKER_FRAME_RESERVE;
    if (name === "MEMORY_GUIDE") {
        if (typeof content !== "string") {
            throw new Error("MEMORY_GUIDE 输入必须是字符串");
        }
        if (utf8ByteLength(content) > budget) {
            throw new Error(`MEMORY_GUIDE 模板字节数 ${utf8ByteLength(content)} ` +
                `超过预算 ${budget}；请缩减 STATIC_GUIDE_TEMPLATE`);
        }
        return { trimmedContent: content, dropped: [] };
    }
    if (name === "MEMORY_OVERVIEW") {
        if (typeof content !== "string") {
            throw new Error("MEMORY_OVERVIEW 输入必须是字符串");
        }
        return trimL0ByPriority(content, effective);
    }
    /* MEMORY_SCENES */
    if (!Array.isArray(content)) {
        throw new Error("MEMORY_SCENES 输入必须是 LtmSummary[]");
    }
    return trimL1ScenesEqualShrink(content, effective);
}
/**
 * L0 按区块优先级裁剪：A 必保 → D 必保 → B 尾截 → C 尾截。
 *
 * 区块以 "## A " / "## B " / "## C " / "## D " 开头识别（兼容更长的标题）。
 */
function trimL0ByPriority(content, eb) {
    if (utf8ByteLength(content) <= eb) {
        return { trimmedContent: content, dropped: [] };
    }
    /* 拆分到区块 */
    const blocks = splitL0Blocks(content);
    const dropped = [];
    /* 按优先级 ['C','B','D','A']：从尾部开始尝试丢/截 */
    const trimOrder = ["C", "B", "D", "A"];
    for (const blkName of trimOrder) {
        let assembled = assembleL0Blocks(blocks);
        if (utf8ByteLength(assembled) <= eb)
            break;
        const blk = blocks[blkName];
        if (!blk)
            continue;
        if (blkName === "A" || blkName === "D") {
            /* 必保区：仅尾截到当前剩余预算 */
            const otherBytes = utf8ByteLength(assembled) - utf8ByteLength(blk);
            const leftover = Math.max(0, eb - otherBytes);
            blocks[blkName] = truncateUtf8Safe(blk, leftover);
            dropped.push(`${blkName}(truncated)`);
        }
        else {
            /* B/C 区：丢条目（按 markdown list 倒序丢） */
            blocks[blkName] = trimL0ListBlock(blk, eb, assembled, dropped);
            if (blocks[blkName].length === 0) {
                dropped.push(`${blkName}(dropped)`);
            }
        }
        assembled = assembleL0Blocks(blocks);
        if (utf8ByteLength(assembled) <= eb)
            break;
    }
    return {
        trimmedContent: assembleL0Blocks(blocks),
        dropped,
    };
}
/** 把 L0 markdown 拆分为 A/B/C/D 四个区块（其余作 "_" 兜底）。 */
function splitL0Blocks(content) {
    const out = { _: "", A: "", B: "", C: "", D: "" };
    const lines = content.split("\n");
    let cur = "_";
    for (const ln of lines) {
        const m = /^##\s+([A-D])(?:\s|$)/.exec(ln);
        if (m) {
            cur = m[1];
            out[cur] = (out[cur] ? out[cur] + "\n" : "") + ln;
        }
        else {
            out[cur] = (out[cur] ? out[cur] + "\n" : "") + ln;
        }
    }
    return out;
}
function assembleL0Blocks(blocks) {
    return ["_", "A", "B", "C", "D"]
        .map((k) => blocks[k])
        .filter((s) => s && s.length > 0)
        .join("\n");
}
/** B/C 区按 markdown list 倒序丢条目。 */
function trimL0ListBlock(block, eb, assembled, dropped) {
    const lines = block.split("\n");
    const headerEnd = lines.findIndex((ln) => /^[-*+]\s/.test(ln));
    if (headerEnd < 0)
        return block;
    const header = lines.slice(0, headerEnd).join("\n");
    const items = lines.slice(headerEnd);
    let hasTruncatedItems = false;
    /* 从尾部开始丢，直到总长度满足预算 */
    while (items.length > 0) {
        const trial = assembleL0ListWithTruncation(header, items, hasTruncatedItems);
        const tryAssembled = assembled.replace(block, trial); /* 估算 */
        if (utf8ByteLength(tryAssembled) <= eb) {
            return trial;
        }
        items.pop();
        dropped.push("L0_block_item");
        hasTruncatedItems = true;
    }
    const fallback = assembleL0ListWithTruncation(header, items, hasTruncatedItems);
    if (utf8ByteLength(assembled.replace(block, fallback)) <= eb) {
        return fallback;
    }
    return header;
}
function assembleL0ListWithTruncation(header, items, hasTruncatedItems) {
    const out = [];
    if (header) {
        out.push(header);
    }
    out.push(...items);
    if (hasTruncatedItems) {
        out.push(L0_LIST_TRUNCATION_LINE);
    }
    return out.join("\n");
}
/** L1 场景：等比缩短保 ALL。 */
function trimL1ScenesEqualShrink(scenes, eb) {
    const N = scenes.length;
    if (N === 0) {
        return {
            trimmedContent: renderContent("MEMORY_SCENES", scenes),
            dropped: [],
            perSummaryCap: PER_SUMMARY_HARD_CAP,
        };
    }
    /* 估算 N 条目的非 summary 部分（"- [<id>] " 前缀）字节。
     *
     * Cache P1.2 follow-up（deep review FIX-5）：原 80B/条估算与新格式
     * 严重背离——P1.2 渲染从 "- [id] `path` — summary"（含 backtick path
     * ~80B）改为 "- [<id>] <summary>"（~14-16B）。继续用 80B 会让
     * summariesBudget 偏小、perCap 偏低，浪费 ~5x 预算。但保守安全：
     * 不会触发"实际 frame 超 budget 估算"问题。
     *
     * 取值 24B：8 字节 ID 占位 + 6 字节固定字符（"- [", "] "）+ 10 字节
     * 缓冲（emoji/中文 id 等）。N=30 场景下 totalFrame ≈ 720B，相比新
     * 格式实际 ~480B 略保守，留 ~33% 浪费换取边界安全。 */
    const ENTRY_FRAME = 24;
    const totalFrame = N * ENTRY_FRAME + 100; /* 加 heading 兜底 */
    const summariesBudget = Math.max(0, eb - totalFrame);
    const perCap = Math.max(0, Math.min(PER_SUMMARY_HARD_CAP, Math.floor(summariesBudget / N)));
    const trimmedContent = renderContent("MEMORY_SCENES", scenes, perCap);
    return {
        trimmedContent,
        dropped: [],
        perSummaryCap: perCap,
    };
}
/**
 * 基于 mkdir 的目录锁：尝试创建 `<filePath>.lock` 目录，成功即获得锁。
 *
 * mkdir() 在 POSIX 与 Windows 均为原子操作（EEXIST 时失败）。stale 检测
 * 通过 mtime 与 staleMs 比对：超时则强制 rmdir 后重试。
 *
 * 返回 release 函数（必须在 finally 中调用）。
 */
async function lockFile(filePath, opts = { retries: 5, staleMs: 5000 }) {
    const lockPath = `${filePath}.lock`;
    let lastErr = null;
    for (let i = 0; i <= opts.retries; i++) {
        try {
            await fsp.mkdir(lockPath, { recursive: false });
            const release = async () => {
                try {
                    await fsp.rmdir(lockPath);
                }
                catch {
                    /* 已经被 stale 接管或本进程崩溃恢复，忽略 */
                }
            };
            return release;
        }
        catch (err) {
            const e = err;
            if (e.code !== "EEXIST") {
                lastErr = err;
                break;
            }
            /* stale 检测 */
            try {
                const st = await fsp.stat(lockPath);
                if (Date.now() - st.mtimeMs > opts.staleMs) {
                    await fsp.rmdir(lockPath).catch(() => { });
                    continue; /* 不计入退避，直接重试 */
                }
            }
            catch {
                /* 锁刚好被释放，下一轮重试 */
            }
            /* 指数退避 */
            await sleep(Math.min(500, 50 * (1 << i)));
        }
    }
    throw new Error(`lockFile failed after ${opts.retries} retries: ${filePath} ` +
        `(${String(lastErr)})`);
}
function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
}
// ---- ensureUppercaseMd ------------------------------------------------------
/**
 * 保证 workspaceDir 下存在大写命名的 MD 文件（OpenClaw 优先加载大写）。
 *
 * 行为：
 *   - 大写已存在 → 直接返回
 *   - 仅小写存在 → 创建大写空文件，**保留小写不动**（避免破坏用户 git）
 *     + 调 logger.warn 提示用户
 *   - 都不存在 → 创建大写空文件
 *
 * @returns 大写文件的绝对路径
 */
export async function ensureUppercaseMd(workspaceDir, name, ctx) {
    const upper = path.join(workspaceDir, name);
    const lower = path.join(workspaceDir, name.toLowerCase());
    await fsp.mkdir(workspaceDir, { recursive: true });
    const upperExists = await fileExists(upper);
    if (upperExists)
        return upper;
    const lowerExists = await fileExists(lower);
    if (lowerExists) {
        ctx.logger?.warn?.(`memory-celia: [safeWriteMarker] WARN ` +
            `lowercase ${name.toLowerCase()} detected; creating ` +
            `uppercase ${name} alongside (lowercase preserved)`);
        emitDFX(ctx, "fixedLoad.lowercase_md_detected", {
            file: name,
            lowercase: name.toLowerCase(),
        });
    }
    let created = false;
    await fsp.writeFile(upper, "", { flag: "wx" }).then(() => {
        created = true;
    }).catch(async (err) => {
        const e = err;
        if (e.code === "EEXIST")
            return; /* 并发竞争：他人已创建 */
        throw err;
    });
    if (created) {
        await appendMemoryActionDescriptions(upper, [fileActionDescription("create", "ensure_uppercase")], ctx);
    }
    return upper;
}
async function fileExists(p) {
    try {
        await fsp.access(p);
        return true;
    }
    catch {
        return false;
    }
}
// ---- 主入口 safeWriteMarker -------------------------------------------------
/**
 * 把内容安全写入指定 MD 文件的 marker 区。
 *
 * 阶段：
 *   1. 渲染输入为 marker 内容（string / LtmSummary[] → markdown）
 *   2. 字节数自校验：超 budget 调 trimByPolicy
 *   3. 取文件锁
 *   4. 读现有文件 → hash 比对 → 同 hash 即 skip
 *   5. hash 比对与篡改检测
 *   6. replaceOrInjectMarker 替换 marker 区
 *   7. 总尺寸校验（仅 warn，不阻塞）
 *   8. 原子写盘（tmp + rename）
 *   9. 释放锁，返回结果
 *
 * 失败语义：任何 IO/解析异常仅 WARN，返回 action='noop'，不抛。
 */
export async function safeWriteMarker(filePath, markerName, inputContent, ctx) {
    const budget = MARKER_BUDGETS[markerName];
    /* ========== 阶段 1：渲染 + 预算自校验 ========== */
    let renderedContent;
    let trimmedReport;
    try {
        const rendered = renderContent(markerName, inputContent);
        if (utf8ByteLength(rendered) <= budget) {
            renderedContent = rendered;
        }
        else {
            const trim = trimByPolicy(markerName, inputContent, budget);
            renderedContent = trim.trimmedContent;
            trimmedReport = {
                dropped: trim.dropped,
                reason: "over_budget",
                perSummaryCap: trim.perSummaryCap,
            };
            emitDFX(ctx, "fixedLoad.trim", {
                marker: markerName,
                dropped: trim.dropped.length,
                perCap: trim.perSummaryCap ?? null,
            });
            /* 等比缩短低于下限：starvation 警告 */
            if (markerName === "MEMORY_SCENES" &&
                trim.perSummaryCap !== undefined &&
                trim.perSummaryCap < PER_SUMMARY_SOFT_FLOOR) {
                ctx.logger?.warn?.(`memory-celia: [safeWriteMarker] WARN ` +
                    `scene_summary_starvation perCap=` +
                    `${trim.perSummaryCap}B < ${PER_SUMMARY_SOFT_FLOOR}B`);
                emitDFX(ctx, "fixedLoad.scene_starvation", {
                    perCap: trim.perSummaryCap,
                    floor: PER_SUMMARY_SOFT_FLOOR,
                });
            }
        }
    }
    catch (err) {
        ctx.logger?.warn?.(`memory-celia: [safeWriteMarker] WARN render_error ` +
            `marker=${markerName} err=${String(err)}`);
        emitDFX(ctx, "fixedLoad.render_error", {
            marker: markerName,
            err: String(err),
        });
        return {
            action: "noop",
            bytes: 0,
            budget,
            hash: "",
        };
    }
    /* 规范化：去除首尾换行后再 hash + 写盘，确保 hash 与磁盘上的 marker
     * body（buildMarkerBlock 也会 trim）一致，避免误判 markerTampered。 */
    renderedContent = renderedContent.replace(/^\n+/, "").replace(/\n+$/, "");
    const hasSemanticContent = hasMarkerSemanticContent(renderedContent);
    const newHash = sha256Short(renderedContent);
    /* ========== 阶段 2：取锁 ========== */
    let release = null;
    try {
        await fsp.mkdir(path.dirname(filePath), { recursive: true });
        release = await lockFile(filePath);
    }
    catch (err) {
        ctx.logger?.warn?.(`memory-celia: [safeWriteMarker] WARN lock_failed ` +
            `file=${filePath} err=${String(err)}`);
        emitDFX(ctx, "fixedLoad.lock_failed", {
            file: path.basename(filePath),
            err: String(err),
        });
        return {
            action: "noop",
            bytes: utf8ByteLength(renderedContent),
            budget,
            hash: newHash,
        };
    }
    try {
        /* ========== 阶段 3：读现有文件 ========== */
        let existing = "";
        try {
            existing = await fsp.readFile(filePath, "utf8");
        }
        catch {
            /* 文件不存在：视为空 */
        }
        const originalExisting = existing;
        const preparation = prepareMdFileForWrite(filePath, existing, [markerName]);
        existing = preparation.content;
        /* ========== 阶段 5：hash 幂等比对 + 用户篡改检测 ========== */
        const parsed = parseMarker(existing, markerName);
        const oldHash = parsed.primary?.meta.hash ?? "";
        const hasLegacyMarkers = hasLegacyMarkerBlocks(existing);
        /* 验证 marker 内容的 actual hash 与 BEGIN 行声明的 hash 是否一致，
         * 不一致 → 用户在 marker 内做了改动（claimed hash 不可信），
         * 强制走更新路径覆盖回插件版本。 */
        const claimedActualHash = parsed.primary
            ? sha256Short(parsed.primary.content)
            : "";
        const markerTampered = parsed.primary !== null && claimedActualHash !== oldHash;
        if (markerTampered) {
            emitDFX(ctx, "fixedLoad.warn_user_modified_marker", {
                marker: markerName,
                claimed_hash: oldHash.slice(0, 19),
                actual_hash: claimedActualHash.slice(0, 19),
            });
        }
        const emptyRawMerged = hasSemanticContent
            ? ""
            : replaceOrInjectMarker(existing, markerName, "", sha256Short(""));
        const emptyMerged = hasSemanticContent
            ? ""
            : normalizeManagedMarkerTails(filePath, emptyRawMerged, [markerName]).content;
        const emptyCleanupChanged = !hasSemanticContent && emptyMerged !== existing;
        const semanticRawMerged = hasSemanticContent
            ? replaceOrInjectMarker(existing, markerName, renderedContent, newHash)
            : "";
        const semanticMerged = hasSemanticContent
            ? normalizeManagedMarkerTails(filePath, semanticRawMerged, [markerName]).content
            : "";
        const semanticCleanupChanged = hasSemanticContent && semanticMerged !== existing;
        if (!hasSemanticContent &&
            parsed.primary === null &&
            parsed.orphans.length === 0 &&
            !parsed.broken &&
            !preparation.changed &&
            !emptyCleanupChanged) {
            emitDFX(ctx, "fixedLoad.write_skip_empty", {
                marker: markerName,
                bytes: 0,
            });
            return {
                action: "skip",
                bytes: 0,
                budget,
                hash: "",
                trimmed: trimmedReport,
            };
        }
        if (hasSemanticContent &&
            !markerTampered &&
            oldHash === newHash &&
            parsed.orphans.length === 0 &&
            !parsed.broken &&
            !hasLegacyMarkers &&
            !preparation.changed &&
            !semanticCleanupChanged) {
            emitDFX(ctx, "fixedLoad.write_skip", {
                marker: markerName,
                hash: newHash,
                bytes: utf8ByteLength(renderedContent),
            });
            return {
                action: "skip",
                bytes: utf8ByteLength(renderedContent),
                budget,
                hash: newHash,
                trimmed: trimmedReport,
            };
        }
        const semanticChanged = hasSemanticContent
            ? markerSemanticChanged(parsed.primary?.content ?? "", renderedContent)
            : false;
        const changeDescriptions = hasSemanticContent
            ? markerChangeDescriptions(markerName, parsed.primary?.content ?? "", renderedContent)
            : [];
        /* ========== 阶段 6：损坏检测 + rescue 备份 ========== */
        if (parsed.broken && originalExisting.length > 0) {
            const rescuePath = `${filePath}.celia-rescue.${Date.now()}`;
            try {
                await fsp.writeFile(rescuePath, originalExisting, "utf8");
                ctx.logger?.warn?.(`memory-celia: [safeWriteMarker] WARN ` +
                    `marker_broken_rescue original_to=${rescuePath}`);
                emitDFX(ctx, "fixedLoad.marker_broken_rescue", {
                    marker: markerName,
                    rescue_path: path.basename(rescuePath),
                });
            }
            catch (err) {
                ctx.logger?.warn?.(`memory-celia: [safeWriteMarker] rescue ` +
                    `failed: ${String(err)}`);
                emitDFX(ctx, "fixedLoad.rescue_failed", {
                    err: String(err),
                });
            }
        }
        /* 用户改了 marker 内（hash 不一致）→ warn 但不阻塞 */
        if (hasSemanticContent && oldHash !== "" && oldHash !== newHash) {
            emitDFX(ctx, "fixedLoad.marker_updated", {
                marker: markerName,
                old_hash: oldHash.slice(0, 19),
                new_hash: newHash.slice(0, 19),
            });
        }
        /* ========== 阶段 7：replace / inject / remove ========== */
        const merged = hasSemanticContent ? semanticMerged : emptyMerged;
        /* ========== 阶段 8：总尺寸警告（不阻塞） ========== */
        const totalBytes = utf8ByteLength(merged);
        if (totalBytes > FILE_TOTAL_LIMIT) {
            ctx.logger?.warn?.(`memory-celia: [safeWriteMarker] WARN total_over_limit ` +
                `file=${path.basename(filePath)} ` +
                `total=${totalBytes} > ${FILE_TOTAL_LIMIT}`);
            emitDFX(ctx, "fixedLoad.warn_total_over_limit", {
                file: path.basename(filePath),
                total: totalBytes,
                limit: FILE_TOTAL_LIMIT,
            });
        }
        /* ========== 阶段 9：原子写盘 ========== */
        const tmp = `${filePath}.tmp.${process.pid}.${Date.now()}`;
        await fsp.writeFile(tmp, merged, "utf8");
        await fsp.rename(tmp, filePath);
        const baseAction = hasSemanticContent && !parsed.primary ? "create" : "update";
        const action = trimmedReport
            ? (baseAction === "create" ? "trim+create" : "trim+update")
            : baseAction;
        const resultBytes = hasSemanticContent
            ? utf8ByteLength(renderedContent)
            : 0;
        const resultHash = hasSemanticContent ? newHash : "";
        const reason = markerActionReason(hasSemanticContent, !hasSemanticContent, semanticChanged, changeDescriptions);
        await appendContentChangeDescriptions(filePath, changeDescriptions, reason, ctx);
        await appendMemoryActionDescriptions(filePath, [
            markerActionDescription(markerName, action, reason, resultBytes, resultHash),
        ], ctx);
        if (changeDescriptions.length === 0) {
            emitDFX(ctx, "fixedLoad.marker_repaired", {
                marker: markerName,
                action,
            });
        }
        emitDFX(ctx, "fixedLoad.write", {
            marker: markerName,
            action,
            bytes: resultBytes,
            budget,
            ratio: Number((resultBytes / budget).toFixed(2)),
            hash: resultHash.slice(0, 19),
        });
        return {
            action,
            bytes: resultBytes,
            budget,
            hash: resultHash,
            trimmed: trimmedReport,
        };
    }
    catch (err) {
        ctx.logger?.warn?.(`memory-celia: [safeWriteMarker] WARN write_failed ` +
            `file=${filePath} err=${String(err)}`);
        emitDFX(ctx, "fixedLoad.write_failed", {
            file: path.basename(filePath),
            err: String(err),
        });
        return {
            action: "noop",
            bytes: utf8ByteLength(renderedContent),
            budget,
            hash: newHash,
            trimmed: trimmedReport,
        };
    }
    finally {
        if (release)
            await release();
    }
}
/**
 * 把单个 patch 渲染并按预算裁剪，与 safeWriteMarker 阶段 1 行为一致。
 *
 * 失败（render_error / GUIDE 超限抛错）时返回 failed=true 的 stub，
 * 让 batch 主循环跳过此 patch 而不阻塞其他 patch。
 */
function renderPatchForBatch(patch, ctx) {
    const budget = MARKER_BUDGETS[patch.markerName];
    let body = "";
    let trimmedReport;
    try {
        const rendered = renderContent(patch.markerName, patch.inputContent);
        if (utf8ByteLength(rendered) <= budget) {
            body = rendered;
        }
        else {
            const trim = trimByPolicy(patch.markerName, patch.inputContent, budget);
            body = trim.trimmedContent;
            trimmedReport = {
                dropped: trim.dropped,
                reason: "over_budget",
                perSummaryCap: trim.perSummaryCap,
            };
            emitDFX(ctx, "fixedLoad.trim", {
                marker: patch.markerName,
                dropped: trim.dropped.length,
                perCap: trim.perSummaryCap ?? null,
            });
            if (patch.markerName === "MEMORY_SCENES" &&
                trim.perSummaryCap !== undefined &&
                trim.perSummaryCap < PER_SUMMARY_SOFT_FLOOR) {
                ctx.logger?.warn?.(`memory-celia: [safeWriteMarkers] WARN ` +
                    `scene_summary_starvation perCap=` +
                    `${trim.perSummaryCap}B < ${PER_SUMMARY_SOFT_FLOOR}B`);
                emitDFX(ctx, "fixedLoad.scene_starvation", {
                    perCap: trim.perSummaryCap,
                    floor: PER_SUMMARY_SOFT_FLOOR,
                });
            }
        }
    }
    catch (err) {
        ctx.logger?.warn?.(`memory-celia: [safeWriteMarkers] WARN render_error ` +
            `marker=${patch.markerName} err=${String(err)}`);
        emitDFX(ctx, "fixedLoad.render_error", {
            marker: patch.markerName,
            err: String(err),
        });
        return {
            markerName: patch.markerName,
            body: "",
            bytes: 0,
            budget,
            newHash: "",
            failed: true,
            hasSemanticContent: false,
        };
    }
    body = body.replace(/^\n+/, "").replace(/\n+$/, "");
    return {
        markerName: patch.markerName,
        body,
        bytes: utf8ByteLength(body),
        budget,
        newHash: sha256Short(body),
        failed: false,
        hasSemanticContent: hasMarkerSemanticContent(body),
        trimmedReport,
    };
}
/**
 * 给定 existing 文件内容与已渲染 patch，决定是否需要重写本 marker。
 *
 * 与 safeWriteMarker 单点逻辑等价：marker 被篡改、解析 broken / orphans
 * 任一为真则强制 write；否则 hash 匹配即 skip。
 */
function decidePatchWrite(filePath, existing, rendered, ctx) {
    const parsed = parseMarker(existing, rendered.markerName);
    const oldHash = parsed.primary?.meta.hash ?? "";
    const oldBody = parsed.primary?.content ?? "";
    const hasLegacyMarkers = hasLegacyMarkerBlocks(existing);
    const claimedActualHash = parsed.primary
        ? sha256Short(parsed.primary.content)
        : "";
    const semanticChanged = markerSemanticChanged(oldBody, rendered.body);
    const markerTampered = parsed.primary !== null && claimedActualHash !== oldHash;
    if (markerTampered) {
        emitDFX(ctx, "fixedLoad.warn_user_modified_marker", {
            marker: rendered.markerName,
            claimed_hash: oldHash.slice(0, 19),
            actual_hash: claimedActualHash.slice(0, 19),
        });
    }
    const emptyRawMerged = rendered.hasSemanticContent
        ? ""
        : replaceOrInjectMarker(existing, rendered.markerName, "", sha256Short(""));
    const emptyMerged = rendered.hasSemanticContent
        ? ""
        : normalizeManagedMarkerTails(filePath, emptyRawMerged, [rendered.markerName]).content;
    const emptyCleanupChanged = !rendered.hasSemanticContent && emptyMerged !== existing;
    const semanticRawMerged = rendered.hasSemanticContent
        ? replaceOrInjectMarker(existing, rendered.markerName, rendered.body, rendered.newHash)
        : "";
    const semanticMerged = rendered.hasSemanticContent
        ? normalizeManagedMarkerTails(filePath, semanticRawMerged, [rendered.markerName]).content
        : "";
    const semanticCleanupChanged = rendered.hasSemanticContent && semanticMerged !== existing;
    if (!rendered.hasSemanticContent &&
        parsed.primary === null &&
        parsed.orphans.length === 0 &&
        !parsed.broken &&
        !emptyCleanupChanged) {
        emitDFX(ctx, "fixedLoad.write_skip_empty", {
            marker: rendered.markerName,
            bytes: 0,
        });
        return {
            mustWrite: false,
            hadPrimary: false,
            semanticChanged: false,
            changeDescriptions: [],
            oldHash,
            removeMarker: false,
        };
    }
    if (!rendered.hasSemanticContent) {
        return {
            mustWrite: true,
            hadPrimary: parsed.primary !== null,
            semanticChanged: false,
            changeDescriptions: [],
            oldHash,
            removeMarker: true,
        };
    }
    const canSkip = !markerTampered &&
        oldHash === rendered.newHash &&
        parsed.orphans.length === 0 &&
        !parsed.broken &&
        !hasLegacyMarkers &&
        !semanticCleanupChanged;
    if (canSkip) {
        emitDFX(ctx, "fixedLoad.write_skip", {
            marker: rendered.markerName,
            hash: rendered.newHash,
            bytes: rendered.bytes,
        });
        return {
            mustWrite: false,
            hadPrimary: parsed.primary !== null,
            semanticChanged: false,
            changeDescriptions: [],
            oldHash,
            removeMarker: false,
        };
    }
    if (oldHash !== "" && oldHash !== rendered.newHash) {
        emitDFX(ctx, "fixedLoad.marker_updated", {
            marker: rendered.markerName,
            old_hash: oldHash.slice(0, 19),
            new_hash: rendered.newHash.slice(0, 19),
        });
    }
    return {
        mustWrite: true,
        hadPrimary: parsed.primary !== null,
        semanticChanged,
        changeDescriptions: markerChangeDescriptions(rendered.markerName, oldBody, rendered.body),
        oldHash,
        removeMarker: false,
    };
}
/**
 * 把内容批量、原子地写入指定 MD 文件的 N 个 marker 区。
 *
 * 与 safeWriteMarker（单 marker 版本）共享语义：渲染裁剪、hash 幂等、
 * tamper 检测、broken rescue、原子 tmp+rename。区别在于
 * **同一文件内多个 marker 共用一次** read / 一次 lock / 一次 write，
 * 避免并发场景下两次 read-modify-write 互相覆盖（A6'）。
 *
 * 失败语义：单 patch 渲染失败仅本 patch noop，不影响其他 patch；
 * 文件级 IO/锁失败则全部 patch 返回 noop（不抛）。
 *
 * @param  filePath  目标 MD 文件绝对路径。
 * @param  patches   N 个补丁；空数组直接返回 []。
 * @param  ctx       trace + logger。
 * @return 与 patches 顺序对齐的结果数组（长度恒等）。
 */
export async function safeWriteMarkers(filePath, patches, ctx) {
    if (patches.length === 0)
        return [];
    /* ========== 阶段 1：逐 patch 渲染 + 预算校验 ========== */
    const rendered = patches.map((p) => renderPatchForBatch(p, ctx));
    /* 渲染失败 / 渲染成功的兜底结果 stub —— 后续根据 mustWrite 调整 action。 */
    const results = rendered.map((r) => ({
        action: r.failed ? "noop" : "skip",
        bytes: r.bytes,
        budget: r.budget,
        hash: r.newHash,
        trimmed: r.trimmedReport,
    }));
    /* ========== 阶段 2：取锁 ========== */
    let release = null;
    try {
        await fsp.mkdir(path.dirname(filePath), { recursive: true });
        release = await lockFile(filePath);
    }
    catch (err) {
        ctx.logger?.warn?.(`memory-celia: [safeWriteMarkers] WARN lock_failed ` +
            `file=${filePath} err=${String(err)}`);
        emitDFX(ctx, "fixedLoad.lock_failed", {
            file: path.basename(filePath),
            err: String(err),
        });
        return results.map((r) => ({ ...r, action: "noop" }));
    }
    try {
        /* ========== 阶段 3：读现有文件（一次） ========== */
        let existing = "";
        try {
            existing = await fsp.readFile(filePath, "utf8");
        }
        catch {
            /* 文件不存在：视为空 */
        }
        const originalExisting = existing;
        const patchMarkers = rendered
            .filter((r) => !r.failed)
            .map((r) => r.markerName);
        const preparation = prepareMdFileForWrite(filePath, existing, patchMarkers);
        existing = preparation.content;
        /* ========== 阶段 5：逐 patch 决策 ========== */
        const decisions = rendered.map((r) => r.failed
            ? {
                mustWrite: false,
                hadPrimary: false,
                semanticChanged: false,
                changeDescriptions: [],
                oldHash: "",
                removeMarker: false,
            }
            : decidePatchWrite(filePath, existing, r, ctx));
        if (preparation.changed) {
            const cleanupIndex = decisions.findIndex((d, i) => !rendered[i].failed &&
                rendered[i].hasSemanticContent &&
                !d.mustWrite);
            if (cleanupIndex >= 0) {
                decisions[cleanupIndex] = {
                    ...decisions[cleanupIndex],
                    mustWrite: true,
                    semanticChanged: false,
                    changeDescriptions: [],
                };
            }
        }
        const anyWrite = decisions.some((d) => d.mustWrite);
        if (!anyWrite && !preparation.changed) {
            return results;
        }
        /* ========== 阶段 6：broken rescue（文件级，仅备份一次） ==========
         * parseMarker 是 per-marker 的，但 broken 字段反映整文件 marker 框架
         * 是否破损；只要任一 patch 解析时报 broken 就备份。 */
        const brokenSeen = rendered.some((r) => !r.failed && parseMarker(existing, r.markerName).broken);
        if (brokenSeen && originalExisting.length > 0) {
            const rescuePath = `${filePath}.celia-rescue.${Date.now()}`;
            try {
                await fsp.writeFile(rescuePath, originalExisting, "utf8");
                ctx.logger?.warn?.(`memory-celia: [safeWriteMarkers] WARN ` +
                    `marker_broken_rescue original_to=${rescuePath}`);
                emitDFX(ctx, "fixedLoad.marker_broken_rescue", {
                    file: path.basename(filePath),
                    rescue_path: path.basename(rescuePath),
                });
            }
            catch (err) {
                ctx.logger?.warn?.(`memory-celia: [safeWriteMarkers] rescue ` +
                    `failed: ${String(err)}`);
                emitDFX(ctx, "fixedLoad.rescue_failed", { err: String(err) });
            }
        }
        /* ========== 阶段 7：依次 replace / inject / remove ========== */
        let merged = existing;
        for (let i = 0; i < rendered.length; i++) {
            const r = rendered[i];
            const d = decisions[i];
            if (r.failed || !d.mustWrite)
                continue;
            merged = d.removeMarker
                ? replaceOrInjectMarker(merged, r.markerName, "", sha256Short(""))
                : replaceOrInjectMarker(merged, r.markerName, r.body, r.newHash);
        }
        merged = normalizeManagedMarkerTails(filePath, merged, patchMarkers).content;
        /* ========== 阶段 8：总尺寸警告 ========== */
        const totalBytes = utf8ByteLength(merged);
        if (totalBytes > FILE_TOTAL_LIMIT) {
            ctx.logger?.warn?.(`memory-celia: [safeWriteMarkers] WARN total_over_limit ` +
                `file=${path.basename(filePath)} ` +
                `total=${totalBytes} > ${FILE_TOTAL_LIMIT}`);
            emitDFX(ctx, "fixedLoad.warn_total_over_limit", {
                file: path.basename(filePath),
                total: totalBytes,
                limit: FILE_TOTAL_LIMIT,
            });
        }
        /* ========== 阶段 9：原子写盘（一次） ========== */
        const tmp = `${filePath}.tmp.${process.pid}.${Date.now()}`;
        try {
            await fsp.writeFile(tmp, merged, "utf8");
            await fsp.rename(tmp, filePath);
        }
        catch (err) {
            ctx.logger?.warn?.(`memory-celia: [safeWriteMarkers] WARN write_failed ` +
                `file=${filePath} err=${String(err)}`);
            emitDFX(ctx, "fixedLoad.write_failed", {
                file: path.basename(filePath),
                err: String(err),
            });
            /* 临时文件清理（best-effort） */
            await fsp.unlink(tmp).catch(() => { });
            return results.map((r) => ({ ...r, action: "noop" }));
        }
        /* ========== 阶段 10：填充结果 ========== */
        for (let i = 0; i < rendered.length; i++) {
            const r = rendered[i];
            const d = decisions[i];
            if (r.failed)
                continue;
            if (!d.mustWrite) {
                if (preparation.changed && !anyWrite) {
                    const resultBytes = r.hasSemanticContent ? r.bytes : 0;
                    const resultHash = r.hasSemanticContent ? r.newHash : "";
                    const action = "update";
                    results[i] = {
                        action,
                        bytes: resultBytes,
                        budget: r.budget,
                        hash: resultHash,
                        trimmed: r.trimmedReport,
                    };
                }
                continue; /* skip 保留 stub 中的 'skip' */
            }
            const baseAction = (d.hadPrimary || d.removeMarker) ? "update" : "create";
            const action = r.trimmedReport
                ? baseAction === "create"
                    ? "trim+create"
                    : "trim+update"
                : baseAction;
            const resultBytes = d.removeMarker ? 0 : r.bytes;
            const resultHash = d.removeMarker ? "" : r.newHash;
            results[i] = {
                action,
                bytes: resultBytes,
                budget: r.budget,
                hash: resultHash,
                trimmed: r.trimmedReport,
            };
            emitDFX(ctx, "fixedLoad.write", {
                marker: r.markerName,
                action,
                bytes: resultBytes,
                budget: r.budget,
                ratio: Number((resultBytes / r.budget).toFixed(2)),
                hash: resultHash.slice(0, 19),
            });
            if (!d.semanticChanged && d.changeDescriptions.length === 0) {
                emitDFX(ctx, "fixedLoad.marker_repaired", {
                    marker: r.markerName,
                    action,
                });
            }
            await appendContentChangeDescriptions(filePath, d.changeDescriptions, markerActionReason(r.hasSemanticContent, d.removeMarker, d.semanticChanged, d.changeDescriptions), ctx);
            await appendMemoryActionDescriptions(filePath, [
                markerActionDescription(r.markerName, action, markerActionReason(r.hasSemanticContent, d.removeMarker, d.semanticChanged, d.changeDescriptions), resultBytes, resultHash),
            ], ctx);
        }
        return results;
    }
    finally {
        if (release)
            await release();
    }
}
