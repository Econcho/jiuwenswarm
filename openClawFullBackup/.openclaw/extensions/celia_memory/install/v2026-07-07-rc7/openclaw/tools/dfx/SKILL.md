---
name: dfx-tokens
description: 当用户想统计 / 分析 GsPD Memory 的 token 消耗或 LLM 调用明细时使用。触发场景：「跑一下 dfx 报告」「parse token logs」「统计 LLM 调用」「analyse memory token usage」。技能调用 `parse_token_logs.py`（来自 GsPD 日志）或 `parse_token_baseline.py`（来自 OpenClaw token-logger 的 JSONL）。Read-only：只看日志、写一个新 SQLite，不改任何运行态。
---

# dfx-tokens 技能 / DFX token analysis

## 用途 / Purpose

包装 `tools/dfx/` 下的两支 Python 分析脚本，提供统一的 token 消耗报表。

Wraps the two Python scripts under `tools/dfx/` to give a one-command token
consumption report.

## 模式 / Modes

### 1. 引擎日志解析 / Engine log parsing

```bash
python3 tools/dfx/parse_token_logs.py <logfile>... [--db=dfx.db] [--summary]
```

输出三张 SQLite 表：`mem_round_stats`、`mem_fg_llm_calls`、`mem_bg_llm_calls`。

Outputs three SQLite tables: `mem_round_stats`, `mem_fg_llm_calls`,
`mem_bg_llm_calls`.

### 2. OpenClaw 基线解析 / OpenClaw baseline parsing

```bash
python3 tools/dfx/parse_token_baseline.py --file token_baseline.jsonl [--json]
```

按 session 维度汇总 input/output/cache token，给出均值 / 标准差 / 总量。

Per-session aggregation of input/output/cache tokens — mean / stddev / totals.

## 只读 / Read-only

工具不会触碰运行中的服务、数据库或配置。仅读日志、写一个新输出文件。
The tool never touches the running service, DB, or config — only reads logs
and writes one new output file.
