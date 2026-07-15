/**
 * Global singleton for CeliaMcpClient — shared across memory-plugin and context-engine-plugin.
 *
 * Uses Symbol.for() + globalThis to ensure a single MCP server process
 * even when two independent plugins are loaded.
 */
import { CeliaMcpClient } from "../memory-plugin/src/core/client.js";
const CELIA_CLIENT_KEY = Symbol.for("openclaw.celia_mcp_client");
const CELIA_SESSIONS_KEY = Symbol.for("openclaw.celia_active_sessions");
/**
 * Get an existing shared client or create a new one.
 * First caller creates the client; subsequent callers reuse it.
 */
export function getOrCreateCeliaClient(binaryPath, dbPath, env, logFilePath) {
    const g = globalThis;
    if (g[CELIA_CLIENT_KEY]) {
        g[CELIA_CLIENT_KEY].refCount++;
        return g[CELIA_CLIENT_KEY].client;
    }
    const client = new CeliaMcpClient(binaryPath, dbPath, env, logFilePath);
    g[CELIA_CLIENT_KEY] = { client, refCount: 1 };
    return client;
}
/** Return the already-created shared client, if full registration owns one. */
export function getExistingCeliaClient() {
    const g = globalThis;
    return g[CELIA_CLIENT_KEY]?.client;
}
/**
 * Tool-discovery mode must not start its own backend, but OpenClaw 5.6 may
 * execute tools registered from that mode. Resolve the full-mode singleton at
 * call time so discovery closures stay executable without duplicating server
 * processes or ref-count ownership.
 */
export function createLazyCeliaClientProxy() {
    const requireClient = () => {
        const client = getExistingCeliaClient();
        if (!client) {
            throw new Error("CELIA MCP client is not initialized");
        }
        return client;
    };
    return {
        start: () => requireClient().start(),
        callTool: (name, args, timeoutMs, opts) => requireClient().callTool(name, args, timeoutMs, opts),
        loadL1Batch: (paths, opts) => requireClient().loadL1Batch(paths, opts),
        close: () => {
            /* Discovery proxy does not own the singleton ref-count. */
        },
        isConnected: () => getExistingCeliaClient()?.isConnected() ?? false,
        waitReady: (timeoutMs) => getExistingCeliaClient()?.waitReady(timeoutMs) ?? Promise.resolve(false),
    };
}
/**
 * Release a reference to the shared client.
 * When refCount reaches 0, the MCP server process is closed.
 */
export function releaseCeliaClient() {
    const g = globalThis;
    if (!g[CELIA_CLIENT_KEY])
        return;
    g[CELIA_CLIENT_KEY].refCount--;
    if (g[CELIA_CLIENT_KEY].refCount <= 0) {
        g[CELIA_CLIENT_KEY].client.close();
        delete g[CELIA_CLIENT_KEY];
    }
}
/**
 * Get the shared active sessions map — persists across plugin re-registrations.
 * Uses globalThis so the same Map instance is reused even when register() is
 * called multiple times per agent turn by OpenClaw's plugin lifecycle.
 */
export function getSharedActiveSessionsMap() {
    const g = globalThis;
    if (!g[CELIA_SESSIONS_KEY]) {
        g[CELIA_SESSIONS_KEY] = new Map();
    }
    return g[CELIA_SESSIONS_KEY];
}
