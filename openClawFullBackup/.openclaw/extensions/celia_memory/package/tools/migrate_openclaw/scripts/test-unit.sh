#!/usr/bin/env bash
# migrate_openclaw 离线单测入口。
#
# 用法：
#   scripts/test-unit.sh                  # 跑全部 93 个用例
#   scripts/test-unit.sh -v               # 详细模式
#   scripts/test-unit.sh -k fence         # 按关键词过滤
#   scripts/test-unit.sh --coverage       # 启用覆盖率（需 pytest-cov）
#   scripts/test-unit.sh --install-deps   # 缺 pytest 时自动装
#
# 完全离线：不需要 Celia 服务，不需要真实 OpenClaw workspace。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# scripts/ -> migrate_openclaw/ -> tools/ -> /
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
TESTS_REL="tools/migrate_openclaw/tests"

COVERAGE=0
INSTALL_DEPS=0
EXTRA_ARGS=()

# ---- 解析参数 --------------------------------------------------
for arg in "$@"; do
    case "$arg" in
        --coverage)
            COVERAGE=1
            ;;
        --install-deps)
            INSTALL_DEPS=1
            ;;
        -h|--help)
            # 打印脚本顶部的 # 开头的文档注释（不含 shebang 和 set 行）
            sed -n '/^set /q;2,$p' "$0"
            exit 0
            ;;
        *)
            EXTRA_ARGS+=("$arg")
            ;;
    esac
done

# ---- 依赖检查 --------------------------------------------------
ensure_pytest() {
    if python3 -c "import pytest" 2>/dev/null; then
        return 0
    fi
    echo "[warn] pytest 未安装"
    if [[ "$INSTALL_DEPS" -eq 1 ]]; then
        echo "[info] 自动安装 pytest..."
        pip3 install --user --break-system-packages pytest \
            || pip3 install --user pytest
        return 0
    fi
    cat <<EOF
请先安装 pytest：

    pip install --user pytest

或重跑本脚本加 --install-deps 自动安装。
EOF
    exit 1
}

ensure_pytest

if [[ "$COVERAGE" -eq 1 ]]; then
    if ! python3 -c "import pytest_cov" 2>/dev/null; then
        echo "[warn] pytest-cov 未安装"
        if [[ "$INSTALL_DEPS" -eq 1 ]]; then
            pip3 install --user --break-system-packages pytest-cov \
                || pip3 install --user pytest-cov
        else
            echo "    pip install --user pytest-cov  或加 --install-deps"
            exit 1
        fi
    fi
    EXTRA_ARGS+=(
        "--cov=tools.migrate_openclaw"
        "--cov-report=term-missing"
    )
fi

# ---- 执行 ------------------------------------------------------
echo "[info] 项目根: $REPO_ROOT"
echo "[info] 测试目录: $TESTS_REL"
echo

cd "$REPO_ROOT"
# -v 默认加上；用户另外传 -v 不冲突（pytest 允许重复 -v）
if [[ "${#EXTRA_ARGS[@]}" -gt 0 ]]; then
    python3 -m pytest "$TESTS_REL" -v "${EXTRA_ARGS[@]}"
else
    python3 -m pytest "$TESTS_REL" -v
fi
