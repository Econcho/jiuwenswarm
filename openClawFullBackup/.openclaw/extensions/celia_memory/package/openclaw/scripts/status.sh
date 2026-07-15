#!/bin/bash
# status.sh — openclaw platform status probe (experimental-memory-status skill backend)
#
# 只读：列出当前 openclaw 部署里 Celia memory plugin 的运行态。
# 输出格式刻意保持机器可读（`key: value`），方便 experimental-memory-status 技能在
# 不同 platform 之间保持同样的解析逻辑。
set +e   # 容忍缺失文件——status 永远不该 hard-fail
set -u

PLUGIN_DIR="${CELIA_PLUGIN_DIR:-$HOME/.openclaw/plugins}"
CONFIG_DIR="${CELIA_CONFIG_DIR:-$HOME/.openclaw}"
CONFIG_FILE="$CONFIG_DIR/openclaw.json"
DB_PATH="${CELIA_DB_PATH:-$CONFIG_DIR/memory/celia_memory.db}"

echo "platform: openclaw"
echo "plugin_dir: $PLUGIN_DIR"
echo "config_dir: $CONFIG_DIR"

# 插件文件存在性
if [ -d "$PLUGIN_DIR/memory-plugin" ]; then
    echo "memory_plugin_present: yes"
    if [ -f "$PLUGIN_DIR/memory-plugin/index.js" ]; then
        echo "memory_plugin_runtime_js_present: yes"
    else
        echo "memory_plugin_runtime_js_present: no"
    fi
    if [ -f "$PLUGIN_DIR/memory-plugin/package.json" ]; then
        version=$(python3 -c "import json; print(json.load(open('$PLUGIN_DIR/memory-plugin/package.json')).get('version','unknown'))" 2>/dev/null || echo unknown)
        echo "memory_plugin_version: $version"
    fi
else
    echo "memory_plugin_present: no"
    echo "memory_plugin_runtime_js_present: no"
fi

# 二进制
if [ -f "$PLUGIN_DIR/celia_memory_mcp_server" ]; then
    size_kb=$(du -k "$PLUGIN_DIR/celia_memory_mcp_server" 2>/dev/null | awk '{print $1}')
    echo "mcp_binary_present: yes"
    echo "mcp_binary_size_kb: ${size_kb:-unknown}"
else
    echo "mcp_binary_present: no"
fi

# 进程
pid=$(pgrep -f celia_memory_mcp_server 2>/dev/null | head -1)
if [ -n "$pid" ]; then
    echo "mcp_pid: $pid"
    if [ -r "/proc/$pid/stat" ]; then
        # /proc 在 macOS 下不存在，仅 Linux 输出
        starttime=$(awk '{print $22}' "/proc/$pid/stat" 2>/dev/null)
        [ -n "$starttime" ] && echo "mcp_starttime_jiffies: $starttime"
    fi
else
    echo "mcp_pid: none"
fi

# DB 状态
if [ -f "$DB_PATH" ]; then
    echo "db_path: $DB_PATH"
    db_size_kb=$(du -k "$DB_PATH" 2>/dev/null | awk '{print $1}')
    echo "db_size_kb: ${db_size_kb:-unknown}"
    if command -v sqlite3 >/dev/null 2>&1; then
        mem_count=$(sqlite3 "$DB_PATH" 'select count(*) from mem_l0;' 2>/dev/null || echo unknown)
        echo "memory_count_l0: $mem_count"
    fi
else
    echo "db_path: $DB_PATH (missing)"
fi

# openclaw.json 中是否启用
if [ -f "$CONFIG_FILE" ]; then
    python3 - "$CONFIG_FILE" <<'PY' 2>/dev/null || {
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

exit 0
