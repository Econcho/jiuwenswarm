#!/bin/bash
# status.sh — celiaclaw platform status probe
#
# 只读：列出当前 celiaclaw 沙盒里 Celia memory plugin 的运行态。
# 输出格式与 openclaw/scripts/status.sh 一致（`key: value`），方便
# 人工运维和历史状态入口可复用同一段解析。
set +e
set -u

OPENCLAW_CONFIG_DIR="${CELIA_CONFIG_DIR:-/home/sandbox/.openclaw}"
RUNTIME_ROOT="${CELIA_PLUGIN_DIR:-$OPENCLAW_CONFIG_DIR/extensions/celia_memory/install/current}"
OPENCLAW_CONFIG_FILE="$OPENCLAW_CONFIG_DIR/openclaw.json"
SUPERVISOR_CONFIG_FILE="${CELIA_SUPERVISORD_CONF:-/home/sandbox/supervisord.conf}"
CELIA_MEMORY_DB_PATH="${CELIA_DB_PATH:-$OPENCLAW_CONFIG_DIR/workspace/memory/celia_memory/celia_memory.db}"

echo "platform: celiaclaw"
echo "plugin_dir: $RUNTIME_ROOT"
echo "config_dir: $OPENCLAW_CONFIG_DIR"

if [ -d "$RUNTIME_ROOT/memory-plugin" ]; then
    echo "memory_plugin_present: yes"
    if [ -f "$RUNTIME_ROOT/memory-plugin/index.js" ]; then
        echo "memory_plugin_runtime_js_present: yes"
    else
        echo "memory_plugin_runtime_js_present: no"
    fi
    if [ -f "$RUNTIME_ROOT/memory-plugin/package.json" ]; then
        version=$(python3 -c "import json; print(json.load(open('$RUNTIME_ROOT/memory-plugin/package.json')).get('version','unknown'))" 2>/dev/null || echo unknown)
        echo "memory_plugin_version: $version"
    fi
else
    echo "memory_plugin_present: no"
    echo "memory_plugin_runtime_js_present: no"
fi

if [ -f "$RUNTIME_ROOT/bin/celia_memory_mcp_server" ]; then
    size_kb=$(du -k "$RUNTIME_ROOT/bin/celia_memory_mcp_server" 2>/dev/null | awk '{print $1}')
    echo "mcp_binary_present: yes"
    echo "mcp_binary_path: $RUNTIME_ROOT/bin/celia_memory_mcp_server"
    echo "mcp_binary_size_kb: ${size_kb:-unknown}"
else
    echo "mcp_binary_present: no"
fi

# 进程（celiaclaw 沙盒下大概率 Linux）
pid=$(pgrep -f celia_memory_mcp_server 2>/dev/null | head -1)
if [ -n "$pid" ]; then
    echo "mcp_pid: $pid"
    if [ -r "/proc/$pid/stat" ]; then
        starttime=$(awk '{print $22}' "/proc/$pid/stat" 2>/dev/null)
        [ -n "$starttime" ] && echo "mcp_starttime_jiffies: $starttime"
    fi
else
    echo "mcp_pid: none"
fi

# Gateway 进程
gw_pid=$(pgrep -f openclaw-gateway 2>/dev/null | head -1)
echo "gateway_pid: ${gw_pid:-none}"

# DB
if [ -f "$CELIA_MEMORY_DB_PATH" ]; then
    echo "db_path: $CELIA_MEMORY_DB_PATH"
    db_size_kb=$(du -k "$CELIA_MEMORY_DB_PATH" 2>/dev/null | awk '{print $1}')
    echo "db_size_kb: ${db_size_kb:-unknown}"
    if command -v sqlite3 >/dev/null 2>&1; then
        mem_count=$(sqlite3 "$CELIA_MEMORY_DB_PATH" 'select count(*) from mem_l0;' 2>/dev/null || echo unknown)
        echo "memory_count_l0: $mem_count"
    fi
else
    echo "db_path: $CELIA_MEMORY_DB_PATH (missing)"
fi

# openclaw.json
if [ -f "$OPENCLAW_CONFIG_FILE" ]; then
    python3 - "$OPENCLAW_CONFIG_FILE" <<'PY' 2>/dev/null || {
import json
import sys

path = sys.argv[1]
cfg = json.load(open(path, encoding='utf-8'))
plugins = cfg.get('plugins', {}) if isinstance(cfg.get('plugins'), dict) else {}
entries = plugins.get('entries', {}) if isinstance(plugins.get('entries'), dict) else {}
slots = plugins.get('slots', {}) if isinstance(plugins.get('slots'), dict) else {}
load = plugins.get('load', {}) if isinstance(plugins.get('load'), dict) else {}
paths = load.get('paths') if isinstance(load.get('paths'), list) else []

registered = 'yes' if 'memory-celia' in entries else 'no'
print(f'registered_in_openclaw_json: {registered}')
memory_entry = entries.get('memory-celia')
enabled = (
    memory_entry.get('enabled', '<missing>')
    if isinstance(memory_entry, dict)
    else '<missing>'
)
print(f'memory_celia_enabled: {enabled}')
print(f"memory_slot: {slots.get('memory', '<missing>')}")

stale = []
for item in paths:
    if not isinstance(item, str):
        continue
    if (
        item.endswith('/memory-plugin')
        and '/celia_memory/' in item
        and '/install/current/' not in item
        and not item.endswith('/install/current/memory-plugin')
    ):
        stale.append(item)
print(f'stale_memory_plugin_paths: {len(stale)}')
for i, item in enumerate(stale, 1):
    print(f'stale_memory_plugin_path_{i}: {item}')
PY
        echo "registered_in_openclaw_json: unknown"
        echo "memory_celia_enabled: unknown"
        echo "memory_slot: unknown"
        echo "stale_memory_plugin_paths: unknown"
    }
else
    echo "registered_in_openclaw_json: no_config"
    echo "memory_celia_enabled: no_config"
    echo "memory_slot: no_config"
    echo "stale_memory_plugin_paths: no_config"
fi

# C 端 CA fallback 路径检查。安装流程不再通过 supervisord 注入
# CURL_CA_BUNDLE；mcp_server 运行时会扫描同一组路径。
ca_fallback_path=""
for ca_candidate in \
    "/etc/ssl/certs/ca-certificates.crt" \
    "/etc/ssl/certs/ca-bundle.crt" \
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem" \
    "/etc/pki/tls/certs/ca-bundle.crt" \
    "/etc/ssl/cert.pem" \
    "/etc/ssl/ca-bundle.pem" \
    "$OPENCLAW_CONFIG_DIR/.ssl/ca.crt" \
    "$OPENCLAW_CONFIG_DIR/ca.crt" \
    "/home/sandbox/.ssl/ca.crt"; do
    if [ -f "$ca_candidate" ]; then
        ca_fallback_path="$ca_candidate"
        break
    fi
done
if [ -n "$ca_fallback_path" ]; then
    echo "ca_fallback_present: yes"
    echo "ca_fallback_path: $ca_fallback_path"
else
    echo "ca_fallback_present: no"
    echo "ca_fallback_path: none"
fi

# supervisord environment 注入
if [ -f "$SUPERVISOR_CONFIG_FILE" ]; then
    python3 - "$SUPERVISOR_CONFIG_FILE" <<'PY' 2>/dev/null || {
import sys

conf = sys.argv[1]
with open(conf, encoding='utf-8') as f:
    lines = f.readlines()

gateway_env = ''
in_section = False
for line in lines:
    if line.strip() == '[program:openclaw-gateway]':
        in_section = True
        continue
    if in_section and line.lstrip().startswith('['):
        break
    if in_section and line.startswith('environment='):
        gateway_env = line
        break

has_log_file = 'CELIA_LOG_FILE=' in gateway_env
has_curl_ca = 'CURL_CA_BUNDLE=' in gateway_env
print(f"supervisord_env_injected: {'yes' if has_log_file else 'no'}")
print(f"supervisord_celia_log_file_present: {'yes' if has_log_file else 'no'}")
print(f"supervisord_curl_ca_bundle_present: {'yes' if has_curl_ca else 'no'}")
PY
        echo "supervisord_env_injected: unknown"
        echo "supervisord_celia_log_file_present: unknown"
        echo "supervisord_curl_ca_bundle_present: unknown"
    }
fi

exit 0
