#!/bin/bash
set -ex
# OpenClaw Chrome 包装脚本 - 强制添加容器必需参数

exec /opt/chrome-linux/chrome \
  --headless \
  --no-sandbox \
  --disable-setuid-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --disable-software-rasterizer \
  --disable-features=IsolateOrigins,site-per-process \
  "$@"