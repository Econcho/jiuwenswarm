#!/bin/sh

# ===================================================================
# 安装技能包（兼容 sh/dash）
# 用法：sh installSkill.sh /path/to/skill.zip
# ===================================================================

set -e

# 定义变量
SCRIPT_NAME=$(basename "$0")
LOG_DIR="/home/sandbox/.openclaw/workspace/logs"
LOG_FILE="$LOG_DIR/skill_install_$(date '+%Y%m%d_%H%M%S').log"
SKILL_ZIP="$1"
mkdir -p "$LOG_DIR" && touch "$LOG_FILE"
# 去掉 .zip 后缀（如果存在）
SKILL_NAME="${SKILL_ZIP##*/}"   # 取 basename
SKILL_NAME="${SKILL_NAME%.zip}" # 去掉 .zip 后缀
# 定义实际安装目标目录
INSTALL_DIR="/home/sandbox/.openclaw/workspace/skills"
rm -rf "$INSTALL_DIR/$SKILL_NAME"
# 日志函数
log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# 错误处理函数
error_exit() {
  log "错误：$*"
  exit 1
}

# 检查参数
if [ -z "$SKILL_ZIP" ]; then
  log "使用方法错误：需要传入一个 ZIP 文件路径"
  log "用法：$SCRIPT_NAME /path/to/skill.zip"
  exit 1
fi

# 检查 ZIP 文件是否存在
if [ ! -f "$SKILL_ZIP" ]; then
  error_exit "ZIP 文件不存在：$SKILL_ZIP"
fi

# 检查是否为 ZIP 文件（简单判断）
if ! file "$SKILL_ZIP" | grep -q "Zip archive data"; then
  error_exit "文件不是有效的 ZIP 格式：$SKILL_ZIP"
fi

TEMP_DIR=$(mktemp -d)
trap "rm -rf '$TEMP_DIR'" EXIT

log "正在解压技能包：$SKILL_ZIP"
unzip -q "$SKILL_ZIP" -d "$TEMP_DIR" || error_exit "解压失败：$SKILL_ZIP"

if unzip -l "$SKILL_ZIP" | grep -q "SKILL.md"; then
    TARGET_DIR="$INSTALL_DIR/$SKILL_NAME"
    mkdir -p "$TARGET_DIR"
    TEMP_CONTENTS=$(ls -A "$TEMP_DIR")
    if [ "$(echo "$TEMP_CONTENTS" | wc -l)" -eq 1 ] && [ -d "$TEMP_DIR/$TEMP_CONTENTS" ]; then
        cp -r "$TEMP_DIR/$TEMP_CONTENTS"/* "$TARGET_DIR/"
    else
        cp -r "$TEMP_DIR"/* "$TARGET_DIR/"
    fi
    log "技能包已成功安装到：$TARGET_DIR"
else
    cp -r "$TEMP_DIR"/* "$INSTALL_DIR/"
    log "技能包已成功安装到：$INSTALL_DIR"
fi
# 构造安装结果 JSON（用 echo + cat 安全生成）
cat <<EOF >/tmp/skill_install_result.json
{
  "success": true,
  "message": "技能包安装成功",
  "skill_name": "${SKILL_NAME}",
  "log_file": "$LOG_FILE",
  "timestamp": "$(date '+%Y-%m-%d %H:%M:%S')"
}
EOF

# 输出结果
cat /tmp/skill_install_result.json

# 删除临时文件
rm -f /tmp/skill_install_result.json

rm ${SKILL_ZIP}

log "技能包安装成功：$SKILL_NAME"