#!/usr/bin/env python3
"""DFX Token 日志解析脚本。

从 Celia Memory 引擎日志中提取 [DFX] 结构化事件，
按 tid + 时间区间关联 LLM_CALL 到 TOOL/TASK 上下文，
用 ROUND_STATS 回填 round_index，输出三张 SQLite 分析表。

用法:
    python3 parse_token_logs.py <logfile>... [--db=dfx.db] [--summary]

输出表:
    mem_round_stats   — 轮次汇总（agent + 引擎 + 工具列表）
    mem_fg_llm_calls  — 前台 LLM 调用明细（detail breakdown）
    mem_bg_llm_calls  — 后台 LLM 调用明细
"""

import argparse
import re
import sqlite3
import sys

DFX_PATTERN = re.compile(r"\[DFX\]\s+(.*)")
KV_PATTERN = re.compile(r"(\w+)=(\S+)")


def parse_kv(text):
    """解析 key=value 对为字典。"""
    result = {}
    for match in KV_PATTERN.finditer(text):
        result[match.group(1)] = match.group(2)
    return result


def parse_dfx_lines(logfile):
    """从日志文件提取所有 [DFX] 事件。"""
    events = []
    with open(logfile, "r", encoding="utf-8",
              errors="ignore") as f:
        for line in f:
            m = DFX_PATTERN.search(line)
            if m:
                kv = parse_kv(m.group(1))
                if "ev" in kv:
                    events.append(kv)
    return events


def correlate_events(events):
    """关联事件，输出五类结果。

    Returns:
        tool_calls:  所有工具调用（含无 LLM 的）
        fg_llm:      前台 LLM 调用明细
        bg_llm:      后台 LLM 调用明细
        unmatched:   未关联的 LLM 调用
        round_stats: ROUND_STATS 事件
    """
    active_tools = {}
    active_tasks = {}

    tool_calls = []
    fg_llm = []
    bg_llm = []
    unmatched = []
    round_stats = []

    for ev in events:
        evt = ev.get("ev", "")
        tid = ev.get("tid", "0")

        if evt == "TOOL_BEGIN":
            ctx = {
                "call_id": ev.get("call_id", "0"),
                "session": ev.get("session", ""),
                "round": ev.get("round", "-1"),
                "tool": ev.get("tool", ""),
                "pipe": ev.get("pipe", "UNKNOWN"),
                "ts_begin": int(ev.get("ts", "0")),
                "llm_calls": 0,
                "llm_tokens": 0,
                "search_result_tokens": 0,
            }
            active_tools[tid] = ctx

        elif evt == "TOOL_END":
            ctx = active_tools.pop(tid, None)
            if ctx:
                ctx["ts_end"] = int(ev.get("ts", "0"))
                ctx["end_prompt"] = int(ev.get("prompt", "0"))
                ctx["end_compl"] = int(ev.get("compl", "0"))
                ctx["end_other"] = int(ev.get("other", "0"))
                tool_calls.append(ctx)

        elif evt == "TASK_BEGIN":
            active_tasks[tid] = {
                "task_id": ev.get("task_id", "0"),
                "pipe": ev.get("pipe", "UNKNOWN"),
            }

        elif evt == "TASK_END":
            active_tasks.pop(tid, None)

        elif evt == "LLM_CALL":
            call = {
                "pipe": ev.get("pipe", "UNKNOWN"),
                "prim": ev.get("prim", "?"),
                "step": ev.get("step", "?"),
                "prompt": int(ev.get("prompt", "0")),
                "compl": int(ev.get("compl", "0")),
                "cached": int(ev.get("cached", "0")),
                "reasoning": int(ev.get("reasoning", "0")),
                "total": int(ev.get("total", "0")),
                "dur": int(ev.get("dur", "0")),
                "tid": tid,
                "ts": int(ev.get("ts", "0")),
                "st": int(ev.get("st", "0")),
            }
            if tid in active_tools:
                ctx = active_tools[tid]
                call["call_id"] = ctx["call_id"]
                call["session"] = ctx["session"]
                call["round"] = ctx["round"]
                call["tool"] = ctx["tool"]
                ctx["llm_calls"] += 1
                ctx["llm_tokens"] += call["total"]
                fg_llm.append(call)
            elif tid in active_tasks:
                ctx = active_tasks[tid]
                call["task_id"] = ctx["task_id"]
                bg_llm.append(call)
            else:
                unmatched.append(call)

        elif evt == "TOOL_RESULT_TOKENS":
            if tid in active_tools:
                ctx = active_tools[tid]
                tok = int(ev.get("tokens", "0"))
                rtype = ev.get("type", "")
                if rtype in ("search", "list", "l1_body"):
                    ctx["search_result_tokens"] += tok

        elif evt == "ROUND_STATS":
            round_stats.append(ev)

    return tool_calls, fg_llm, bg_llm, unmatched, round_stats


def backfill_round_index(tool_calls, fg_llm, round_stats):
    """用 ROUND_STATS 的 round 回填工具调用的 round_index。

    逻辑：对每个 session，按时间排列 ROUND_STATS，
    工具调用的 round = 该 session 最近一条（ts <= 工具 ts）
    的 ROUND_STATS.round。若无匹配则取下一条。
    """
    # 按 session 收集 round_stats 时间线
    session_rounds = {}
    for r in round_stats:
        sid = r.get("session", "")
        ts = int(r.get("ts", "0"))
        rd = int(r.get("round", "0"))
        session_rounds.setdefault(sid, []).append(
            (ts, rd))
    for sid in session_rounds:
        session_rounds[sid].sort()

    def find_round(sid, ts):
        """找 session 中 ts 最近的 round。"""
        timeline = session_rounds.get(sid, [])
        if not timeline:
            return -1
        # 找 ts 之后最近的 ROUND_STATS（工具在上报前调）
        for rts, rd in timeline:
            if rts >= ts:
                return rd
        # 都在之前，取最后一个
        return timeline[-1][1]

    for tc in tool_calls:
        tc["round"] = find_round(
            tc["session"], tc["ts_begin"])

    for fc in fg_llm:
        fc["round"] = find_round(
            fc["session"], fc["ts"])


"""插件内部工具（非 agent 调用），不计入 tool_count。"""
PLUGIN_INTERNAL_TOOLS = {
    "memory_open",
    "memory_close",
    "memory_get_l0_global_summary",
    "memory_get_l1_index",
    "memory_report_round_usage",
    "memory_flush",
}


def build_round_summary(tool_calls, fg_llm, round_stats):
    """构建每轮汇总：合并 agent 上报 + 工具调用 + 引擎 token。"""
    # 按 (session, round) 聚合工具调用
    tool_agg = {}
    for tc in tool_calls:
        if not tc["session"] or tc["round"] < 0:
            continue
        key = (tc["session"], tc["round"])
        if key not in tool_agg:
            tool_agg[key] = {
                "tools": [],
                "tool_count": 0,
                "llm_calls": 0,
                "llm_tokens": 0,
                "search_result_tokens": 0,
                "ts": tc["ts_begin"],
            }
        agg = tool_agg[key]
        is_agent = tc["tool"] not in PLUGIN_INTERNAL_TOOLS
        if is_agent:
            agg["tools"].append(tc["tool"])
            agg["tool_count"] += 1
        agg["llm_calls"] += tc["llm_calls"]
        agg["llm_tokens"] += tc["llm_tokens"]
        agg["search_result_tokens"] += tc.get(
            "search_result_tokens", 0)

    # 按 (session, round) 聚合 agent 上报
    agent_agg = {}
    for r in round_stats:
        sid = r.get("session", "")
        rd = int(r.get("round", "0"))
        key = (sid, rd)
        agent_agg[key] = {
            "agent_prompt": int(r.get("agent_prompt", "0")),
            "agent_cache_read": int(
                r.get("agent_cache_read", "0")),
            "agent_compl": int(r.get("agent_compl", "0")),
            "llm_turns": int(r.get("llm_turns", "0")),
            "recall": int(r.get("recall", "0")),
            "auto_recall": int(r.get("auto_recall", "0")),
            "fixed_load_tokens": int(
                r.get("fixed_load_tokens", "0")),
            "prompt_section_bytes": int(
                r.get("prompt_section_bytes", "0")),
            "ts": int(r.get("ts", "0")),
        }

    # 合并（跳过 round=-1 的无 session 工具调用）
    all_keys = set(tool_agg.keys()) | set(agent_agg.keys())
    summaries = []
    for key in sorted(all_keys):
        if key[1] < 0:
            continue
        sid, rd = key
        ta = tool_agg.get(key, {})
        aa = agent_agg.get(key, {})
        tools = ta.get("tools", [])
        # 去重保持顺序
        seen = set()
        unique_tools = []
        for t in tools:
            if t not in seen:
                seen.add(t)
                unique_tools.append(t)
        summaries.append({
            "session": sid,
            "round": rd,
            "agent_prompt": aa.get("agent_prompt", 0),
            "agent_cache_read": aa.get(
                "agent_cache_read", 0),
            "agent_compl": aa.get("agent_compl", 0),
            "llm_turns": aa.get("llm_turns", 0),
            "recall": aa.get("recall", 0),
            "auto_recall": aa.get("auto_recall", 0),
            "tool_count": ta.get("tool_count", 0),
            "tool_list": ",".join(unique_tools),
            "engine_llm_calls": ta.get("llm_calls", 0),
            "engine_total_tokens": ta.get("llm_tokens", 0),
            "fixed_load_tokens": aa.get("fixed_load_tokens", 0),
            "prompt_section_bytes": aa.get(
                "prompt_section_bytes", 0),
            "search_result_tokens": ta.get(
                "search_result_tokens", 0),
            "ts": aa.get("ts", 0) or ta.get("ts", 0),
        })
    return summaries


def create_tables(conn):
    """创建三张分析表。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mem_round_stats (
            session_id              TEXT,
            round_index             INTEGER,
            agent_prompt_tokens     INTEGER,
            agent_cache_read_tokens INTEGER,
            agent_completion_tokens INTEGER,
            llm_turns               INTEGER,
            recall_tokens           INTEGER,
            auto_recall_tokens      INTEGER,
            tool_count              INTEGER,
            tool_list               TEXT,
            engine_llm_calls        INTEGER,
            engine_total_tokens     INTEGER,
            fixed_load_tokens        INTEGER,
            prompt_section_bytes    INTEGER,
            search_result_tokens    INTEGER,
            timestamp_ms            INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mem_fg_llm_calls (
            call_id           INTEGER,
            session_id        TEXT,
            round_index       INTEGER,
            tool_name         TEXT,
            pipeline          TEXT,
            primitive         TEXT,
            step_name         TEXT,
            prompt_tokens     INTEGER,
            completion_tokens INTEGER,
            cached_tokens     INTEGER,
            reasoning_tokens  INTEGER,
            total_tokens      INTEGER,
            duration_ms       INTEGER,
            timestamp_ms      INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mem_bg_llm_calls (
            task_id           INTEGER,
            pipeline          TEXT,
            primitive         TEXT,
            step_name         TEXT,
            prompt_tokens     INTEGER,
            completion_tokens INTEGER,
            cached_tokens     INTEGER,
            reasoning_tokens  INTEGER,
            total_tokens      INTEGER,
            duration_ms       INTEGER,
            timestamp_ms      INTEGER
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_round_session"
        " ON mem_round_stats(session_id, round_index)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fg_call"
        " ON mem_fg_llm_calls(call_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fg_session"
        " ON mem_fg_llm_calls(session_id, round_index)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bg_task"
        " ON mem_bg_llm_calls(task_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bg_pipe"
        " ON mem_bg_llm_calls(pipeline, timestamp_ms)")


def insert_data(conn, summaries, fg_llm, bg_llm):
    """批量插入数据。"""
    conn.executemany(
        "INSERT INTO mem_round_stats VALUES"
        " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(s["session"], s["round"],
          s["agent_prompt"], s["agent_cache_read"],
          s["agent_compl"], s["llm_turns"],
          s["recall"], s["auto_recall"],
          s["tool_count"], s["tool_list"],
          s["engine_llm_calls"],
          s["engine_total_tokens"],
          s["fixed_load_tokens"],
          s["prompt_section_bytes"],
          s["search_result_tokens"],
          s["ts"])
         for s in summaries])
    conn.executemany(
        "INSERT INTO mem_fg_llm_calls VALUES"
        " (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(int(c["call_id"]), c["session"],
          int(c["round"]), c["tool"],
          c["pipe"], c["prim"], c["step"],
          c["prompt"], c["compl"],
          c["cached"], c["reasoning"],
          c["total"], c["dur"], c["ts"])
         for c in fg_llm])
    conn.executemany(
        "INSERT INTO mem_bg_llm_calls VALUES"
        " (?,?,?,?,?,?,?,?,?,?,?)",
        [(int(c["task_id"]), c["pipe"],
          c["prim"], c["step"],
          c["prompt"], c["compl"],
          c["cached"], c["reasoning"],
          c["total"], c["dur"], c["ts"])
         for c in bg_llm])
    conn.commit()


def print_summary(conn):
    """打印汇总报告。"""
    print("=" * 70)
    print("  DFX Token 消耗汇总报告")
    print("=" * 70)

    # 轮次汇总
    print("\n--- 轮次汇总（前台 + Agent） ---\n")
    rows = conn.execute("""
        SELECT session_id, round_index,
               tool_count, tool_list,
               engine_llm_calls, engine_total_tokens,
               agent_prompt_tokens, agent_cache_read_tokens,
               agent_completion_tokens, llm_turns,
               recall_tokens, fixed_load_tokens,
               search_result_tokens
        FROM mem_round_stats
        ORDER BY session_id, round_index
    """).fetchall()
    if rows:
        print(
            f"  {'session':<20} {'round':>5}"
            f" {'turns':>5} {'tools':>5}"
            f" {'eng_tok':>7} {'ag_prompt':>9}"
            f" {'ag_cache':>8} {'ag_compl':>8}"
            f" {'recall':>6} {'fixed':>7}")
        print("  " + "-" * 91)
        for r in rows:
            print(
                f"  {r[0]:<20} {r[1]:>5}"
                f" {r[9]:>5} {r[2]:>5}"
                f" {r[5]:>7} {r[6]:>9}"
                f" {r[7]:>8} {r[8]:>8}"
                f" {r[10]:>6} {r[11]:>7}")
            if r[3]:
                print(f"    tools: {r[3]}")
        # 均值
        avg = conn.execute("""
            SELECT AVG(engine_total_tokens),
                   AVG(agent_prompt_tokens),
                   AVG(agent_cache_read_tokens),
                   AVG(agent_completion_tokens),
                   AVG(fixed_load_tokens),
                   AVG(search_result_tokens),
                   AVG(recall_tokens),
                   AVG(auto_recall_tokens),
                   COUNT(*)
            FROM mem_round_stats
            WHERE round_index >= 0
        """).fetchone()
        if avg[0] is not None:
            print(f"\n  共 {avg[8]} 轮，平均每轮: "
                  f"引擎 {avg[0]:.0f} tokens, "
                  f"agent prompt {avg[1]:.0f} tokens")
            print(f"  agent cache_read {avg[2]:.0f} tokens, "
                  f"agent completion {avg[3]:.0f} tokens")
            print(f"  其中记忆引擎注入: "
                  f"fixed load {avg[4]:.0f} tokens, "
                  f"search result {avg[5]:.0f} tokens, "
                  f"recall {avg[6]:.0f} tokens, "
                  f"auto recall {avg[7]:.0f} tokens")
    else:
        print("  （无数据）")

    # 前台 LLM 明细：按工具
    print("\n--- 前台 LLM 明细（按工具） ---\n")
    rows = conn.execute("""
        SELECT tool_name, pipeline,
               COUNT(*) as calls,
               SUM(prompt_tokens) as prompt,
               SUM(completion_tokens) as compl,
               SUM(cached_tokens) as cached,
               SUM(reasoning_tokens) as reasoning,
               SUM(total_tokens) as total
        FROM mem_fg_llm_calls
        GROUP BY tool_name, pipeline
        ORDER BY total DESC
    """).fetchall()
    if rows:
        print(
            f"  {'tool':<25} {'pipe':<15}"
            f" {'calls':>5} {'prompt':>8}"
            f" {'compl':>8} {'cached':>6}"
            f" {'reason':>6} {'total':>8}")
        print("  " + "-" * 95)
        for r in rows:
            print(
                f"  {r[0]:<25} {r[1]:<15}"
                f" {r[2]:>5} {r[3]:>8}"
                f" {r[4]:>8} {r[5]:>6}"
                f" {r[6]:>6} {r[7]:>8}")
    else:
        print("  （无数据）")

    # 后台
    print("\n--- 后台（按 pipeline） ---\n")
    rows = conn.execute("""
        SELECT pipeline,
               COUNT(*) as calls,
               COUNT(DISTINCT task_id) as triggers,
               SUM(prompt_tokens) as prompt,
               SUM(completion_tokens) as compl,
               SUM(total_tokens) as total
        FROM mem_bg_llm_calls
        GROUP BY pipeline
        ORDER BY total DESC
    """).fetchall()
    if rows:
        print(
            f"  {'pipeline':<15} {'calls':>5}"
            f" {'triggers':>8} {'prompt':>8}"
            f" {'compl':>8} {'total':>8}")
        print("  " + "-" * 58)
        for r in rows:
            print(
                f"  {r[0]:<15} {r[1]:>5}"
                f" {r[2]:>8} {r[3]:>8}"
                f" {r[4]:>8} {r[5]:>8}")
    else:
        print("  （无数据）")

    print()


def main():
    """主入口。"""
    parser = argparse.ArgumentParser(
        description="DFX Token 日志解析")
    parser.add_argument(
        "logfiles", nargs="+", help="日志文件路径（支持多个）")
    parser.add_argument(
        "--db", default="dfx.db",
        help="SQLite 输出路径 (默认: dfx.db)")
    parser.add_argument(
        "--summary", action="store_true",
        help="打印汇总报告")
    args = parser.parse_args()

    events = []
    for lf in args.logfiles:
        ev = parse_dfx_lines(lf)
        if not ev:
            print(f"未找到 [DFX] 事件: {lf}")
        else:
            print(f"  {lf}: {len(ev)} 条 [DFX] 事件")
            events.extend(ev)
    if not events:
        print("所有文件均无 [DFX] 事件")
        sys.exit(1)

    print(f"共解析 {len(events)} 条 [DFX] 事件"
          f"（{len(args.logfiles)} 个文件）")

    tool_calls, fg, bg, unmatched, rounds = \
        correlate_events(events)

    # 用 ROUND_STATS 回填 round_index
    backfill_round_index(tool_calls, fg, rounds)

    # 构建轮次汇总
    summaries = build_round_summary(
        tool_calls, fg, rounds)

    print(
        f"  工具调用: {len(tool_calls)} 次, "
        f"前台 LLM: {len(fg)} 条, "
        f"后台 LLM: {len(bg)} 条, "
        f"未关联: {len(unmatched)} 条, "
        f"轮次汇总: {len(summaries)} 条")

    if unmatched:
        print(
            f"  警告: {len(unmatched)} 条 LLM_CALL"
            " 未关联到 TOOL/TASK")

    conn = sqlite3.connect(args.db)
    create_tables(conn)
    insert_data(conn, summaries, fg, bg)
    print(f"已写入: {args.db}")

    if args.summary:
        print_summary(conn)

    conn.close()


if __name__ == "__main__":
    main()
