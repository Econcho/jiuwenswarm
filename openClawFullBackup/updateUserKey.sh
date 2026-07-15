#!/bin/sh

# ===================================================================
# 动态更新apikey （兼容 sh/dash）+ 耗时统计
# 用法：sh updateUserKey.sh $1
# ===================================================================

set -e

# 定义变量
SCRIPT_NAME=$(basename "$0")
LOG_DIR="/home/sandbox/.openclaw/workspace/logs"
LOG_FILE="$LOG_DIR/update_user_key_$(date '+%Y%m%d_%H%M%S').log"
userKey="$1"
mkdir -p "$LOG_DIR" && touch "$LOG_FILE"

# 记录开始时间（纳秒级）
start_time=$(date +%s.%N)

# 日志函数
log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# 错误处理函数
error_exit() {
  log "错误：$*"
  # 计算并记录耗时（即使失败也记录）
  end_time=$(date +%s.%N)
  duration=$(echo "$end_time $start_time" | awk '{printf "%.6f", $1 - $2}')
  log "脚本执行耗时：${duration}s"
  exit 1
}

# 检查参数
if [ -z "$userKey" ]; then
  log "使用方法错误：需要传入一个 userKey"
  log "用法：$SCRIPT_NAME userKey"
  error_exit "缺少 userKey 参数"
fi

log "开始更新用户密钥：$userKey"

# 执行 sed 替换（模糊匹配）
sed -i "s|PERSONAL-API-KEY=.*|PERSONAL-API-KEY=${userKey}|" /home/sandbox/.openclaw/.xiaoyienv

# 更新 openclaw 配置
openclaw config set channels.xiaoyi-channel.apiKey "${userKey}"
openclaw config set models.providers.xiaoyiprovider.headers.x-api-key "${userKey}"

# 计算耗时（关键：避免 - 被误解析）
end_time=$(date +%s.%N)
duration=$(echo "$end_time $start_time" | awk '{printf "%.6f", $1 - $2}')

log "密钥更新成功，总耗时：${duration}s"

# 构造安装结果 JSON（用 echo + cat 安全生成）
cat <<EOF >/tmp/user_key_update_result.json
{
  "success": true,
  "message": "秘钥更新成功",
  "userKey": "${userKey}",
  "log_file": "$LOG_FILE",
  "timestamp": "$(date '+%Y-%m-%d %H:%M:%S')",
  "duration_seconds": ${duration}
}
EOF

# 输出结果
cat /tmp/user_key_update_result.json

# 删除临时文件
rm -f /tmp/user_key_update_result.json

# 删除现有的 models.json
rm ~/.openclaw/agents/main/agent/models.json
openclaw models status

# 清理 gateway
if pgrep -f "openclaw-gateway" >/dev/null; then
  pkill -TERM -f "openclaw-gateway" 2>/dev/null
  sleep 1
  pkill -KILL -f "openclaw-gateway" 2>/dev/null
fi

# 最终日志确认
log "userKey 更新成功，脚本执行耗时：${duration}s"