/**
 * @file borrowed-config.ts
 * @brief 从 openclaw.json 借用 embed/chat 凭据 + sandbox 额外 headers。
 *
 * 拆分自 `index.ts`：把纯解析逻辑独立出来便于 UT 测试，并新增整段
 * headers 的提取（除 x-api-key / x-uid 外全部透传给 C 端 celia_memory_mcp_server，
 * 由 mcp_main 解析 OPENAI_{EMBED,CHAT}_HEADERS_JSON 注入到 sandbox 请求）。
 *
 * 优先级（高 → 低）:
 *   1. memory-celia.config.{embed,chat}      — plugin 显式配置
 *   2. agents.defaults.memorySearch.remote  — embed
 *      models.providers.<SERVICE_URL 沙箱默认 provider> — chat（沙箱时）
 *      models.providers.<primary>           — chat（默认回退）
 *
 * **headers 字段语义：整体覆盖（非合并）**
 *   - `cfg.embed.headers` 给了哪怕 1 条 → borrow 来的 headers 整段被丢弃
 *   - `cfg.embed.headers` 完全省略（undefined）→ 用 borrow 来的
 *   - `cfg.embed.headers = {}` → 显式空头（C 端发送时只剩 x-api-key + x-uid）
 *
 * 与 baseUrl/apiKey/model 的"逐字段优先级"不同——后者 cfg 字段空就 fallback
 * borrow。headers 选了整体覆盖,因为字段级 merge 容易让运维误以为「我没动
 * 这个 header 它就还在」,而实际上 borrow 来的可能含 stale 值。
 * 想要"在 borrow 之上加一条"的场景：直接编辑 openclaw.json 的对应 headers。
 *
 * 不读 process.env。env fallback 由 index.ts 的 buildMemoryClientEnv
 * 统一放在 cfg / borrow 之后处理，避免旧 env 提前短路 openclaw.json
 * provider 借用结果。
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { MEMORY_CHAT_MODEL_MAP } from "./memory-chat-model-map.js";
const EMPTY_BORROW = { embed: {}, chat: {} };
const CELIA_SANDBOX_CHAT_PROVIDER = "xiaoyiprovider";
export function resolveBorrowedChatProvider(env = process.env) {
    return env.SERVICE_URL ? CELIA_SANDBOX_CHAT_PROVIDER : undefined;
}
/* ---------------- 路径解析 ---------------- */
/**
 * 找到 openclaw.json 实际路径，按 CELIA_CONFIG_DIR / OPENCLAW_CONFIG_DIR /
 * $HOME/.openclaw / celiaclaw 沙盒默认路径依次尝试。
 */
export function findOpenclawJsonPath() {
    const candidates = [];
    for (const v of ["CELIA_CONFIG_DIR", "OPENCLAW_CONFIG_DIR"]) {
        const dir = process.env[v];
        if (dir)
            candidates.push(path.join(dir, "openclaw.json"));
    }
    const home = os.homedir();
    if (home)
        candidates.push(path.join(home, ".openclaw", "openclaw.json"));
    candidates.push("/home/sandbox/.openclaw/openclaw.json"); /* celiaclaw */
    for (const p of candidates) {
        try {
            if (fs.statSync(p).isFile())
                return p;
        }
        catch {
            /* ignore */
        }
    }
    return null;
}
/* ---------------- 内部 helpers ---------------- */
/**
 * xiaoyi 风格 OpenClaw 配置：apiKey 字段经常是字面量占位符，
 * headers["x-api-key"] 才是真实 SK 凭据。优先 headers["x-api-key"]，
 * 后退到顶层 apiKey。
 */
function pickRealApiKey(headers, topLevel) {
    if (headers
        && typeof headers["x-api-key"] === "string"
        && headers["x-api-key"].length > 0) {
        return headers["x-api-key"];
    }
    if (typeof topLevel === "string" && topLevel.length > 0) {
        return topLevel;
    }
    return undefined;
}
/**
 * 从 openclaw.json headers 子对象中提取沙盒额外 headers。
 *
 * 排除规则:
 *   - `x-api-key` / `x-uid`（大小写不敏感）→ 已通过专用字段走，
 *     重复写会被 C 端 BuildAuthHeaders 误用为额外槽位。
 *   - 非字符串 / 空字符串 → 静默丢弃（保持兼容）。
 *
 * 不做名称白名单（x-* / Accept 等）—— 让 openclaw.json 完全决定，
 * 维护方在配置层就能控制。
 */
export function pickExtraHeaders(headers) {
    const out = {};
    if (!headers)
        return out;
    for (const [k, v] of Object.entries(headers)) {
        const kl = k.toLowerCase();
        if (kl === "x-api-key" || kl === "x-uid")
            continue;
        if (typeof v === "string" && v.length > 0) {
            out[k] = v;
        }
    }
    return out;
}
/**
 * 判断模型名是否明显指向 reasoning/thinking 变体。
 *
 * openclaw.json 的 provider.models 中有些模型没有 reasoning 元数据，
 * 因此除显式 reasoning=true 外，再用名称做保守识别。
 */
function isThinkingModelName(modelName) {
    const lower = modelName.toLowerCase();
    return lower.includes("thinking") || lower.includes("reasoning");
}
/**
 * provider.models 里的单个模型 id。id 为空或非字符串时返回 undefined。
 */
function getProviderModelId(item) {
    if (!item || typeof item !== "object")
        return undefined;
    const id = item["id"];
    return typeof id === "string" && id.length > 0 ? id : undefined;
}
/**
 * 从同 provider 中给记忆结构化抽取选择 chat model。
 *
 * Agent 主模型可以是 thinking 变体，但记忆抽取需要稳定输出 JSON 数组。
 * 因此先查 MEMORY_CHAT_MODEL_MAP 显式映射；没有映射时，如果 provider
 * 显式声明了 reasoning=false 的模型，再选该模型；否则保留 primary。
 */
function pickMemoryChatModel(provider, primaryModelName) {
    if (!primaryModelName)
        return undefined;
    const mapped = MEMORY_CHAT_MODEL_MAP[primaryModelName];
    if (mapped)
        return mapped;
    const modelsRaw = provider["models"];
    if (!Array.isArray(modelsRaw)) {
        return primaryModelName;
    }
    const primary = modelsRaw.find((item) => getProviderModelId(item) === primaryModelName);
    if (primary?.["reasoning"] === false)
        return primaryModelName;
    const needsSafeModel = primary?.["reasoning"] === true || isThinkingModelName(primaryModelName);
    if (!needsSafeModel)
        return primaryModelName;
    for (const item of modelsRaw) {
        const rec = item;
        const id = getProviderModelId(item);
        if (id && rec["reasoning"] === false)
            return id;
    }
    return primaryModelName;
}
function pickProviderDefaultChatModel(provider) {
    const modelsRaw = provider["models"];
    if (!Array.isArray(modelsRaw))
        return undefined;
    let firstId;
    for (const item of modelsRaw) {
        const id = getProviderModelId(item);
        if (!firstId)
            firstId = id;
        if (id && item["reasoning"] === false) {
            return id;
        }
    }
    return firstId;
}
function isBorrowableChatProvider(provider) {
    if (!provider)
        return false;
    const headers = provider["headers"];
    return typeof provider["baseUrl"] === "string"
        && provider["baseUrl"].length > 0
        && Boolean(pickRealApiKey(headers, provider["apiKey"]));
}
function buildChatBorrow(provider, modelName) {
    const headers = provider["headers"];
    return {
        baseUrl: typeof provider["baseUrl"] === "string"
            ? provider["baseUrl"] : undefined,
        apiKey: pickRealApiKey(headers, provider["apiKey"]),
        model: modelName ? pickMemoryChatModel(provider, modelName) : undefined,
        uid: headers && typeof headers["x-uid"] === "string"
            ? headers["x-uid"] : undefined,
        headers: pickExtraHeaders(headers),
    };
}
/* ---------------- 主入口（纯函数 + 文件入口两个变体） ---------------- */
/**
 * 从已解析的 openclaw.json 顶层对象中借用 embed + chat 配置。
 *
 * 纯函数入口，不读磁盘 / 不读 env，便于 UT 测试。
 */
export function parseBorrowedConfig(data, options = {}) {
    if (!data || typeof data !== "object")
        return EMPTY_BORROW;
    const result = { embed: {}, chat: {} };
    const agents = data["agents"];
    const defaults = agents?.["defaults"];
    /* ---- embed: agents.defaults.memorySearch.remote ---- */
    const memSearch = defaults?.["memorySearch"];
    if (memSearch && memSearch["enabled"] !== false) {
        const remote = memSearch["remote"];
        if (remote) {
            const headers = remote["headers"];
            result.embed = {
                baseUrl: typeof remote["baseUrl"] === "string"
                    ? remote["baseUrl"] : undefined,
                apiKey: pickRealApiKey(headers, remote["apiKey"]),
                model: typeof memSearch["model"] === "string"
                    ? memSearch["model"] : undefined,
                uid: headers && typeof headers["x-uid"] === "string"
                    ? headers["x-uid"] : undefined,
                vectorDim: typeof memSearch["outputDimensionality"] === "number"
                    && Number.isFinite(memSearch["outputDimensionality"])
                    && memSearch["outputDimensionality"] > 0
                    ? Math.trunc(memSearch["outputDimensionality"])
                    : undefined,
                headers: pickExtraHeaders(headers),
            };
        }
    }
    /* ---- chat: 沙箱默认 provider 优先，否则 models.providers.<primary> ---- */
    const primary = defaults?.["model"]?.["primary"];
    const models = data["models"];
    const providers = models?.["providers"];
    let primaryProviderName;
    let primaryModelName;
    if (typeof primary === "string" && primary.includes("/")) {
        const slash = primary.indexOf("/");
        primaryProviderName = primary.slice(0, slash);
        primaryModelName = primary.slice(slash + 1);
    }
    const preferredProvider = options.chatProvider
        ? providers?.[options.chatProvider]
        : undefined;
    if (isBorrowableChatProvider(preferredProvider)) {
        result.chat = buildChatBorrow(preferredProvider, pickProviderDefaultChatModel(preferredProvider));
    }
    else if (primaryProviderName) {
        const provider = providers?.[primaryProviderName];
        if (provider) {
            result.chat = buildChatBorrow(provider, primaryModelName);
        }
    }
    return result;
}
/**
 * 把 OpenClaw JSON 里常见的 `${VAR}` 占位符解析成运行时环境变量。
 *
 * parseBorrowedConfig 保持纯函数、不读 env；只有文件入口在真正运行时解析。
 */
function resolveEnvPlaceholders(value) {
    if (!value || !value.includes("${"))
        return value;
    return value.replace(/\$\{([^}]+)\}/g, (_m, name) => {
        return process.env[name] ?? "";
    });
}
/** @brief 解析 headers 中的 `${VAR}`，空值丢弃。 */
function resolveHeaderPlaceholders(headers) {
    if (!headers)
        return headers;
    const out = {};
    for (const [key, value] of Object.entries(headers)) {
        const resolved = resolveEnvPlaceholders(value);
        if (resolved)
            out[key] = resolved;
    }
    return out;
}
/** @brief 解析 BorrowedConfig 中所有可含 `${VAR}` 的字符串字段。 */
function resolveBorrowedConfigPlaceholders(config) {
    return {
        embed: {
            ...config.embed,
            baseUrl: resolveEnvPlaceholders(config.embed.baseUrl),
            apiKey: resolveEnvPlaceholders(config.embed.apiKey),
            model: resolveEnvPlaceholders(config.embed.model),
            uid: resolveEnvPlaceholders(config.embed.uid),
            headers: resolveHeaderPlaceholders(config.embed.headers),
        },
        chat: {
            ...config.chat,
            baseUrl: resolveEnvPlaceholders(config.chat.baseUrl),
            apiKey: resolveEnvPlaceholders(config.chat.apiKey),
            model: resolveEnvPlaceholders(config.chat.model),
            uid: resolveEnvPlaceholders(config.chat.uid),
            headers: resolveHeaderPlaceholders(config.chat.headers),
        },
    };
}
/**
 * 从 openclaw.json 文件读取并借用 embed + chat 配置。
 *
 * 读取失败 / 路径找不到 → 返回空 BorrowedConfig，并在提供 logger 时
 * 输出 warn。文件不存在不视作错误，保持 plugin 向下兼容。
 */
export function readBorrowedConfig(logger, options = {}) {
    const p = findOpenclawJsonPath();
    if (!p)
        return EMPTY_BORROW;
    let raw;
    try {
        raw = fs.readFileSync(p, "utf8");
    }
    catch (e) {
        logger?.warn?.(`celia: cannot read ${p} for openclaw.json borrow: ${String(e)}`);
        return EMPTY_BORROW;
    }
    let data;
    try {
        data = JSON.parse(raw);
    }
    catch (e) {
        logger?.warn?.(`celia: ${p} is not valid JSON: ${String(e)}`);
        return EMPTY_BORROW;
    }
    const result = resolveBorrowedConfigPlaceholders(parseBorrowedConfig(data, options));
    if (result.embed.apiKey || result.chat.apiKey) {
        const embedHeaderKeys = Object.keys(result.embed.headers ?? {});
        const chatHeaderKeys = Object.keys(result.chat.headers ?? {});
        logger?.info?.(`celia: borrowed config from openclaw.json — `
            + `embed{model=${result.embed.model ?? "(unset)"} `
            + `baseUrl=${result.embed.baseUrl ? "(set)" : "(unset)"} `
            + `apiKey=${result.embed.apiKey ? `len=${result.embed.apiKey.length}` : "(unset)"} `
            + `uid=${result.embed.uid ? `len=${result.embed.uid.length}` : "(unset)"} `
            + `extraHeaders=[${embedHeaderKeys.join(",")}]} `
            + `chat{model=${result.chat.model ?? "(unset)"} `
            + `baseUrl=${result.chat.baseUrl ? "(set)" : "(unset)"} `
            + `apiKey=${result.chat.apiKey ? `len=${result.chat.apiKey.length}` : "(unset)"} `
            + `extraHeaders=[${chatHeaderKeys.join(",")}]}`);
    }
    return result;
}
