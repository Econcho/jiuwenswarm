#!/usr/bin/env bash
# migrate_openclaw 端到端冒烟：造假 workspace → dry-run → export → verify。
#
# 前置：需要一个运行中的 Celia HTTP 服务，默认 http://localhost:3000
#       先在另一个终端起：
#           cd celia_memo && ./build.sh --http
#           source demo/config-local.sh
#           $SERVER_PATH --http 3000 /tmp/migrate-smoke.db
#
# 用法：
#   scripts/test-e2e.sh                            # 默认 CELIA_BASE_URL
#   CELIA_BASE_URL=http://other:4000 scripts/test-e2e.sh
#   scripts/test-e2e.sh --keep                    # 跑完不清理临时目录
#
# 验证点：
#   1. dry-run 能识别出正确段数
#   2. 真实 export 全部 [ok]，manifest 全 ok
#   3. 召回探针能命中 [LONG_TERM]
#   4. 再跑一次 export 全是 [skip]（幂等性）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
CELIA_BASE_URL="${CELIA_BASE_URL:-http://localhost:3000}"

KEEP=0
for arg in "$@"; do
    case "$arg" in
        --keep) KEEP=1 ;;
        -h|--help) sed -n '/^set /q;2,$p' "$0"; exit 0 ;;
    esac
done

# ---- 临时目录 --------------------------------------------------
TMPDIR="$(mktemp -d -t migrate-oc-e2e-XXXXXX)"
WORKSPACE="$TMPDIR/ws"
OUT_DIR="$TMPDIR/out"
mkdir -p "$WORKSPACE" "$OUT_DIR"

cleanup() {
    if [[ "$KEEP" -eq 1 ]]; then
        echo
        echo "[keep] 保留临时目录供排查: $TMPDIR"
    else
        rm -rf "$TMPDIR"
    fi
}
trap cleanup EXIT

# ---- 造一份带边界内容的 MEMORY.md ------------------------------
cat > "$WORKSPACE/MEMORY.md" <<'EOF'
# 用户偏好

用户偏好使用 Go 语言开发后端服务；对 Java 态度中性。

# 个人信息

对花生严重过敏（EpiPen 随身）。

# 常用命令

```bash
# 快速查状态
git status -sb
```

结束说明。
EOF

AGENT_ID="e2e-smoke"
USER_ID="openclaw-$AGENT_ID"

echo "============================================================"
echo " migrate_openclaw 端到端冒烟"
echo "============================================================"
echo " workspace:     $WORKSPACE"
echo " out-dir:       $OUT_DIR"
echo " agent-id:      $AGENT_ID  (user-id: $USER_ID)"
echo " Celia base-url: $CELIA_BASE_URL"
echo "============================================================"
echo

cd "$REPO_ROOT"

# ---- 连通性预检 ------------------------------------------------
echo "[check] 探测 Celia /health ..."
if ! curl -fsS "$CELIA_BASE_URL/health" >/dev/null 2>&1; then
    cat <<EOF
[error] 无法访问 $CELIA_BASE_URL/health
请先在另一终端起 Celia HTTP 服务：

    cd celia_memo
    ./build.sh --http
    source demo/config-local.sh
    \$SERVER_PATH --http 3000 /tmp/migrate-smoke.db

然后重跑本脚本。若端口不同，传 CELIA_BASE_URL 环境变量。
EOF
    exit 2
fi
echo "[check] Celia 可达。"
echo

# ---- 阶段一：dry-run -----------------------------------------
echo "----- 阶段 1/4：dry-run（只切段不写） -----"
python3 -m tools.migrate_openclaw.migrate export \
    --workspace "$WORKSPACE" \
    --agent-id "$AGENT_ID" \
    --celia-base "$CELIA_BASE_URL" \
    --out-dir "$OUT_DIR" \
    --dry-run
echo

# ---- 阶段二：真实 export ---------------------------------------
echo "----- 阶段 2/4：真实 export -----"
python3 -m tools.migrate_openclaw.migrate export \
    --workspace "$WORKSPACE" \
    --agent-id "$AGENT_ID" \
    --celia-base "$CELIA_BASE_URL" \
    --out-dir "$OUT_DIR"
echo

# ---- 阶段三：verify ------------------------------------------
echo "----- 阶段 3/4：verify + 召回探针 -----"
PROBES_FILE="$TMPDIR/probes.txt"
cat > "$PROBES_FILE" <<'EOF'
# 召回探针，每行一个查询
编程语言偏好
食物过敏
git 命令
EOF
python3 -m tools.migrate_openclaw.migrate verify \
    --workspace "$WORKSPACE" \
    --agent-id "$AGENT_ID" \
    --celia-base "$CELIA_BASE_URL" \
    --out-dir "$OUT_DIR" \
    --recall-probes-file "$PROBES_FILE"
echo

# ---- 阶段四：幂等性 --------------------------------------------
echo "----- 阶段 4/4：再跑一次 export，验证幂等 -----"
RERUN_OUT="$(python3 -m tools.migrate_openclaw.migrate export \
    --workspace "$WORKSPACE" \
    --agent-id "$AGENT_ID" \
    --celia-base "$CELIA_BASE_URL" \
    --out-dir "$OUT_DIR")"
echo "$RERUN_OUT"
if echo "$RERUN_OUT" | grep -q "written=0"; then
    echo "[ok] 幂等性验证通过：第二次 run 没有任何新写入"
else
    echo "[FAIL] 幂等性验证失败：第二次 run 仍有写入"
    exit 1
fi
echo

# ---- 产物汇总 --------------------------------------------------
echo "============================================================"
echo " 产物"
echo "============================================================"
for f in \
    "$OUT_DIR/manifest-$AGENT_ID.jsonl" \
    "$OUT_DIR/report-$USER_ID.md" \
    "$OUT_DIR/report-$USER_ID.json" \
    "$OUT_DIR/failures-$USER_ID.jsonl"
do
    if [[ -f "$f" ]]; then
        echo "  $f  ($(wc -l < "$f") 行)"
    fi
done

FAILURES="$OUT_DIR/failures-$USER_ID.jsonl"
if [[ -f "$FAILURES" && -s "$FAILURES" ]]; then
    echo
    echo "[warn] failures 非空，人工排查："
    cat "$FAILURES"
    exit 3
fi

echo
echo "[ok] 端到端冒烟通过。"
