/**
 * @file client-env.ts
 * @brief 计算传给 celia_memory_mcp_server 的最终环境变量。
 */
import { withCeliaSandboxHeaders } from "./sandbox-headers.js";
const CELIA_CHAT_PATH = "/celia-claw/v1/sse-api";
const CHAT_REQUEST_FROM_HEADER = "x-request-from";
const CHAT_ACCEPT_HEADER = "Accept";
const SENSITIVE_HEADER_NAMES = new Set([
    "authorization",
    "x-api-key",
    "api-key",
    "x-auth-token",
]);
function Value(v) {
    return v && v.length > 0 ? v : "";
}
function HasAnyEndpointField(c) {
    return Boolean(c.baseUrl || c.apiKey || c.model);
}
function IsReady(c) {
    /* C backend enables an endpoint with baseUrl + apiKey. model is optional
     * there, so keep it source-local instead of borrowing it from another
     * candidate. */
    return Boolean(c.baseUrl && c.apiKey);
}
function UrlHost(url) {
    if (!url) {
        return "(unset)";
    }
    try {
        return new URL(url).host || "(invalid-url)";
    }
    catch {
        return "(invalid-url)";
    }
}
function IsSandboxChatUrl(url) {
    return url.includes("/celia-claw/") || url.includes("/sse-api");
}
function HeaderValue(headers, name) {
    const needle = name.toLowerCase();
    for (const [key, value] of Object.entries(headers)) {
        if (key.toLowerCase() === needle && typeof value === "string") {
            return value.trim();
        }
    }
    return "";
}
function HeaderKeysForLog(headers) {
    const keys = Object.keys(headers)
        .filter((key) => !SENSITIVE_HEADER_NAMES.has(key.toLowerCase()))
        .sort();
    return `[${keys.join(",")}]`;
}
function ParseHeadersJson(headersJson) {
    if (!headersJson) {
        return { ok: true, headers: {} };
    }
    try {
        const parsed = JSON.parse(headersJson);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            return { ok: true, headers: parsed };
        }
    }
    catch {
        /* handled below */
    }
    return { ok: false, headers: {} };
}
function DisableChatEndpoint(clientEnv, chat) {
    clientEnv.OPENAI_CHAT_BASE_URL = "";
    clientEnv.OPENAI_CHAT_API_KEY = "";
    clientEnv.OPENAI_CHAT_MODEL = "";
    clientEnv.OPENAI_CHAT_HEADERS_JSON = "";
    clientEnv.CELIA_CHAT_UID = "";
    chat.baseUrl = "";
    chat.apiKey = "";
    chat.model = "";
    chat.headers = {};
    chat.uid = "";
    chat.uidSource = "none";
    chat.auth = "bearer";
}
function InvalidChatSandboxWarning(clientEnv, chat, reason, headerKeys) {
    return "celia: invalid chat sandbox config; "
        + `reason=${reason}; source=${chat.source} auth=${chat.auth} `
        + `host=${UrlHost(clientEnv.OPENAI_CHAT_BASE_URL)} `
        + `model=${clientEnv.OPENAI_CHAT_MODEL || "(unset)"} `
        + `uidLen=${Value(clientEnv.CELIA_CHAT_UID).length} `
        + `apiKeyLen=${Value(clientEnv.OPENAI_CHAT_API_KEY).length} `
        + `headerKeys=${headerKeys} action=chat_endpoint_disabled`;
}
function ValidateFinalChatSandboxEnv(clientEnv, chat, warnings) {
    if (!Value(clientEnv.CELIA_CHAT_UID)) {
        return;
    }
    const parsed = ParseHeadersJson(clientEnv.OPENAI_CHAT_HEADERS_JSON);
    const missing = [];
    const reasonParts = [];
    if (!Value(clientEnv.OPENAI_CHAT_BASE_URL)) {
        missing.push("OPENAI_CHAT_BASE_URL");
    }
    if (!Value(clientEnv.OPENAI_CHAT_API_KEY)) {
        missing.push("OPENAI_CHAT_API_KEY");
    }
    if (!parsed.ok) {
        reasonParts.push("invalid OPENAI_CHAT_HEADERS_JSON");
    }
    else {
        if (!HeaderValue(parsed.headers, CHAT_REQUEST_FROM_HEADER)) {
            missing.push(CHAT_REQUEST_FROM_HEADER);
        }
        if (!HeaderValue(parsed.headers, CHAT_ACCEPT_HEADER)) {
            missing.push(CHAT_ACCEPT_HEADER);
        }
    }
    if (missing.length > 0) {
        reasonParts.push(`missing ${missing.join(", ")}`);
    }
    if (reasonParts.length === 0) {
        return;
    }
    warnings.push(InvalidChatSandboxWarning(clientEnv, chat, reasonParts.join("; "), HeaderKeysForLog(parsed.headers)));
    DisableChatEndpoint(clientEnv, chat);
}
function SelectEndpoint(candidates) {
    const selected = candidates.find(IsReady);
    if (!selected) {
        return {
            source: "none",
            baseUrl: "",
            apiKey: "",
            model: "",
            headers: {},
            uid: "",
            uidSource: "none",
            auth: "bearer",
        };
    }
    const uid = Value(selected.uid);
    return {
        source: selected.source,
        baseUrl: Value(selected.baseUrl),
        apiKey: Value(selected.apiKey),
        model: Value(selected.model),
        headers: selected.headers ?? {},
        uid,
        uidSource: selected.uidSource ?? "none",
        auth: uid ? "sandbox" : "bearer",
    };
}
function BuildEnvChatCandidate(env) {
    const serviceBaseUrl = env.SERVICE_URL
        ? `${env.SERVICE_URL}${CELIA_CHAT_PATH}` : "";
    const baseUrl = Value(env.OPENAI_CHAT_BASE_URL) || serviceBaseUrl;
    const apiKey = Value(env.OPENAI_CHAT_API_KEY)
        || Value(env.PERSONAL_API_KEY);
    const model = Value(env.OPENAI_CHAT_MODEL);
    const isCelia = Boolean(serviceBaseUrl && !env.OPENAI_CHAT_BASE_URL)
        || IsSandboxChatUrl(baseUrl);
    const uid = isCelia
        ? Value(env.CELIA_CHAT_UID) || Value(env.PERSONAL_UID) : "";
    const uidSource = uid
        ? (env.CELIA_CHAT_UID ? "CELIA_CHAT_UID" : "PERSONAL_UID") : "none";
    return {
        source: "env",
        baseUrl,
        apiKey,
        model,
        headers: {},
        uid,
        uidSource,
    };
}
function BuildChatCandidates(cfg, borrowed, env) {
    const cfgHeaders = cfg.chat.headers !== undefined ? cfg.chat.headers : {};
    return [
        {
            source: "config",
            baseUrl: cfg.chat.baseUrl,
            apiKey: cfg.chat.apiKey,
            model: cfg.chat.model,
            headers: cfgHeaders,
        },
        {
            source: "borrowed",
            baseUrl: borrowed.chat.baseUrl,
            apiKey: borrowed.chat.apiKey,
            model: borrowed.chat.model,
            headers: borrowed.chat.headers ?? {},
            uid: borrowed.chat.uid,
            uidSource: borrowed.chat.uid ? "provider.headers" : "none",
        },
        BuildEnvChatCandidate(env),
    ];
}
function BuildEmbedCandidates(cfg, borrowed) {
    const cfgHeaders = cfg.embed.headers !== undefined ? cfg.embed.headers : {};
    return [
        {
            source: "config",
            baseUrl: cfg.embed.baseUrl,
            apiKey: cfg.embed.apiKey,
            model: cfg.embed.model,
            headers: cfgHeaders,
        },
        {
            source: "borrowed",
            baseUrl: borrowed.embed.baseUrl,
            apiKey: borrowed.embed.apiKey,
            model: borrowed.embed.model,
            headers: borrowed.embed.headers ?? {},
            uid: borrowed.embed.uid,
            uidSource: borrowed.embed.uid ? "memorySearch.headers" : "none",
        },
    ];
}
function EndpointSummary(name, ep) {
    return `${name}{source=${ep.source} host=${UrlHost(ep.baseUrl)} `
        + `model=${ep.model || "(unset)"} apiKeyLen=${ep.apiKey.length} `
        + `auth=${ep.auth} uidLen=${ep.uid.length} uidSource=${ep.uidSource}}`;
}
function AppendIncompleteWarnings(name, selected, candidates, warnings) {
    for (const c of candidates) {
        if (c.source === selected.source) {
            break;
        }
        if (HasAnyEndpointField(c) && !IsReady(c)) {
            warnings.push(`celia: ${name}.${c.source} incomplete `
                + `(baseUrl=${c.baseUrl ? "set" : "unset"} `
                + `apiKey=${c.apiKey ? "set" : "unset"} `
                + `model=${c.model ? c.model : "(unset)"}), ignored`);
        }
    }
}
function AppendMissingModelWarning(name, selected, candidates, warnings) {
    if (selected.source === "none" || selected.model) {
        return;
    }
    const selectedIndex = candidates.findIndex((c) => c.source === selected.source);
    const hasLaterModel = candidates
        .slice(selectedIndex + 1)
        .some((c) => Boolean(c.model));
    if (!hasLaterModel) {
        return;
    }
    warnings.push(`celia: ${name}.${selected.source} selected without model; `
        + "model remains source-local and is not borrowed from lower-priority "
        + "candidates");
}
function AppendChatWarnings(chat, candidates, warnings) {
    AppendIncompleteWarnings("chat", chat, candidates, warnings);
    const borrowed = candidates.find((c) => c.source === "borrowed");
    if (chat.source === "env" && borrowed?.model && !IsReady(borrowed)) {
        warnings.push("celia: chat borrowed model exists but borrowed endpoint is incomplete; "
            + "selected env endpoint without cross-source model merge");
    }
    AppendMissingModelWarning("chat", chat, candidates, warnings);
    if (chat.uid && !IsSandboxChatUrl(chat.baseUrl)) {
        warnings.push("celia: chat sandbox uid is set for a non-celia endpoint; "
            + "request will use x-api-key/x-uid auth");
    }
}
function AppendEmbedInfo(embed, chat, info) {
    if (embed.uid && chat.auth === "bearer") {
        info.push("celia: embed sandbox uid kept separate from chat bearer auth");
    }
}
/**
 * @brief 生成 C 子进程 env，并返回脱敏诊断信息。
 */
export function buildMemoryClientEnv(cfg, borrowed, env = process.env) {
    const chatCandidates = BuildChatCandidates(cfg, borrowed, env);
    const embedCandidates = BuildEmbedCandidates(cfg, borrowed);
    const chat = SelectEndpoint(chatCandidates);
    const embed = SelectEndpoint(embedCandidates);
    if (cfg.chat.model) {
        chat.model = cfg.chat.model;
    }
    const effChatHeaders = withCeliaSandboxHeaders(chat.headers);
    const effEmbedHeaders = withCeliaSandboxHeaders(embed.headers);
    const effVectorDim = cfg.vectorDim || borrowed.embed.vectorDim;
    const clientEnv = {
        OPENAI_EMBED_BASE_URL: embed.baseUrl,
        OPENAI_EMBED_API_KEY: embed.apiKey,
        OPENAI_EMBED_MODEL: embed.model,
        OPENAI_CHAT_BASE_URL: chat.baseUrl,
        OPENAI_CHAT_API_KEY: chat.apiKey,
        OPENAI_CHAT_MODEL: chat.model,
        CELIA_CHAT_UID: chat.uid,
        CELIA_TENANT_ID: cfg.tenantId ?? "default",
    };
    if (embed.uid) {
        clientEnv.CELIA_EMBED_UID = embed.uid;
    }
    if (Object.keys(effEmbedHeaders).length > 0) {
        clientEnv.OPENAI_EMBED_HEADERS_JSON = JSON.stringify(effEmbedHeaders);
    }
    if (Object.keys(effChatHeaders).length > 0) {
        clientEnv.OPENAI_CHAT_HEADERS_JSON = JSON.stringify(effChatHeaders);
    }
    if (cfg.rerank.baseUrl) {
        clientEnv.OPENAI_RERANK_BASE_URL = cfg.rerank.baseUrl;
    }
    if (cfg.rerank.apiKey) {
        clientEnv.OPENAI_RERANK_API_KEY = cfg.rerank.apiKey;
    }
    if (cfg.rerank.model) {
        clientEnv.OPENAI_RERANK_MODEL = cfg.rerank.model;
    }
    if (cfg.proceduralDir) {
        clientEnv.CELIA_PROCEDURAL_DIR = cfg.proceduralDir;
    }
    if (cfg.proceduralLearnDebug) {
        clientEnv.CELIA_PROCEDURAL_LEARN_DEBUG = "1";
    }
    if (effVectorDim) {
        clientEnv.CELIA_VECTOR_DIM = String(effVectorDim);
    }
    if (env.CELIA_VECTOR_DIM) {
        clientEnv.CELIA_VECTOR_DIM = env.CELIA_VECTOR_DIM;
    }
    else if (env.EMBED_DIM) {
        clientEnv.CELIA_VECTOR_DIM = env.EMBED_DIM;
    }
    const warnings = [];
    AppendIncompleteWarnings("embed", embed, embedCandidates, warnings);
    AppendMissingModelWarning("embed", embed, embedCandidates, warnings);
    AppendChatWarnings(chat, chatCandidates, warnings);
    ValidateFinalChatSandboxEnv(clientEnv, chat, warnings);
    const info = [
        `celia: effective LLM config — ${EndpointSummary("embed", embed)} `
            + `${EndpointSummary("chat", chat)}`,
    ];
    AppendEmbedInfo(embed, chat, info);
    return { clientEnv, chat, embed, info, warnings };
}
