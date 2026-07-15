#!/usr/bin/env python3
"""OpenClaw Token 基线分析脚本。

解析 token-logger 插件输出的 JSONL 文件，生成统计报告。
每行 JSONL 记录一轮对话的 token 消耗（input/output/cache 等）。

用法:
    python3 parse_token_baseline.py --file token_baseline.jsonl
    cat token_baseline.jsonl | python3 parse_token_baseline.py
    python3 parse_token_baseline.py --file token_baseline.jsonl --json

输出:
    默认输出人类可读的统计报告（均值、标准差、总量、session 分组）。
    --json 模式输出机器可读的 JSON 格式。
"""

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime


def parse_args():
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="解析 token-logger JSONL，生成 token 消耗统计报告",
    )
    parser.add_argument(
        "--file", "-f",
        default=None,
        help="JSONL 文件路径（默认从 stdin 读取）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="输出 JSON 格式（机器可读）",
    )
    return parser.parse_args()


def load_records(source):
    """从文件或 stdin 逐行读取 JSONL，跳过格式错误行。"""
    records = []
    errors = 0
    for line_no, line in enumerate(source, 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            records.append(record)
        except json.JSONDecodeError:
            errors += 1
            if errors <= 3:
                print(
                    f"[WARN] 跳过第 {line_no} 行: JSON 解析失败",
                    file=sys.stderr,
                )
    if errors > 3:
        print(
            f"[WARN] 共 {errors} 行解析失败（仅显示前 3 条）",
            file=sys.stderr,
        )
    return records


def mean(values):
    """计算均值。"""
    if not values:
        return 0.0
    return sum(values) / len(values)


def stddev(values):
    """计算标准差。"""
    if len(values) < 2:
        return 0.0
    avg = mean(values)
    variance = sum((x - avg) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(variance)


def format_num(n):
    """格式化数字，千分位分隔。"""
    if isinstance(n, float):
        return f"{n:,.1f}"
    return f"{n:,}"


def ts_to_str(ts_ms):
    """毫秒时间戳转可读字符串。"""
    try:
        return datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return "?"


def analyze(records):
    """分析 JSONL 记录，返回统计字典。"""
    if not records:
        return None

    llm_calls = [r.get("llmCalls", 0) for r in records]
    inputs = [r.get("input", 0) for r in records]
    outputs = [r.get("output", 0) for r in records]
    cache_reads = [r.get("cacheRead", 0) for r in records]
    cache_writes = [r.get("cacheWrite", 0) for r in records]
    totals = [r.get("total", 0) for r in records]
    sys_chars = [r.get("sysPromptChars", 0) for r in records]
    user_chars = [r.get("userChars", 0) for r in records]
    assistant_chars = [r.get("assistantChars", 0) for r in records]

    estimated_count = sum(1 for r in records if r.get("isEstimated"))

    timestamps = [r.get("ts", 0) for r in records if r.get("ts")]
    period_start = ts_to_str(min(timestamps)) if timestamps else "?"
    period_end = ts_to_str(max(timestamps)) if timestamps else "?"

    # session 分组
    sessions = defaultdict(list)
    for r in records:
        sid = r.get("sessionId", "unknown")
        sessions[sid].append(r)

    session_stats = []
    for sid, recs in sessions.items():
        session_stats.append({
            "sessionId": sid,
            "rounds": len(recs),
            "totalInput": sum(r.get("input", 0) for r in recs),
            "totalOutput": sum(r.get("output", 0) for r in recs),
            "totalTokens": sum(r.get("total", 0) for r in recs),
        })

    return {
        "periodStart": period_start,
        "periodEnd": period_end,
        "rounds": len(records),
        "sessions": len(sessions),
        "avgRoundsPerSession": (
            len(records) / len(sessions) if sessions else 0
        ),
        "perRound": {
            "llmCalls": {
                "mean": mean(llm_calls),
                "stddev": stddev(llm_calls),
            },
            "input": {"mean": mean(inputs), "stddev": stddev(inputs)},
            "output": {"mean": mean(outputs), "stddev": stddev(outputs)},
            "cacheRead": {
                "mean": mean(cache_reads),
                "stddev": stddev(cache_reads),
            },
            "cacheWrite": {
                "mean": mean(cache_writes),
                "stddev": stddev(cache_writes),
            },
            "total": {"mean": mean(totals), "stddev": stddev(totals)},
            "sysPromptChars": {
                "mean": mean(sys_chars),
                "stddev": stddev(sys_chars),
            },
            "userChars": {
                "mean": mean(user_chars),
                "stddev": stddev(user_chars),
            },
            "assistantChars": {
                "mean": mean(assistant_chars),
                "stddev": stddev(assistant_chars),
            },
        },
        "grandTotal": {
            "llmCalls": sum(llm_calls),
            "input": sum(inputs),
            "output": sum(outputs),
            "cacheRead": sum(cache_reads),
            "cacheWrite": sum(cache_writes),
            "total": sum(totals),
        },
        "estimated": {
            "count": estimated_count,
            "total": len(records),
            "pct": (
                estimated_count / len(records) * 100
                if records else 0
            ),
        },
        "sessionDetails": session_stats,
    }


def print_report(stats):
    """输出人类可读的统计报告。"""
    print(f"=== OpenClaw Token Baseline Report ===")
    print(f"Period: {stats['periodStart']} ~ {stats['periodEnd']}")
    print(f"Rounds: {stats['rounds']}")
    print()

    pr = stats["perRound"]
    print("── Per-round Average ──")
    for key, label in [
        ("llmCalls", "LLM calls"),
        ("input", "Input tokens"),
        ("output", "Output tokens"),
        ("cacheRead", "Cache read"),
        ("cacheWrite", "Cache write"),
        ("total", "Total"),
        ("sysPromptChars", "Sys prompt chars"),
        ("userChars", "User chars"),
        ("assistantChars", "Assistant chars"),
    ]:
        m = pr[key]["mean"]
        s = pr[key]["stddev"]
        print(f"  {label + ':':22s} {format_num(m):>10s}  (σ={format_num(s)})")
    print()

    print("── Session Summary ──")
    print(f"  Sessions:            {stats['sessions']}")
    print(f"  Avg rounds/session:  {stats['avgRoundsPerSession']:.1f}")
    print()

    gt = stats["grandTotal"]
    print("── Grand Total ──")
    print(f"  LLM calls:   {format_num(gt['llmCalls']):>12s}")
    print(f"  Input:       {format_num(gt['input']):>12s}")
    print(f"  Output:      {format_num(gt['output']):>12s}")
    print(f"  Cache read:  {format_num(gt['cacheRead']):>12s}")
    print(f"  Cache write: {format_num(gt['cacheWrite']):>12s}")
    print(f"  Total:       {format_num(gt['total']):>12s}")
    print()

    est = stats["estimated"]
    if est["count"] > 0:
        print("── Estimated Rounds ──")
        print(
            f"  {est['count']} of {est['total']} rounds "
            f"used char-based estimation ({est['pct']:.0f}%)"
        )
        print()

    if len(stats["sessionDetails"]) > 1:
        print("── Per-session Breakdown ──")
        for s in stats["sessionDetails"]:
            sid_short = s["sessionId"][:16]
            print(
                f"  {sid_short:16s}  "
                f"rounds={s['rounds']:3d}  "
                f"input={format_num(s['totalInput']):>10s}  "
                f"output={format_num(s['totalOutput']):>10s}  "
                f"total={format_num(s['totalTokens']):>10s}"
            )


def main():
    """主入口。"""
    args = parse_args()

    if args.file:
        with open(args.file, "r") as f:
            records = load_records(f)
    else:
        records = load_records(sys.stdin)

    if not records:
        print("无数据", file=sys.stderr)
        sys.exit(1)

    stats = analyze(records)
    if stats is None:
        print("分析失败", file=sys.stderr)
        sys.exit(1)

    if args.json_output:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
    else:
        print_report(stats)


if __name__ == "__main__":
    main()
