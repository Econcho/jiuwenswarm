/**
 * AGENTS.md 形状强制入口（运行时写入已禁用）。
 *
 * AGENTS.md 由 Docker/celiaclaw 环境预置，install skill 和插件运行时
 * 不再创建、迁移、重写或备份该文件。保留 no-op 导出是为了兼容旧
 * 调用方。
 */

import { emitDFX } from "../core/dfx.js";

/** logger 简化接口（与 OpenClaw api.logger 兼容）。 */
export interface EnforceLogger {
    info?: (msg: string) => void;
    warn?: (msg: string) => void;
}

/** enforceAgentsMdShape 调用上下文。 */
export interface EnforceCtx {
    traceId: string;
    logger?: EnforceLogger;
}

/** enforceAgentsMdShape 返回结果（便于单测断言 + DFX 上报）。 */
export type EnforceAction =
    | "noop"            /* AGENTS.md 由镜像/环境预置，未写盘 */
    | "create"          /* 旧兼容枚举，当前不会返回 */
    | "relocate"        /* 旧兼容枚举，当前不会返回 */
    | "rewrite"         /* 旧兼容枚举，当前不会返回 */
    | "io_error";       /* 旧兼容枚举，当前不会返回 */

export interface EnforceResult {
    action: EnforceAction;
    /** 触发原因短码，用于 DFX。 */
    reason: string;
    /** 旧兼容字段；当前不会产生备份。 */
    rescuePath?: string;
}

/**
 * AGENTS.md 形状强制入口（已禁用）。
 *
 * 副作用：无。保留导出是为了兼容旧调用方，避免重新引入文件写入。
 */
export async function enforceAgentsMdShape(
    _workspaceDir: string,
    ctx: EnforceCtx,
): Promise<EnforceResult> {
    ctx.logger?.info?.(
        "memory-celia: [enforceShape] skipped reason=agents_md_preplaced_by_image",
    );
    emitDFX(ctx, "enforceShape.noop", {
        reason: "agents_md_preplaced_by_image",
    });
    return {
        action: "noop",
        reason: "agents_md_preplaced_by_image",
    };
}
