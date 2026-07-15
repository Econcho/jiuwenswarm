/**
 * celia 沙盒专属的 .xiaoyienv 环境变量加载与 alias 映射。
 *
 * 默认从 /home/sandbox/.openclaw/.xiaoyienv 读取 KEY=VALUE 对，
 * 注入到 process.env（不覆盖已有值）。
 * 路径可通过 CELIA_XIAOYI_ENV_PATH 环境变量覆盖（测试注入用）。
 * 两个插件共享一个进程，通过 loaded 标记确保只加载一次。
 *
 * 命名约定：TS 符号（函数/常量）已全部改为 Celia*；文件名、env 变量
 * 名、以及 URL 路径片段仍保留 xiaoyi—— 这些是与沙盒运行时之间的
 * **外部契约**，改名会破坏集成。
 *
 * 通用工具函数（resolveEnvVars / resolveWithEnvFallback / assertAllowedKeys）
 * 参见 integration/openclaw/shared/env-utils.ts。
 */
import { readFileSync } from "node:fs";
const DEFAULT_CELIA_ENV_PATH = "/home/sandbox/.openclaw/.xiaoyienv";
let loaded = false;
/**
 * 解析 .xiaoyienv 路径。优先级：
 *   CELIA_XIAOYI_ENV_PATH 环境变量（测试注入用）
 *   > /home/sandbox/.openclaw/.xiaoyienv（celia 沙盒默认）
 *
 * 不使用 os.homedir() — 硬编码沙盒路径同时起到"环境识别"作用：
 * 开发者本地 /home/sandbox/ 不存在，插件不会意外激活 celia alias 路径。
 */
function getCeliaEnvPath() {
    return process.env.CELIA_XIAOYI_ENV_PATH || DEFAULT_CELIA_ENV_PATH;
}
/**
 * 从 .xiaoyienv 加载环境变量到 process.env。
 *
 * - KEY=VALUE 格式，支持引号包裹
 * - 跳过空行和 # 注释
 * - KEY 中的 `-` 转为 `_`（shell 变量不支持连字符）
 * - 不覆盖已有 process.env 值
 * - 文件不存在 / 不可读时打印警告，不阻塞启动
 */
export function loadCeliaEnv() {
    if (!loaded) {
        loaded = true;
        const envPath = getCeliaEnvPath();
        let content = null;
        try {
            content = readFileSync(envPath, "utf-8");
        }
        catch (err) {
            const code = err.code;
            if (code === "ENOENT") {
                console.warn(`[celia] .xiaoyienv not found at ${envPath}, skipping`);
            }
            else {
                console.warn(`[celia] .xiaoyienv read failed at ${envPath} ` +
                    `(${code ?? "unknown"}): ${err.message}, skipping`);
            }
        }
        if (content !== null) {
            for (const line of content.split("\n")) {
                const trimmed = line.trim();
                if (!trimmed || trimmed.startsWith("#"))
                    continue;
                const eqIdx = trimmed.indexOf("=");
                if (eqIdx <= 0)
                    continue;
                const rawKey = trimmed.slice(0, eqIdx).trim();
                let value = trimmed.slice(eqIdx + 1).trim();
                /* 去掉引号包裹 */
                if ((value.startsWith('"') && value.endsWith('"')) ||
                    (value.startsWith("'") && value.endsWith("'"))) {
                    value = value.slice(1, -1);
                }
                /* KEY 中 `-` 转 `_` */
                const key = rawKey.replace(/-/g, "_");
                /* 不覆盖已有值 */
                if (process.env[key] === undefined) {
                    process.env[key] = value;
                }
            }
        }
    }
    /* 别名桥接：幂等，每次调用都跑。
     * 文件一次性加载不影响 alias 的触发——用户本地直接 export SERVICE_URL
     * 也能走同一条路径（不依赖 .xiaoyienv 文件存在）。 */
    aliasCeliaChatEnv();
}
/**
 * @internal 测试专用：重置模块级 loaded 状态。
 *
 * 生产代码禁止调用。仅 env-loader.test.ts 在多个文件加载场景间
 * reset 状态使用（配合 CELIA_XIAOYI_ENV_PATH 指向临时 fixture 文件）。
 */
export function _resetCeliaEnvLoadedForTest() {
    loaded = false;
}
/**
 * 把 .xiaoyienv 里 celia 沙盒用的 chat 认证 key 映射到代码统一使用的
 * OPENAI_CHAT_* 命名上。
 *
 * 触发条件：process.env.SERVICE_URL 非空（"是否在用 celia 沙盒"的信号）。
 * 映射规则（仅在目标 key 尚未显式设置时注入）：
 *   SERVICE_URL         → OPENAI_CHAT_BASE_URL
 *   PERSONAL_API_KEY    → OPENAI_CHAT_API_KEY
 *
 * 注意：chat.model 不再由本函数填充。模型来自 openclaw.json 的
 * memory-celia.config.chat.model 字面量，或由 memory-plugin/index.ts 从
 * 全局模型配置借用；env-loader 不设置 OPENAI_CHAT_MODEL。
 *
 * baseUrl/apiKey 的 alias 保留作为运行时兜底：当 openclaw.json 的
 * chat 字面量缺失时，让 memory-plugin/config.ts 的 env fallback 路径
 * 仍能拿到值（不至于直接启动失败）。
 *
 * embed 不参与映射（celia 场景 embed 另走 openclaw.json 字面量）。
 * PERSONAL_UID → CELIA_CHAT_UID 的透传在 memory-plugin/index.ts 已处理，
 * 不在此重复。
 */
export function aliasCeliaChatEnv() {
    if (!process.env.SERVICE_URL)
        return;
    const serviceUrl = process.env.SERVICE_URL + '/celia-claw/v1/sse-api';
    if (process.env.OPENAI_CHAT_BASE_URL === undefined) {
        process.env.OPENAI_CHAT_BASE_URL = serviceUrl;
    }
    const apiKey = process.env.PERSONAL_API_KEY;
    if (apiKey && process.env.OPENAI_CHAT_API_KEY === undefined) {
        process.env.OPENAI_CHAT_API_KEY = apiKey;
    }
    /* 日志：一次性记录 alias 触发及来源；不打印 apiKey/uid 原值。
     * 出现"x-uid/Authorization 没带上"问题时，先确认这条日志是否出现。 */
    console.warn(`[celia] celia env alias applied: ` +
        `chat_base=${serviceUrl} ` +
        `api_key_len=${apiKey ? apiKey.length : 0}`);
}
