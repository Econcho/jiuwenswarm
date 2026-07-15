#!/bin/bash
# token-logger-install.sh — OpenClaw token 消耗观察插件一键安装/卸载
#
# 用法:
#   ./token-logger-install.sh install     # 部署插件 + 注册配置 + 重启
#   ./token-logger-install.sh uninstall   # 移除插件 + 清理配置 + 重启
#   ./token-logger-install.sh status      # 查看安装状态和数据量
#   ./token-logger-install.sh help        # 帮助
set -e

# ---- 颜色与日志 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

LOG_FILE="/tmp/token_logger_install.log"

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"  | tee -a "$LOG_FILE"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1" | tee -a "$LOG_FILE"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"    | tee -a "$LOG_FILE"; }
log_step()  { echo -e "${BLUE}[STEP]${NC} $1"    | tee -a "$LOG_FILE"; }

error_exit() { log_error "$1"; exit 1; }

# ---- 配置（支持环境变量覆盖，本地开发时使用） ----
PLUGIN_DIR="${CELIA_PLUGIN_DIR:-/home/sandbox/plugins}"
PLUGIN_PATH="$PLUGIN_DIR/token-logger"
CONFIG_DIR="${OPENCLAW_CONFIG_DIR:-/home/sandbox/.openclaw}"
CONFIG_FILE="$CONFIG_DIR/openclaw.json"
JSONL_PATH="$CONFIG_DIR/token_baseline.jsonl"
SUPERVISORD_CONF="${SUPERVISORD_CONF:-/home/sandbox/supervisord.conf}"

# ---- 帮助 ----
show_help() {
    cat <<'EOF'
OpenClaw Token 消耗观察插件

纯观察插件——只记录每轮 token 消耗到 JSONL，不注入任何 context。
用于评估 OpenClaw 原生 token 基线。

用法: ./token-logger-install.sh [install|uninstall|status|help]

子命令:
  install    部署插件 + 注册到 openclaw.json + 重启 gateway
  uninstall  移除插件 + 从 openclaw.json 清理 + 重启 gateway
             （JSONL 数据保留，不删除）
  status     查看安装状态、JSONL 行数和时间跨度
  help       显示此帮助

数据输出: ~/.openclaw/token_baseline.jsonl（JSONL 格式）
分析工具: python3 parse_token_baseline.py < token_baseline.jsonl
EOF
}

# ---- restart_gateway（复用 install.sh 同款） ----
restart_gateway() {
    if command -v supervisorctl >/dev/null 2>&1 \
        && supervisorctl status >/dev/null 2>&1; then
        log_info "通过 supervisorctl 点重启 openclaw-gateway..."
        supervisorctl restart openclaw-gateway || {
            log_warn "supervisorctl restart 返回非零，准备回退..."
        }
        sleep 5
        if pgrep -f "openclaw-gateway" >/dev/null; then
            log_info "✓ openclaw-gateway 已重启（supervisorctl 快速路径）"
            return 0
        fi
        log_warn "supervisorctl 重启后未见 gateway 进程，回退到全盘 respawn"
    else
        log_info "supervisorctl 控制 socket 不可用，走 pkill 回退路径"
    fi

    pkill -f "supervisor.supervisord" 2>/dev/null || true
    sleep 2
    if ! pgrep -f "supervisor.supervisord" >/dev/null 2>&1; then
        log_info "supervisord 未自动拉起，手动启动..."
        python3 -m supervisor.supervisord -c "$SUPERVISORD_CONF" &
    fi
    log_info "等待 40 秒让服务启动..."
    sleep 40
    if pgrep -f "openclaw-gateway" >/dev/null; then
        log_info "✓ openclaw-gateway 已重启（全盘 respawn 路径）"
        return 0
    fi
    return 1
}

restart_services() {
    log_step "重启服务..."
    local max_retries=3
    local retry=0
    local gateway_ok=false

    while [ $retry -lt $max_retries ]; do
        retry=$((retry + 1))
        log_info "第 $retry 次尝试重启 openclaw-gateway..."
        if restart_gateway; then
            gateway_ok=true
            break
        fi
        log_warn "✗ openclaw-gateway 未起"
        if [ $retry -lt $max_retries ]; then
            log_info "将在 5 秒后重试..."
            sleep 5
        fi
    done

    if [ "$gateway_ok" != true ]; then
        log_error "openclaw-gateway 启动失败，已重试 $max_retries 次"
        exit 1
    fi
}

# ---- install ----
do_install() {
    log_step "安装 token-logger 插件..."

    # 环境检查
    command -v python3 >/dev/null 2>&1 || error_exit "未找到 python3"
    [ -f "$CONFIG_FILE" ] || error_exit "未找到 $CONFIG_FILE"

    # 备份
    cp "$CONFIG_FILE" "$CONFIG_FILE.bak.$(date +%Y%m%d%H%M%S)"
    log_info "已备份 openclaw.json"

    # 写入插件文件
    mkdir -p "$PLUGIN_PATH"

    log_info "写入 package.json..."
    cat > "$PLUGIN_PATH/package.json" <<'PKGJSON'
{
  "name": "@openclaw/token-logger",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "openclaw": {
    "extensions": ["./index.ts"]
  }
}
PKGJSON

    log_info "写入 openclaw.plugin.json..."
    cat > "$PLUGIN_PATH/openclaw.plugin.json" <<'MANIFEST'
{
  "id": "token-logger",
  "kind": "observer",
  "configSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {}
  }
}
MANIFEST

    log_info "写入 index.ts..."
    cat > "$PLUGIN_PATH/index.ts" <<'PLUGINTS'
/**
 * Token Usage Logger — 纯观察插件。
 *
 * 监听 llm_input / agent_end 事件，将每轮 token 消耗写入 JSONL。
 * 不注册任何 tool / context-engine / service，零副作用。
 */

import { appendFileSync } from "node:fs";
import {
  definePluginEntry,
  type OpenClawPluginApi,
} from "openclaw/plugin-sdk/plugin-entry";

const JSONL_PATH = process.env.TOKEN_LOGGER_OUTPUT
  ?? (process.env.HOME + "/.openclaw/token_baseline.jsonl");

/** 每个 session 的 round 计数器。 */
const sessionRounds = new Map<string, number>();

/**
 * llm_input 快照：每次 llm_input 事件暂存 prompt 元信息，
 * 在同 session 的 agent_end 时合并写入 JSONL。
 */
interface PromptSnapshot {
  sysPromptChars: number;
  msgCount: number;
}
const lastPromptSnapshot = new Map<string, PromptSnapshot>();

export default definePluginEntry({
  id: "token-logger",
  name: "Token Usage Logger",
  kind: "observer" as const,

  register(api: OpenClawPluginApi) {
    /* ---- llm_input: 记录 prompt 结构 ---- */
    api.on("llm_input", (event, ctx) => {
      const msgs = (event.messages ?? []) as Array<Record<string, unknown>>;
      const sysMsg = msgs.find((m) => m?.role === "system");
      let sysChars = 0;
      if (sysMsg) {
        const c = sysMsg.content;
        if (typeof c === "string") {
          sysChars = c.length;
        } else if (Array.isArray(c)) {
          for (const b of c as Array<Record<string, unknown>>) {
            if (typeof b?.text === "string") sysChars += (b.text as string).length;
          }
        }
      }
      const sid = ctx.sessionId ?? "unknown";
      lastPromptSnapshot.set(sid, { sysPromptChars: sysChars, msgCount: msgs.length });
    });

    /* ---- agent_end: 统计 token 并写 JSONL ---- */
    api.on("agent_end", (event, ctx) => {
      if (!event.messages) return;
      const msgs = event.messages as Array<Record<string, unknown>>;
      const sid = ctx.sessionId ?? "unknown";

      /* 定位本轮起点 */
      let lastUserIdx = -1;
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i]?.role === "user") { lastUserIdx = i; break; }
      }
      if (lastUserIdx === -1) return;

      /* 累加 assistant usage + 统计 LLM 调用次数 */
      let input = 0;
      let output = 0;
      let cacheRead = 0;
      let cacheWrite = 0;
      let isEstimated = false;
      let llmCalls = 0;

      for (let i = lastUserIdx + 1; i < msgs.length; i++) {
        if (msgs[i]?.role !== "assistant") continue;
        llmCalls += 1;
        const u = msgs[i]?.usage as Record<string, number> | undefined;
        if (!u) continue;
        input += u.input ?? u.prompt_tokens ?? 0;
        output += u.output ?? u.completion_tokens ?? 0;
        cacheRead += u.cacheRead ?? 0;
        cacheWrite += u.cacheWrite ?? 0;
      }

      /* 兜底：usage 全零时用字符数估算 */
      if (input === 0 && output === 0) {
        isEstimated = true;
        for (let i = lastUserIdx; i < msgs.length; i++) {
          const m = msgs[i];
          const role = String(m?.role ?? "");
          const content = m?.content;
          const text = typeof content === "string"
            ? content
            : Array.isArray(content)
              ? (content as Array<Record<string, unknown>>)
                  .filter((b) => b?.type === "text" || b?.type === "thinking")
                  .map((b) => String(b?.text ?? b?.thinking ?? ""))
                  .join("")
              : "";
          if (!text) continue;
          const tokens = Math.ceil(text.length / 1.5);
          if (role === "user" || role === "toolResult") {
            input += tokens;
          } else if (role === "assistant") {
            output += tokens;
          }
        }
      }

      if (input === 0 && output === 0) return;

      /* 计算 user / assistant 字符数 */
      let userChars = 0;
      let assistantChars = 0;
      for (let i = lastUserIdx; i < msgs.length; i++) {
        const m = msgs[i];
        const role = String(m?.role ?? "");
        const content = m?.content;
        const text = typeof content === "string"
          ? content
          : Array.isArray(content)
            ? (content as Array<Record<string, unknown>>)
                .filter((b) => b?.type === "text")
                .map((b) => String(b?.text ?? ""))
                .join("")
            : "";
        if (role === "user") userChars += text.length;
        else if (role === "assistant") assistantChars += text.length;
      }

      /* roundIndex */
      const prev = sessionRounds.get(sid) ?? 0;
      sessionRounds.set(sid, prev + 1);

      /* prompt snapshot */
      const snap = lastPromptSnapshot.get(sid);

      const record = {
        ts: Date.now(),
        sessionId: sid,
        roundIndex: prev + 1,
        llmCalls,
        input,
        output,
        cacheRead,
        cacheWrite,
        total: input + output,
        isEstimated,
        sysPromptChars: snap?.sysPromptChars ?? 0,
        msgCount: snap?.msgCount ?? 0,
        userChars,
        assistantChars,
      };

      try {
        appendFileSync(JSONL_PATH, JSON.stringify(record) + "\n");
      } catch (err) {
        api.logger.warn?.(`[token-logger] write failed: ${String(err)}`);
      }

      api.logger.info?.(
        `[token-logger] round=${record.roundIndex} ` +
        `llmCalls=${llmCalls} ` +
        `input=${input} output=${output} cache=${cacheRead} ` +
        `total=${record.total} estimated=${isEstimated}`,
      );
    });

    api.logger.info?.("[token-logger] registered — pure observer, zero side effects");
  },
});
PLUGINTS

    log_info "插件文件已写入 $PLUGIN_PATH"

    # 修改 openclaw.json
    log_step "注册插件到 openclaw.json..."
    _CFG="$CONFIG_FILE" _PPATH="$PLUGIN_PATH" python3 - <<'PYEOF'
import json, os, datetime

cfg_path = os.environ["_CFG"]
plugin_path = os.environ["_PPATH"]

with open(cfg_path, "r") as f:
    data = json.load(f)

plugins = data.setdefault("plugins", {})

# load.paths 追加（去重）
paths = plugins.setdefault("load", {}).setdefault("paths", [])
if plugin_path not in paths:
    paths.append(plugin_path)

# entries
plugins.setdefault("entries", {})["token-logger"] = {"enabled": True}

# installs
plugins.setdefault("installs", {})["token-logger"] = {
    "source": "path",
    "sourcePath": plugin_path,
    "installPath": plugin_path,
    "version": "0.1.0",
    "installedAt": datetime.datetime.utcnow().isoformat() + "Z",
}

with open(cfg_path, "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
PYEOF

    # 校验
    if ! python3 -c "import json; json.load(open('$CONFIG_FILE'))"; then
        error_exit "openclaw.json 修改后 JSON 格式错误"
    fi
    log_info "openclaw.json 已更新"

    restart_services

    log_info "✓ token-logger 已安装并启动"
    log_info ""
    log_info "数据输出: $JSONL_PATH"
    log_info "实时查看: tail -f $JSONL_PATH"
    log_info "卸载:     ./token-logger-install.sh uninstall"
}

# ---- uninstall ----
do_uninstall() {
    log_step "卸载 token-logger 插件..."

    if [ ! -f "$CONFIG_FILE" ]; then
        error_exit "未找到 $CONFIG_FILE"
    fi

    # 备份
    cp "$CONFIG_FILE" "$CONFIG_FILE.bak.$(date +%Y%m%d%H%M%S)"

    # 从 openclaw.json 移除
    _CFG="$CONFIG_FILE" _PPATH="$PLUGIN_PATH" python3 - <<'PYEOF'
import json, os

cfg_path = os.environ["_CFG"]
plugin_path = os.environ["_PPATH"]

with open(cfg_path, "r") as f:
    data = json.load(f)

plugins = data.setdefault("plugins", {})

# load.paths 移除
paths = plugins.get("load", {}).get("paths", [])
plugins.setdefault("load", {})["paths"] = [
    p for p in paths if p != plugin_path
]

# entries 删除
plugins.get("entries", {}).pop("token-logger", None)

# installs 删除
plugins.get("installs", {}).pop("token-logger", None)

with open(cfg_path, "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
PYEOF

    log_info "已从 openclaw.json 移除 token-logger"

    # 删除插件目录
    if [ -d "$PLUGIN_PATH" ]; then
        rm -rf "$PLUGIN_PATH"
        log_info "已删除 $PLUGIN_PATH"
    fi

    restart_services

    log_info "✓ token-logger 已卸载"
    if [ -f "$JSONL_PATH" ]; then
        local lines
        lines=$(wc -l < "$JSONL_PATH")
        log_info "JSONL 数据保留: $JSONL_PATH ($lines 行)"
    fi
}

# ---- status ----
do_status() {
    echo "=== token-logger 状态 ==="

    # 插件目录
    if [ -d "$PLUGIN_PATH" ]; then
        echo "插件目录:  $PLUGIN_PATH (存在)"
    else
        echo "插件目录:  未安装"
    fi

    # openclaw.json 注册
    if [ -f "$CONFIG_FILE" ]; then
        local enabled
        enabled=$(python3 -c "
import json
data = json.load(open('$CONFIG_FILE'))
e = data.get('plugins',{}).get('entries',{}).get('token-logger',{})
print(e.get('enabled', 'not registered'))
" 2>/dev/null)
        echo "配置状态:  enabled=$enabled"
    fi

    # JSONL 数据
    if [ -f "$JSONL_PATH" ]; then
        local lines first_ts last_ts
        lines=$(wc -l < "$JSONL_PATH")
        first_ts=$(head -1 "$JSONL_PATH" | python3 -c "
import json, sys, datetime
d = json.loads(sys.stdin.read())
print(datetime.datetime.fromtimestamp(d['ts']/1000).strftime('%Y-%m-%d %H:%M'))
" 2>/dev/null || echo "?")
        last_ts=$(tail -1 "$JSONL_PATH" | python3 -c "
import json, sys, datetime
d = json.loads(sys.stdin.read())
print(datetime.datetime.fromtimestamp(d['ts']/1000).strftime('%Y-%m-%d %H:%M'))
" 2>/dev/null || echo "?")
        echo "JSONL 数据: $JSONL_PATH"
        echo "  行数:     $lines"
        echo "  时间跨度: $first_ts ~ $last_ts"
    else
        echo "JSONL 数据: 无（尚未产生）"
    fi

    # Celia 状态
    if [ -f "$CONFIG_FILE" ]; then
        local celia_enabled
        celia_enabled=$(python3 -c "
import json
data = json.load(open('$CONFIG_FILE'))
e = data.get('plugins',{}).get('entries',{}).get('memory-celia',{})
print(e.get('enabled', 'not registered'))
" 2>/dev/null)
        local memory_slot
        memory_slot=$(python3 -c "
import json
data = json.load(open('$CONFIG_FILE'))
print(data.get('plugins',{}).get('slots',{}).get('memory', '(none)'))
" 2>/dev/null)
        echo "Celia 状态:  enabled=$celia_enabled  slot=$memory_slot"
    fi
}

# ---- 主入口 ----
main() {
    local cmd="${1:-help}"

    case "$cmd" in
        install)
            echo "token-logger 安装脚本" | tee -a "$LOG_FILE"
            date | tee -a "$LOG_FILE"
            do_install
            ;;
        uninstall)
            echo "token-logger 卸载脚本" | tee -a "$LOG_FILE"
            date | tee -a "$LOG_FILE"
            do_uninstall
            ;;
        status)
            do_status
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
}

main "$@"
