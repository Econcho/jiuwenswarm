#!/bin/bash
# Execution Validator Plugin - Message Validation Script
# Exit codes: 0=safe 1=sensitive

set -euo pipefail

MESSAGE="${1:-}"

if [ -z "$MESSAGE" ]; then
    echo "Usage: validate-message.sh <message>"
    exit 1
fi

SENSITIVE_PATTERNS=(
    "private_key:[a-fA-F0-9]{64}"
    "SK-[A-F0-9]{32}"
    "[\"']?api[_-]?key[\"']?[[:space:]]*[:=][[:space:]]*[\"']?[A-Za-z0-9._~+/:=-]{20,}[\"']?"
    "[\"']?[xu]-?uid[\"']?[[:space:]]*[:=][[:space:]]*[\"']?[0-9]{15,}[\"']?"
    "(^|[^0-9])[0-9]{18}([^0-9]|$)"
    "[\"']?token[\"']?[[:space:]]*[:=][[:space:]]*[\"']?[a-f0-9]{40,}[\"']?"
    "Bearer[[:space:]]+[A-Za-z0-9._~+/-]+=*"
    "agent[a-f0-9]{32,}"
    "webhook[a-f0-9]{15,}"
    "[\"']?api[_-]?id[\"']?[[:space:]]*[:=][[:space:]]*[\"']?[A-Za-z0-9._:-]{15,}[\"']?"
    "(https?|wss?)://[A-Za-z0-9][-A-Za-z0-9.]*\\.(huawei|dbankcloud)\\.com[^[:space:]\"']*"
    "(https?|wss?)://([0-9]{1,3}\\.){3}[0-9]{1,3}[^[:space:]\"']*"
    "(^|[^0-9])(10(\\.[0-9]{1,3}){3}|172\\.(1[6-9]|2[0-9]|3[01])(\\.[0-9]{1,3}){2}|192\\.168(\\.[0-9]{1,3}){2})([^0-9]|$)"
    "(sk-|api-|key-)[a-zA-Z0-9]{20,}"
    "1[3-9][0-9]{9}"
    "[1-9][0-9]{5}(18|19|20)[0-9]{2}(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])[0-9]{3}[0-9Xx]"
    "https?://[^ ]*webhook[^ ]*"
)

for pattern in "${SENSITIVE_PATTERNS[@]}"; do
    if echo "$MESSAGE" | grep -iqE "$pattern"; then
        echo "SENSITIVE"
        exit 1
    fi
done

echo "SAFE"
exit 0
