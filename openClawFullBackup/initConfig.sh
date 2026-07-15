#!/bin/bash
# ***********************************************************************
# Copyright: (c) Huawei Technologies Co., Ltd. 2019. All rights reserved.
# OpenClaw 初始化配置脚本（优化版：安全、容错、无cd、无sudo）
# 版本：1.4.0（含详细错误日志与绝对路径优化）
# ***********************************************************************

# ================ 日志配置 =================
set -ex
LOG_DIR="/home/sandbox/.openclaw/workspace/logs"
LOG_FILE="${LOG_DIR}/init_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "$LOG_DIR"
SECONDS=0
# 定义日志写入函数（同时输出到屏幕和文件）
log() {
  local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
  echo "[$timestamp] $@" | tee -a "$LOG_FILE"
}

log_error() {
  local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
  echo "[$timestamp] ❌ ERROR: $@" | tee -a "$LOG_FILE"
}

# ================ 默认参数值 =================
apiKey=""
userId=""
wsUrl1=""
wsUrl2=""
agentId=""
apiId=""
fileUploadUrl=""
pushUrl=""
modelUrl=""
cVersion="latest"
rVersion="latest"
llmUrl=""
model=""
zipUrl=""
CMD_CVERSION=""

# ================ 参数解析 =================
while [[ $# -gt 0 ]]; do
  case $1 in
  -k | --apiKey)
    apiKey="$2"
    shift 2
    ;;
  -u | --userId)
    userId="$2"
    shift 2
    ;;
  -c | --cVersion)
    CMD_CVERSION="$2"
    shift 2
    ;;
  -ws1 | --wsUrl1)
    wsUrl1="$2"
    shift 2
    ;;
  -ws2 | --wsUrl2)
    wsUrl2="$2"
    shift 2
    ;;
  -f | --fileUploadUrl)
    fileUploadUrl="$2"
    shift 2
    ;;
  -p | --pushUrl)
    pushUrl="$2"
    shift 2
    ;;
  -b | --modelUrl)
    modelUrl="$2"
    shift 2
    ;;
  -a | --agentId)
    agentId="$2"
    shift 2
    ;;
  -i | --apiId)
    apiId="$2"
    shift 2
    ;;
  -m | --model)
    model="$2"
    shift 2
    ;;
  -zip)
    zipUrl="$2"
    shift 2
    ;;
  -h | --help)
    echo "用法: $0 [选项]"
    echo "  -c, --cVersion      小艺插件版本号（默认：从${CONFIG_FILE}读取，命令行参数优先级更高）"
    echo "  -k, --apiKey          API Key"
    echo "  -u, --userId          User ID"
    echo "  -ws1, --wsUrl1       WebSocket URL"
    echo "  -ws2, --wsUrl2       WebSocket URL"
    echo "  -f, --fileUploadUrl   文件上传地址"
    echo "  -p, --pushUrl         推送消息服务地址"
    echo "  -b, --modelUrl          模型 API 基地址"
    echo "  -a, --agentId           Agent ID"
    echo "  -i, --apiId              API ID"
    echo "  -m, --model              模型名称"
    echo "  -h, --help               显示帮助信息"
    exit 0
    ;;
  *)
    log_error "未知参数: $1"
    shift 1
    ;;
  esac
done

# ================ 必填参数校验 =================
if [[ -z "${apiKey}" || -z "${userId}" ]]; then
  log_error "错误：apiKey 和 userId 是必填参数"
  exit 1
fi

# ================ 读取配置文件（优先级最高） =================
CONFIG_FILE="/home/sandbox/.openclaw_conf"
XIAOYI_CHANNEL_VERSION=""
if [[ -f "$CONFIG_FILE" ]]; then
  XIAOYI_CHANNEL_VERSION=$(grep -E "^[[:space:]]*xiaoyiChannelVersion[[:space:]]*=" "$CONFIG_FILE" | sed 's/.*=[[:space:]]*//' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
fi
if [[ -n "$XIAOYI_CHANNEL_VERSION" ]]; then
  cVersion="$XIAOYI_CHANNEL_VERSION"
elif [[ -n "$CMD_CVERSION" ]]; then
  cVersion="$CMD_CVERSION"
fi

# 1. 先清空
#parts=()
#
## 2. 使用 awk 按 ### 分割，并替换为空格，然后读取
#read -ra parts <<< "$(echo "$wsUrl1" | awk -F'###' '{print $1, $2, $3}')"

# 3. 检查长度
#if [ ${#parts[@]} -lt 3 ]; then
#    echo "❌ 分割失败，parts 数量: ${#parts[@]}"
#    echo "内容: ${parts[@]}"
#    exit 1
#fi
#wsUrl=${parts[0]}
#llmUrl=${parts[1]}
#rVersion=${parts[2]}

# ================ 开始记录日志 =================
log "========================================"
log "🚀 OpenClaw 初始化脚本启动 (v1.4.0)"
log "📅 时间：$(date)"
log "📂 日志文件：$LOG_FILE"
log "========================================"
log "🔧 使用参数："
log "  API Key: ${apiKey:0:10}..."
log "  User ID: ${userId}"
log "  cVersion: ${cVersion}"
log "  rVersion: ${rVersion}"
log "  llmUrl: ${llmUrl}"
log "  wsUrl1: ${wsUrl1}"
log "  wsUrl2: ${wsUrl2}"
log "  File Upload URL: ${fileUploadUrl}"
log "  Push URL: ${pushUrl}"
log "  Base URL: ${modelUrl}"
log "  Model: ${model}"
log "========================================"

# ================ 1. 基础环境配置 =================
#log "🛠️ 正在配置 SSH 环境..."
## 注意：移除 sudo，假设用户已有权限
## 如果 sshd 启动需要 sudo，请确保当前用户有权限，或联系管理员
#if ! sudo -u combox /usr/sbin/sshd -f /home/combox/.ssh/longshin_sshd/sshd_config 2>&1 | tee -a "$LOG_FILE"; then
#    log "⚠️ sshd 配置执行完毕（可能有非关键错误，已记录）"
#fi
#
## 使用绝对路径，避免 cd
#ssh-keyscan -p 2222 -t ed25519,ecdsa,rsa 127.0.0.1 localhost 2>/dev/null >> "/home/sandbox/.ssh/known_hosts"
#chown sandbox:sandbox /home/sandbox/.ssh/known_hosts
#log "✅ SSH 环境配置完成"
# 停止服务
if ! python3 -m supervisor.supervisorctl stop all 2>&1 | tee -a "$LOG_FILE"; then
  log "⚠️ 停止服务失败（可能已停止），继续..."
fi

cVersion=$(echo "${cVersion}" | tr -d '[:space:]')
# 或者更温和的 trim
cVersion=$(echo "${cVersion}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
# ================ 2. 安装小艺插件 =================
log "📦 正在安装插件 @ynhcj/xiaoyi-channel@${cVersion}..."

# 使用绝对路径，避免 cd
PLUGIN_DIR="/home/sandbox/.openclaw"
if [[ ! -d "$PLUGIN_DIR" ]]; then
  log_error "错误：插件目录不存在：$PLUGIN_DIR"
  exit 1
fi

cd "$PLUGIN_DIR" || {
  log_error "无法进入插件目录：$PLUGIN_DIR"
  exit 1
}

install_xiaoyi_plugin() {
  log "[INFO] 开始下载小艺插件包..."
  NPM_OUTPUT=$(npm pack @ynhcj/xiaoyi-channel@"${cVersion}" 2>&1)
  NPM_RC=$?
  echo "$NPM_OUTPUT" >> "$LOG_FILE"
  if [[ $NPM_RC -ne 0 ]]; then
    log_error "[ERROR] npm pack 下载失败"
    return 1
  fi
  FILENAME=""
  while IFS= read -r line; do
    cleaned=$(echo "$line" | tr -d '\r\n')
    if [[ "$cleaned" == *.tgz ]]; then
      FILENAME="$cleaned"
    fi
  done <<< "$NPM_OUTPUT"
  if [[ -z "$FILENAME" ]]; then
    log_error "[ERROR] FILENAME 为空，npm pack 可能未生成包名"
    return 1
  fi
  log "[INFO] 下载完成，开始安装插件..."
  bash -c "openclaw plugins install '$FILENAME'" >> "$LOG_FILE" 2>&1
  INSTALL_RC=$?
  log "[INFO] 安装完成，清理临时文件..."
  rm -f "$FILENAME"
  if [[ $INSTALL_RC -ne 0 ]]; then
    log_error "[ERROR] openclaw plugins install 安装失败"
    return 1
  fi
  log "[OK] 小艺插件安装成功"
  return 0
}

install_xiaoyi_plugin || {
  log "[WARN] 小艺插件安装失败，准备重试..."
  install_xiaoyi_plugin || {
    log_error "[ERROR] 小艺插件重试安装仍失败"
    exit 1
  }
}
# 回到安全目录（可选，但建议）
cd /home/sandbox/ || {
  log_error "无法返回主目录：/home/sandbox/"
  exit 1
}


# ================ 3. 创建安全密钥文件 =================
log "🔑 正在创建安全密钥文件..."
mkdir -p /home/sandbox/.openclaw
cat >/home/sandbox/.openclaw/.xiaoyienv <<EOF
PERSONAL-API-KEY=${apiKey}
PERSONAL-UID=${userId}
SERVICE_URL=${fileUploadUrl}
EOF
chmod 600 /home/sandbox/.openclaw/.xiaoyienv
chmod -R 700 /home/sandbox/.openclaw/extensions/xiaoyi-channel
log "✅ 安全密钥文件创建完成"

# ================ 4. 配置 Channel =================
log "⚙️ 正在配置 Channel (xiaoyi-channel)..."
# 确保 Python 脚本路径正确，或使用绝对路径
CONFIG_SCRIPT="/home/sandbox/update_config.py" # 假设路径，需根据实际情况调整
if [[ ! -f "$CONFIG_SCRIPT" ]]; then
  log_error "错误：配置文件脚本不存在：$CONFIG_SCRIPT"
  exit 1
fi

if ! python3 "$CONFIG_SCRIPT" --input /home/sandbox/openclaw.json --output /home/sandbox/.openclaw/openclaw.json \
  --wsUrl1 "${wsUrl1}" --wsUrl2 "${wsUrl2}" --apiKey "${apiKey}" \
  --agentId "${agentId}" --apiId "${apiId}" --uid "${userId}" \
  --model "${model}" --fileUploadUrl "${fileUploadUrl}" --pushUrl "${pushUrl}" --overwrite 2>&1 | tee -a "$LOG_FILE"; then
  log_error "配置 Channel 失败"
  exit 1
fi
log "✅ Channel 配置完成"

# ================ 5. 配置模型 =================
log "🤖 正在配置模型..."

if ! openclaw config set --batch-json "$(cat <<EOF
[
  {"path": "models.providers.xiaoyiprovider.headers.x-uid", "value": "${userId}"},
  {"path": "models.providers.xiaoyiprovider.headers.x-api-key", "value": "${apiKey}"},
  {"path": "models.providers.xiaoyiprovider.baseUrl", "value": "${modelUrl}"},
  {"path": "agents.defaults.memorySearch.remote.baseUrl", "value": "${fileUploadUrl}/celia-claw/v1/rest-api"}
]
EOF
)" 2>&1 | tee -a "$LOG_FILE"; then
  log_error "模型配置失败"
  exit 1
fi

rm -f ~/.openclaw/agents/main/agent/models.json
log "✅ 模型配置完成"

log "celia_memory 配置"
#export CELIA_CONFIG_DIR="${CELIA_CONFIG_DIR:-/home/sandbox/.openclaw}"
#export CELIA_INSTALL_ROOT="${CELIA_INSTALL_ROOT:-$CELIA_CONFIG_DIR/extensions/celia_memory}"
#export CELIA_TARBALL_DIR="${CELIA_TARBALL_DIR:-$CELIA_INSTALL_ROOT/package}"
#export CELIA_SUPERVISORD_CONF="${CELIA_SUPERVISORD_CONF:-/home/sandbox/supervisord.conf}"
#export CELIA_LANG="${CELIA_LANG:-zh}"
#export CELIA_SKIP_GATEWAY_RESTART="${CELIA_SKIP_GATEWAY_RESTART:-1}"
#export CELIA_EXTRACT_ROOT="$CELIA_RELEASE_ROOT"
#export CELIA_PLUGIN_DIR="$CELIA_RELEASE_ROOT"
#export CELIA_INSTALL_ROOT="$CELIA_RELEASE_ROOT"
export CELIA_CONFIG_DIR="${CELIA_CONFIG_DIR:-/home/sandbox/.openclaw}"
export CELIA_EXTRACT_ROOT="${CELIA_EXTRACT_ROOT:-$CELIA_CONFIG_DIR/extensions/celia_memory/package}"
export CELIA_SUPERVISORD_CONF="${CELIA_SUPERVISORD_CONF:-/home/sandbox/supervisord.conf}"
export CELIA_SKIP_GATEWAY_RESTART="${CELIA_SKIP_GATEWAY_RESTART:-1}"

bash "$CELIA_EXTRACT_ROOT/scripts/install.sh"

log "config dir: $CELIA_CONFIG_DIR"
log "install root: $CELIA_EXTRACT_ROOT"
log "skip gateway restart: $CELIA_SKIP_GATEWAY_RESTART"
log "celia_memory 配置完成"

# ================ 6. 启动服务与后续操作 =================
log "🚀 正在启动服务并执行后续任务..."
if ! python3 -m supervisor.supervisorctl start all 2>&1 | tee -a "$LOG_FILE"; then
  log_error "启动服务失败"
  exit 1
fi

if [[ -n "$zipUrl" ]]; then
  UPDATE_MD_SCRIPT="/home/sandbox/update_md.py" # 假设路径
  if [[ ! -f "$UPDATE_MD_SCRIPT" ]]; then
    log_error "错误：update_md.py 脚本不存在：$UPDATE_MD_SCRIPT"
    exit 1
  fi
  if ! python3 "$UPDATE_MD_SCRIPT" --url "$zipUrl" 2>&1 | tee -a "$LOG_FILE"; then
    log_error "执行 update_md.py 失败"
    exit 1
  fi
  log "✅ update_md.py 已执行，URL: $zipUrl"
else
  log "⚠️ zipUrl 为空，跳过执行"
fi

# 下载技能包
log "📥 正在下载技能包..."
if ! curl -fsSL https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/install/install.sh | bash -s -- --no-skills 2>&1 | tee -a "$LOG_FILE"; then
  log "⚠️ 下载技能包失败（可能网络问题），继续..."
fi

# 备份配置
log "💾 正在备份配置文件..."
cp /home/sandbox/.openclaw/openclaw.json /home/sandbox/.openclaw/original.json
log "✅ 配置文件已备份"

# ================ 7. 耗时统计 =================
elapsed_time="$SECONDS"
log "========================================"
log "✅ OpenClaw 初始化配置完成"
log "⏱️  总耗时：$(format_time "$elapsed_time")"
log "📂 日志文件路径：$LOG_FILE"
log "========================================"

# 格式化时间函数
format_time() {
  local t=$1
  if ((t < 60)); then
    echo "${t} 秒"
  elif ((t < 3600)); then
    local m=$((t / 60))
    local s=$((t % 60))
    printf "%d 分 %02d 秒" "$m" "$s"
  else
    local h=$((t / 3600))
    local m=$(((t % 3600) / 60))
    local s=$((t % 60))
    printf "%d 小时 %d 分 %02d 秒" "$h" "$m" "$s"
  fi
}