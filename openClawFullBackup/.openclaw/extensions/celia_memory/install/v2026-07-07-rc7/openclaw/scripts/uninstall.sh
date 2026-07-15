#!/bin/bash
# uninstall.sh — openclaw platform uninstall hook
#
# 调用方：GaussPD_Skills/skills/celiaclaw/experimental-memory-uninstall 技能。
# 技能层负责 DB / config 备份（如 --purge 也含 DB 删除），本脚本只做
# 通用 openclaw 运行时下的"目录清理 + openclaw.json 反向修改"。
#
# 模式（由 $CELIA_UNINSTALL_MODE 透传）：
#   disable  仅在 openclaw.json 里关掉 memory-celia 条目，文件保留
#   remove   disable + 删除插件文件 + 删除二进制（默认）
#   purge    remove + 删除 ~/.openclaw 下的本插件 DB / 配置（技能层会再次确认）
#
# 退出码：0 成功；50 关键步骤失败。
set -e

PLUGIN_DIR="${CELIA_PLUGIN_DIR:-$HOME/.openclaw/plugins}"
CONFIG_DIR="${CELIA_CONFIG_DIR:-$HOME/.openclaw}"
CONFIG_FILE="$CONFIG_DIR/openclaw.json"
DB_PATH="${CELIA_DB_PATH:-$CONFIG_DIR/memory/celia_memory.db}"
LOG_FILE="${CELIA_UNINSTALL_LOG_PATH:-/tmp/celia_uninstall_openclaw.log}"
MODE="${CELIA_UNINSTALL_MODE:-remove}"

log_info()  { echo "[INFO]  $1" | tee -a "$LOG_FILE"; }
log_warn()  { echo "[WARN]  $1" | tee -a "$LOG_FILE"; }
log_error() { echo "[ERROR] $1" | tee -a "$LOG_FILE"; }

# ---- disable: 把 memory-celia 从 openclaw.json 的 plugins.entries 移除 ----
disable_plugin() {
    [ -f "$CONFIG_FILE" ] || { log_warn "openclaw.json 不存在，跳过 disable"; return; }
    cp "$CONFIG_FILE" "$CONFIG_FILE.bak.uninstall.$(date +%Y%m%d%H%M%S)"
    _CFG="$CONFIG_FILE" python3 - <<'PYEOF'
import json, os
p = os.environ['_CFG']
with open(p) as f:
    cfg = json.load(f)
entries = cfg.get('plugins', {}).get('entries', {})
if 'memory-celia' in entries:
    del entries['memory-celia']
    print('removed plugins.entries.memory-celia')
installs = cfg.get('plugins', {}).get('installs', {})
if 'memory-celia' in installs:
    del installs['memory-celia']
    print('removed plugins.installs.memory-celia')
with open(p, 'w') as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write('\n')
PYEOF
    log_info "已从 openclaw.json 中 disable memory-celia"
}

# ---- remove: disable + 删插件文件 ----
remove_files() {
    [ -d "$PLUGIN_DIR/memory-plugin" ]         && rm -rf "$PLUGIN_DIR/memory-plugin" && log_info "rm memory-plugin/"
    [ -d "$PLUGIN_DIR/context-engine-plugin" ] && rm -rf "$PLUGIN_DIR/context-engine-plugin" && log_info "rm context-engine-plugin/"
    [ -d "$PLUGIN_DIR/shared" ]                && rm -rf "$PLUGIN_DIR/shared" && log_info "rm shared/"
    [ -f "$PLUGIN_DIR/celia_memory_mcp_server" ]       && rm -f "$PLUGIN_DIR/celia_memory_mcp_server" && log_info "rm celia_memory_mcp_server"
}

# ---- purge: remove + 删 DB + 配置 ----
purge_data() {
    if [ -f "$DB_PATH" ]; then
        rm -f "$DB_PATH"
        log_info "rm DB $DB_PATH"
    fi
    if [ -d "$CONFIG_DIR/memory" ]; then
        rm -rf "$CONFIG_DIR/memory"
        log_info "rm $CONFIG_DIR/memory/"
    fi
}

case "$MODE" in
    disable) disable_plugin ;;
    remove)  disable_plugin; remove_files ;;
    purge)   disable_plugin; remove_files; purge_data ;;
    *) log_error "未知 CELIA_UNINSTALL_MODE='$MODE' (期望 disable|remove|purge)"; exit 50 ;;
esac

log_info "openclaw 卸载完成 (mode=$MODE)"
