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

/**
 * 借用结果。embed/chat 各自独立；headers 排除 x-api-key/x-uid（已通过
 * 专用 apiKey/uid 字段透传，C 端会重复写）。
 */
export type BorrowedConfig = {
  embed: {
    baseUrl?: string;
    apiKey?: string;
    model?: string;
    uid?: string;
    vectorDim?: number;
    headers?: Record<string, string>;
  };
  chat: {
    baseUrl?: string;
    apiKey?: string;
    model?: string;
    uid?: string;
    headers?: Record<string, string>;
  };
};

export type BorrowedConfigOptions = {
  chatProvider?: string;
};

type EnvLike = Record<string, string | undefined>;

const EMPTY_BORROW: BorrowedConfig = { embed: {}, chat: {} };
const CELIA_SANDBOX_CHAT_PROVIDER = "xiaoyiprovider";

export function resolveBorrowedChatProvider(
  env: EnvLike = process.env,
): string | undefined {
  return env.SERVICE_URL ? CELIA_SANDBOX_CHAT_PROVIDER : undefined;
}

/* ---------------- 路径解析 ---------------- */

/**
 * 找到 openclaw.json 实际路径，按 CELIA_CONFIG_DIR / OPENCLAW_CONFIG_DIR /
 * $HOME/.openclaw / celiaclaw 沙盒默认路径依次尝试。
 */
export function findOpenclawJsonPath(): string | null {
  const candidates: string[] = [];
  for (const v of ["CELIA_CONFIG_DIR", "OPENCLAW_CONFIG_DIR"]) {
    const dir = process.env[v];
    if (dir) candidates.push(path.join(dir, "openclaw.json"));
  }
  const home = os.homedir();
  if (home) candidates.push(path.join(home, ".openclaw", "openclaw.json"));
  candidates.push("/home/sandbox/.openclaw/openclaw.json"); /* celiaclaw */
  for (const p of candidates) {
    try {
      if (fs.statSync(p).isFile()) return p;
    } catch {
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
function pickRealApiKey(
  headers: Record<string, unknown> | undefined,
  topLevel: unknown,
): string | undefined {
  if (
    headers
    && typeof headers["x-api-key"] === "string"
    && (headers["x-api-key"] as string).length > 0
  ) {
    return headers["x-api-key"] as string;
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
export function pickExtraHeaders(
  headers: Record<string, unknown> | undefined,
): Record<string, string> {
  const out: Record<string, string> = {};
  if (!headers) return out;
  for (const [k, v] of Object.entries(headers)) {
    const kl = k.toLowerCase();
    if (kl === "x-api-key" || kl === "x-uid") continue;
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
function isThinkingModelName(modelName: string): boolean {
  const lower = modelName.toLowerCase();
  return lower.includes("thinking") || lower.includes("reasoning");
}

/**
 * provider.models 里的单个模型 id。id 为空或非字符串时返回 undefined。
 */
function getProviderModelId(item: unknown): string | undefined {
  if (!item || typeof item !== "object") return undefined;
  const id = (item as Record<string, unknown>)["id"];
  return typeof id === "string" && id.length > 0 ? id : undefined;
}

/**
 * 从同 provider 中给记忆结构化抽取选择 chat model。
 *
 * Agent 主模型可以是 thinking 变体，但记忆抽取需要稳定输出 JSON 数组。
 * 因此先查 MEMORY_CHAT_MODEL_MAP 显式映射；没有映射时，如果 provider
 * 显式声明了 reasoning=false 的模型，再选该模型；否则保留 primary。
 */
function pickMemoryChatModel(
  provider: Record<string, unknown>,
  primaryModelName: string,
): string | undefined {
  if (!primaryModelName) return undefined;
  const mapped = MEMORY_CHAT_MODEL_MAP[primaryModelName];
  if (mapped) return mapped;

  const modelsRaw = provider["models"];
  if (!Array.isArray(modelsRaw)) {
    return primaryModelName;
  }

  const primary = modelsRaw.find(
    (item) => getProviderModelId(item) === primaryModelName,
  ) as Record<string, unknown> | undefined;
  if (primary?.["reasoning"] === false) return primaryModelName;

  const needsSafeModel =
    primary?.["reasoning"] === true || isThinkingModelName(primaryModelName);
  if (!needsSafeModel) return primaryModelName;

  for (const item of modelsRaw) {
    const rec = item as Record<string, unknown>;
    const id = getProviderModelId(item);
    if (id && rec["reasoning"] === false) return id;
  }
  return primaryModelName;
}

function pickProviderDefaultChatModel(
  provider: Record<string, unknown>,
): string | undefined {
  const modelsRaw = provider["models"];
  if (!Array.isArray(modelsRaw)) return undefined;

  let firstId: string | undefined;
  for (const item of modelsRaw) {
    const id = getProviderModelId(item);
    if (!firstId) firstId = id;
    if (id && (item as Record<string, unknown>)["reasoning"] === false) {
      return id;
    }
  }
  return firstId;
}

function isBorrowableChatProvider(
  provider: Record<string, unknown> | undefined,
): provider is Record<string, unknown> {
  if (!provider) return false;
  const headers =
    provider["headers"] as Record<string, unknown> | undefined;
  return typeof provider["baseUrl"] === "string"
    && (provider["baseUrl"] as string).length > 0
    && Boolean(pickRealApiKey(headers, provider["apiKey"]));
}

function buildChatBorrow(
  provider: Record<string, unknown>,
  modelName: string | undefined,
): BorrowedConfig["chat"] {
  const headers =
    provider["headers"] as Record<string, unknown> | undefined;
  return {
    baseUrl: typeof provider["baseUrl"] === "string"
      ? (provider["baseUrl"] as string) : undefined,
    apiKey:  pickRealApiKey(headers, provider["apiKey"]),
    model:   modelName ? pickMemoryChatModel(provider, modelName) : undefined,
    uid: headers && typeof headers["x-uid"] === "string"
      ? (headers["x-uid"] as string) : undefined,
    headers: pickExtraHeaders(headers),
  };
}

/* ---------------- 主入口（纯函数 + 文件入口两个变体） ---------------- */

/**
 * 从已解析的 openclaw.json 顶层对象中借用 embed + chat 配置。
 *
 * 纯函数入口，不读磁盘 / 不读 env，便于 UT 测试。
 */
export function parseBorrowedConfig(
  data: Record<string, unknown> | null | undefined,
  options: BorrowedConfigOptions = {},
): BorrowedConfig {
  if (!data || typeof data !== "object") return EMPTY_BORROW;
  const result: BorrowedConfig = { embed: {}, chat: {} };

  const agents   = data["agents"] as Record<string, unknown> | undefined;
  const defaults = agents?.["defaults"] as Record<string, unknown> | undefined;

  /* ---- embed: agents.defaults.memorySearch.remote ---- */
  const memSearch =
    defaults?.["memorySearch"] as Record<string, unknown> | undefined;
  if (memSearch && memSearch["enabled"] !== false) {
    const remote =
      memSearch["remote"] as Record<string, unknown> | undefined;
    if (remote) {
      const headers =
        remote["headers"] as Record<string, unknown> | undefined;
      result.embed = {
        baseUrl: typeof remote["baseUrl"] === "string"
          ? (remote["baseUrl"] as string) : undefined,
        apiKey:  pickRealApiKey(headers, remote["apiKey"]),
        model:   typeof memSearch["model"] === "string"
          ? (memSearch["model"] as string) : undefined,
        uid: headers && typeof headers["x-uid"] === "string"
          ? (headers["x-uid"] as string) : undefined,
        vectorDim: typeof memSearch["outputDimensionality"] === "number"
          && Number.isFinite(memSearch["outputDimensionality"])
          && memSearch["outputDimensionality"] > 0
          ? Math.trunc(memSearch["outputDimensionality"] as number)
          : undefined,
        headers: pickExtraHeaders(headers),
      };
    }
  }

  /* ---- chat: 沙箱默认 provider 优先，否则 models.providers.<primary> ---- */
  const primary = (defaults?.["model"] as Record<string, unknown> | undefined)
    ?.["primary"];
  const models =
    data["models"] as Record<string, unknown> | undefined;
  const providers =
    models?.["providers"] as Record<string, unknown> | undefined;
  let primaryProviderName: string | undefined;
  let primaryModelName: string | undefined;
  if (typeof primary === "string" && primary.includes("/")) {
    const slash        = primary.indexOf("/");
    primaryProviderName = primary.slice(0, slash);
    primaryModelName    = primary.slice(slash + 1);
  }

  const preferredProvider = options.chatProvider
    ? providers?.[options.chatProvider] as Record<string, unknown> | undefined
    : undefined;
  if (isBorrowableChatProvider(preferredProvider)) {
    result.chat = buildChatBorrow(
      preferredProvider,
      pickProviderDefaultChatModel(preferredProvider),
    );
  } else if (primaryProviderName) {
    const provider =
      providers?.[primaryProviderName] as Record<string, unknown> | undefined;
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
function resolveEnvPlaceholders(value: string | undefined): string | undefined {
  if (!value || !value.includes("${")) return value;
  return value.replace(/\$\{([^}]+)\}/g, (_m, name: string) => {
    return process.env[name] ?? "";
  });
}

/** @brief 解析 headers 中的 `${VAR}`，空值丢弃。 */
function resolveHeaderPlaceholders(
  headers: Record<string, string> | undefined,
): Record<string, string> | undefined {
  if (!headers) return headers;
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(headers)) {
    const resolved = resolveEnvPlaceholders(value);
    if (resolved) out[key] = resolved;
  }
  return out;
}

/** @brief 解析 BorrowedConfig 中所有可含 `${VAR}` 的字符串字段。 */
function resolveBorrowedConfigPlaceholders(
  config: BorrowedConfig,
): BorrowedConfig {
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
export function readBorrowedConfig(
  logger?: { info?: (m: string) => void; warn?: (m: string) => void },
  options: BorrowedConfigOptions = {},
): BorrowedConfig {
  const p = findOpenclawJsonPath();
  if (!p) return EMPTY_BORROW;
  let raw: string;
  try {
    raw = fs.readFileSync(p, "utf8");
  } catch (e) {
    logger?.warn?.(
      `celia: cannot read ${p} for openclaw.json borrow: ${String(e)}`,
    );
    return EMPTY_BORROW;
  }
  let data: Record<string, unknown>;
  try {
    data = JSON.parse(raw) as Record<string, unknown>;
  } catch (e) {
    logger?.warn?.(`celia: ${p} is not valid JSON: ${String(e)}`);
    return EMPTY_BORROW;
  }
  const result =
    resolveBorrowedConfigPlaceholders(parseBorrowedConfig(data, options));

  if (result.embed.apiKey || result.chat.apiKey) {
    const embedHeaderKeys = Object.keys(result.embed.headers ?? {});
    const chatHeaderKeys  = Object.keys(result.chat.headers ?? {});
    logger?.info?.(
      `celia: borrowed config from openclaw.json — `
        + `embed{model=${result.embed.model ?? "(unset)"} `
        + `baseUrl=${result.embed.baseUrl ? "(set)" : "(unset)"} `
        + `apiKey=${
          result.embed.apiKey ? `len=${result.embed.apiKey.length}` : "(unset)"
        } `
        + `uid=${
          result.embed.uid ? `len=${result.embed.uid.length}` : "(unset)"
        } `
        + `extraHeaders=[${embedHeaderKeys.join(",")}]} `
        + `chat{model=${result.chat.model ?? "(unset)"} `
        + `baseUrl=${result.chat.baseUrl ? "(set)" : "(unset)"} `
        + `apiKey=${
          result.chat.apiKey ? `len=${result.chat.apiKey.length}` : "(unset)"
        } `
        + `extraHeaders=[${chatHeaderKeys.join(",")}]}`,
    );
  }
  return result;
}
