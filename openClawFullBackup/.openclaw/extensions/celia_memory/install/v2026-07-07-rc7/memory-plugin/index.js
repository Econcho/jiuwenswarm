/**
 * OpenClaw Celia Plugin — Memory
 *
 * Provides:
 * - Four semantic memory layers (original conversations / atomic facts /
 *   scene memory / global overview) with conflict detection
 * - Skills are managed internally by the C engine through unified search
 *   (injection) and OrcEngine auto-learn (learning). No separate plugin
 *   hooks are needed.
 *
 * Backed by C/SQLite engine via MCP (celia_memory_mcp_server).
 */
import { definePluginEntry, } from "openclaw/plugin-sdk/plugin-entry";
import { readBorrowedConfig, resolveBorrowedChatProvider, } from "./src/core/borrowed-config.js";
import { deriveCeliaLogFilePath, ensureCeliaLogDir, } from "./src/core/log-path.js";
import { createLazyCeliaClientProxy, getOrCreateCeliaClient, } from "../shared/celia-client-singleton.js";
import { buildMemoryClientEnv } from "./src/core/client-env.js";
import { celiaMemoryConfigSchema } from "./src/core/config.js";
import { registerService } from "./src/core/service.js";
import { registerMdFileSync, registerConversationCapture, registerServedL1Cleanup, registerTokenStatsCapture, registerGatewayObserver, } from "./src/memory/hooks.js";
import { registerTools, registerCli } from "./src/memory/tools.js";
// ============================================================================
// openclaw.json auto-borrow（embed + chat）
// ============================================================================
//
// memory-celia 的 embed/chat 凭据来源链（高 → 低优先级,三档）:
//   1. memory-celia.config.{embed,chat}  （用户在 plugin scope 显式配置 → 最高）
//   2. openclaw.json 全局配置:
//        embed ← agents.defaults.memorySearch.remote
//        chat  ← SERVICE_URL 沙箱默认 provider 或 primary provider
//   3. env 兜底（仅在 1+2 都空时生效）:
//        celia 沙盒: SERVICE_URL → xiaoyi 网关, PERSONAL_API_KEY → apiKey
//        openclaw 通用: OPENAI_CHAT_BASE_URL/API_KEY (celia_env.json)
//
// openclaw.json 是 single source of truth;env 兜底不再通过
// resolveWithEnvFallback 隐式前置，而是由 index.ts 显式计算为第三档，
// 确保 borrow 链优先于 env（celia 沙盒的 SERVICE_URL alias 不再劫持
// chat baseUrl/apiKey）。
//
// 解析逻辑见 ./src/core/borrowed-config.ts（拆出便于 UT 测试）。
//
// sandbox 额外 header（如 x-request-from / x-hag-trace-id / Accept）走
// OPENAI_{EMBED,CHAT}_HEADERS_JSON env 透传给 celia_memory_mcp_server，由 C 端
// mcp_env_headers 解析后注入到 BuildEmbedAuthHeaders / BuildChatAuthHeaders。
// memory-celia 会补插件级静态默认头；显式配置同名 header 时保留用户值。
export default definePluginEntry({
    id: "memory-celia",
    name: "Celia Memory",
    description: "Four semantic memory layers with original conversations, atomic facts, " +
        "scene memory, and global overview backed by C/SQLite engine via MCP",
    kind: "memory",
    configSchema: celiaMemoryConfigSchema,
    register(api) {
        const mode = api.registrationMode;
        /* cli-metadata / setup-only / discovery：无需任何注册 */
        if (mode !== "full" && mode !== "tool-discovery") {
            return;
        }
        const cfg = celiaMemoryConfigSchema.parse(api.pluginConfig);
        /* ========== tool-discovery：只注册工具 schema ==========
         * OpenClaw 5.6 在每轮 agent run 解析可用工具时用 "tool-discovery"
         * 模式加载插件。此模式下只需要 tool factory（name/description/
         * parameters）。OpenClaw 5.6 会执行 discovery registry 里的 tool
         * closure，因此这里不能捕获 null client；传懒代理，在 execute 时
         * 转发到 full 模式创建的 singleton client。 */
        if (mode === "tool-discovery") {
            registerTools(api, createLazyCeliaClientProxy(), cfg);
            return;
        }
        /* ========== full 模式：完整注册 ========== */
        const resolvedDbPath = api.resolvePath(cfg.dbPath);
        /* embed/chat 优先级链（高 → 低,三档）:
         *   1. cfg.{embed,chat}      — memory-celia.config JSON 字面量 / ${VAR}
         *   2. openclaw.json 全局借 — embed ← agents.defaults.memorySearch.remote
         *                              chat  ← SERVICE_URL sandbox 或 primary
         *   3. env 兜底              — celia 沙盒 SERVICE_URL / openclaw celia_env.json
         *
         * env 兜底不再通过 resolveWithEnvFallback 隐式进入 cfg（会短路 borrow），
         * 改为 index.ts 显式计算为第三档，确保 borrow 链优先。
         * chat 借的关键作用:避开 mcp_main 中 LlmEndpointsComplete() 的
         * chat/embed 联动判定（任一缺失则两个全 noop）——memorySearch 只配
         * embed,chat 必须从 models.providers 借才能让两端齐活。
         */
        const borrowed = readBorrowedConfig(api.logger, {
            chatProvider: resolveBorrowedChatProvider(process.env),
        });
        const envBuild = buildMemoryClientEnv(cfg, borrowed, process.env);
        const clientEnv = envBuild.clientEnv;
        for (const msg of envBuild.info) {
            api.logger.info?.(msg);
        }
        for (const msg of envBuild.warnings) {
            api.logger.warn?.(msg);
        }
        /* ========== Dreaming 开关：openclaw.json → env 传给 C 端 ==========
         * 三档语义（与 Celia_DreamingConfigMode 对齐）：
         *   undefined / 0 / "inherit"  → 不设 env，看 CELIA_DREAMING_ENABLED
         *   true / "on" / 2            → CELIA_DREAMING_ENABLED=1
         *   false / "off" / 1          → CELIA_DREAMING_ENABLED=0
         */
        const dreamCfg = cfg
            .dreamingEnabled;
        if (dreamCfg === true || dreamCfg === "on" || dreamCfg === 2) {
            clientEnv.CELIA_DREAMING_ENABLED = "1";
            api.logger.info?.("celia: dreaming enabled via openclaw.json");
        }
        else if (dreamCfg === false || dreamCfg === "off" || dreamCfg === 1) {
            clientEnv.CELIA_DREAMING_ENABLED = "0";
            api.logger.info?.("celia: dreaming disabled via openclaw.json");
        }
        /* ========== LLM 降级警告 ==========
         * cfg.embed 与 memorySearch 都没给出 apiKey+baseUrl 时，C 端会降级
         * 为 noop LLM（mcp_main 中 LoadOpenAiLlmEnv 走 fallback 分支）——插件
         * 功能大幅弱化但不卡死。显式
         * 告警以便运维快速定位"为何提取不工作"。 */
        if (!envBuild.embed.apiKey || !envBuild.embed.baseUrl) {
            api.logger.warn("celia: embed not configured (memory-celia.config.embed + " +
                "agents.defaults.memorySearch both empty), " +
                "LLM embedding disabled — vector search unavailable, " +
                "falling back to FTS keyword search");
        }
        if (!envBuild.chat.apiKey || !envBuild.chat.baseUrl) {
            api.logger.warn("celia: chat not configured (memory-celia.config.chat + " +
                "models.providers.<primary> both empty), " +
                "LLM generation disabled — extraction will no-op " +
                "AND embed will be force-disabled by mcp_main.c chat/embed coupling");
        }
        const logFilePath = deriveCeliaLogFilePath(resolvedDbPath);
        try {
            ensureCeliaLogDir(logFilePath);
        }
        catch (err) {
            api.logger.warn(`celia: failed to create log dir for ${logFilePath}: ${String(err)}`);
        }
        const client = getOrCreateCeliaClient(cfg.serverBinaryPath, resolvedDbPath, clientEnv, logFilePath);
        void client.start()
            .then(() => {
            api.logger.info(`celia: server prewarmed (binary: ${cfg.serverBinaryPath}, db: ${resolvedDbPath})`);
        })
            .catch((err) => {
            api.logger.warn?.(`celia: server prewarm failed: ${String(err)}`);
        });
        api.logger.info(`celia: registered (binary: ${cfg.serverBinaryPath}, db: ${resolvedDbPath})`);
        /* Celia 使用规则随 before_prompt_build 的 guidance system context
         * 返回，不再写入环境 AGENTS.md。 */
        registerTools(api, client, cfg);
        registerCli(api, client, cfg);
        /* 固定加载下沉 MEMORY.md（progressive-loading v1）。
         * append / both 分支已删除（含 fixedContextStrategy / memoryMdPath /
         * fixedContextMaxBytes 配置字段）。详见
         * docs/design/specs/2026-04-25-fixed-load-via-md.md。 */
        registerMdFileSync(api, client, cfg);
        /* 对话捕获 hook：每轮 agent_end 把对话清洗后写入 mem_conversation
         * 原始表，由 C 端 worker 异步消费 ingest pipeline。ingestMode 由
         * memory_store 工具的 store-signal 决定 deferred / deferred-urgent。 */
        registerConversationCapture(api, client, cfg);
        /* Token stats DFX（始终启用，无额外配置开关） */
        registerTokenStatsCapture(api, client, cfg);
        /* session_end 清理渐进加载 served-L1 状态，避免长进程会话状态累积。 */
        registerServedL1Cleanup(api, cfg);
        /* Gateway 观察器:llm_input 时打印最终 prompt 的 L0 + progressive retrieval 可见性 */
        registerGatewayObserver(api);
        registerService(api, client, cfg, resolvedDbPath);
    },
});
