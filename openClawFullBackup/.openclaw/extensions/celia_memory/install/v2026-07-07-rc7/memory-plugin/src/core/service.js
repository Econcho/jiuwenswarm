/**
 * 背景服务实现：MCP server 生命周期管理 + 内部 HTTP 路由。
 *
 * 暴露两个内部 HTTP 路由，供 chat.py 在 OpenClaw 模式下调用，
 * 避免 chat.py 自己 spawn celia_memory_mcp_server 与 gateway 竞争同一 DB 文件：
 *
 *   POST /celia/memory_add   { userId, content, scope }  → memory_add MCP 工具
 *   POST /celia/clear_user   {}                           → 停进程 + 删 DB + 重启
 */
import { existsSync, unlinkSync } from "node:fs";
async function readBody(req) {
    return new Promise((resolve, reject) => {
        let data = "";
        req.on("data", (chunk) => { data += chunk.toString(); });
        req.on("end", () => resolve(data));
        req.on("error", reject);
    });
}
export function registerService(api, client, cfg, resolvedDbPath) {
    api.registerService({
        id: "celia",
        start: async (_ctx) => {
            await client.start();
            api.logger.info(`celia: server started (binary: ${cfg.serverBinaryPath}, db: ${resolvedDbPath})`);
        },
        stop: (_ctx) => {
            client.close();
            api.logger.info("celia: server stopped");
        },
    });
    // ── POST /celia/memory_add ──────────────────────────────────────────────────
    // Internal route: chat.py calls this to add a memory record without spawning
    // its own celia_memory_mcp_server process.
    api.registerHttpRoute({
        path: "/celia/memory_add",
        auth: "plugin",
        match: "exact",
        replaceExisting: true,
        handler: async (req, res) => {
            let result;
            try {
                const body = await readBody(req);
                const { userId, content, scope } = JSON.parse(body);
                result = await client.callTool("memory_add", { userId, content, scope });
                res.statusCode = 200;
            }
            catch (e) {
                result = { error: String(e) };
                res.statusCode = 500;
            }
            res.setHeader("Content-Type", "application/json");
            res.end(JSON.stringify(result));
            return true;
        },
    });
    // ── POST /celia/clear_user ──────────────────────────────────────────────────
    // Internal route: stop celia_memory_mcp_server, delete DB file, restart.
    // Used by chat.py clear_memories in openclaw mode.
    api.registerHttpRoute({
        path: "/celia/clear_user",
        auth: "plugin",
        match: "exact",
        replaceExisting: true,
        handler: async (_req, res) => {
            try {
                client.close();
                for (const suffix of ["", "-wal", "-shm"]) {
                    const p = resolvedDbPath + suffix;
                    if (existsSync(p))
                        unlinkSync(p);
                }
                await client.start();
                res.statusCode = 200;
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ ok: true }));
            }
            catch (e) {
                res.statusCode = 500;
                res.setHeader("Content-Type", "application/json");
                res.end(JSON.stringify({ error: String(e) }));
            }
            return true;
        },
    });
}
