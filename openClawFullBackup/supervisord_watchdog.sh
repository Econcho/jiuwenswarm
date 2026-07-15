#!/bin/bash
# ***********************************************************************
# Copyright: (c) Huawei Technologies Co., Ltd. 2019. All rights reserved.
# script for build
# version: 1.0.0
# change log:
# ***********************************************************************
set -ex
set -o pipefail
# 监控 supervisord，如果挂了则清理所有管理的进程

SUPERVISOR_PID="$1"
LOG_FILE="/tmp/supervisord_watchdog.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

if [ -z "$SUPERVISOR_PID" ]; then
    echo "Usage: $0 <supervisord_pid>" >&2
    exit 1
fi

log "Watchdog started, monitoring supervisord PID: $SUPERVISOR_PID"

# 监控 supervisord
while kill -0 "$SUPERVISOR_PID" 2>/dev/null; do
    sleep 2
done

# supervisord 已退出
log "Supervisord (PID: $SUPERVISOR_PID) has exited, cleaning up..."

# 清理 gateway
if pgrep -f "openclaw-gateway" >/dev/null; then
    pkill -TERM -f "openclaw-gateway" 2>/dev/null
    sleep 2
    pkill -KILL -f "openclaw-gateway" 2>/dev/null
fi

log "Cleanup completed"
exit 0