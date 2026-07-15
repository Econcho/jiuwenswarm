/**
 * CELIA_GUIDE 模板（four semantic memory layers guidance）。
 *
 * 静态模板，不依赖 MCP 调用。环境 AGENTS.md 由 Docker/celiaclaw
 * 预置，install skill 和插件运行时不再写入该文件。
 *
 * 设计参考：spec §6.4.6（GUIDE 模板正文）。
 *
 * 重要约束：模板字节数 ≤ MARKER_BUDGETS.CELIA_GUIDE (1.5 KB)，
 * 超限时 safeWriteMarker → trimByPolicy("CELIA_GUIDE") 直接抛 Error，
 * 由 CI 单测拦截，不允许上线。
 */
import type {
    MarkerWriteResult,
    SafeWriteCtx,
} from "./safeWriteMarker.js";

/**
 * 自研记忆系统在 before_prompt_build guidance system context 中
 * 使用的静态指南模板。
 *
 * 实测字节数 ≈ 1.5 KB，预算 1.5 KB。
 *
 * 设计原则：
 *   - 只写规则与索引，不放任何动态摘要内容（动态内容全部下沉到 MEMORY.md
 *     或走渐进加载工具，避免主 agent 与 subagent/cron 看到的记忆不一致）。
 *   - 引导从"细节必查"改为"已加载上下文足够则不查"，避免过度触发事实检索。
 *   - 与当前工具实际行为对齐：默认 top_k=5、`_trim` 截断报告、
 *     `memory_scene_load` 支持 paths[] 批量、`memory_record_search` 支持
 *     is_procedural 专用召回、subagent 用 `memory_get_global_summary`
 *     按需取全局概览。
 *   - 对 Agent 只呈现"全局概览 / 场景记忆 / 原子事实 / 原始会话"
 *     四层语义化记忆，不暴露内部 L0/L1/L2/L3 层号。
 *
 * Phase 1 防 NOT_FOUND 加固（2026-04-29）：明确 summary path
 * 必须来自 `memory_scene_list_load`；不要凭名字拼 path 调 load。
 */
export const STATIC_GUIDE_TEMPLATE: string =
    "## 自研记忆系统 GUIDE\n\n" +
    "四层语义化记忆：全局概览、场景记忆、原子事实、原始会话；" +
    "已加载内容可答则不调工具。\n" +
    "不足=缺关键字段/冲突/只见概览但要细节/完整列表/原话来源。\n" +
    "按需求选工具：场景记忆→scene tools，原子事实→record search，" +
    "原始会话→chat history；不因含数字默认查原子事实。\n\n" +
    "### 工具选择\n" +
    "- 全局概览：主会话通常已固定加载；subagent 用 " +
    "`memory_get_global_summary(tier=\"edge\")`。\n" +
    "- 场景记忆：部分场景已固定加载；需更多场景时，path 必须来自 " +
    "`memory_scene_list_load`，再用 `memory_scene_load(paths[])`。\n" +
    "- 原子事实：`memory_record_search(query, top_k=5, " +
    "is_procedural?, time_hint?)`。\n" +
    "- 原始会话：`memory_chat_history_search(query, top_k=5)`。\n\n" +
    "### 决策\n" +
    "1. 固定加载的概览/场景记忆可答则不调工具；不足才继续检索。\n" +
    "2. 画像/偏好/待办先读全局概览；字段缺失查原子事实。\n" +
    "3. 主题/场景/经历/偏好模式先读场景记忆；不足再 scene load。\n" +
    "4. 原子事实用于缺失精确事实、overflow 完整列表、冲突校验。\n" +
    "5. 执行/修复/调试/构建/发布前用原子事实 `is_procedural=true` " +
    "查流程/偏好/坑。\n" +
    "6. 时间相关的原子事实查询可传 `time_hint=true`；\n" +
    "7. 原始会话用于原子事实不足或需原话/上下文/来源。\n\n" +
    "### 预算\n" +
    "`memory_scene_load`/`memory_record_search`/`memory_chat_history_search` 累计 ≤ 3 次；" +
    "返回 `_trim` 时缩 query。\n";

/**
 * AGENTS.md 文件同步入口（已禁用）。
 *
 * GUIDE 仍通过 before_prompt_build 的 guidance system context 注入
 * 本轮 prompt；
 * 此函数保留兼容旧调用方，但不再创建、替换或更新环境 AGENTS.md。
 */
export async function syncGuideToAgentsMd(
    _workspaceDir: string,
    _ctx: SafeWriteCtx,
): Promise<MarkerWriteResult | null> {
    return null;
}
