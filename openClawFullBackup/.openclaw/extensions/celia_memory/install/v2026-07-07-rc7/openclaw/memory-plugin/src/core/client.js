/**
 * Celia MCP Client — stdio JSON-RPC 2.0 通信层。
 */
import { spawn } from "node:child_process";
import { accessSync, chmodSync, constants, existsSync, statSync, } from "node:fs";
import { emitDFX, genTraceId } from "./dfx.js";
import { clearSessionCache } from "./session.js";
export function parseSearchResponse(raw) {
    if (raw && typeof raw === "object" && "results" in raw) {
        return raw;
    }
    return { count: 0, status: 0, results: [] };
}
/**
 * 把 C 端原始响应解析为 LoadL1BatchResponse；shape 异常时返回空集合。
 * 对响应字段做防御式解析，避免工具异常输出影响调用方。
 */
export function parseLoadL1BatchResponse(raw) {
    if (!raw || typeof raw !== "object") {
        return { results: [], totalBytes: 0, trimmed: false };
    }
    const obj = raw;
    const rawResults = Array.isArray(obj.results) ? obj.results : [];
    const results = [];
    for (const r of rawResults) {
        if (!r || typeof r !== "object")
            continue;
        const item = r;
        if (typeof item.path !== "string" || typeof item.status !== "string") {
            continue;
        }
        results.push(item);
    }
    const totalBytes = typeof obj.total_bytes === "number" ? obj.total_bytes : 0;
    const trimmed = obj.trimmed === true;
    return { results, totalBytes, trimmed };
}
// ============================================================================
// Debug tool whitelist — logs only these tool calls to keep noise low
// ============================================================================
const DEBUG_TOOLS = new Set([
    "memory_get_l0_global_summary",
    "memory_get_l1_index",
    "memory_load_l1",
    "memory_search_l2",
    "memory_search_l3",
]);
/** 从 `parsed` 顶层提取 _meta；非对象 / 缺字段都返回 undefined。
 *
 * 导出为测试可见（client.test.ts），生产代码通过 emitMcpEcho 间接调用。
 */
export function extractRespMeta(parsed) {
    if (!parsed || typeof parsed !== "object")
        return undefined;
    const meta = parsed._meta;
    if (!meta || typeof meta !== "object")
        return undefined;
    return meta;
}
/** scope_denied 三个维度求和（任一字段缺失视为 0）。导出为测试可见。 */
export function sumScopeDenied(sd) {
    if (!sd)
        return 0;
    return (sd.tenant ?? 0) + (sd.user ?? 0) + (sd.session ?? 0);
}
/**
 * B5：解析响应 _meta 并发射 mcp.echo DFX 事件。
 *
 * 老服务端不返回 _meta 时静默跳过；任何失败 swallow（emitDFX 内部已 swallow）。
 */
function emitMcpEcho(toolName, parsed, traceId) {
    const meta = extractRespMeta(parsed);
    if (!meta)
        return;
    emitDFX({ traceId }, "mcp.echo", {
        tool: toolName,
        dur_ms: meta.dur_ms,
        rerank_status: meta.rerank_status,
        scope_denied_total: sumScopeDenied(meta.scope_denied),
        dedup_dropped: meta.dedup_info?.dropped_count,
    });
}
// ============================================================================
// Celia MCP Client (stdio JSON-RPC 2.0)
// ============================================================================
// Auto-restart policy
// ============================================================================
/** Exponential backoff base. First retry waits this long. */
export const RESTART_BACKOFF_BASE_MS = 1000;
/** Backoff cap. Each retry doubles up to this. */
export const RESTART_BACKOFF_MAX_MS = 30_000;
/**
 * Hard ceiling on **consecutive** failed restarts. Prevents an
 * infinitely-looping crashy server from burning CPU.  Counter is
 * cleared once the process survives RESTART_STABILITY_WINDOW_MS.
 */
export const RESTART_MAX_CONSECUTIVE_ATTEMPTS = 10;
/**
 * "Process is healthy" threshold.  If a freshly spawned process
 * stays alive at least this long, we consider the previous restart
 * genuinely successful and reset the attempt counter.  Without
 * this, an init-then-immediately-crash loop would silently never
 * trigger the consecutive-attempts ceiling.
 */
export const RESTART_STABILITY_WINDOW_MS = 30_000;
export function buildCeliaArgs(dbPath, logFilePath) {
    if (logFilePath && logFilePath.trim()) {
        return [dbPath, "--log-file", logFilePath];
    }
    return [dbPath];
}
const CELIA_TO_GSPD_ENV_ALIASES = [
    ["CELIA_LOG_LEVEL", "GSPD_LOG_LEVEL"],
    ["CELIA_LOG_FILE", "GSPD_LOG_FILE"],
    ["CELIA_LOG_MAX_BYTES", "GSPD_LOG_MAX_BYTES"],
    ["CELIA_LOG_BACKUPS", "GSPD_LOG_BACKUPS"],
    ["CELIA_MCP_PORT", "GSPD_MCP_PORT"],
    ["CELIA_VECTOR_DIM", "GSPD_VECTOR_DIM"],
    ["CELIA_CHAT_UID", "GSPD_CHAT_UID"],
    ["CELIA_EMBED_UID", "GSPD_EMBED_UID"],
    ["CELIA_PROCEDURAL_DIR", "GSPD_PROCEDURAL_DIR"],
    ["CELIA_AGG_CHECK_INTERVAL", "GSPD_AGG_CHECK_INTERVAL"],
    ["CELIA_TENANT_ID", "GSPD_TENANT_ID"],
    ["CELIA_CA_BUNDLE", "GSPD_CA_BUNDLE"],
    ["CELIA_PROMOTE_SKIP_EVIDENCE", "GSPD_PROMOTE_SKIP_EVIDENCE"],
    ["CELIA_PROCEDURAL_LEARN_DEBUG", "GSPD_PROCEDURAL_LEARN_DEBUG"],
    [
        "CELIA_PROCEDURAL_PREFILTER_ENABLED",
        "GSPD_PROCEDURAL_PREFILTER_ENABLED",
    ],
    ["CELIA_DREAMING_ENABLED", "GSPD_DREAMING_ENABLED"],
];
export function withGspdLegacyEnvAliases(env) {
    const out = { ...env };
    for (const [celiaKey, gspdKey] of CELIA_TO_GSPD_ENV_ALIASES) {
        if (out[gspdKey] === undefined && out[celiaKey] !== undefined) {
            out[gspdKey] = out[celiaKey];
        }
    }
    return out;
}
/**
 * Pure decision function — given how the child process exited, describe
 * why the live child should be auto-restarted.  Lifecycle ownership stays
 * in the exit handler: when close() intentionally tears down the child it
 * flips shouldRestart=false before sending SIGTERM, so this helper is only
 * consulted while OpenClaw still expects the MCP backend to stay alive.
 *
 * Policy:
 *   - Any signal → restart while shouldRestart=true.
 *   - exit code 0 → restart while shouldRestart=true.
 *   - exit code != 0 (incl. null with no signal) → restart.
 *
 * Why this matters: external process managers or operators may terminate
 * celia_memory_mcp_server directly.  A supervisor-started replacement cannot inherit
 * the existing stdio pipe, so the plugin itself must respawn the child.
 */
export function shouldRestartOnExit(code, signal) {
    if (signal === "SIGSEGV" ||
        signal === "SIGABRT" ||
        signal === "SIGBUS" ||
        signal === "SIGILL" ||
        signal === "SIGFPE") {
        return { restart: true, reason: `crashed with ${signal}` };
    }
    if (signal != null) {
        return { restart: true, reason: `terminated by ${signal}` };
    }
    if (code === 0) {
        return { restart: true, reason: "clean exit while backend is expected" };
    }
    return { restart: true, reason: `exited with code ${code}` };
}
/**
 * Ensure the MCP server binary is executable before spawn().
 *
 * Backup/restore tools may unpack release files through ZIP or generic copy
 * paths that drop POSIX executable bits.  Treat that as repairable state:
 * preserve the existing mode and add x bits for owner/group/other.
 */
export function ensureExecutableBinary(binaryPath) {
    if (!existsSync(binaryPath)) {
        return {
            ok: false,
            repaired: false,
            reason: `celia_memory_mcp_server binary not found: ${binaryPath}. ` +
                `Build with ./build.sh or check serverBinaryPath config.`,
        };
    }
    let stat;
    try {
        stat = statSync(binaryPath);
    }
    catch (err) {
        return {
            ok: false,
            repaired: false,
            reason: `cannot stat celia_memory_mcp_server binary ${binaryPath}: ${String(err)}`,
        };
    }
    if (!stat.isFile()) {
        return {
            ok: false,
            repaired: false,
            reason: `celia_memory_mcp_server binary path is not a file: ${binaryPath}`,
        };
    }
    try {
        accessSync(binaryPath, constants.X_OK);
        return { ok: true, repaired: false };
    }
    catch {
        /* Continue below: chmod may repair a restore-time mode loss. */
    }
    try {
        chmodSync(binaryPath, stat.mode | 0o111);
        accessSync(binaryPath, constants.X_OK);
        return { ok: true, repaired: true };
    }
    catch (err) {
        return {
            ok: false,
            repaired: false,
            reason: `celia_memory_mcp_server binary is not executable and chmod repair failed: ` +
                `${binaryPath}: ${String(err)}`,
        };
    }
}
// ============================================================================
export class CeliaMcpClient {
    binaryPath;
    dbPath;
    env;
    logFilePath;
    proc = null;
    nextId = 1;
    pending = new Map();
    buffer = "";
    /** Set to false by close() to suppress auto-restart on intentional shutdown. */
    shouldRestart = false;
    /** Exponential backoff state for restart attempts. */
    restartAttempts = 0;
    /**
     * Wall-clock timestamp (ms) when the most recent spawn() finished its
     * initialize handshake.  Used to determine whether the process has
     * lived long enough to count as "stable" — see RESTART_STABILITY_WINDOW_MS.
     * 0 = no successful spawn yet this lifetime.
     */
    lastSpawnSucceededAtMs = 0;
    /**
     * Monotonically increasing generation counter.  Each spawn() call gets its
     * own generation ID so that a stale `exit` event from a previously-killed
     * process (which may arrive asynchronously AFTER a new process has already
     * been started) is silently discarded rather than nullifying the new process
     * reference or triggering an unwanted restart.
     */
    procGeneration = 0;
    /** Reentrance guard: if start() is already in flight, return its promise. */
    startPromise = null;
    constructor(binaryPath, dbPath, env, logFilePath) {
        this.binaryPath = binaryPath;
        this.dbPath = dbPath;
        this.env = env;
        this.logFilePath = logFilePath;
        process.once("exit", () => {
            this.close();
        });
    }
    async start() {
        if (this.isConnected())
            return;
        if (this.startPromise)
            return this.startPromise;
        this.startPromise = this.doStart();
        try {
            await this.startPromise;
        }
        finally {
            this.startPromise = null;
        }
    }
    async doStart() {
        this.shouldRestart = true;
        this.restartAttempts = 0;
        await this.spawn();
    }
    async spawn() {
        const generation = ++this.procGeneration;
        clearSessionCache();
        const binaryCheck = ensureExecutableBinary(this.binaryPath);
        if (!binaryCheck.ok) {
            throw new Error(binaryCheck.reason);
        }
        if (binaryCheck.repaired) {
            process.stderr.write(`[celia-mcp] repaired executable bit: ${this.binaryPath}\n`);
        }
        this.proc = spawn(this.binaryPath, buildCeliaArgs(this.dbPath, this.logFilePath), {
            stdio: ["pipe", "pipe", "pipe"],
            env: { ...process.env, ...withGspdLegacyEnvAliases(this.env) },
        });
        this.proc.unref();
        this.proc.stdin?.unref?.();
        this.proc.stdout?.unref?.();
        this.proc.stderr?.unref?.();
        this.proc.stdout.on("data", (chunk) => this.onData(chunk));
        this.proc.stderr.on("data", (chunk) => {
            process.stderr.write(`[celia-mcp] ${chunk}`);
        });
        // spawn errors (ENOENT, EACCES) indicate binary/config issues that
        // won't self-heal — no auto-restart here (unlike the 'exit' handler).
        this.proc.on("error", (err) => {
            if (generation !== this.procGeneration)
                return;
            process.stderr.write(`[celia-mcp] spawn error: ${err.message}\n`);
            for (const { reject, timeout } of this.pending.values()) {
                clearTimeout(timeout);
                reject(new Error(`celia_memory_mcp_server spawn failed: ${err.message}`));
            }
            this.pending.clear();
            this.proc = null;
        });
        this.proc.on("exit", (code, signal) => {
            // Discard stale exit events from previously-killed processes.
            // This prevents a delayed exit from an old process from nullifying the
            // new process reference or triggering an unwanted restart loop.
            if (generation !== this.procGeneration)
                return;
            for (const { reject, timeout } of this.pending.values()) {
                clearTimeout(timeout);
                reject(new Error(`celia_memory_mcp_server exited with code ${code}, signal ${signal}`));
            }
            this.pending.clear();
            this.proc = null;
            if (!this.shouldRestart)
                return;
            // Reset attempt counter if process survived long enough to be
            // considered stable — without this, init-then-crash loops would
            // never trip the consecutive-attempts ceiling below.
            const survivedMs = this.lastSpawnSucceededAtMs > 0
                ? Date.now() - this.lastSpawnSucceededAtMs
                : 0;
            if (survivedMs >= RESTART_STABILITY_WINDOW_MS) {
                this.restartAttempts = 0;
            }
            const decision = shouldRestartOnExit(code, signal);
            if (!decision.restart) {
                process.stderr.write(`[celia-mcp] process ${decision.reason}, not restarting\n`);
                return;
            }
            if (this.restartAttempts >= RESTART_MAX_CONSECUTIVE_ATTEMPTS) {
                process.stderr.write(`[celia-mcp] giving up after ${RESTART_MAX_CONSECUTIVE_ATTEMPTS} ` +
                    `consecutive failed restarts; manual intervention required\n`);
                return;
            }
            // Exponential backoff: 1s, 2s, 4s, 8s, ... capped at 30s.
            const delayMs = Math.min(RESTART_BACKOFF_BASE_MS * Math.pow(2, this.restartAttempts), RESTART_BACKOFF_MAX_MS);
            this.restartAttempts++;
            process.stderr.write(`[celia-mcp] process ${decision.reason} (code=${code} signal=${signal}); ` +
                `restarting in ${delayMs}ms (attempt ${this.restartAttempts}/${RESTART_MAX_CONSECUTIVE_ATTEMPTS})\n`);
            const timer = setTimeout(() => {
                if (this.shouldRestart) {
                    this.spawn().catch((err) => {
                        process.stderr.write(`[celia-mcp] restart failed: ${err}\n`);
                    });
                }
            }, delayMs);
            timer.unref();
        });
        await this.request("initialize", {
            protocolVersion: "2025-03-26",
            capabilities: {},
            clientInfo: { name: "openclaw-memory-celia", version: "0.1.0" },
        });
        this.send({ jsonrpc: "2.0", method: "notifications/initialized" });
        this.lastSpawnSucceededAtMs = Date.now();
    }
    async callTool(name, args, timeoutMs = 120_000, opts) {
        await this.start();
        /* 跨层 trace_id 注入：优先复用调用方传入；不传则 args 里已有的
         * `_trace_id`（如调用方自己塞的）；都没有则兜底生成。
         * 下划线前缀表示协议扩展字段（C 端 `_trace_id` 解析），不会和
         * 业务字段冲突。 */
        const existing = opts?.traceId
            ?? (typeof args._trace_id === "string" ? args._trace_id : undefined);
        const traceId = existing ?? genTraceId();
        const augmentedArgs = existing ? args : { ...args, _trace_id: traceId };
        if (DEBUG_TOOLS.has(name)) {
            const argsPreview = JSON.stringify(augmentedArgs).slice(0, 200);
            console.info(`memory-celia: [MCP→] trace_id=${traceId} tool=${name} args=${argsPreview}`);
        }
        const result = (await this.request("tools/call", {
            name,
            arguments: augmentedArgs,
        }, timeoutMs));
        if (result.isError) {
            const text = result.content?.[0]?.text ?? "Unknown MCP tool error";
            if (DEBUG_TOOLS.has(name)) {
                console.info(`memory-celia: [MCP←] tool=${name} error="${text.slice(0, 200)}"`);
            }
            throw new Error(text);
        }
        const text = result.content?.[0]?.text;
        let parsed = result;
        if (text) {
            try {
                parsed = JSON.parse(text);
                // C server sometimes double-encodes JSON (string wrapping an object).
                // Unwrap one extra layer if needed.
                if (typeof parsed === "string") {
                    try {
                        parsed = JSON.parse(parsed);
                    }
                    catch { /* keep as string */ }
                }
            }
            catch {
                parsed = text;
            }
        }
        if (DEBUG_TOOLS.has(name)) {
            const respPreview = JSON.stringify(parsed).slice(0, 300);
            console.info(`memory-celia: [MCP←] tool=${name} resp=${respPreview}`);
        }
        /* B5：解析响应 _meta（C 端 mcp_tools_meta 输出）并发射 mcp.echo DFX。
         * 老服务端不返回 _meta → 静默跳过；不影响调用方拿到的 result。 */
        emitMcpEcho(name, parsed, traceId);
        return parsed;
    }
    /**
     * memory_load_l1 批量调用 wrapper。
     *
     * 把 paths[] 透传给 C 端 batch 路径（mcp_tools_load_l1.c 解析 args.paths
     * 数组分支），返回结构化结果。空 paths 直接返回零结果，不发起调用。
     *
     * tenantId / userId 可选；为空时由 C 端按 instance/default-user
     * fallback。OpenClaw 插件正常会显式传入配置中的 userId。
     *
     * @param  paths  L1 文档相对路径数组。
     * @param  opts   可选 traceId / timeoutMs / tenantId / userId。
     */
    async loadL1Batch(paths, opts) {
        if (!Array.isArray(paths) || paths.length === 0) {
            return { results: [], totalBytes: 0, trimmed: false };
        }
        const args = { paths };
        if (opts?.tenantId)
            args.tenant_id = opts.tenantId;
        if (opts?.userId)
            args.user_id = opts.userId;
        const raw = await this.callTool("memory_load_l1", args, opts?.timeoutMs ?? 120_000, opts?.traceId ? { traceId: opts.traceId } : undefined);
        return parseLoadL1BatchResponse(raw);
    }
    /**
     * Gracefully stop the server.
     *
     * Sends SIGTERM first to let the C side flush any in-flight ingest +
     * close the SQLite handle cleanly, then schedules a SIGKILL fallback
     * after `graceMs` for the case where the server is already wedged.
     * Auto-restart is suppressed regardless of how the child exits.
     */
    close(graceMs = 5000) {
        this.shouldRestart = false;
        clearSessionCache();
        if (!this.proc)
            return;
        const proc = this.proc;
        this.proc = null;
        try {
            proc.kill("SIGTERM");
        }
        catch {
            // already exited — nothing to do
            return;
        }
        // Fallback: if the process hasn't acknowledged SIGTERM by graceMs,
        // escalate to SIGKILL.  unref() so the timer never blocks the
        // event loop on its own.
        const killTimer = setTimeout(() => {
            if (proc.exitCode == null && proc.signalCode == null) {
                try {
                    proc.kill("SIGKILL");
                }
                catch {
                    /* already exited between the two checks — ignore */
                }
            }
        }, graceMs);
        killTimer.unref();
    }
    isConnected() {
        return this.proc !== null && this.proc.exitCode === null;
    }
    /** Wait until the server is connected, with timeout. */
    async waitReady(timeoutMs = 5000) {
        if (this.isConnected() && !this.startPromise)
            return true;
        let timeoutHandle;
        const ready = (this.startPromise ?? this.start())
            .then(() => true)
            .catch((err) => {
            process.stderr.write(`[celia-mcp] start failed while waiting: ${err}\n`);
            return false;
        })
            .finally(() => {
            // Clear the timer once start resolves either way — without this
            // the setTimeout would keep the event loop alive past resolution.
            if (timeoutHandle)
                clearTimeout(timeoutHandle);
        });
        const timeout = new Promise((resolve) => {
            timeoutHandle = setTimeout(() => resolve(false), timeoutMs);
            timeoutHandle.unref();
        });
        return Promise.race([ready, timeout]);
    }
    request(method, params, timeoutMs = 120_000) {
        return new Promise((resolve, reject) => {
            const id = this.nextId++;
            const timeout = setTimeout(() => {
                if (this.pending.has(id)) {
                    this.pending.delete(id);
                    reject(new Error(`MCP request timeout: ${method}`));
                }
            }, timeoutMs);
            this.pending.set(id, { resolve, reject, timeout });
            try {
                this.send({ jsonrpc: "2.0", id, method, params });
            }
            catch (err) {
                this.pending.delete(id);
                clearTimeout(timeout);
                reject(err instanceof Error ? err : new Error(String(err)));
            }
        });
    }
    send(msg) {
        if (!this.proc?.stdin?.writable) {
            throw new Error("celia_memory_mcp_server is not running");
        }
        this.proc.stdin.write(JSON.stringify(msg) + "\n");
    }
    onData(chunk) {
        this.buffer += chunk.toString();
        const lines = this.buffer.split("\n");
        this.buffer = lines.pop() ?? "";
        for (const line of lines) {
            if (!line.trim())
                continue;
            try {
                const msg = JSON.parse(line);
                if (msg.id != null && this.pending.has(msg.id)) {
                    const { resolve, reject, timeout } = this.pending.get(msg.id);
                    this.pending.delete(msg.id);
                    clearTimeout(timeout);
                    if (msg.error) {
                        reject(new Error(`MCP error ${msg.error.code}: ${msg.error.message}`));
                    }
                    else {
                        resolve(msg.result);
                    }
                }
            }
            catch {
                // Ignore unparseable lines
            }
        }
    }
}
