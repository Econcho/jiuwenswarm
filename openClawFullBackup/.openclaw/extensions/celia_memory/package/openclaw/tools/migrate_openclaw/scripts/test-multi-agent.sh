#!/usr/bin/env bash
# migrate_openclaw 多 agent 隔离验证。
#
# 模拟 OpenClaw 下一个用户有多个 agent（比如 main / work）的场景：
#   - 各自的 workspace 内容不同
#   - 分别迁到 openclaw-main / openclaw-work 两个 userId
#   - manifest 文件名按 agent-id 区分，互不覆盖
#   - Celia 侧两边看到的数据互不串
#
# 前置：Celia HTTP 服务在 $CELIA_BASE_URL（默认 http://localhost:3000）。
#
# 用法：
#   scripts/test-multi-agent.sh
#   CELIA_BASE_URL=http://other:4000 scripts/test-multi-agent.sh
#   scripts/test-multi-agent.sh --keep
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

TMPDIR="$(mktemp -d -t migrate-oc-multi-XXXXXX)"
OUT_DIR="$TMPDIR/out"
WS_MAIN="$TMPDIR/ws-main"
WS_WORK="$TMPDIR/ws-work"
mkdir -p "$OUT_DIR" "$WS_MAIN" "$WS_WORK"

cleanup() {
    if [[ "$KEEP" -eq 1 ]]; then
        echo
        echo "[keep] 保留临时目录: $TMPDIR"
    else
        rm -rf "$TMPDIR"
    fi
}
trap cleanup EXIT

# ---- 造两份截然不同的 workspace --------------------------------
cat > "$WS_MAIN/MEMORY.md" <<'EOF'
# 主要偏好

主 agent：偏好 Go 开发后端，在北京工作。

# 个人信息

对花生严重过敏。
EOF

cat > "$WS_WORK/MEMORY.md" <<'EOF'
# 工作上下文

工作 agent：负责 memory-core 模块，团队分布上海深圳。

# 项目约束

CI 不允许 --no-verify。
EOF

echo "============================================================"
echo " migrate_openclaw 多 agent 隔离验证"
echo "============================================================"
echo " workspace main: $WS_MAIN"
echo " workspace work: $WS_WORK"
echo " out-dir:        $OUT_DIR"
echo " Celia base-url:  $CELIA_BASE_URL"
echo "============================================================"
echo

cd "$REPO_ROOT"

# ---- 连通性预检 ------------------------------------------------
if ! curl -fsS "$CELIA_BASE_URL/health" >/dev/null 2>&1; then
    echo "[error] 无法访问 $CELIA_BASE_URL/health — 请先起 Celia HTTP 服务"
    exit 2
fi

# ---- 迁移 agent=main --------------------------------------------
echo "----- agent=main -----"
python3 -m tools.migrate_openclaw.migrate run \
    --workspace "$WS_MAIN" \
    --agent-id main \
    --celia-base "$CELIA_BASE_URL" \
    --out-dir "$OUT_DIR"
echo

# ---- 迁移 agent=work --------------------------------------------
echo "----- agent=work -----"
python3 -m tools.migrate_openclaw.migrate run \
    --workspace "$WS_WORK" \
    --agent-id work \
    --celia-base "$CELIA_BASE_URL" \
    --out-dir "$OUT_DIR"
echo

# ---- 验证：两份 manifest 独立 ----------------------------------
MF_MAIN="$OUT_DIR/manifest-main.jsonl"
MF_WORK="$OUT_DIR/manifest-work.jsonl"

echo "============================================================"
echo " 隔离性检查"
echo "============================================================"

check_file_nonempty() {
    local f="$1"
    if [[ ! -s "$f" ]]; then
        echo "[FAIL] 期望非空: $f"
        exit 3
    fi
    echo "  $f  ($(wc -l < "$f") 行)"
}
check_file_nonempty "$MF_MAIN"
check_file_nonempty "$MF_WORK"

# 两份 manifest 的 user_id 不应互串
if grep -q '"user_id":"openclaw-work"' "$MF_MAIN"; then
    echo "[FAIL] manifest-main 里出现了 openclaw-work"
    exit 3
fi
if grep -q '"user_id":"openclaw-main"' "$MF_WORK"; then
    echo "[FAIL] manifest-work 里出现了 openclaw-main"
    exit 3
fi
echo "[ok] 两份 manifest 的 user_id 互不串"

# 两份 manifest 的 hash 互不相交
python3 - <<PY
import json
main_hashes = {json.loads(l)["hash"] for l in open("$MF_MAIN")}
work_hashes = {json.loads(l)["hash"] for l in open("$MF_WORK")}
overlap = main_hashes & work_hashes
if overlap:
    print(f"[FAIL] main/work manifest 有共同 hash: {overlap}")
    raise SystemExit(3)
print(f"[ok] main 有 {len(main_hashes)} 条 hash，work 有 {len(work_hashes)} 条，两两不相交")
PY

# 两份报告内容互不串
REPORT_MAIN="$OUT_DIR/report-openclaw-main.md"
REPORT_WORK="$OUT_DIR/report-openclaw-work.md"
if [[ -f "$REPORT_MAIN" && -f "$REPORT_WORK" ]]; then
    if grep -q "openclaw-work" "$REPORT_MAIN"; then
        echo "[FAIL] main 报告引用了 openclaw-work"
        exit 3
    fi
    if grep -q "openclaw-main" "$REPORT_WORK"; then
        echo "[FAIL] work 报告引用了 openclaw-main"
        exit 3
    fi
    echo "[ok] main/work 两份报告互不引用对方 userId"
fi

echo
echo "[ok] 多 agent 隔离验证通过。"
