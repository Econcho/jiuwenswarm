#!/bin/sh

# ===================================================================
# 安装技能包（兼容 sh/dash）
# 用法：sh skillHubInstall.sh  gog
# ===================================================================

set -e

# 定义变量
SCRIPT_NAME=$(basename "$0")
LOG_DIR="/home/sandbox/.openclaw/workspace/logs"
LOG_FILE="$LOG_DIR/skill_hub_install_$(date '+%Y%m%d_%H%M%S').log"
SKILL_NAME="$1"
mkdir -p "$LOG_DIR" && touch "$LOG_FILE"
# 定义实际安装目标目录
INSTALL_DIR="/home/sandbox/.openclaw/workspace/skills"

INSTALL_TMP_DIR="/home/sandbox/.openclaw/workspace/tmp"
# 日志函数
log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}


# 错误处理函数
error_exit() {
  log "错误：$*"
  log "安装流程中断，检查日志：$LOG_FILE"
  exit 1
}

# 检查参数
if [ -z "$SKILL_NAME" ]; then
  log "使用方法错误：需要传入一个 技能名"
  exit 1
fi

# 执行 skillhub 安装，并显式检查返回码
if ! skillhub --dir "${INSTALL_TMP_DIR}" install "$SKILL_NAME"; then
  error_exit "skillhub 安装失败：$SKILL_NAME"
fi

rm -rf $INSTALL_DIR/$SKILL_NAME

mv ${INSTALL_TMP_DIR}/$SKILL_NAME $INSTALL_DIR


log "技能包已成功安装到：$INSTALL_DIR/$SKILL_NAME"
# 构造安装结果 JSON（用 echo + cat 安全生成）
cat <<EOF >/tmp/skill_hub_install_result.json
{
  "success": true,
  "message": "技能包安装成功",
  "skill_name": "${SKILL_NAME}",
  "log_file": "$LOG_FILE",
  "timestamp": "$(date '+%Y-%m-%d %H:%M:%S')"
}
EOF

# 输出结果
cat /tmp/skill_hub_install_result.json

# 删除临时文件
rm -f /tmp/skill_hub_install_result.json


log "技能包安装成功：$SKILL_NAME"