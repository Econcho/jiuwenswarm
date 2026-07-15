#!/bin/bash
# install.sh — openclaw platform install hook
#
# 调用方：GaussPD_Skills/skills/celiaclaw/experimental-memory-install 技能。技能在
# `~/.celia/openclaw/<v>/` 解开 release tarball 后，把该目录通过
# $CELIA_EXTRACT_ROOT 透传给本脚本，由本脚本完成把插件铺到通用 OpenClaw
# 运行时（非沙盒）的工作。
#
# 与 celiaclaw 平台的差别：
#   - 没有沙盒约定路径，目标目录由 $CELIA_PLUGIN_DIR 决定（默认
#     $HOME/.openclaw/plugins/）；不存在的目录自动创建；
#   - 没有 supervisord / AGENTS.md marker 注入 / 历史记忆迁移这些
#     celiaclaw 专属步骤；
#   - openclaw.json 还是做合并（不是覆盖），保留用户的运行时字段。
#
# 退出码：0 成功；50 关键步骤失败；其他 = 不应发生的内部错误。
set -e

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# ---- 路径定位 -----------------------------------------------------------
INSTALL_DIR="${CELIA_EXTRACT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PLUGIN_DIR="${CELIA_PLUGIN_DIR:-$HOME/.openclaw/plugins}"
CONFIG_DIR="${CELIA_CONFIG_DIR:-$HOME/.openclaw}"
LOG_FILE="${CELIA_LOG_FILE_PATH:-/tmp/celia_install_openclaw.log}"

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"  | tee -a "$LOG_FILE"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1" | tee -a "$LOG_FILE"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"   | tee -a "$LOG_FILE"; }
log_step()  { echo -e "${BLUE}[STEP]${NC} $1"   | tee -a "$LOG_FILE"; }

error_exit() { log_error "$1"; exit 50; }

# ========== 阶段一：环境检查 ==========
runtime_dependency_count() {
    python3 - "$1" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as f:
    pkg = json.load(f)
deps = {}
for key in ("dependencies", "optionalDependencies"):
    deps.update(pkg.get(key) or {})
print(len(deps))
PY
}

check_environment() {
    log_step "检查环境..."

    command -v node >/dev/null 2>&1 || error_exit "未找到 node（openclaw 插件需要 Node.js）"
    command -v python3 >/dev/null 2>&1 || error_exit "未找到 python3（openclaw.json 合并需要）"

    if [ ! -d "$INSTALL_DIR/memory-plugin" ]; then
        error_exit "解压目录布局异常: $INSTALL_DIR/memory-plugin/ 不存在"
    fi

    mkdir -p "$PLUGIN_DIR" "$CONFIG_DIR" || error_exit "无法创建目标目录"

    log_info "环境检查通过"
    log_info "  INSTALL_DIR  = $INSTALL_DIR  (tarball 解压根)"
    log_info "  PLUGIN_DIR   = $PLUGIN_DIR    (插件铺设目标)"
    log_info "  CONFIG_DIR   = $CONFIG_DIR    (openclaw.json 所在)"
}

# ========== 阶段二：复制插件 + 共享库 + 二进制（如有）==========
copy_plugin_tree() {
    log_step "复制插件树到 $PLUGIN_DIR..."

    cp -r "$INSTALL_DIR/memory-plugin" "$PLUGIN_DIR/"
    [ -d "$INSTALL_DIR/context-engine-plugin" ] && \
        cp -r "$INSTALL_DIR/context-engine-plugin" "$PLUGIN_DIR/"
    cp -r "$INSTALL_DIR/shared" "$PLUGIN_DIR/"

    # full 变体的 celia_memory_mcp_server 在 $INSTALL_DIR/bin/celia_memory_mcp_server
    if [ -f "$INSTALL_DIR/bin/celia_memory_mcp_server" ]; then
        mkdir -p "$PLUGIN_DIR/bin"
        install -m 0755 "$INSTALL_DIR/bin/celia_memory_mcp_server" \
            "$PLUGIN_DIR/bin/celia_memory_mcp_server"
        ln -sf "bin/celia_memory_mcp_server" "$PLUGIN_DIR/celia_memory_mcp_server"
        log_info "已复制 core 二进制 → $PLUGIN_DIR/bin/celia_memory_mcp_server"
    else
        log_warn "本次为 plugins-only 安装，需要确保目标主机已有 celia_memory_mcp_server"
    fi

    log_info "插件复制完成"
}

# ========== 阶段三：运行时依赖校验 ==========
install_dependencies() {
    log_step "验证插件运行时依赖..."

    local memory_pkg="$PLUGIN_DIR/memory-plugin/package.json"
    local runtime_deps
    if [ ! -f "$memory_pkg" ]; then
        error_exit "memory-plugin 缺少 package.json"
    fi
    if ! runtime_deps="$(runtime_dependency_count "$memory_pkg")"; then
        error_exit "无法解析 memory-plugin package.json"
    fi
    if [ "$runtime_deps" != "0" ]; then
        error_exit "memory-plugin 仍包含运行时 npm dependencies，拒绝安装"
    fi
    log_info "memory-plugin 无运行时 npm dependencies，跳过依赖安装"

    if [ -d "$PLUGIN_DIR/context-engine-plugin" ]; then
        log_info "context-engine-plugin 运行时依赖由 OpenClaw 宿主提供"
    fi
}

verify_runtime_js() {
    log_step "校验插件运行时 JS..."

    if [ ! -f "$PLUGIN_DIR/memory-plugin/index.js" ]; then
        error_exit "memory-plugin 缺少运行时入口 index.js，请使用新发布包重装"
    fi
    if [ -d "$PLUGIN_DIR/context-engine-plugin" ] \
       && [ ! -f "$PLUGIN_DIR/context-engine-plugin/index.js" ]; then
        error_exit "context-engine-plugin 缺少运行时入口 index.js，请使用新发布包重装"
    fi
    log_info "插件运行时 JS 校验通过"
}

# ========== 阶段四：openclaw.json 合并 ==========
patch_openclaw_config() {
    log_step "合并 openclaw.json 增量配置..."

    local src_file="$INSTALL_DIR/config/openclaw.json"
    local dst_file="$CONFIG_DIR/openclaw.json"

    if [ ! -f "$src_file" ]; then
        log_warn "参考配置不存在: $src_file，跳过合并"
        return
    fi

    if [ ! -f "$dst_file" ]; then
        log_info "目标配置不存在，创建空配置后执行增量合并"
        printf '{}\n' > "$dst_file"
    else
        cp "$dst_file" "$dst_file.bak.$(date +%Y%m%d%H%M%S)"
    fi

    local server_bin=""
    if [ -x "$PLUGIN_DIR/bin/celia_memory_mcp_server" ]; then
        server_bin="$PLUGIN_DIR/bin/celia_memory_mcp_server"
    elif [ -n "${CELIA_SERVER_BINARY_PATH:-}" ]; then
        server_bin="$CELIA_SERVER_BINARY_PATH"
    elif command -v celia_memory_mcp_server >/dev/null 2>&1; then
        server_bin="$(command -v celia_memory_mcp_server)"
    fi

    _PATCH_SRC="$src_file" _PATCH_DST="$dst_file" _PLUGIN_DIR="$PLUGIN_DIR" \
        _CONFIG_DIR="$CONFIG_DIR" _SERVER_BIN="$server_bin" \
        python3 - <<'PYEOF'
import json, os, sys

src_file = os.environ['_PATCH_SRC']
dst_file = os.environ['_PATCH_DST']
plugin_dir = os.environ['_PLUGIN_DIR']
config_dir = os.environ['_CONFIG_DIR']
server_bin = os.environ.get('_SERVER_BIN', '')

with open(src_file, 'r') as f:
    src = json.load(f)
with open(dst_file, 'r') as f:
    dst = json.load(f)

# memoryFlush 配置（合并 agents.defaults.compaction.memoryFlush）
s_agents = src.get('agents', {}).get('defaults', {}).get('compaction', {})
if 'memoryFlush' in s_agents:
    dst.setdefault('agents', {}).setdefault('defaults', {}).setdefault('compaction', {})['memoryFlush'] = s_agents['memoryFlush']

# plugins.slots / load.paths / entries / installs 合并（缺失则补，存在则保留用户值）
s_plugins = src.get('plugins', {})
if 'slots' in s_plugins:
    dst.setdefault('plugins', {})['slots'] = s_plugins['slots']
if 'load' in s_plugins and 'paths' in s_plugins['load']:
    d_plugins = dst.setdefault('plugins', {})
    if not isinstance(d_plugins.get('load'), dict):
        d_plugins['load'] = {}
    d_load = d_plugins['load']
    if not isinstance(d_load.get('paths'), list):
        d_load['paths'] = []
    deduped_paths = []
    existing_paths = set()
    for path in d_load['paths']:
        if path not in existing_paths:
            deduped_paths.append(path)
            existing_paths.add(path)
    d_load['paths'] = deduped_paths
    for p in s_plugins['load']['paths']:
        if p not in existing_paths:
            d_load['paths'].append(p)
            existing_paths.add(p)
for k in ('entries', 'installs'):
    if k in s_plugins:
        for e_k, e_v in s_plugins[k].items():
            if e_k not in dst.setdefault('plugins', {}).setdefault(k, {}):
                dst['plugins'][k][e_k] = e_v

# OpenClaw 5.6 适配：allowConversationAccess（非 bundled 插件的
# agent_end/llm_input hook 必须显式开启，否则 handler 不注册）
celia_entry = dst.setdefault('plugins', {}).setdefault('entries', {}).get('memory-celia', {})
celia_hooks = celia_entry.setdefault('hooks', {})
if 'allowConversationAccess' not in celia_hooks:
    celia_hooks['allowConversationAccess'] = True
celia_cfg = celia_entry.setdefault('config', {})
if server_bin and not os.path.exists(celia_cfg.get('serverBinaryPath', '')):
    celia_cfg['serverBinaryPath'] = server_bin
src_entry = (((src.get('plugins') or {}).get('entries') or {})
             .get('memory-celia') or {})
src_cfg = src_entry.get('config') if isinstance(src_entry, dict) else {}
if isinstance(src_cfg, dict):
    src_chat = src_cfg.get('chat')
    if isinstance(src_chat, dict):
        src_headers = src_chat.get('headers')
        dst_chat = celia_cfg.setdefault('chat', {})
        if isinstance(dst_chat, dict) and isinstance(src_headers, dict):
            dst_chat.setdefault('headers', src_headers)
dst['plugins']['entries']['memory-celia'] = celia_entry

# OpenClaw 5.6 适配：plugins.allow 白名单追加
# （用户已设白名单时不含 memory-celia 则插件不加载）
d_plugins = dst.setdefault('plugins', {})
if 'allow' in d_plugins and isinstance(d_plugins['allow'], list):
    if 'memory-celia' not in d_plugins['allow']:
        d_plugins['allow'].append('memory-celia')

# OpenClaw 5.6 适配：supportsUsageInStreaming
# （不设则 agent_end hook 中 usage 全零，token DFX 失真）
providers = dst.get('models', {}).get('providers', {})
for prov in providers.values():
    for model in prov.get('models', []):
        compat = model.setdefault('compat', {})
        if 'supportsUsageInStreaming' not in compat:
            compat['supportsUsageInStreaming'] = True

# 解析占位符 → 实际安装目录。
def resolve_placeholders(obj):
    if isinstance(obj, dict):
        return {k: resolve_placeholders(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_placeholders(v) for v in obj]
    if isinstance(obj, str):
        return (
            obj.replace('${CELIA_INSTALL_ROOT}', plugin_dir)
               .replace('${CELIA_PLUGIN_ROOT}', plugin_dir)
               .replace('${CELIA_CONFIG_ROOT}', config_dir)
        )
    return obj

def dedupe_load_paths(data):
    plugins = data.get('plugins')
    if not isinstance(plugins, dict):
        return
    load = plugins.get('load')
    if not isinstance(load, dict):
        return
    paths = load.get('paths')
    if not isinstance(paths, list):
        return
    deduped_paths = []
    seen_paths = set()
    for path in paths:
        if path not in seen_paths:
            deduped_paths.append(path)
            seen_paths.add(path)
    load['paths'] = deduped_paths

dst = resolve_placeholders(dst)
dedupe_load_paths(dst)

mem = dst.get('plugins', {}).get('entries', {}).get('memory-celia', {})
cfg = mem.get('config', {}) if isinstance(mem, dict) else {}
db_path = cfg.get('dbPath')
if isinstance(db_path, str) and db_path:
    db_dir = os.path.dirname(os.path.expanduser(db_path))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

with open(dst_file, 'w') as f:
    json.dump(dst, f, indent=2, ensure_ascii=False)
    f.write('\n')

print(f'OK merged → {dst_file}')
PYEOF

    if python3 -c "import json; json.load(open('$dst_file'))" 2>/dev/null; then
        log_info "openclaw.json 合并完成"
    else
        log_error "合并后 openclaw.json JSON 格式错误"
        exit 50
    fi

    local resolved_server_bin
    resolved_server_bin="$(python3 - "$dst_file" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
cfg = data.get('plugins', {}).get('entries', {}).get('memory-celia', {}) \
    .get('config', {})
print(cfg.get('serverBinaryPath', ''))
PY
)"
    if [ ! -x "$resolved_server_bin" ]; then
        error_exit "celia_memory_mcp_server 不存在或不可执行: $resolved_server_bin。\
请安装 full 变体，或设置 CELIA_SERVER_BINARY_PATH 指向已有二进制后重试。"
    fi
}

# ========== 阶段四B：运行时 env 兜底文件 ==========
write_runtime_env_file() {
    log_step "生成 OpenClaw 运行时 env 兜底..."

    local cfg_file="$CONFIG_DIR/openclaw.json"
    local env_file="${CELIA_OPENCLAW_ENV_FILE:-$CONFIG_DIR/celia_env.json}"

    if [ ! -f "$cfg_file" ]; then
        log_warn "未找到 $cfg_file，跳过 env 兜底生成"
        return
    fi

    _CFG="$cfg_file" _OUT="$env_file" python3 - <<'PYEOF' || { log_warn "生成 OpenClaw 运行时 env 兜底失败"; return; }
import json
import os
import re
import sys

cfg_path = os.environ["_CFG"]
out_path = os.environ["_OUT"]

with open(cfg_path, "r", encoding="utf-8") as f:
    cfg = json.load(f)


def resolve(value):
    """Resolve ${VAR} with current install-time env; keep literals."""
    if not isinstance(value, str) or not value:
        return ""
    if "${" not in value:
        return value
    return re.sub(r"\$\{([^}]+)\}", lambda m: os.environ.get(m.group(1), ""), value)


def pick_real_api_key(headers, top_level):
    if isinstance(headers, dict):
        hk = resolve(headers.get("x-api-key"))
        if hk:
            return hk
    return resolve(top_level)


def pick_extra_headers(headers):
    out = {}
    if not isinstance(headers, dict):
        return out
    for key, value in headers.items():
        if not isinstance(key, str):
            continue
        if key.lower() in ("x-api-key", "x-uid"):
            continue
        resolved = resolve(value)
        if resolved:
            out[key] = resolved
    return out


def plugin_config():
    return (((cfg.get("plugins") or {}).get("entries") or {})
            .get("memory-celia") or {}).get("config") or {}


def memory_search():
    ms = (((cfg.get("agents") or {}).get("defaults") or {})
          .get("memorySearch") or {})
    if ms.get("enabled", True) is False:
        return {}, {}, {}
    remote = ms.get("remote") or {}
    headers = remote.get("headers") or {}
    return ms, remote, headers


def primary_provider():
    defaults = ((cfg.get("agents") or {}).get("defaults") or {})
    primary = (defaults.get("model") or {}).get("primary")
    if not isinstance(primary, str) or "/" not in primary:
        return "", "", {}
    provider_name, model_name = primary.split("/", 1)
    providers = ((cfg.get("models") or {}).get("providers") or {})
    provider = providers.get(provider_name) or {}
    return provider_name, model_name, provider


pcfg = plugin_config()
ms, ms_remote, ms_headers = memory_search()
_provider_name, primary_model, provider = primary_provider()
provider_headers = provider.get("headers") or {}

embed_cfg = pcfg.get("embed") or {}
chat_cfg = pcfg.get("chat") or {}

embed_headers = embed_cfg.get("headers") if isinstance(embed_cfg, dict) else None
if not isinstance(embed_headers, dict) or not embed_headers:
    embed_headers = ms_headers

chat_headers = chat_cfg.get("headers") if isinstance(chat_cfg, dict) else None
if not isinstance(chat_headers, dict) or not chat_headers:
    chat_headers = provider_headers

embed_base = resolve(embed_cfg.get("baseUrl")) or resolve(ms_remote.get("baseUrl"))
embed_key = pick_real_api_key(embed_headers, embed_cfg.get("apiKey")) \
    or pick_real_api_key(ms_headers, ms_remote.get("apiKey"))
embed_model = resolve(embed_cfg.get("model")) or resolve(ms.get("model"))

chat_base = resolve(chat_cfg.get("baseUrl")) or resolve(provider.get("baseUrl"))
chat_key = pick_real_api_key(chat_headers, chat_cfg.get("apiKey")) \
    or pick_real_api_key(provider_headers, provider.get("apiKey"))
chat_model = resolve(chat_cfg.get("model")) or resolve(primary_model)

vector_dim = pcfg.get("vectorDim")
if not isinstance(vector_dim, int):
    vector_dim = ms.get("outputDimensionality")

embed_uid = resolve(ms_headers.get("x-uid")) if isinstance(ms_headers, dict) else ""
chat_uid = resolve(provider_headers.get("x-uid")) if isinstance(provider_headers, dict) else ""

env = {}


def put(key, value):
    if isinstance(value, str) and value:
        env[key] = value


put("OPENCLAW_EMBED_BASE_URL", embed_base)
put("OPENCLAW_EMBED_API_KEY", embed_key)
put("OPENCLAW_EMBED_MODEL", embed_model)
put("OPENCLAW_CHAT_BASE_URL", chat_base)
put("OPENCLAW_CHAT_API_KEY", chat_key)
put("OPENCLAW_CHAT_MODEL", chat_model)

# Also provide the C backend's canonical env names for direct/manual launches.
put("OPENAI_EMBED_BASE_URL", embed_base)
put("OPENAI_EMBED_API_KEY", embed_key)
put("OPENAI_EMBED_MODEL", embed_model)
put("OPENAI_CHAT_BASE_URL", chat_base)
put("OPENAI_CHAT_API_KEY", chat_key)
put("OPENAI_CHAT_MODEL", chat_model)

extra_embed = pick_extra_headers(embed_headers)
extra_chat = pick_extra_headers(chat_headers)
if extra_embed:
    env["OPENAI_EMBED_HEADERS_JSON"] = json.dumps(
        extra_embed, ensure_ascii=False, separators=(",", ":"))
if extra_chat:
    env["OPENAI_CHAT_HEADERS_JSON"] = json.dumps(
        extra_chat, ensure_ascii=False, separators=(",", ":"))
if chat_uid:
    env["CELIA_CHAT_UID"] = chat_uid
if embed_uid:
    env["CELIA_EMBED_UID"] = embed_uid
if isinstance(vector_dim, int) and vector_dim > 0:
    env["CELIA_VECTOR_DIM"] = str(vector_dim)

missing = [name for name, value in (
    ("embed.baseUrl", embed_base),
    ("embed.apiKey", embed_key),
    ("embed.model", embed_model),
    ("chat.baseUrl", chat_base),
    ("chat.apiKey", chat_key),
    ("chat.model", chat_model),
) if not value]

os.makedirs(os.path.dirname(out_path), exist_ok=True)
tmp_path = out_path + ".tmp"
with open(tmp_path, "w", encoding="utf-8") as f:
    json.dump(env, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.replace(tmp_path, out_path)
try:
    os.chmod(out_path, 0o600)
except OSError:
    pass

print(f"OK env_file={out_path}")
print(f"OK keys={','.join(sorted(env.keys()))}")
if missing:
    print("WARN missing=" + ",".join(missing), file=sys.stderr)
PYEOF

    log_info "OpenClaw 运行时 env 兜底已生成: $env_file"
    log_info "插件启动时会读取该文件；已有 process.env 不会被覆盖"
}

# ========== 阶段四C：AGENTS.md 增量注入（已禁用） ==========
inject_agents_md() {
    log_step "跳过 AGENTS.md 注入"
    log_info "AGENTS.md 由 Docker 镜像发布前人工审核维护，安装不再变更"
}

# ========== 阶段五：下一步提示 ==========
print_next_steps() {
    cat <<EOF | tee -a "$LOG_FILE"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✓ openclaw 插件安装完成

下一步：
  1. 重启你的 OpenClaw 实例（或刷新插件目录），让运行时加载新版插件。
  2. 验证：openclaw plugins list  # 应能看到 memory-plugin
  3. 实时日志：tail -f $LOG_FILE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF
}

# ========== 主流程 ==========
main() {
    echo "Celia 插件安装脚本（openclaw 通用环境）" | tee -a "$LOG_FILE"
    date | tee -a "$LOG_FILE"

    check_environment
    copy_plugin_tree
    install_dependencies
    verify_runtime_js
    patch_openclaw_config
    write_runtime_env_file
    inject_agents_md
    print_next_steps

    log_info "openclaw 安装流程结束"
}

main "$@"
