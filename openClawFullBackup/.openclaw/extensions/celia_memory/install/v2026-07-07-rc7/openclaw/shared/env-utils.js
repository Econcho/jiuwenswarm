/**
 * 通用环境变量与配置辅助函数。
 *
 * 从原 shared/env-loader.ts 抽出的通用部分：
 *   - resolveEnvVars            解析 ${ENV_VAR} 占位符
 *   - resolveWithEnvFallback    三层回退：占位符 → 字面量 → process.env
 *   - assertAllowedKeys         对象 key 白名单校验
 *
 * 不含任何集成方专属逻辑（xiaoyi/celia 的 .xiaoyienv 加载与 alias 映射
 * 移至 integration/celiaclaw/shared/env-loader.ts）。
 */
/**
 * 解析字符串中的 ${ENV_VAR} 引用，从 process.env 取值。
 */
export function resolveEnvVars(value) {
    return value.replace(/\$\{([^}]+)\}/g, (_, envVar) => {
        let envValue = process.env[envVar];
        /* Windows fallbacks for common Unix env vars */
        if (!envValue && process.platform === "win32") {
            if (envVar === "HOME")
                envValue = process.env.USERPROFILE;
            else if (envVar === "USER")
                envValue = process.env.USERNAME;
        }
        if (!envValue) {
            throw new Error(`Environment variable ${envVar} is not set`);
        }
        return envValue;
    });
}
/**
 * @brief 配置值三层回退解析：显式占位符 → 字面量 → process.env。
 *
 * - ${FOO} 占位符：调用 resolveEnvVars 严格解析，取不到抛错（显式声明必须兑现）。
 * - 非空字面量：原样返回。
 * - undefined / 空串：回退到 process.env[envKey]；仍无则返回空串
 *   （由调用方决定是否降级，而不是在此抛错）。
 */
export function resolveWithEnvFallback(value, envKey) {
    if (typeof value === "string" && value.includes("${")) {
        return resolveEnvVars(value);
    }
    if (typeof value === "string" && value.length > 0) {
        return value;
    }
    return process.env[envKey] ?? "";
}
/**
 * @brief 仅解析 JSON 来源（占位符 + 字面量），不做 process.env 回退。
 *
 * 用于"openclaw.json 是 single source of truth、env 仅作兜底"
 * 的场景——env fallback 留给上层调用方（index.ts）按 memorySearch 等
 * 中间来源的优先级自行编排。
 *
 * - ${FOO} 占位符：严格解析，取不到抛错。
 * - 非空字面量：原样返回。
 * - undefined / 空串：返回空串（不读 process.env）。
 */
export function resolveJsonOnly(value) {
    if (typeof value === "string" && value.includes("${")) {
        return resolveEnvVars(value);
    }
    if (typeof value === "string" && value.length > 0) {
        return value;
    }
    return "";
}
/**
 * 校验对象只含允许的 key，否则抛错。
 */
export function assertAllowedKeys(value, allowed, label) {
    const unknown = Object.keys(value).filter((key) => !allowed.includes(key));
    if (unknown.length > 0) {
        throw new Error(`${label} has unknown keys: ${unknown.join(", ")}`);
    }
}
