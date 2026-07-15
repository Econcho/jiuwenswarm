"""JSONL 形式的幂等日志，用于可恢复的长期记忆迁移。

每一行是一次写入尝试。判断"是否已经写成功"以该 hash 的**最新一条**
状态为准——不是看历史上有没有成功过。这样：
  - 先 ok 后被手动删掉再重跑，会被正确识别为需要补写。
  - 先失败后成功，被识别为已完成。

条目字段：

    {
      "hash": "<sha256>",
      "source_ref": "MEMORY.md:L12-L28",
      "typetag": "[LONG_TERM]",
      "user_id": "openclaw-main",
      "status": "ok" | "write_error" | "skipped",
      "memory_id": "abc123",         // 仅 status=ok 时出现
      "error": {                     // 仅非 ok 时出现
        "kind": "...",
        "message": "...",
        "attempts": 3
      },
      "ts": "2026-04-17T10:22:33Z",
      "run_id": "20260417a"
    }
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

Status = Literal["ok", "write_error", "skipped"]


@dataclass
class ManifestEntry:
    """一条写入尝试的结构化记录。"""

    hash: str
    source_ref: str
    typetag: str
    user_id: str
    status: Status
    run_id: str
    ts: str = field(
        default_factory=lambda: _dt.datetime.now(
            _dt.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    memory_id: str | None = None
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict:
        """序列化为 JSONL 可写入的 dict，跳过 None 字段。"""
        d: dict[str, Any] = {
            "hash": self.hash,
            "source_ref": self.source_ref,
            "typetag": self.typetag,
            "user_id": self.user_id,
            "status": self.status,
            "run_id": self.run_id,
            "ts": self.ts,
        }
        if self.memory_id is not None:
            d["memory_id"] = self.memory_id
        if self.error is not None:
            d["error"] = self.error
        return d


def append_entry(path: Path, entry: ManifestEntry) -> None:
    """追加一行 JSONL；flush + fsync，避免崩溃时丢日志。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry.to_dict(), ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            # 某些伪文件系统（tmpfs 的某些挂载、网络盘）不支持 fsync
            # 此时优先保证功能可用；flush 已经把数据交给 OS
            pass


def load_entries(path: Path) -> list[dict]:
    """按顺序加载全部 JSONL 行；容忍末尾的一条损坏行。"""
    if not path.is_file():
        return []
    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                # 写入时的崩溃可能产生不完整的末尾行，跳过即可
                continue
    return out


def _parse_ts(s: str) -> _dt.datetime:
    """把 ISO-8601 时间串解析成 datetime；失败时返回 epoch。

    接受末尾的 Z 或 +00:00。解析失败不抛异常——损坏的 ts 不应让
    整个 manifest 报废。
    """
    if not s:
        return _dt.datetime.fromtimestamp(0, tz=_dt.timezone.utc)
    try:
        # fromisoformat 在 3.11+ 支持 Z 后缀；之前版本需要替换
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(s)
    except ValueError:
        return _dt.datetime.fromtimestamp(0, tz=_dt.timezone.utc)


def latest_per_hash(path: Path) -> dict[str, dict]:
    """按 hash 分组，返回每个 hash 最新的那条。

    比较方式：先按 ts 解析成 datetime，再按 (ts, 在文件中的行号) 比较。
    行号作为次要排序键，处理同一 ts 出现多次的情况（同秒内重试）。
    """
    best: dict[str, tuple[_dt.datetime, int, dict]] = {}
    for lineno, e in enumerate(load_entries(path)):
        h = e.get("hash")
        if not h:
            continue
        ts = _parse_ts(str(e.get("ts") or ""))
        key = (ts, lineno)
        prev = best.get(h)
        if prev is None or key > (prev[0], prev[1]):
            best[h] = (ts, lineno, e)
    return {h: v[2] for h, v in best.items()}


def ok_hashes(path: Path) -> set[str]:
    """视作"已经写进去"的 hash 集合——export 以此决定跳过哪些。

    规则：最新状态在 {ok, skipped} 之内就算。
      - ok：本次或之前的某次 run 成功写入
      - skipped：之前已 ok，后续 run 只是重新扫到后跳过（审计痕迹）
    反过来：一旦后续写入了 write_error（手动清理脚本或重试失败），
    最新状态变 write_error，就不再算 ok——下次 export 会重新补写。

    设计取舍：把 skipped 等同 ok 而不是只认 ok，是因为 export 会把
    "被 already 命中"的条目也记一条 skipped 审计日志；若只认 ok，
    下次 run 调 ok_hashes 时会把这些条目误判为需要重写。
    """
    return {h for h, e in latest_per_hash(path).items()
            if e.get("status") in ("ok", "skipped")}


def failures(path: Path) -> list[dict]:
    """最新状态既不是 ok 也不是 skipped 的条目——人工排查用。"""
    return [e for e in latest_per_hash(path).values()
            if e.get("status") not in ("ok", "skipped")]


def write_failures_file(failures_path: Path,
                        entries: Iterable[dict]) -> int:
    """把失败条目写到 failures.jsonl 供人工审阅，返回写入条数。"""
    failures_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(failures_path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
            count += 1
    return count
