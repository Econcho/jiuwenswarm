#!/bin/bash
# seed_demo.sh - 把 demo 种子的 mem_conversation 追加到生产 Celia DB。
#
# 零停服 append 模式：
#   - 不停 openclaw-gateway / celia_memory_mcp_server，不删 .db-shm / .db-wal
#   - 只对 main.mem_conversation 做 INSERT（id 由 AUTOINCREMENT 重新分配，
#     避免跟生产已用 id 冲突）；不动派生表 / 向量索引 / FTS5 / celia_meta 游标
#   - 新批次行 status=0 / processed_at_ms=NULL，worker 后台轮询时自动消费
#   - 不 VACUUM
#
# 用法（小艺claw 安装 Demo 一句话触发）：
#   nohup sh seed_demo.sh > /home/sandbox/demo/seed_demo.log 2>&1 &
#
# 路径约定（可通过环境变量覆盖）：
#   DEMO_DB   demo 种子 DB（默认: $SCRIPT_DIR/../../Demo/celia_memory.db
#                          或 $SCRIPT_DIR/Demo/celia_memory.db）
#   PROD_DB   生产 DB    （默认: /home/sandbox/.openclaw/workspace/memory/celia_memory.db）
#
# 前提：已经通过 install.sh 装好 Celia（生产 DB 已存在）。
# 重复执行：每跑一次会再次追加；demo 对话会出现 N 份（按需自行去重）。

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

LOG_FILE="${LOG_FILE:-/tmp/celia_seed_demo.log}"
log_info()  { echo -e "${GREEN}[INFO]${NC} $1"  | tee -a "$LOG_FILE"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1" | tee -a "$LOG_FILE"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"   | tee -a "$LOG_FILE"; }
log_step()  { echo -e "${BLUE}[STEP]${NC} $1"   | tee -a "$LOG_FILE"; }
error_exit() { log_error "$1"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

resolve_demo_db() {
    [ -n "${DEMO_DB:-}" ] && [ -f "$DEMO_DB" ] && { echo "$DEMO_DB"; return; }
    local c
    # 候选 1：tarball scripts/ 内调用，向上 2 级到 mirror 根目录
    # 候选 2：mirror 根目录直接调用，跟脚本同级
    for c in "$SCRIPT_DIR/../../Demo/celia_memory.db" \
             "$SCRIPT_DIR/Demo/celia_memory.db"; do
        [ -f "$c" ] && { echo "$c"; return; }
    done
}

DEMO_DB="$(resolve_demo_db)"
PROD_DB="${PROD_DB:-/home/sandbox/.openclaw/workspace/memory/celia_memory.db}"

# 直接用 stdlib sqlite3 完成 INSERT；不动 vec0 / FTS5 虚表，所以不需要
# sqlite_vec 扩展。事务用 sqlite3 模块默认隔离级别（DEFERRED auto-BEGIN），
# 跟 worker 的并发写入由 SQLite WAL 锁机制串行化（毫秒级竞争）。
run_append() {
    log_step "追加 demo mem_conversation 到生产 DB（零停服）..."
    [ -f "$DEMO_DB" ] || error_exit "找不到 demo DB: $DEMO_DB"
    [ -f "$PROD_DB" ] || error_exit "找不到生产 DB: $PROD_DB（请先装 Celia）"
    log_info "demo DB:  $DEMO_DB"
    log_info "prod DB:  $PROD_DB"

    DEMO_DB="$DEMO_DB" PROD_DB="$PROD_DB" python3 - <<'PY'
import os, sqlite3, sys

demo = os.environ["DEMO_DB"]
prod = os.environ["PROD_DB"]

conn = sqlite3.connect(prod, timeout=30.0)  # 等 worker 写锁释放最长 30s
conn.execute("ATTACH DATABASE ? AS src", (demo,))


def cols(schema):
    rows = conn.execute(
        f'PRAGMA "{schema}".table_info(mem_conversation)'
    ).fetchall()
    # PRAGMA: cid, name, type, notnull, dflt_value, pk —— 跳过 cid 比较
    return [(r[1], r[2], r[3], r[4], r[5]) for r in rows]


main_cols = cols("main")
src_cols = cols("src")
if main_cols != src_cols:
    print("[ERROR] mem_conversation schema 不一致，append 中止：",
          file=sys.stderr)
    print(f"  prod: {main_cols}", file=sys.stderr)
    print(f"  demo: {src_cols}",  file=sys.stderr)
    sys.exit(2)

# 跳过 id 列：让 AUTOINCREMENT 给新行分配新 id，避免撞生产已用 id
non_id = [c[0] for c in main_cols if c[0] != "id"]
col_list = ", ".join(f'"{c}"' for c in non_id)

try:
    old_max = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM main.mem_conversation"
    ).fetchone()[0]
    rc = conn.execute(
        f"INSERT INTO main.mem_conversation ({col_list}) "
        f"SELECT {col_list} FROM src.mem_conversation"
    ).rowcount
    # 把刚追加的批次的处理状态字段重置；worker FetchPending 看 status=0
    # 会自动消费，跑 ingest 抽事实/向量/聚合，无需任何外部触发。
    conn.execute(
        "UPDATE main.mem_conversation SET "
        "  status=0, processed_at_ms=NULL, "
        "  retry_count=0, last_error=NULL "
        "WHERE id > ?",
        (old_max,),
    )
    conn.commit()
    print(f"[INFO] 追加 {rc} 行（id > {old_max}），worker 将自动消费")
except Exception as e:
    conn.rollback()
    print(f"[ERROR] {e}", file=sys.stderr)
    sys.exit(1)
finally:
    conn.execute("DETACH DATABASE src")
    conn.close()
PY
}

main() {
    log_step "=== Celia Demo 追加流程开始（零停服）==="
    run_append
    log_info "=== ✓ Demo 对话已追加；服务一直在线 ==="
    log_info "    worker 后台轮询会自动消费追加进来的 mem_conversation"
    log_info "    （观察 [WORKER0] / [INGEST_WINDOW] 日志验证消费进展）"
}

main "$@"
