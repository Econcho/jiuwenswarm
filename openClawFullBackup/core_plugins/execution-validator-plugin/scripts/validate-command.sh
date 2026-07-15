#!/bin/bash
# Execution Validator Plugin - Command Validation Script
# Exit codes: 0=PASS 1=CONFIRM 2=BLOCK

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$SCRIPT_DIR/../config"
COMMAND="${1:-}"

if [ -z "$COMMAND" ]; then
    echo "Usage: validate-command.sh <command>"
    exit 1
fi

if [ ! -f "$CONFIG_DIR/dangerous-commands.json" ] || [ ! -f "$CONFIG_DIR/warning-commands.json" ]; then
    echo "validator config files are missing"
    exit 2
fi

parse_commands() {
    local json_file="$1"
    if command -v python3 >/dev/null 2>&1; then
        python3 - "$json_file" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)

for item in payload.get("commands", []):
    if isinstance(item, str) and item.strip():
        print(item)
PY
        return
    fi

    sed -n '/"commands"[[:space:]]*:/,/\]/p' "$json_file" \
        | tr '\n' ' ' \
        | sed -E 's/.*"commands"[[:space:]]*:[[:space:]]*\[(.*)\].*/\1/' \
        | tr ',' '\n' \
        | sed -E 's/^[[:space:]]*"//; s/"[[:space:]]*$//; /^[[:space:]]*$/d'
}

check_patterns() {
    local patterns=("$@")

    for pattern in "${patterns[@]}"; do
        if echo "$COMMAND" | grep -iqE "$pattern"; then
            return 0
        fi
    done
    return 1
}

is_allowed_bootstrap_delete() {
    echo "$COMMAND" | grep -Eq '^[[:space:]]*(rm|unlink)([[:space:]]+--?[-[:alnum:]]+)*[[:space:]]+["'"'"']?([^"'"'"'[:space:]]*/)?BOOTSTRAP\.md["'"'"']?[[:space:]]*$'
}

if is_allowed_bootstrap_delete; then
    echo "PASS: BOOTSTRAP.md deletion is allowlisted."
    exit 0
fi

DANGEROUS_PATTERNS=($(parse_commands "$CONFIG_DIR/dangerous-commands.json"))
if check_patterns "${DANGEROUS_PATTERNS[@]}"; then
    echo "BLOCK: High-risk command detected. Execution is strictly prohibited."
    exit 2
fi

WARNING_PATTERNS=($(parse_commands "$CONFIG_DIR/warning-commands.json"))
if check_patterns "${WARNING_PATTERNS[@]}"; then
    echo "NEED CONFIRM: Medium-risk command detected. Explicit user approval is required before execution."
    exit 1
fi

if echo "$COMMAND" | grep -Eiq '\b(cat|head|tail|less|more|nl|bat)\b.*(openclaw\.json|\.xiaoyienv)'; then
    echo "BLOCK: Reading openclaw.json or .xiaoyienv through shell commands is prohibited."
    exit 2
fi

if echo "$COMMAND" | grep -Eiq '\b(cat|head|tail|less|more|nl|bat)\b.*/tmp/xy_channel'; then
    echo "NEED CONFIRM: Reading /tmp/xy_channel through shell commands requires explicit user approval."
    exit 1
fi

if echo "$COMMAND" | grep -Eiq '\b(cat|head|tail|less|more|nl|bat)\b.*/var(/|$)'; then
    echo "NEED CONFIRM: Reading /var through shell commands requires explicit user approval."
    exit 1
fi

if echo "$COMMAND" | grep -Eiq '\bcurl\b.*((-[X][[:space:]]*(POST|PUT|PATCH))|(--request(=|[[:space:]]+)(POST|PUT|PATCH)))'; then
    echo "NEED CONFIRM: Outbound curl write request detected. Explicit user approval is required before execution."
    exit 1
fi

if echo "$COMMAND" | grep -Eiq '\bcurl\b.*((-[F][[:space:]]*[^[:space:]]*@)|(--form(=|[[:space:]]+)[^[:space:]]*@)|((-[d]|--data|--data-binary|--data-raw|--data-urlencode|--json)(=|[[:space:]]+)@)|((-[T])([[:space:]]+|[^[:space:]]*))|(--upload-file(=|[[:space:]]+)[^[:space:]]+))'; then
    echo "NEED CONFIRM: Curl file upload or outbound payload detected. Explicit user approval is required before execution."
    exit 1
fi

if echo "$COMMAND" | grep -Eiq '\bwget\b.*((--post-file(=|[[:space:]]+)[^[:space:]]+)|(--post-data(=|[[:space:]]+)[^[:space:]]+)|(--body-file(=|[[:space:]]+)[^[:space:]]+)|(--body-data(=|[[:space:]]+)[^[:space:]]+)|(--method(=|[[:space:]]+)(POST|PUT|PATCH)))'; then
    echo "NEED CONFIRM: Wget outbound write request detected. Explicit user approval is required before execution."
    exit 1
fi

if echo "$COMMAND" | grep -Eiq '\b(scp|rsync)\b.*((rsync|scp|sftp|https?)://|[[:alnum:]_.-]+@[[:alnum:]._-]+:|[[:alnum:]._-]+:[^[:space:]])'; then
    echo "NEED CONFIRM: Remote file transfer detected. Explicit user approval is required before execution."
    exit 1
fi

CRITICAL_DIRS=("/etc" "/root" "/boot" "/sys" "/proc" "/dev")
for dir in "${CRITICAL_DIRS[@]}"; do
    if echo "$COMMAND" | grep -q "$dir"; then
        if echo "$COMMAND" | grep -qE "(rm|mkfs|dd|chmod|chown|tee|touch|mkdir).*$dir"; then
            echo "NEED CONFIRM: Medium-risk command detected. Explicit user approval is required before execution."
            exit 1
        fi
    fi
done

echo "PASS"
exit 0