---
name: mem-list
description: 当用户想直接看当前 GsPD 库里都存了哪些记忆时使用（独立于 MCP 客户端，直连本地 server）。触发：「列一下记忆」「看下 memory 都有什么」「list my memories」「dump memory db」。可按 level（l0/l1/l2）和 user 过滤。Read-only。
---

# mem-list 技能 / List memories

## 用途 / Purpose

包装 `tools/mem-list/mem-list.sh`：临时启动一个 `celia_memory_mcp_server` stdio
进程，发 `memory_list` MCP 工具调用，把结果格式化打印。

Wraps `tools/mem-list/mem-list.sh`: spawns a transient `celia_memory_mcp_server`
stdio process, sends a `memory_list` MCP tool call, formats the result.

## 入口 / Entry

```bash
bash tools/mem-list/mem-list.sh [--level l0|l1|l2] [--limit N] [--user UID] [--db PATH]
```

默认：`--level l0 --limit 20`。

## 环境变量 / Env

- `GSPD_SERVER` — `celia_memory_mcp_server` 二进制路径（默认 `<root>/build/bin/celia_memory_mcp_server.exe`）
- `GSPD_DB` — sqlite 数据库路径（默认 `<root>/build/gspd_memory.db`）

## 只读 / Read-only

仅读取数据库，不写入、不重启任何运行态服务。
Reads the DB only; never writes or restarts any running service.
