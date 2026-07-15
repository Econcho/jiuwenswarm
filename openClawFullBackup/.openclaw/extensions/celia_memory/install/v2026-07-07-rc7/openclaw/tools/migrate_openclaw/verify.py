"""迁移后的校验 + 报告生成。

out/ 目录下产出三个文件：
  - report-<user_id>.md       人类可读摘要
  - report-<user_id>.json     机器可读
  - failures-<user_id>.jsonl  非 ok 条目，供人工排查
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .ir import TYPE_TAG, normalize_text
from .manifest import (
    failures as manifest_failures,
    latest_per_hash,
    write_failures_file,
)
from .mcp_client import CeliaClient, McpError

# 用于近似匹配的空白规范化
_WS_RE = re.compile(r"\s+")


def _normalize_for_match(text: str) -> str:
    """把文本折叠成单行小写，空白全部挤成单个空格；仅用于近似比对。"""
    return _WS_RE.sub(" ", text).strip().lower()


def _looks_like_migrated_content(text: str) -> bool:
    """粗判是不是迁移工具写入的记录。

    旧版以 `[LONG_TERM]` 前缀做标记；新版（MR !70+）用 JSON 包装成
    `[{"role":"user","content":"..."}]`。这里同时接受两种形态，让
    verify 在过渡期对两种历史 content 都能识别。
    """
    s = text.lstrip()
    if s.upper().startswith(TYPE_TAG):
        return True
    # JSON 数组 + user role：format 对上了就算我们写的。
    return s.startswith('[{"role":"user"') or s.startswith("[{'role':'user'")


@dataclass
class VerifyReport:
    """校验结果汇总结构。"""

    user_id: str
    counts: dict[str, int] = field(default_factory=dict)
    celia_found: int = 0
    sample_results: list[dict] = field(default_factory=list)
    recall_results: list[dict] = field(default_factory=list)

    def success_rate(self) -> float:
        """ok 占最新状态总数的比例。分母为 0 时按 1 处理。"""
        ok = self.counts.get("ok", 0)
        total = sum(self.counts.values()) or 1
        return ok / total


def _count_manifest(entries: dict[str, dict]) -> dict[str, int]:
    """按 status 统计每个 hash 的最新状态分布。"""
    counts: dict[str, int] = {}
    for e in entries.values():
        s = str(e.get("status") or "unknown")
        counts[s] = counts.get(s, 0) + 1
    return counts


def _fetch_celia_records(client: CeliaClient, user_id: str,
                        limit: int = 10000) -> list[dict]:
    """把 Celia 侧该 userId 的全部记录拉回来，供对账使用。"""
    response = client.memory_list(user_id=user_id, limit=limit)
    l2 = response.get("l2")
    if isinstance(l2, dict):
        return list(l2.get("records") or [])
    return list(response.get("records") or [])


def _record_text(record: dict) -> str:
    """从 Celia 返回的 record dict 里拿出内容字段，兼容命名变化。"""
    for key in ("content", "text", "body"):
        val = record.get(key)
        if val:
            return str(val)
    return ""


def _sample_readback(manifest_entries: dict[str, dict],
                     records: list[dict],
                     workspace_items: list[dict] | None = None) -> list[dict]:
    """逐条检查 manifest.ok 的条目在 Celia 侧是否能找到。

    优先按 memory_id 精确匹配；memory_id 缺失或找不到时，
    退化到按 content 前缀 + 规范化文本包含关系近似匹配——
    这样可以保护服务端响应 schema 微变化时的对账可用性。
    长期记忆数量通常 < 1000，全量检查代价可接受。
    """
    # 用 memory_id 建索引
    records_by_id: dict[str, dict] = {
        str(r.get("id")): r for r in records if r.get("id")
    }
    # 规范化文本 → record 的索引，用于兜底匹配
    records_by_norm_text: dict[str, dict] = {}
    for r in records:
        txt = _record_text(r)
        if _looks_like_migrated_content(txt):
            key = _normalize_for_match(txt)
            records_by_norm_text.setdefault(key, r)

    # 如果提供了原 workspace items，可以用它们的原文做兜底匹配
    workspace_by_hash: dict[str, str] = {}
    if workspace_items:
        for wi in workspace_items:
            workspace_by_hash[wi["hash"]] = wi.get("text") or ""

    out: list[dict] = []
    for e in manifest_entries.values():
        if e.get("status") != "ok":
            continue
        mid = e.get("memory_id") or ""
        h = e.get("hash") or ""
        matched_by = "none"
        found = False
        record_text = ""

        # 第一优先级：精确 id 匹配
        if mid and str(mid) in records_by_id:
            record_text = _record_text(records_by_id[str(mid)])
            found = True
            matched_by = "id"

        # 第二优先级：按 workspace 原文的规范化匹配。同时尝试两种历史
        # 包装：新版 JSON `[{role:user,content:...}]` 和旧版 `[LONG_TERM]`
        # 前缀，哪个能命中就算找到。
        if not found and h in workspace_by_hash:
            expected = workspace_by_hash[h]
            new_wrapped = json.dumps(
                [{"role": "user", "content": expected.strip()}],
                ensure_ascii=False, separators=(",", ":"),
            )
            keys = [
                _normalize_for_match(new_wrapped),
                _normalize_for_match(f"{TYPE_TAG} {expected}"),
            ]
            for key in keys:
                if key in records_by_norm_text:
                    record_text = _record_text(records_by_norm_text[key])
                    found = True
                    matched_by = "content"
                    break

        matched = found and _looks_like_migrated_content(record_text)
        out.append({
            "hash": h,
            "source_ref": e.get("source_ref"),
            "memory_id": mid or None,
            "found": found,
            "matched": matched,
            "matched_by": matched_by,
        })
    return out


def _recall_probes(client: CeliaClient, user_id: str,
                   probes: list[str], top_k: int = 5) -> list[dict]:
    """跑一组召回探针，检查关键词能命中长期记忆。"""
    results: list[dict] = []
    for query in probes:
        try:
            resp = client.memory_search_l2(
                user_id=user_id, query=query, top_k=top_k,
            )
            hits = resp.get("results") or []
            has_hit = any(
                _looks_like_migrated_content(str(h.get("content") or ""))
                for h in hits
            )
            results.append({
                "query": query,
                "hits": len(hits),
                "has_longterm_hit": has_hit,
            })
        except McpError as e:
            # 单条探针失败不影响其他探针
            results.append({
                "query": query,
                "error": {"kind": e.kind, "message": e.message},
            })
    return results


def run(*, user_id: str, client: CeliaClient, manifest_path: Path,
        out_dir: Path,
        recall_probes: list[str] | None = None,
        workspace_items: list[dict] | None = None) -> VerifyReport:
    """执行校验主流程并落地三份产物。"""
    out_dir.mkdir(parents=True, exist_ok=True)

    # 阶段一：读 manifest
    latest = latest_per_hash(manifest_path)
    report = VerifyReport(user_id=user_id)
    report.counts = _count_manifest(latest)

    # 阶段二：拉 Celia 侧记录
    try:
        records = _fetch_celia_records(client, user_id)
    except McpError as e:
        print(f"  [warn] memory_list failed: [{e.kind}] {e.message}")
        records = []
    report.celia_found = sum(
        1 for r in records if _looks_like_migrated_content(_record_text(r))
    )

    # 阶段三：逐条对账
    report.sample_results = _sample_readback(
        latest, records, workspace_items,
    )

    # 阶段四：可选的召回探针
    if recall_probes:
        report.recall_results = _recall_probes(
            client, user_id, recall_probes,
        )

    # 阶段五：落地三份产物
    failures_path = out_dir / f"failures-{user_id}.jsonl"
    count = write_failures_file(failures_path, manifest_failures(manifest_path))
    print(f"  failures: {count} -> {failures_path}")

    json_path = out_dir / f"report-{user_id}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "user_id": user_id,
            "counts": report.counts,
            "celia_found": report.celia_found,
            "manifest_ok": report.counts.get("ok", 0),
            "diff": report.celia_found - report.counts.get("ok", 0),
            "sample_results": report.sample_results,
            "recall_results": report.recall_results,
            "success_rate": report.success_rate(),
        }, f, ensure_ascii=False, indent=2)

    md_path = out_dir / f"report-{user_id}.md"
    md_path.write_text(_render_markdown(report), encoding="utf-8")
    print(f"  report: {md_path}")
    return report


def _render_markdown(r: VerifyReport) -> str:
    """把 VerifyReport 渲染成中文 Markdown 摘要。"""
    lines: list[str] = []
    lines.append(f"# OpenClaw → Celia 长期记忆迁移报告 ({r.user_id})")
    lines.append("")

    # 清单统计
    lines.append("## 清单统计（按 hash 最新状态）")
    lines.append("")
    lines.append("| status | count |")
    lines.append("|---|---:|")
    for status in sorted(r.counts):
        lines.append(f"| {status} | {r.counts[status]} |")
    total = sum(r.counts.values())
    ok = r.counts.get("ok", 0)
    pct = f"{r.success_rate() * 100:.2f}%" if total else "n/a"
    lines.append("")
    lines.append(f"**总计** ok={ok} / total={total}  成功率={pct}")
    lines.append("")

    # Celia 侧对账
    lines.append("## Celia 端对账")
    lines.append("")
    lines.append(f"- manifest.ok：{ok}")
    lines.append(f"- Celia 侧找到 [LONG_TERM]：{r.celia_found}")
    diff = r.celia_found - ok
    status_label = "一致" if diff == 0 else ("Celia 多" if diff > 0 else "缺失")
    lines.append(f"- 差值：{diff:+d}  ({status_label})")
    lines.append("")

    # 回读校验
    if r.sample_results:
        found_ct = sum(1 for s in r.sample_results if s["found"])
        matched_ct = sum(1 for s in r.sample_results if s["matched"])
        by_id = sum(1 for s in r.sample_results if s.get("matched_by") == "id")
        by_content = sum(
            1 for s in r.sample_results if s.get("matched_by") == "content"
        )
        lines.append("## 回读校验")
        lines.append("")
        lines.append(
            f"- 检查条目：{len(r.sample_results)}  "
            f"found={found_ct}  matched={matched_ct}"
        )
        lines.append(
            f"- 匹配方式：by_id={by_id}  by_content_fallback={by_content}"
        )
        mismatches = [s for s in r.sample_results if not s["matched"]]
        if mismatches:
            lines.append("")
            lines.append("### 不匹配（最多 10 条）")
            for s in mismatches[:10]:
                lines.append(
                    f"- `{s['hash'][:12]}` `{s['source_ref']}` — "
                    f"found={s['found']} matched={s['matched']}"
                )
        lines.append("")

    # 召回探针
    if r.recall_results:
        lines.append("## 召回探针")
        lines.append("")
        for rp in r.recall_results:
            q = rp.get("query")
            if "error" in rp:
                lines.append(f"- `{q}` → ERROR: {rp['error']}")
                continue
            lines.append(
                f"- `{q}` → hits={rp['hits']}  "
                f"long_term_hit={rp['has_longterm_hit']}"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


# normalize_text 从 ir 导入只是为了避免 lint 报未用；verify 侧真正调用的是
# _normalize_for_match。保留导入是方便将来扩展逻辑时直接可用。
_ = normalize_text
