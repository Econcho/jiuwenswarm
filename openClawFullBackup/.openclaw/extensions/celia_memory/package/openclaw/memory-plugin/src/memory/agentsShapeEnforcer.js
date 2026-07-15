/**
 * AGENTS.md 形状强制入口（运行时写入已禁用）。
 *
 * AGENTS.md 由 Docker/celiaclaw 环境预置，install skill 和插件运行时
 * 不再创建、迁移、重写或备份该文件。保留 no-op 导出是为了兼容旧
 * 调用方。
 */
import { emitDFX } from "../core/dfx.js";
/**
 * AGENTS.md 形状强制入口（已禁用）。
 *
 * 副作用：无。保留导出是为了兼容旧调用方，避免重新引入文件写入。
 */
export async function enforceAgentsMdShape(_workspaceDir, ctx) {
    ctx.logger?.info?.("memory-celia: [enforceShape] skipped reason=agents_md_preplaced_by_image");
    emitDFX(ctx, "enforceShape.noop", {
        reason: "agents_md_preplaced_by_image",
    });
    return {
        action: "noop",
        reason: "agents_md_preplaced_by_image",
    };
}
