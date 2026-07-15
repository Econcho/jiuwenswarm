---
name: migrate-openclaw
description: 当用户要求把 OpenClaw 历史长期记忆（USER.md / Memory.md）迁移到 GsPD 时使用。触发场景：「迁移 OpenClaw 记忆」「import OpenClaw memory」「把老的 USER.md 导入 GsPD」。技能调用 `python3 -m migrate_openclaw.migrate run` 工具，要求一个临时运行的 `celia_memory_mcp_server`。Trigger only when the user explicitly asks to migrate; not for new memory creation.
---

# migrate-openclaw 技能 / Migrate OpenClaw memories

## 用途 / Purpose

调用 `migrate_openclaw` 工具，把 OpenClaw workspace 下的历史长期记忆
（`USER.md` / `Memory.md` 系列）通过 MCP JSON-RPC 写入运行中的 GsPD 库。
工具自带 manifest，重复运行幂等。

The skill invokes the bundled `migrate_openclaw` Python tool, which streams
historical entries from an OpenClaw workspace into a running GsPD MCP server.
The tool keeps a manifest, so reruns are idempotent.

## 入口 / Entry

```bash
# 工具默认装在 <install_root>/tools/migrate_openclaw/ 下
GSPD_BASE_URL=http://127.0.0.1:<port> \
PYTHONPATH=<install_root>/tools \
python3 -m migrate_openclaw.migrate run \
    --workspace <openclaw_workspace_dir> \
    --agent-id <user_id> \
    --user-id <user_id> \
    --out-dir <workspace>/.gspd_migrate
```

## 前置条件 / Prerequisites

1. `celia_memory_mcp_server` 已在 `--http <port>` 模式启动并可连接。
2. OpenClaw workspace 路径下存在 `USER.md` 或 `Memory.md`（任一）。
3. 至少 50 MB 临时空间用于 manifest + .gspd_migrate 输出。

## 失败回退 / Fallback

工具失败时把 manifest 留在 `<out-dir>` 中，后续重跑只补做未完成条目。
完整说明见 `tools/migrate_openclaw/README.md`（同 `docs/handbook/migrate-openclaw.md`）。
