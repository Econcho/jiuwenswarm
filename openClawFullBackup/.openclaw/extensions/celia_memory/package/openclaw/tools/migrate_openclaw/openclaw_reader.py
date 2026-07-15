"""读取 OpenClaw workspace 里的 MEMORY.md，按段切出 MemoryItem。

范围：只处理长期记忆。只找 workspace 根下的 MEMORY.md 或 memory.md。
切分规则：先按 Markdown heading 切，再在每个 heading 的正文里按空行
切段；平铺 bullet 列表逐行原子化；≤3 行的短段落并入上一段，
避免切得太碎。

注意：切段时会跟踪 ```\u200b```\u200b 围栏代码块状态，代码块内部的空行
不算段落分隔符，URL/命令/配置能原样保留。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .ir import MemoryItem

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# 围栏代码块的起止行：``` 或 ~~~，后面可跟语言标注。
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")

# ---- 系统托管 marker 块的预处理正则 ----
# Memory.md 可能含 Celia 自家写入的场景块或概览块。这些是 Celia 的输出
# 而非用户原文，必须在切段前剥离，否则回灌会形成反馈环。
_MEMORY_SCENES_BLOCK_RE = re.compile(
    r"<!--\s*MEMORY_SCENES_BEGIN\b[\s\S]*?"
    r"<!--\s*MEMORY_SCENES_END\s*-->\n?"
)
_MEMORY_OVERVIEW_BLOCK_RE = re.compile(
    r"<!--\s*MEMORY_OVERVIEW_BEGIN\b[\s\S]*?"
    r"<!--\s*MEMORY_OVERVIEW_END\s*-->\n?"
)
_MANAGED_BLOCK_RES = (
    _MEMORY_SCENES_BLOCK_RE,
    _MEMORY_OVERVIEW_BLOCK_RE,
)


def strip_celia_managed_blocks(text: str) -> str:
    """剥掉 Memory.md 里 Celia 托管的场景/概览 marker 块。

    规则：
      - 空内容或无任何 marker → 原样返回。
      - 完整的场景/概览 marker 块 → 整块（含尾换行）删除。
      - marker 损坏（只有 BEGIN 或只有 END）→ 原样返回。BLOCK_RE 要求
        BEGIN..END 完整配对，单边 marker 天然不匹配，不需要额外 guard。
    """
    if not text:
        return text
    out = text
    for block_re in _MANAGED_BLOCK_RES:
        out = block_re.sub("", out)
    return out


@dataclass
class ParsedSection:
    """从 Markdown body 中切出的一个段落，带原始行号范围。"""

    heading: str | None
    text: str
    start_line: int  # 1-indexed，段落首行在原文中的行号
    end_line: int    # 1-indexed，段落末行（含）

    def __post_init__(self) -> None:
        if self.end_line < self.start_line:
            self.end_line = self.start_line


def _is_blank(line: str, in_code_fence: bool) -> bool:
    """判断某行是否算作"段落分隔空行"。

    代码块内部的空行不算——否则 fenced code 会被切碎成多段。
    """
    if in_code_fence:
        return False
    return not line.strip()


_LIST_LINE_RE = re.compile(r"^([ \t]*)([-*+]|\d+[.)])\s+")
_BULLET_LINE_RE = re.compile(r"^([ \t]*)([-*+])\s+")


def _indent_width(line: str) -> int:
    """返回行首缩进宽度；tab 按 4 列展开。"""
    col = 0
    for ch in line:
        if ch == " ":
            col += 1
        elif ch == "\t":
            col += 4 - (col % 4)
        else:
            break
    return col


def _list_line_indent(line: str) -> int | None:
    """如果是 Markdown 列表项，返回其缩进宽度。"""
    m = _LIST_LINE_RE.match(line)
    if not m:
        return None
    return _indent_width(m.group(1))


def _bullet_line_indent(line: str) -> int | None:
    """如果是无序 bullet 列表项，返回其缩进宽度。"""
    m = _BULLET_LINE_RE.match(line)
    if not m:
        return None
    return _indent_width(m.group(1))


def _is_flat_bullet_paragraph(lines: list[str]) -> bool:
    """判断段落是否为同一缩进层级的纯无序 bullet 列表。"""
    if not lines:
        return False
    base_indent: int | None = None
    for line in lines:
        if not line.strip():
            return False
        indent = _bullet_line_indent(line)
        if indent is None:
            return False
        if base_indent is None:
            base_indent = indent
        elif indent != base_indent:
            return False
    return True


def _split_flat_bullet_paragraphs(
    paragraphs: list[tuple[int, int, list[str]]],
) -> list[tuple[int, int, list[str]]]:
    """把平铺 bullet 段落拆成逐行段落，复杂列表保持原样。"""
    out: list[tuple[int, int, list[str]]] = []
    for start, end, lines in paragraphs:
        if not _is_flat_bullet_paragraph(lines):
            out.append((start, end, lines))
            continue
        for offset, line in enumerate(lines):
            line_no = start + offset
            out.append((line_no, line_no, [line]))
    return out


def _is_single_flat_bullet_paragraph(
    para: tuple[int, int, list[str]],
) -> bool:
    """判断段落是否为一个已原子化的单行 bullet。"""
    start, end, lines = para
    return start == end and _is_flat_bullet_paragraph(lines)


def _next_nonblank_line(
    lines: list[str], start: int,
) -> tuple[int, str] | None:
    """从 start 开始找下一条非空行，返回 `(offset, line)`。"""
    for offset in range(start, len(lines)):
        line = lines[offset]
        if line.strip():
            return offset, line
    return None


def _blank_keeps_list_context(
    block_lines: list[str], blank_offset: int, run_lines: list[str],
) -> bool:
    """判断空行是否仍属于当前 Markdown 列表块。

    Memory.md 常包含：
      - 父项

        - 缩进子项

    如果按普通空行切段，父标签与子项会被拆成两条迁移记录，后续 LLM
    抽取容易丢上下文。这里仅在"空行前最近正文是列表项，空行后下一
    个非空行是更深缩进列表项"时保留空行，不切段。
    """
    if not run_lines:
        return False
    prev_indent = _list_line_indent(run_lines[-1])
    if prev_indent is None:
        return False
    nxt = _next_nonblank_line(block_lines, blank_offset + 1)
    if nxt is None:
        return False
    next_indent = _list_line_indent(nxt[1])
    return next_indent is not None and next_indent > prev_indent


def _parse_markdown_sections(body: str) -> list[ParsedSection]:
    """把一整篇 Markdown 切成 section 列表。

    规则按优先级：
      1. 每个 heading 行开启一个新的 heading 块，带上它的标题。
      2. heading 块内，按空行切段；平铺 bullet 列表按行拆。
      3. 单段非空行数 ≤3 的短段落合并进上一段，
         已原子化 bullet 除外。
      4. 没有任何 heading 的情况，整体按空行切段（不带 heading）。
      5. 围栏代码块（``` / ~~~）内部的空行和 heading 都不生效，
         代码块完整保留在所属段落里。
    """
    lines = body.splitlines()

    # 阶段一：按 heading 切成 heading_blocks。代码块内的 heading 被忽略。
    heading_blocks: list[tuple[str | None, int, list[str]]] = []
    current_heading: str | None = None
    current_lines: list[str] = []
    current_start = 1
    in_fence = False
    for i, line in enumerate(lines):
        fence_m = _FENCE_RE.match(line)
        if fence_m:
            # 围栏本身算作正文的一部分，切换状态后直接加进当前段
            in_fence = not in_fence
            current_lines.append(line)
            continue
        m = _HEADING_RE.match(line)
        if m and not in_fence:
            # 新 heading：把之前累积的块收起来
            if current_lines or current_heading is not None:
                heading_blocks.append(
                    (current_heading, current_start, current_lines)
                )
            current_heading = m.group(2).strip()
            current_lines = []
            current_start = i + 2  # heading 下一行在原文中的 1-indexed 行号
        else:
            current_lines.append(line)
    heading_blocks.append((current_heading, current_start, current_lines))

    # 阶段二：在每个 heading 块里按段落切分
    sections: list[ParsedSection] = []
    for heading, block_start, block_lines in heading_blocks:
        # 没 heading 又没内容的空块直接丢
        if heading is None and not any(ln.strip() for ln in block_lines):
            continue

        paragraphs = _split_block_into_paragraphs(block_lines)
        if not paragraphs:
            continue

        atomized = _split_flat_bullet_paragraphs(paragraphs)
        merged = _merge_short_paragraphs(atomized)

        for p_start, p_end, p_lines in merged:
            text = "\n".join(p_lines).strip()
            if not text:
                continue
            sections.append(ParsedSection(
                heading=heading,
                text=text,
                start_line=block_start + p_start,
                end_line=block_start + p_end,
            ))
    return sections


def _split_block_into_paragraphs(
    block_lines: list[str],
) -> list[tuple[int, int, list[str]]]:
    """把一个 heading 块的内容按空行切段，代码块空行不算分隔。

    返回：[(段落起始 offset, 结束 offset, 段落行列表), ...]，
    offset 是相对 block_lines 的 0-indexed。
    """
    paragraphs: list[tuple[int, int, list[str]]] = []
    run_start: int | None = None
    run_lines: list[str] = []
    in_fence = False
    for offset, ln in enumerate(block_lines):
        if _FENCE_RE.match(ln):
            in_fence = not in_fence
            if run_start is None:
                run_start = offset
            run_lines.append(ln)
            continue
        if _is_blank(ln, in_fence):
            if run_start is not None and _blank_keeps_list_context(
                block_lines, offset, run_lines,
            ):
                run_lines.append(ln)
                continue
            # 段落结束
            if run_start is not None:
                paragraphs.append((run_start, offset - 1, run_lines))
                run_start = None
                run_lines = []
        else:
            if run_start is None:
                run_start = offset
            run_lines.append(ln)
    if run_start is not None:
        paragraphs.append(
            (run_start, len(block_lines) - 1, run_lines)
        )
    return paragraphs


def _merge_short_paragraphs(
    paragraphs: list[tuple[int, int, list[str]]],
) -> list[tuple[int, int, list[str]]]:
    """把 ≤3 行的小段并入上一段，减少过度碎片化。

    合并时插入一个空行分隔符，以便原文的段落结构仍可辨认。
    """
    merged: list[tuple[int, int, list[str]]] = []
    for para in paragraphs:
        _, _, plines = para
        if (merged and len(plines) <= 3
                and not _is_single_flat_bullet_paragraph(para)):
            prev_start, _, prev_lines = merged[-1]
            merged[-1] = (
                prev_start, para[1],
                prev_lines + [""] + plines,
            )
        else:
            merged.append(para)
    return merged


def _read_utf8(path: Path) -> str | None:
    """读取一个可能用 UTF-8 或 latin-1 编码的文本文件。

    UTF-8 失败时降级到 latin-1 ——把 mojibake 也作为"可迁移数据"
    保留下来，比直接丢弃强。
    """
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def _find_exact_file(workspace: Path, filename: str) -> Path | None:
    """按目录项名字精确查找文件，避免大小写不敏感 FS 误命中。"""
    try:
        entries = workspace.iterdir()
    except OSError:
        return None
    for entry in entries:
        if entry.name == filename and entry.is_file():
            return entry
    return None


def read_long_term(workspace: Path, user_id: str) -> Iterator[MemoryItem]:
    """遍历 workspace 下的 Memory.md，每个段落产出一个 MemoryItem。

    文件名优先级（命中第一个就停）：
      - Memory.md（OpenClaw 当前命名）
      - MEMORY.md（历史命名）
      - memory.md（小写回退）

    读入后先 `strip_celia_managed_blocks` 剥掉 Celia 自家写入的场景/概览
    托管块，再切段；避免把 Celia 的输出回灌形成反馈环。
    明确不看子目录、不看 DIARY.md / dreams / journal 等其他文件——
    那些数据类别超出本工具范围。
    """
    for filename in ("Memory.md", "MEMORY.md", "memory.md"):
        path = _find_exact_file(workspace, filename)
        raw = _read_utf8(path) if path is not None else None
        if raw is None:
            continue
        body = strip_celia_managed_blocks(raw)
        for section in _parse_markdown_sections(body):
            yield MemoryItem(
                user_id=user_id,
                text=section.text,
                source_ref=f"{filename}:L{section.start_line}-"
                           f"L{section.end_line}",
                heading=section.heading,
                start_line=section.start_line,
                end_line=section.end_line,
            )
        return  # 命中一个文件就停，避免重复入库


def read_all_for_migration(
    workspace: Path, user_id: str
) -> Iterator[MemoryItem]:
    """按迁移场景期望的顺序把 USER.md + Memory.md 的内容串起来产出。

    顺序：先 USER.md（用户画像，只一条），再 Memory.md（按段切分）。
    先导画像的意义是：即便 Memory.md 提取失败（LLM 抽不出事实、或中途
    出错），用户画像至少落库，首轮召回不会对用户"完全失忆"。
    """
    yield from read_user_profile(workspace, user_id)
    yield from read_long_term(workspace, user_id)


# ---- 空模板判定的正则 -------------------------------------------------------
# 占位 token：TBD / TODO / N/A / 待填写 / 未填 / <placeholder> 等。
# 大小写不敏感，只要行里只剩这些 token + 标点 + 空白，视作没真实内容。
_PLACEHOLDER_TOKENS_RE = re.compile(
    r"\b(?:TBD|TODO|N/?A|FIXME|placeholder|unknown|unset|待填|未填|待填写|无)\b",
    re.IGNORECASE,
)
# Markdown heading 整行：`# ...` 到 `###### ...`
_HEADING_LINE_RE = re.compile(r"^\s{0,3}#{1,6}\s+.*$", re.MULTILINE)
# HTML 注释：可能跨行
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
# 方括号 / 尖括号里的占位：`[your name]`、`<name>`、也接受 `[]`/`<>`
# （占位 token 已被上游 sub 成空壳时仍需要继续清掉外壳）。
_ANGLE_BRACKET_PLACEHOLDER_RE = re.compile(r"<[^>\n]{0,40}>")
_SQUARE_BRACKET_PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{0,60}\]")
# OpenClaw 模板里的斜体提示：`_(You can add ...)_`、`_(Build over time)_`
# ——下划线 + 括号 + 一段说明，全是给 agent 看的填写指引，不是用户真数据。
_ITALIC_INSTRUCTION_RE = re.compile(r"_\([^)\n]{0,300}\)_")
# 纯空壳列表项：`- `、`* `、`1.` 后面没实际内容
_EMPTY_BULLET_RE = re.compile(r"^\s*[-*+]\s*$", re.MULTILINE)
_EMPTY_NUMBERED_RE = re.compile(r"^\s*\d+\.\s*$", re.MULTILINE)
# 只剩标签、冒号后没值：`Name:`、`- Age:`、中文 `姓名：`
# `[:：]` 同时接受 ASCII 冒号和中文全角冒号
_EMPTY_LABEL_RE = re.compile(
    r"^\s*(?:[-*+]\s*)?[^:：\n]{1,30}[:：]\s*$", re.MULTILINE,
)
# Markdown bold 包裹的标签："**Name:** "、"**姓名：**"
# 占位内容被上游 sub 掉后这些会变成纯空壳 label，要一并剥。
_EMPTY_BOLD_LABEL_RE = re.compile(
    r"^\s*(?:[-*+]\s*)?\*\*[^*\n]{1,40}[:：]?\*\*\s*[:：]?\s*$", re.MULTILINE,
)
# Markdown 水平分隔线 `---` / `***` / `___`
_HR_RE = re.compile(r"^\s{0,3}([-*_])\s*\1\s*\1[\s\1]*$", re.MULTILINE)

# 剥完所有脚手架后只要还有任何非空白字符，就视作含真数据。
# 之前取 6 是为了防误判，但这样会把"Alice"、"姓名：小美"这种极短
# 但真实的 profile 也当成模板误杀。规则足够精准后降为 1——模板
# 经过全量剥离几乎总是归零，短真实数据则至少留下几个 token。
_USER_MD_MIN_REAL_CHARS = 1


def _is_user_md_template(text: str) -> bool:
    """判断 USER.md 内容是不是"空模板"（只有脚手架、没真实用户信息）。

    判定步骤（按顺序剥离）：
      1. 移除 HTML 注释
      2. 移除所有 markdown heading 行
      3. 移除常见占位 token（TBD / TODO / <placeholder> / 待填 等）
      4. 移除空的列表项 / 空的 `Label:` 行
      5. 去除全部空白字符
    剩余字符数 < `_USER_MD_MIN_REAL_CHARS` → 模板。

    设计选择：宁可**偶尔漏导**一份信息极少的真实 profile（用户再补内容
    后重装会补上；manifest 不会阻拦新 hash），**也不要误导**空模板污染
    记忆库——模板条目在召回里没有任何正向价值。
    """
    if not text or not text.strip():
        return True
    stripped = _HTML_COMMENT_RE.sub("", text)
    stripped = _ITALIC_INSTRUCTION_RE.sub("", stripped)
    stripped = _HEADING_LINE_RE.sub("", stripped)
    stripped = _HR_RE.sub("", stripped)
    # 占位 token 先剥：之后 [] / <> 外壳可能就空了，下一步继续剥外壳
    stripped = _PLACEHOLDER_TOKENS_RE.sub("", stripped)
    stripped = _ANGLE_BRACKET_PLACEHOLDER_RE.sub("", stripped)
    stripped = _SQUARE_BRACKET_PLACEHOLDER_RE.sub("", stripped)
    stripped = _EMPTY_BULLET_RE.sub("", stripped)
    stripped = _EMPTY_NUMBERED_RE.sub("", stripped)
    # bold-label 要在普通 label 前剥：`**Name:**` 本身含一对 `:` 但
    # 是粗体语法的一部分；先去 bold 包裹，残留的 `Name:` 再由下一条兜底。
    stripped = _EMPTY_BOLD_LABEL_RE.sub("", stripped)
    stripped = _EMPTY_LABEL_RE.sub("", stripped)
    remaining = re.sub(r"\s+", "", stripped)
    return len(remaining) < _USER_MD_MIN_REAL_CHARS


# ---- USER.md 原子化 bullet 抽取 --------------------------------------------
# OpenClaw 模板里每条用户事实都是 `- **标签：** 值` 形式：
#   - **姓名 / 称呼：** 小美
#   - **所在地与时区：** 中国，GMT+8
# bold 标签里的 `:` 可以是 ASCII 或中文全角。
_BOLD_BULLET_RE = re.compile(
    r"^\s*[-*+]\s*\*\*([^*\n]+?)\s*[:：]\s*\*\*\s*(.*)$",
)
# 普通 `- 标签: 值` 兜底（非 bold 时用）
_PLAIN_BULLET_RE = re.compile(
    r"^\s*[-*+]\s+([^:：\n*]{1,40})\s*[:：]\s*(.+)$",
)
# 括号包裹的整段说明："（例如：...）"、"(e.g. ...)"——模板提示，非真数据
_PAREN_ONLY_RE = re.compile(r"^[(（][^)）\n]*[)）]\s*$")
# 模板示例起始词（"例如"、"如"、"e.g."）
_EXAMPLE_PREFIX_RE = re.compile(
    r"^\s*[(（]\s*(?:例如|如|e\.?g\.?|例)[:：]?", re.IGNORECASE,
)
_LIST_ITEM_RE = re.compile(r"^([ \t]*)([-*+]|\d+[.)])\s+(.*)$")
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_CHECKBOX_PREFIX_RE = re.compile(r"^\[[ xX]\]\s+")
_BOLD_LABEL_RE = re.compile(
    r"^\*\*([^*\n]+?)\s*[:：]\s*\*\*\s*(.*)$",
)
_PLAIN_LABEL_RE = re.compile(
    r"^([^:：\n*]{1,40})\s*[:：]\s*(.*)$",
)


@dataclass
class _UserBullet:
    """USER.md 中一条可迁移事实，包含起止行号。"""

    label: str
    value: str
    start_line: int
    end_line: int


@dataclass
class _UserListNode:
    """USER.md 中一条 Markdown 列表项，保留层级关系。"""

    indent: int
    label: str | None
    value: str
    start_line: int
    end_line: int
    children: list["_UserListNode"] = field(default_factory=list)


def _is_trivial_bullet_value(value: str) -> bool:
    """判断 bullet 的 value 是不是"脚手架级别"（空、占位、括号示例）。"""
    v = value.strip()
    if not v:
        return True
    if _PAREN_ONLY_RE.match(v):
        return True
    if _EXAMPLE_PREFIX_RE.match(v):
        return True
    # 仅占位 token + 括号 / 空白
    stripped = _PLACEHOLDER_TOKENS_RE.sub("", v)
    stripped = _SQUARE_BRACKET_PLACEHOLDER_RE.sub("", stripped)
    stripped = _ANGLE_BRACKET_PLACEHOLDER_RE.sub("", stripped)
    remaining = re.sub(r"[\s、,，。.;；]+", "", stripped)
    return len(remaining) < 1


def _line_indent(line: str) -> int:
    """返回行首缩进列数；tab 按 4 列展开。"""
    col = 0
    for ch in line:
        if ch == " ":
            col += 1
        elif ch == "\t":
            col += 4 - (col % 4)
        else:
            break
    return col


def _clean_list_body(body: str) -> str:
    """清理 list marker 后的正文，兼容 task-list checkbox。"""
    return _CHECKBOX_PREFIX_RE.sub("", body.strip(), count=1).strip()


def _match_label_body(body: str) -> tuple[str, str] | None:
    """匹配 `**标签：** 值` 或 `标签: 值` 正文。"""
    m = _BOLD_LABEL_RE.match(body) or _PLAIN_LABEL_RE.match(body)
    if not m:
        return None
    label = m.group(1).strip().rstrip("：:")
    if not label:
        return None
    return label, m.group(2).strip()


def _match_list_item(line: str) -> tuple[int, str, int] | None:
    """匹配 Markdown 列表项，返回 `(indent, body, is_ordered)`。"""
    m = _LIST_ITEM_RE.match(line)
    if not m:
        return None
    marker = m.group(2)
    body = _clean_list_body(m.group(3))
    if not body:
        return None
    return _line_indent(line), body, marker[0].isdigit()


def _match_user_bullet(line: str) -> tuple[str, str, int] | None:
    """匹配一行 USER.md bullet，返回 `(label, value, indent)`。"""
    item = _match_list_item(line)
    if not item:
        return None
    indent, body, _ = item
    matched = _match_label_body(body)
    if matched is None:
        return None
    label, value = matched
    return label, value, indent


def _is_trivial_continuation_line(line: str) -> bool:
    """判断缩进续行是不是纯模板提示。"""
    stripped = line.strip()
    if not stripped:
        return True
    without_bullet = _BULLET_PREFIX_RE.sub("", stripped)
    return _is_trivial_bullet_value(without_bullet)


def _strip_bullet_prefix(line: str) -> str:
    """去掉续行前导 bullet 符号，返回事实正文。"""
    item = _match_list_item(line.strip())
    if item:
        return item[1]
    return _BULLET_PREFIX_RE.sub("", line.strip(), count=1).strip()


def _node_value_append(node: _UserListNode, value: str, line_no: int) -> None:
    """把续行追加到节点 value，更新结束行。"""
    if not value.strip():
        return
    if node.value:
        node.value = f"{node.value}\n{value.strip()}"
    else:
        node.value = value.strip()
    node.end_line = line_no


def _emit_user_list_node(
    node: _UserListNode, inherited_label: str | None, out: list[_UserBullet]
) -> None:
    """按源文件顺序把列表树扁平化为可迁移事实。"""
    effective_label = node.label or inherited_label
    child_label = node.label or inherited_label
    if effective_label and not _is_trivial_bullet_value(node.value):
        out.append(_UserBullet(
            label=effective_label,
            value=node.value.strip(),
            start_line=node.start_line,
            end_line=node.end_line,
        ))
    for child in node.children:
        _emit_user_list_node(child, child_label, out)


def _format_user_bullet_text(label: str, value: str) -> str:
    """把 label/value 组装成最终迁移文本。"""
    if value.startswith(("- ", "* ", "+ ")):
        return f"{label}:\n{value}"
    return f"{label}: {value}"


def _parse_user_md_bullets(
    text: str,
) -> list[_UserBullet]:
    """扫描 USER.md 正文抽出所有可迁移事实。

    优先匹配 bold 样式 `- **LABEL：** VALUE`（OpenClaw 模板约定），
    再用普通 `- LABEL: VALUE` 兜底。缩进子列表项继承最近的显式
    label 并拆成独立事实；子项自带 label 时改用子 label。非列表
    缩进续行并入当前列表项，避免多行说明被截断。
    空值、括号示例、纯占位一律丢弃。
    """
    roots: list[_UserListNode] = []
    stack: list[_UserListNode] = []

    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue

        item = _match_list_item(line)
        if item is not None:
            indent, body, isOrdered = item
            while stack and stack[-1].indent >= indent:
                stack.pop()
            if not stack and isOrdered:
                continue
            matched = _match_label_body(body)
            label: str | None = None
            value = body
            if matched is not None:
                label, value = matched
            if _is_trivial_bullet_value(value):
                value = ""
            node = _UserListNode(
                indent=indent,
                label=label,
                value=value.strip(),
                start_line=idx,
                end_line=idx,
            )
            if stack:
                stack[-1].children.append(node)
            else:
                roots.append(node)
            stack.append(node)
            continue

        if stack and _line_indent(line) > stack[-1].indent:
            if not _is_trivial_continuation_line(stripped):
                _node_value_append(stack[-1], stripped, idx)
            continue

        stack = []

    out: list[_UserBullet] = []
    for root in roots:
        _emit_user_list_node(root, None, out)
    return out


def read_user_profile(
    workspace: Path, user_id: str
) -> Iterator[MemoryItem]:
    """把 workspace 根下的 USER.md 原子化成**多条** MemoryItem。

    生产验证显示整篇 USER.md 以单条 memory_add 下发时，async 窗口提取
    prompt（`window_extract.zh.md`）把整篇 markdown 当成一次"用户回合"
    喂给 LLM，LLM 只抓到最显眼的一条（通常是姓名）就收手，其余事实
    全部丢失。解法：客户端按 `- **标签：** 值` bullet 切开，每条 bullet
    单独 memory_add，worker 按 session 批量组装成多轮对话，LLM 在
    "多个用户回合"上逐条提取——正好匹配 prompt 的设计假设。

    产出顺序：按 USER.md 原文行号。每条 MemoryItem 的 `text` 是
    `"LABEL: VALUE"`（统一 ASCII 冒号），保留标签帮 LLM 理解语境。

    不产出 item 的情况：
      - 文件不存在 / 空 / 纯空白
      - 空模板（`_is_user_md_template` 全量剥离后无真数据）
      - 内容里没有任何 bullet 形式（free-form markdown）→ 兜底为
        整篇单条 item（避免完全失导；此时 LLM 不会原子化，但
        比丢弃强）
    """
    path = _find_exact_file(workspace, "USER.md")
    if path is None:
        return
    raw = _read_utf8(path)
    if raw is None:
        return
    text = raw.strip()
    if not text:
        return
    if _is_user_md_template(text):
        return

    bullets = _parse_user_md_bullets(raw)
    if bullets:
        for bullet in bullets:
            source_ref = (
                f"USER.md:L{bullet.start_line}"
                if bullet.start_line == bullet.end_line
                else f"USER.md:L{bullet.start_line}-L{bullet.end_line}"
            )
            yield MemoryItem(
                user_id=user_id,
                text=_format_user_bullet_text(bullet.label, bullet.value),
                source_ref=source_ref,
                heading=None,
                start_line=bullet.start_line,
                end_line=bullet.end_line,
            )
        return

    # 兜底：free-form USER.md（无 bullet 结构）整篇作为一条
    total_lines = raw.count("\n") + (0 if raw.endswith("\n") else 1)
    if total_lines == 0:
        total_lines = 1
    yield MemoryItem(
        user_id=user_id,
        text=text,
        source_ref=f"USER.md:L1-L{total_lines}",
        heading=None,
        start_line=1,
        end_line=total_lines,
    )
