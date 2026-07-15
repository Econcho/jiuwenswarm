#!/bin/bash
# ============================================================================
# mem-list.sh — 独立的记忆列表查询工具（直接调用 celia_memory_mcp_server）
#
# 用法:
#   bash tools/mem-list.sh                          # 默认全局概览
#   bash tools/mem-list.sh --level l2               # 原子事实原文
#   bash tools/mem-list.sh --level l1 --limit 10    # 场景记忆概述，最多 10 条
#   bash tools/mem-list.sh --user alice              # 按用户过滤
#   bash tools/mem-list.sh --db /path/to/db          # 指定数据库
# ============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# 这个工具被移到 tools/mem-list/ 下；ROOT_DIR 是 repo 根（向上两级）
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SERVER="${CELIA_SERVER:-$ROOT_DIR/build/bin/celia_memory_mcp_server.exe}"
DB_PATH="${CELIA_DB:-$ROOT_DIR/build/celia_memory.db}"

# Parse arguments
LEVEL="l0"
LIMIT=20
OFFSET=0
USER_ID=""

while [ $# -gt 0 ]; do
    case "$1" in
        --level)  LEVEL="$2";   shift 2 ;;
        --limit)  LIMIT="$2";   shift 2 ;;
        --offset) OFFSET="$2";  shift 2 ;;
        --user)   USER_ID="$2"; shift 2 ;;
        --db)     DB_PATH="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: mem-list.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --level <l0|l1|l2>   Detail level (default: l0)"
            echo "  --limit <n>          Max records (default: 20)"
            echo "  --offset <n>         Pagination offset (default: 0)"
            echo "  --user <userId>      Filter by user ID"
            echo "  --db <path>          Database path"
            echo ""
            echo "Environment:"
            echo "  CELIA_SERVER  Path to celia_memory_mcp_server binary"
            echo "  CELIA_DB      Path to database file"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [ ! -f "$SERVER" ]; then
    echo "Error: celia_memory_mcp_server not found at $SERVER"
    echo "Set CELIA_SERVER or run from project root after building."
    exit 1
fi

if [ ! -f "$DB_PATH" ]; then
    echo "Error: database not found at $DB_PATH"
    echo "Set CELIA_DB or use --db <path>."
    exit 1
fi

# Build memory_list arguments JSON
ARGS="{\"layers\":[\"$LEVEL\"],\"limit\":$LIMIT,\"offset\":$OFFSET"
if [ -n "$USER_ID" ]; then
    ARGS="$ARGS,\"userId\":\"$USER_ID\""
fi
ARGS="$ARGS}"

# Send init + list request via stdin, capture responses
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{}}}'
LIST="{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"memory_list\",\"arguments\":$ARGS}}"

RESP=$(printf '%s\n%s\n' "$INIT" "$LIST" | "$SERVER" "$DB_PATH" 2>/dev/null | tail -1)

# Extract the inner JSON text from the MCP response
# The response is: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"..."}],"isError":false}}
# We need to extract the "text" field value and unescape it
INNER=$(echo "$RESP" | python3 -c "
import sys, json
try:
    msg = json.loads(sys.stdin.read())
    text = msg['result']['content'][0]['text']
    data = json.loads(text)
    layer = '$LEVEL'
    section = data.get(layer, data)
    if layer == 'l2':
        count = section.get('count', 0)
        offset = section.get('offset', 0)
        records = section.get('records', [])
    else:
        print(json.dumps(section, ensure_ascii=False, indent=2))
        sys.exit(0)
    if count == 0:
        print('No memories found.')
    else:
        print(f'{count} memories (offset={offset}):')
        print()
        for i, r in enumerate(records):
            idx = offset + i + 1
            rid = r.get('id', '?')
            txt = r.get('text', '(empty)')
            print(f'  {idx}. [id:{rid}] {txt}')
except Exception as e:
    print(f'Error parsing response: {e}', file=sys.stderr)
    print(sys.stdin.read() if hasattr(sys.stdin, 'read') else '', file=sys.stderr)
" 2>&1)

echo "$INNER"
