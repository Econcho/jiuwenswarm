"""长期记忆导出：把 MEMORY.md 切出的每一段用 memory_add 写入 Celia。

行为：
  - 幂等：通过 manifest.jsonl 做去重，重跑会跳过最新状态为 ok 的 hash。
  - 顺序写入（数量通常不大，且顺序有助于调试日志对应）。
  - 单条写失败时记录 write_error 并继续下一条——不中断整批，
    失败条目在同一次 run 的后续重跑中会再尝试。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .ir import MemoryItem, TYPE_TAG
from .manifest import ManifestEntry, append_entry, ok_hashes
from .mcp_client import CeliaClient, McpError
from .openclaw_reader import read_long_term


@dataclass
class ExportStats:
    """一次 export 的统计摘要。"""

    attempted: int = 0  # 本次遍历到的条目总数
    written: int = 0    # 本次真实写入（含 dry-run 的模拟）
    skipped: int = 0    # 幂等命中被跳过的条目
    failed: int = 0     # 本次新增的 write_error

    def summary(self) -> str:
        return (
            f"LONG_TERM  attempted={self.attempted}  "
            f"written={self.written}  skipped={self.skipped}  "
            f"failed={self.failed}"
        )


def run(*, workspace: Path, user_id: str, client: CeliaClient,
        manifest_path: Path, run_id: str,
        dry_run: bool = False,
        reader: Callable[[Path, str], Iterable[MemoryItem]] = read_long_term,
        ingest_mode: str | None = None,
        ) -> ExportStats:
    """执行导出主流程。参数全部 keyword-only，避免调用方位置错位。

    `reader` 控制 items 来源，默认为 `read_long_term`（只读 Memory.md）。
    迁移场景下 CLI 会传 `read_all_for_migration`，先导 USER.md 再导
    Memory.md——把"用户画像"放第一条入库，即便后续 Memory.md 段落因
    LLM 抖动失败，画像仍已就位、首轮召回不会"完全失忆"。

    `ingest_mode` 直接透传给 `memory_add`：
      - None / 省略 → 服务端 `immediate` 同步 LLM 提取；短内容 OK，
        markdown 文档（如完整 USER.md）常被整篇存为一条 CAT_UNKNOWN。
      - "deferred" → 入队 mem_conversation，由 InstanceWorker 按对话
        窗口管线批量提取出多条原子 L2 事实。迁移场景**首选**。
      - "deferred-urgent" → 同 deferred，但立即 signal worker。
    """
    stats = ExportStats()
    already = ok_hashes(manifest_path)

    # 预先把列表化，是为了在进度条里打印 "N/total"
    items = list(reader(workspace, user_id))
    total = len(items)

    for idx, item in enumerate(items, start=1):
        prefix = f"[{idx}/{total}]"
        h = item.hash()
        stats.attempted += 1

        # 阶段一：幂等过滤
        # 已 ok 的 hash 直接跳过；不再追加 status="skipped"，避免每次
        # 重装都把 manifest 拉长一轮（前序 ok 行已经代表了这个 hash 的
        # 终态，再记 skipped 只会污染日志）。
        if h in already:
            stats.skipped += 1
            print(f"  {prefix} [skip] {h[:12]} ({item.source_ref})")
            continue

        # 阶段二：dry-run 只打印不发请求
        if dry_run:
            stats.written += 1
            print(f"  {prefix} [dry-run] {h[:12]} ({item.source_ref}) "
                  f"{item.text[:60]!r}")
            continue

        # 阶段三：真正写入
        try:
            response = client.memory_add(
                user_id=user_id,
                content=item.celia_content(),
                scope="user",
                ingest_mode=ingest_mode,
            )
        except McpError as e:
            stats.failed += 1
            append_entry(manifest_path, ManifestEntry(
                hash=h, source_ref=item.source_ref,
                typetag=TYPE_TAG, user_id=user_id,
                status="write_error", run_id=run_id,
                error={
                    "kind": e.kind,
                    "message": e.message,
                    "attempts": e.attempts,
                },
            ))
            print(f"  {prefix} [fail] {h[:12]}: [{e.kind}] {e.message}")
            continue

        # 兼容服务端可能返回的多种 id 字段名
        memory_id = _extract_memory_id(response)
        stats.written += 1
        append_entry(manifest_path, ManifestEntry(
            hash=h, source_ref=item.source_ref,
            typetag=TYPE_TAG, user_id=user_id,
            status="ok", memory_id=memory_id or None,
            run_id=run_id,
        ))
        print(f"  {prefix} [ok]   {h[:12]} -> {memory_id or '(no-id)'}  "
              f"({item.source_ref})")

    print(stats.summary())
    return stats


def _extract_memory_id(response: dict) -> str:
    """从 memory_add 响应中提取 memory id，容忍多种字段命名。

    Celia 的 memory_add 响应字段可能是 id / memoryId / ids[0] 之一，
    这里按常见命名依次尝试，失败时返回空串（调用方自己处理）。
    """
    # 单个 id
    for key in ("id", "memoryId", "memory_id"):
        val = response.get(key)
        if val not in (None, "", 0):
            return str(val)
    # 批量接口可能返回 ids 数组
    ids = response.get("ids")
    if isinstance(ids, list) and ids:
        first = ids[0]
        if first not in (None, "", 0):
            return str(first)
    return ""
