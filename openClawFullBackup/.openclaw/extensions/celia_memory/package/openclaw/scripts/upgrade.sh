#!/bin/bash
# upgrade.sh — openclaw platform upgrade hook
#
# 通用 openclaw 运行时下，"升级"实际就是"重装新版 + 留旧版备份"。备份
# 由 experimental-memory-install 技能层（GaussPD_Skills）统一做（atomic dir swap +
# DB snapshot + retention）。本脚本只做"沙盒侧的状态切换"——这里没有
# 沙盒，所以就是单纯重新调用 install.sh。
#
# 退出码：0 成功；50 关键步骤失败。
set -e

INSTALL_DIR="${CELIA_EXTRACT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PLUGIN_DIR="${CELIA_PLUGIN_DIR:-$HOME/.openclaw/plugins}"
LOG_FILE="${CELIA_UPGRADE_LOG_PATH:-/tmp/celia_upgrade_openclaw.log}"

log_info()  { echo "[INFO]  $1" | tee -a "$LOG_FILE"; }
log_warn()  { echo "[WARN]  $1" | tee -a "$LOG_FILE"; }
log_error() { echo "[ERROR] $1" | tee -a "$LOG_FILE"; }

# 旧版插件目录就地清空（技能层已经做了完整 backup，这里安全）
log_info "清理旧版插件文件..."
[ -d "$PLUGIN_DIR/memory-plugin" ]         && rm -rf "$PLUGIN_DIR/memory-plugin"
[ -d "$PLUGIN_DIR/context-engine-plugin" ] && rm -rf "$PLUGIN_DIR/context-engine-plugin"
[ -d "$PLUGIN_DIR/shared" ]                && rm -rf "$PLUGIN_DIR/shared"

# 委派 install.sh 执行重装
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install_script="$script_dir/install.sh"
if [ ! -x "$install_script" ]; then
    log_error "找不到可执行的 install.sh: $install_script"
    exit 50
fi

log_info "委派 install.sh..."
CELIA_EXTRACT_ROOT="$INSTALL_DIR" \
CELIA_PLUGIN_DIR="$PLUGIN_DIR" \
    bash "$install_script"

log_info "openclaw 升级流程结束"
