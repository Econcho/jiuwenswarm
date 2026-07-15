"""长期记忆条目的中间表示（IR）。

openclaw_reader 把 MEMORY.md 解析成 MemoryItem，
export_longterm 再把 MemoryItem 通过 memory_add 写入 Celia。

只处理 LONG_TERM——短期日记/梦境日志不在本工具范围内。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

TYPE_TAG = "[LONG_TERM]"


def normalize_text(text: str) -> str:
    """把文本做一次稳定规范化，供 hash 和去重比较使用。

    只去掉首尾空白 + 每行的尾部空白，保留内部的换行结构。
    这样做的目的是：对编辑器常见的"存盘时修剪尾空格"抖动免疫，
    又不把两个不同的段落误合并成一个。
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    return "\n".join(lines).strip()


def compute_hash(user_id: str, text: str) -> str:
    """计算 MemoryItem 的幂等键。

    键 = sha256(user_id | TYPE_TAG | normalize_text(text))。
    LONG_TERM 不带时间维度，所以不混入日期或阶段字段。
    抽成模块级函数是因为 MemoryItem 是 frozen dataclass，
    避免把可变缓存塞进冻结对象里。
    """
    h = hashlib.sha256()
    h.update(user_id.encode("utf-8"))
    h.update(b"|")
    h.update(TYPE_TAG.encode("utf-8"))
    h.update(b"|")
    h.update(normalize_text(text).encode("utf-8"))
    return h.hexdigest()


@dataclass(frozen=True)
class MemoryItem:
    """一条已经可以直接迁移的长期记忆条目。"""

    user_id: str
    # 原文（未加 TYPE_TAG 前缀），保留作者手写的格式。
    text: str
    # 源引用，如 "MEMORY.md:L12-L28"，只用于人工排查，不进入 hash。
    source_ref: str
    # 可选的段落标题，仅用于报告展示，不参与身份判定。
    heading: str | None = None
    start_line: int = 0
    end_line: int = 0

    def hash(self) -> str:
        """返回本条记忆的幂等键。

        sha256 对短文本是微秒级操作，不需要缓存；
        频繁调用就直接算即可。
        """
        return compute_hash(self.user_id, self.text)

    def celia_content(self) -> str:
        """拼出发送给 Celia memory_add 的 content 字符串。

        生产环境 `mem_conversation.content` 是 `JSON.stringify(cleaned)`
        的字符串，其中 `cleaned` 是 `[{role, content}, ...]` 数组
        （见 memory-plugin/src/memory/hooks.ts `roundText = JSON.stringify(cleaned)`）。
        迁移的 item 沿用同一 shape 包装成**单个 user 回合**的 JSON 数组，
        好处：
          1. 和正常 chat 捕获写入的 row 格式统一，C 侧 FormatWindow
             拼出来的 dialogue 窗口、LLM 看到的输入形状一致。
          2. 抽取管线针对这个 shape 做过优化（prompt few-shot 也按此）。
          3. 不再塞 `[LONG_TERM]` 前缀——那原本是迁移工具内部 tag，
             LLM 看到会产生困扰，去掉更干净。
        """
        payload = [{"role": "user", "content": self.text.strip()}]
        # ensure_ascii=False 让中文保持可读，separators 去空格压紧
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容的 dict。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryItem":
        """从 dict 反序列化；容忍历史字段（静默忽略未知键）。"""
        known = {k: d[k] for k in (
            "user_id", "text", "source_ref",
            "heading", "start_line", "end_line",
        ) if k in d}
        return cls(**known)


def to_memory_add_args(item: MemoryItem) -> dict:
    """拼出 Celia memory_add 工具调用的 arguments dict。"""
    return {
        "user_id": item.user_id,
        "content": item.celia_content(),
        "scope": "user",
    }
