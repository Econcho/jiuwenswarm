#!/bin/bash
# uninstall.sh — Celia 插件卸载/禁用脚本
#
# 用法:
#   ./uninstall.sh           # 默认 disable
#   ./uninstall.sh disable   # 禁用 Celia（保留文件，仅改 openclaw.json）
#   ./uninstall.sh remove    # 完全卸载（禁用 + 删除插件文件/二进制）
#   ./uninstall.sh help      # 帮助
set -e

# ---- 颜色与日志 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

UNINSTALL_LOG_FILE="/tmp/celia_uninstall.log"

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"  | tee -a "$UNINSTALL_LOG_FILE"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1" | tee -a "$UNINSTALL_LOG_FILE"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"    | tee -a "$UNINSTALL_LOG_FILE"; }
log_step()  { echo -e "${BLUE}[STEP]${NC} $1"    | tee -a "$UNINSTALL_LOG_FILE"; }

error_exit() { log_error "$1"; exit 1; }

# ---- 配置（支持环境变量覆盖，本地开发时使用） ----
OPENCLAW_CONFIG_DIR="${CELIA_CONFIG_DIR:-/home/sandbox/.openclaw}"
RUNTIME_ROOT="${CELIA_PLUGIN_DIR:-$OPENCLAW_CONFIG_DIR/extensions/celia_memory/install/current}"
OPENCLAW_CONFIG_FILE="$OPENCLAW_CONFIG_DIR/openclaw.json"
SUPERVISOR_CONFIG_FILE="${CELIA_SUPERVISORD_CONF:-/home/sandbox/supervisord.conf}"

# ---- 帮助 ----
show_help() {
    cat <<'EOF'
Celia 插件卸载脚本

用法: ./uninstall.sh [disable|remove|help]

子命令:
  disable  禁用 Celia（保留文件，仅改 openclaw.json 配置）
           之后可通过 install.sh 重新启用
  remove   完全卸载（禁用 + 删除插件文件和二进制）
           数据库 ~/.openclaw/workspace/memory/ 不删除
  help     显示此帮助

默认（无参数）等同于 disable。
EOF
}

# ---- restart_gateway（复用 install.sh 同款） ----
## 异步调度 gateway 重启。**关键：必须 detached + delayed**，否则同步
## supervisorctl restart 会触发 self-kill：
##
##   人工运维或历史 lifecycle skill 触发时，agent 在
##   openclaw-gateway 进程的 sub-tree 下执行。orchestrator → hook →
##   restart_gateway → supervisorctl restart openclaw-gateway = 把
##   gateway 杀掉 = 把自己（hook + orchestrator + agent）一起 SIGKILL。
##
## 等价问题在 install 端 commit c3521a5a 已经修过；uninstall 端这次
## 同步过来，保持两端 pattern 一致。生产实测：发卸载命令后 do_disable
## 走到 restart_services 同步路径，agent 连接断开（用户体感"环境一直
## 在重启"），uninstall 在 do_disable 阶段就被 SIGKILL，后续 do_remove
## 删插件文件 / 删 entries.memory-celia 注册 / 跳过 AGENTS.md / 删软链
## 全部没跑完，状态半残。
##
## 修复：用 setsid + nohup + & + disown 让 supervisorctl 调用脱离当前
## process group。再 sleep N 秒（CELIA_RESTART_DELAY，默认 5）让调用方
## (hook + orchestrator) 完成扫尾才真重启。
##
## 返回 0 表示调度成功（不等待重启完成）；返回 1 仅当连 supervisord
## socket 都摸不到、且 fallback 路径调度失败时。
restart_gateway() {
    local delay="${CELIA_RESTART_DELAY:-5}"

    # 快速路径：supervisorctl 控制 socket 可用
    if command -v supervisorctl >/dev/null 2>&1 \
        && supervisorctl status >/dev/null 2>&1; then
        log_info "异步调度 supervisorctl restart openclaw-gateway（${delay}s 后执行；避免 self-kill）..."
        # detached subprocess: 新 session、忽略 SIGHUP、后台、disown
        ( setsid nohup bash -c "
            sleep $delay
            supervisorctl restart openclaw-gateway >/dev/null 2>&1
        " </dev/null >/dev/null 2>&1 & disown 2>/dev/null || true ) >/dev/null 2>&1
        log_info "✓ gateway 重启已调度"
        return 0
    fi

    # 回退路径：pkill supervisord 然后手起；同样 detach
    log_info "supervisorctl socket 不可用；异步调度 pkill+respawn (${delay}s 后)"
    ( setsid nohup bash -c "
        sleep $delay
        pkill -f 'supervisor.supervisord' 2>/dev/null || true
        sleep 2
        if ! pgrep -f 'supervisor.supervisord' >/dev/null 2>&1; then
            python3 -m supervisor.supervisord -c '$SUPERVISOR_CONFIG_FILE' >/dev/null 2>&1 &
        fi
    " </dev/null >/dev/null 2>&1 & disown 2>/dev/null || true ) >/dev/null 2>&1
    log_info "✓ gateway 重启已调度（pkill+respawn 路径）"
    return 0
}

## AGENTS.md 由 Docker 镜像发布前人工审核维护。
## 卸载阶段不再还原、裁剪或覆盖生产环境 AGENTS.md。
restore_agents_md() {
    log_step "跳过 AGENTS.md 还原"
    log_info "AGENTS.md 由 Docker 镜像发布前人工审核维护，卸载不再变更"
}

restart_services() {
    log_step "调度 openclaw-gateway 异步重启..."
    # restart_gateway 已经异步 detach + delay；它返回 0 仅表示调度成功，
    # 不等待重启完成（也不能等：等就是 self-kill）。retry 循环对异步调度
    # 没意义 —— 重启的成败要等 hook + orchestrator 退出后才显现，运行时
    # 探测交给 reboot 完成后的日志/人工状态检查。
    if ! restart_gateway; then
        log_error "无法调度 gateway 重启（连 supervisord socket 都摸不到）"
        exit 1
    fi

    log_info "重启已调度，hook 退出后由 supervisord 在后台执行"
}

# ---- disable: 禁用 Celia（仅改配置） ----
do_disable() {
    log_step "禁用 Celia 插件..."

    if [ ! -f "$OPENCLAW_CONFIG_FILE" ]; then
        error_exit "未找到 $OPENCLAW_CONFIG_FILE"
    fi

    # 备份
    cp "$OPENCLAW_CONFIG_FILE" "$OPENCLAW_CONFIG_FILE.bak.$(date +%Y%m%d%H%M%S)"
    log_info "已备份 openclaw.json"

    # Python 修改 JSON
    _CFG="$OPENCLAW_CONFIG_FILE" python3 - <<'PYEOF'
import json, os

cfg_path = os.environ["_CFG"]
with open(cfg_path, "r") as f:
    data = json.load(f)

plugins = data.setdefault("plugins", {})

# 1. 删除 slots.memory
slots = plugins.get("slots", {})
if "memory" in slots:
    del slots["memory"]

# 2. entries.memory-celia.enabled → false
entries = plugins.get("entries", {})
if "memory-celia" in entries:
    if isinstance(entries["memory-celia"], dict):
        entries["memory-celia"]["enabled"] = False
    else:
        entries["memory-celia"] = {"enabled": False}

# 保留 load.paths 和 installs，方便 re-enable

with open(cfg_path, "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
PYEOF

    # 校验
    if ! python3 -c "import json; json.load(open('$OPENCLAW_CONFIG_FILE'))"; then
        error_exit "openclaw.json 修改后 JSON 格式错误"
    fi
    log_info "openclaw.json 已更新：memory-celia disabled"

    # 杀掉 MCP server
    if pgrep -f "celia_memory_mcp_server" >/dev/null; then
        log_info "停止 celia_memory_mcp_server..."
        pkill -f "celia_memory_mcp_server" 2>/dev/null || true
        sleep 2
        if pgrep -f "celia_memory_mcp_server" >/dev/null; then
            log_warn "celia_memory_mcp_server 仍在运行，将在 gateway 重启后自动终止"
        else
            log_info "✓ celia_memory_mcp_server 已停止"
        fi
    fi

    restart_services

    log_info "Celia 已禁用"
    log_info "重新启用方式："
    log_info "  方式 A: bash install.sh （幂等，推荐）"
    log_info "  方式 B: 手动编辑 openclaw.json："
    log_info "    plugins.slots.memory = \"memory-celia\""
    log_info "    plugins.entries.memory-celia.enabled = true"
    log_info "    然后 supervisorctl restart openclaw-gateway"
}

# ---- remove: 完全卸载 ----
do_remove() {
    log_step "完全卸载 Celia 插件..."

    # 先执行 disable
    do_disable

    log_step "清理插件文件..."

    # 删除新 layout 下的运行时产物（RUNTIME_ROOT == .openclaw/extensions/
    # celia_memory/install/current 软链 → 当前版本目录）。memory-plugin/、
    # shared/、celiaclaw/ 归本插件所有；migrate_openclaw/ 仅清理旧版残留。
    local removed=0
    for target in \
        "$RUNTIME_ROOT/memory-plugin" \
        "$RUNTIME_ROOT/shared" \
        "$RUNTIME_ROOT/celiaclaw" \
        "$RUNTIME_ROOT/migrate_openclaw"; do
        if [ -d "$target" ]; then
            rm -rf "$target"
            log_info "已删除 $target"
            removed=$((removed + 1))
        fi
    done

    if [ -f "$RUNTIME_ROOT/bin/celia_memory_mcp_server" ]; then
        rm -f "$RUNTIME_ROOT/bin/celia_memory_mcp_server"
        log_info "已删除 celia_memory_mcp_server"
        removed=$((removed + 1))
    fi

    if [ -f "$RUNTIME_ROOT/bin/celia_memory_mcp_server.old" ]; then
        rm -f "$RUNTIME_ROOT/bin/celia_memory_mcp_server.old"
    fi
    # bin/ 现在可能已空，清掉避免遗留空壳
    if [ -d "$RUNTIME_ROOT/bin" ] && [ -z "$(ls -A "$RUNTIME_ROOT/bin" 2>/dev/null)" ]; then
        rmdir "$RUNTIME_ROOT/bin" 2>/dev/null || true
    fi

    # 从 openclaw.json 清理残留注册
    _CFG="$OPENCLAW_CONFIG_FILE" python3 - <<'PYEOF'
import json, os

cfg_path = os.environ["_CFG"]
with open(cfg_path, "r") as f:
    data = json.load(f)

plugins = data.setdefault("plugins", {})

# load.paths 移除 memory-plugin 路径
paths = plugins.get("load", {}).get("paths", [])
plugins.setdefault("load", {})["paths"] = [
    p for p in paths
    if "memory-plugin" not in p
]

# entries 删除
plugins.get("entries", {}).pop("memory-celia", None)

# installs 删除
plugins.get("installs", {}).pop("memory-celia", None)

with open(cfg_path, "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
PYEOF

    log_info "已从 openclaw.json 移除 memory-celia 注册"

    restore_agents_md

    # 清掉 install/{current,previous,celiaclaw} 软链 — 防御性，避免后续
    # status 解引用到刚被删的目录得到 ENOENT。orchestrator 路径也做
    # current/previous；celiaclaw facade 软链由 install hook 创建，这里一并清。
    # hook 单独跑（绕过 orchestrator）时这里兜底。
    local install_root_parent="$OPENCLAW_CONFIG_DIR/extensions/celia_memory/install"
    for link in current previous celiaclaw; do
        if [ -L "$install_root_parent/$link" ]; then
            rm -f "$install_root_parent/$link"
            log_info "已删除软链 $install_root_parent/$link"
        fi
    done

    log_info "共清理 $removed 个文件/目录"
    log_info ""
    log_info "以下内容未删除（需手动清理）："
    log_info "  数据库:  $OPENCLAW_CONFIG_DIR/workspace/memory/celia_memory/celia_memory.db"
    log_info "  凭据:    $OPENCLAW_CONFIG_DIR/.xiaoyienv"
}

# ---- 主入口 ----
main() {
    echo "Celia 插件卸载脚本" | tee -a "$UNINSTALL_LOG_FILE"
    date | tee -a "$UNINSTALL_LOG_FILE"

    local cmd="${1:-disable}"

    case "$cmd" in
        disable)
            do_disable
            ;;
        remove)
            do_remove
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            log_error "未知子命令: $cmd"
            show_help
            exit 1
            ;;
    esac

    log_info "完成"
}

main "$@"
