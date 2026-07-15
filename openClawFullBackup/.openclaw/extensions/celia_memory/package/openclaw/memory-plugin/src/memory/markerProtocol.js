/**
 * Marker 协议解析与替换工具。
 *
 * 记忆插件在 OpenClaw `MEMORY.md` / `AGENTS.md` 中以 HTML 注释形式
 * 包围一个自治区域，由插件自动维护，其他写入者承诺不修改。
 *
 * Marker 协议：
 *
 *   <!-- <NAME>_BEGIN h=<short-hex> -->
 *   <内容>
 *   <!-- <NAME>_END -->
 *
 * 设计要点（详见 spec §6.4.2）：
 *   - 整体替换语义：每次写入仅替换 BEGIN/END 之间内容，永不 append。
 *   - h 字段为内容指纹（短 hex），用于幂等跳过 + tamper 检测。
 *   - 多对同名 marker 共存视为异常 → 收敛到第一对，其余作 orphan 删除。
 */
import { createHash } from "node:crypto";
const CURRENT_MARKER_WIRE_NAMES = {
    MEMORY_OVERVIEW: "CELIA_MEMORY_OVERVIEW",
    MEMORY_SCENES: "CELIA_MEMORY_SCENES",
    MEMORY_GUIDE: "MEMORY_GUIDE",
};
const MEMORY_MD_LEGACY_MARKER_NAMES = [
    "MEMORY_OVERVIEW",
    "MEMORY_SCENES",
];
/**
 * 短 hash 长度（hex 字符数）。
 *
 * 16 hex = 64 位；单文件内 4 个 marker 碰撞概率可忽略（∼10^-19）。
 * 避免把全长指纹重复写进 prompt。
 */
export const MARKER_HASH_LEN = 16;
// ---- 工具函数 ---------------------------------------------------------------
/** 计算 UTF-8 字节长度。 */
export function utf8ByteLength(s) {
    return Buffer.byteLength(s, "utf8");
}
/**
 * 计算字符串的 sha256 短摘要——前 MARKER_HASH_LEN 个 hex 字符。
 *
 * 用于 marker BEGIN 行的 `h=` 字段：节省 token、避免无意义的全长指纹
 * 每轮重复进 prompt。碰撞域 64 位，单文件 4 marker 实践无碰撞风险。
 */
export function sha256Short(s) {
    return createHash("sha256")
        .update(s, "utf8")
        .digest("hex")
        .slice(0, MARKER_HASH_LEN);
}
const ELLIPSIS = "...";
const SENTENCE_END_CHARS = new Set([
    ".",
    "!",
    "?",
    "。",
    "！",
    "？",
]);
const CLAUSE_END_CHARS = new Set([
    ";",
    "；",
    ",",
    "，",
    "、",
    ":",
    "：",
    "\n",
]);
const CLOSING_TAIL_CHARS = new Set([
    '"',
    "'",
    ")",
    "]",
    "}",
    "”",
    "’",
    "）",
    "】",
    "》",
    "」",
    "』",
]);
function sliceUtf8Safe(s, maxBytes) {
    if (maxBytes <= 0)
        return "";
    if (utf8ByteLength(s) <= maxBytes)
        return s;
    const chars = Array.from(s);
    let lo = 0;
    let hi = chars.length;
    while (lo < hi) {
        const mid = (lo + hi + 1) >>> 1;
        if (utf8ByteLength(chars.slice(0, mid).join("")) <= maxBytes) {
            lo = mid;
        }
        else {
            hi = mid - 1;
        }
    }
    return chars.slice(0, lo).join("");
}
function sliceCodePoints(s, count) {
    return Array.from(s).slice(0, count).join("");
}
function isAsciiAlnum(ch) {
    return ch !== undefined && /^[A-Za-z0-9]$/.test(ch);
}
function isSentenceEnd(ch, prev, next) {
    if (!SENTENCE_END_CHARS.has(ch))
        return false;
    if (ch === "." && isAsciiAlnum(prev) && isAsciiAlnum(next)) {
        return false;
    }
    return true;
}
function findLastNaturalBoundary(prefix, kind) {
    const chars = Array.from(prefix);
    let best = -1;
    for (let i = 0; i < chars.length; i++) {
        const ch = chars[i];
        const prev = i > 0 ? chars[i - 1] : undefined;
        const next = i + 1 < chars.length ? chars[i + 1] : undefined;
        const matched = kind === "sentence"
            ? isSentenceEnd(ch, prev, next)
            : CLAUSE_END_CHARS.has(ch);
        if (!matched)
            continue;
        let end = i + 1;
        while (end < chars.length && CLOSING_TAIL_CHARS.has(chars[end])) {
            end++;
        }
        best = end;
    }
    return best;
}
/**
 * 安全截断 UTF-8 字符串到指定字节数，不切到中文字中间。
 *
 * 若需截断，末尾追加 "..."（3 字节 UTF-8）。返回长度保证 ≤ maxBytes。
 */
export function truncateUtf8Safe(s, maxBytes) {
    if (utf8ByteLength(s) <= maxBytes)
        return s;
    const ellipsisBytes = utf8ByteLength(ELLIPSIS);
    if (maxBytes < ellipsisBytes)
        return "";
    const targetBytes = Math.max(0, maxBytes - ellipsisBytes);
    return sliceUtf8Safe(s, targetBytes) + ELLIPSIS;
}
/**
 * 可读性优先的 UTF-8 截断。
 *
 * 用于 fixed-load 摘要：超预算时优先停在完整句子边界，其次停在从句 /
 * 分隔符边界，最后才退回到普通 UTF-8 安全截断，避免常见的半句话省略。
 */
export function truncateUtf8Readable(s, maxBytes) {
    if (utf8ByteLength(s) <= maxBytes)
        return s;
    const ellipsisBytes = utf8ByteLength(ELLIPSIS);
    if (maxBytes < ellipsisBytes)
        return "";
    const targetBytes = Math.max(0, maxBytes - ellipsisBytes);
    const prefix = sliceUtf8Safe(s, targetBytes).trimEnd();
    const sentenceEnd = findLastNaturalBoundary(prefix, "sentence");
    const clauseEnd = findLastNaturalBoundary(prefix, "clause");
    const boundaryEnd = sentenceEnd > 0 ? sentenceEnd : clauseEnd;
    if (boundaryEnd > 0) {
        return sliceCodePoints(prefix, boundaryEnd).trimEnd() + ELLIPSIS;
    }
    return truncateUtf8Safe(s, maxBytes);
}
// ---- marker 解析 ------------------------------------------------------------
/** BEGIN 行整体匹配，多行模式。 */
const MARKER_BEGIN_RE = /<!--\s+([A-Z0-9_]+)_BEGIN\s+h=([^\s>]+)\s*-->/g;
/** END 行匹配。 */
const MARKER_END_RE = /<!--\s+([A-Z0-9_]+)_END\s*-->/g;
const LEGACY_MARKER_NAMES = [
    "CELIA_L0",
    "CELIA_L1_SCENES",
    "CELIA_GUIDE",
];
export function hasLegacyMarkerBlocks(fileContent) {
    for (const name of LEGACY_MARKER_NAMES) {
        const markerLineRe = new RegExp(`<!--\\s+${name}_(?:BEGIN|END)[^>]*-->`);
        if (markerLineRe.test(fileContent))
            return true;
    }
    return false;
}
function removeNamedMarkerBlocks(fileContent, name) {
    let cleaned = fileContent;
    const blockRe = new RegExp(`<!--\\s+${name}_BEGIN[^>]*-->[\\s\\S]*?` +
        `<!--\\s+${name}_END\\s*-->\\n?`, "g");
    cleaned = cleaned.replace(blockRe, "");
    const orphanLineRe = new RegExp(`<!--\\s+${name}_(?:BEGIN|END)[^>]*-->\\n?`, "g");
    cleaned = cleaned.replace(orphanLineRe, "");
    return cleaned;
}
function escapeRegExp(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
function unwrapNamedMarkerLines(fileContent, name) {
    const escapedName = escapeRegExp(name);
    const markerLineRe = new RegExp(`^[\\t ]*<!--\\s+${escapedName}_(?:BEGIN|END)[^>]*-->` +
        `[\\t ]*(?:\\r?\\n|$)`, "gm");
    return fileContent.replace(markerLineRe, "");
}
function unwrapNamedMarkerBlocks(fileContent, name) {
    const escapedName = escapeRegExp(name);
    const blockRe = new RegExp(`^[\\t ]*<!--\\s+${escapedName}_BEGIN[^>]*-->` +
        `[\\t ]*(?:\\r?\\n|$)` +
        `([\\s\\S]*?)` +
        `^[\\t ]*<!--\\s+${escapedName}_END[^>]*-->` +
        `[\\t ]*(?:\\r?\\n|$)`, "gm");
    const unwrapped = fileContent.replace(blockRe, (_match, body) => body.trim() === "" ? "" : body);
    return unwrapNamedMarkerLines(unwrapped, name);
}
function removeLegacyMarkerBlocks(fileContent) {
    let cleaned = fileContent;
    for (const name of LEGACY_MARKER_NAMES) {
        cleaned = removeNamedMarkerBlocks(cleaned, name);
    }
    return cleaned;
}
function normalizeBlankOnlyContent(fileContent) {
    return fileContent.length > 0 && fileContent.trim() === ""
        ? ""
        : fileContent;
}
function normalizeBeforeReplaceOrInject(fileContent) {
    const withoutLegacyBlocks = removeLegacyMarkerBlocks(fileContent);
    const unwrappedMemory = unwrapLegacyMemoryMarkers(withoutLegacyBlocks);
    return normalizeBlankOnlyContent(unwrappedMemory);
}
export function removeMarkerBlocks(fileContent, markerName) {
    return removeNamedMarkerBlocks(fileContent, CURRENT_MARKER_WIRE_NAMES[markerName]);
}
/**
 * 解包旧版 MEMORY.md marker：只删除 BEGIN / END 标记行，保留中间正文。
 */
export function unwrapLegacyMemoryMarkers(fileContent) {
    let cleaned = fileContent;
    for (const name of MEMORY_MD_LEGACY_MARKER_NAMES) {
        cleaned = unwrapNamedMarkerBlocks(cleaned, name);
    }
    return cleaned;
}
/**
 * 在文件内容中解析所有形如 <markerName>_BEGIN/END 的 marker。
 *
 * 返回 primary（首个完整对）、orphans（其余完整对）、broken（找到 BEGIN
 * 但缺 END 时为 true）。
 *
 * @param fileContent  待解析的文件全文
 * @param markerName   要查找的 marker 名（如 MEMORY_OVERVIEW）
 */
export function parseMarker(fileContent, markerName) {
    const result = {
        primary: null,
        orphans: [],
        broken: false,
    };
    const targetTag = CURRENT_MARKER_WIRE_NAMES[markerName];
    const matches = [];
    MARKER_BEGIN_RE.lastIndex = 0;
    let m;
    while ((m = MARKER_BEGIN_RE.exec(fileContent)) !== null) {
        if (m[1] !== targetTag)
            continue;
        matches.push({
            kind: "begin",
            index: m.index,
            endIndex: m.index + m[0].length,
            hash: m[2],
        });
    }
    MARKER_END_RE.lastIndex = 0;
    while ((m = MARKER_END_RE.exec(fileContent)) !== null) {
        if (m[1] !== targetTag)
            continue;
        matches.push({
            kind: "end",
            index: m.index,
            endIndex: m.index + m[0].length,
            hash: "",
        });
    }
    matches.sort((a, b) => a.index - b.index);
    /* ========== 阶段二：配对 BEGIN/END，收集完整对 ========== */
    const completedPairs = [];
    let pendingBegin = null;
    for (const mm of matches) {
        if (mm.kind === "begin") {
            if (pendingBegin !== null) {
                /* 上一个 BEGIN 没有 END 就出现新 BEGIN：上一个视为 broken */
                result.broken = true;
            }
            pendingBegin = mm;
        }
        else {
            if (pendingBegin === null)
                continue; /* 孤立 END，忽略 */
            const begin = pendingBegin;
            const content = fileContent
                .slice(begin.endIndex, mm.index)
                .replace(/^\n/, "")
                .replace(/\n$/, "");
            completedPairs.push({
                name: markerName,
                meta: { hash: begin.hash },
                content,
                range: { start: begin.index, end: mm.endIndex },
            });
            pendingBegin = null;
        }
    }
    if (pendingBegin !== null)
        result.broken = true;
    /* ========== 阶段三：选 primary、其余作 orphan ========== */
    if (completedPairs.length > 0) {
        result.primary = completedPairs[0];
        result.orphans = completedPairs.slice(1);
    }
    return result;
}
// ---- marker 替换 / 注入 -----------------------------------------------------
/** 构造 BEGIN 行（v2 短格式，仅 h=<short>）。 */
function buildBeginLine(name, hash) {
    return `<!-- ${CURRENT_MARKER_WIRE_NAMES[name]}_BEGIN h=${hash} -->`;
}
/** 构造 END 行。 */
function buildEndLine(name) {
    return `<!-- ${CURRENT_MARKER_WIRE_NAMES[name]}_END -->`;
}
/**
 * 构造完整 marker 块（含 BEGIN/内容/END，两端各保证 1 个换行）。
 *
 * @param hash  内容的短 hex 指纹（MARKER_HASH_LEN 字符），由调用方计算
 *              （通常 sha256Short(content)）
 */
export function buildMarkerBlock(name, content, hash) {
    const begin = buildBeginLine(name, hash);
    const end = buildEndLine(name);
    const body = content.replace(/^\n+/, "").replace(/\n+$/, "");
    return `${begin}\n${body}\n${end}\n`;
}
/**
 * 替换或注入 marker；已有 marker 时永不追加重复块。
 *
 * 行为：
 *   1. 解析现有 marker：
 *      - 找到 primary → 整体替换 primary.range，并删除所有 orphans。
 *      - 找不到 → 注入到文件**尾部**。
 *      - 损坏（BEGIN 无 END）→ 调用方应先做 rescue 备份，本函数仅按
 *        "找不到"处理（删除孤立 BEGIN 行、新建 marker）。
 *   2. 注入位置规则：文件尾插入时，若文件非空，marker 前追加 "\n"
 *      与原内容隔开。
 *
 * @param fileContent  原文件内容（可空字符串）
 * @param markerName   marker 名
 * @param newContent   marker 内容（不含 BEGIN/END 行）
 * @param newHash      内容短 hex 指纹（sha256Short），写入 BEGIN 行 h= 字段
 * @returns            替换后的完整文件内容
 */
export function replaceOrInjectMarker(fileContent, markerName, newContent, newHash) {
    const normalizedContent = normalizeBeforeReplaceOrInject(fileContent);
    if (newContent.trim() === "") {
        const cleaned = removeMarkerBlocks(normalizedContent, markerName);
        return normalizeBlankOnlyContent(cleaned);
    }
    const block = buildMarkerBlock(markerName, newContent, newHash);
    const parsed = parseMarker(normalizedContent, markerName);
    /* ========== 路径 A：找到 primary ========== */
    if (parsed.primary !== null) {
        /* 先按字节顺序倒序删除所有 orphan 范围（避免偏移漂移） */
        const ranges = [
            ...parsed.orphans.map((o) => o.range),
            parsed.primary.range,
        ].sort((a, b) => b.start - a.start);
        let updated = normalizedContent;
        for (let i = 0; i < ranges.length - 1; i++) {
            /* 删除 orphan：连同其后一个换行（如有） */
            const r = ranges[i];
            const tailNl = updated.charAt(r.end) === "\n" ? 1 : 0;
            updated =
                updated.slice(0, r.start) +
                    updated.slice(r.end + tailNl);
        }
        /* 最后处理 primary：替换为新 block */
        const r = ranges[ranges.length - 1];
        const tailNl = updated.charAt(r.end) === "\n" ? 1 : 0;
        updated =
            updated.slice(0, r.start) +
                block +
                updated.slice(r.end + tailNl);
        return updated;
    }
    /* ========== 路径 B：未找到（或损坏视同未找到） ========== */
    /* 损坏时清掉孤立的 BEGIN/END 行，避免后续解析再次混淆 */
    let cleaned = normalizedContent;
    if (parsed.broken) {
        cleaned = removeOrphanBeginEndLines(cleaned, markerName);
    }
    if (cleaned.length === 0)
        return block;
    const sep = cleaned.endsWith("\n\n") ? "" :
        cleaned.endsWith("\n") ? "\n" : "\n\n";
    return cleaned + sep + block;
}
/**
 * 清除文件中所有孤立的 BEGIN/END 单行（仅当本函数被 broken 路径调用时使用）。
 */
function removeOrphanBeginEndLines(fileContent, markerName) {
    const re = new RegExp(`<!--\\s+${CURRENT_MARKER_WIRE_NAMES[markerName]}_` +
        `(?:BEGIN|END)[^>]*-->\\n?`, "g");
    return fileContent.replace(re, "");
}
