/**
 * L1 场景摘要 → MEMORY.md 同步器（progressive-loading v1）。
 *
 * 在 `session_start` / `after_compaction` 钩子中调用：
 *   1. 通过 MCP `memory_get_l1_index` 拉取全部 L1 索引条目。
 *   2. 过滤 type=SCENE。
 *   3. 固定加载仅写 preset 全量 + 动态场景 top-K，剩余场景通过
 *      `memory_load_l1` 渐进下钻。
 *
 * 设计参考：spec §5.3 / §6.4.5；裁剪策略见 safeWriteMarker.trimByPolicy。
 */

import * as path from "node:path";
import {
    safeWriteMarker,
    type LtmSummary,
    type MarkerWriteResult,
    type SafeWriteCtx,
} from "./safeWriteMarker.js";
import {
    extractL1Entries,
    type McpClientLike,
    type L1IndexEntryDto,
} from "./fixedContext.js";

/** L1 index fetch deadline（毫秒）。 */
const L1_INDEX_FETCH_DEADLINE_MS = 3000;

/** 把 C 层 L1IndexEntryDto.type 映射到 LtmSummary.type。
 *  兼容两种格式：
 *   - 数字：0=MEMTYPE, 1=SCENE
 *   - 字符串（C 端实际写出）："memtype" → MEMTYPE，"scene" → SCENE
 *  C 端 BuildIndexRoot 实际把 "memtype"/"scene" 字符串写入 L1_index.json，
 *  本 fallback 同时接受字符串和数字，避免 type 字段不匹配导致 SCENE
 *  filter 永远 0 条的 bug。 */
function mapEntryType(t: unknown): "MEMTYPE" | "SCENE" {
    if (typeof t === "number") {
        return t === 1 ? "SCENE" : "MEMTYPE";
    }
    if (typeof t === "string") {
        return t === "scene" ? "SCENE" : "MEMTYPE";
    }
    return "MEMTYPE";
}

/**
 * 拉取 L1 全量 index（单次 MCP 调用），返回原始 entry 数组。
 *
 * 调用方可配合 `filterL1ByType` 按 type 过滤，避免多个消费方
 * 各自发起重复的 MCP 请求。
 */
export async function fetchL1Index(
    client: McpClientLike,
    tenantId: string,
    userId: string,
    ctx: SafeWriteCtx & { deadlineMs?: number },
): Promise<L1IndexEntryDto[]> {
    const ms = ctx.deadlineMs ?? L1_INDEX_FETCH_DEADLINE_MS;
    const traceArgs: Record<string, unknown> = ctx.traceId
        ? { _trace_id: ctx.traceId }
        : {};
    const promise = client.callTool("memory_get_l1_index", {
        tenant_id: tenantId,
        user_id: userId,
        ...traceArgs,
    });
    const raw = await Promise.race([
        promise,
        new Promise((_, reject) =>
            setTimeout(
                () => reject(new Error("L1 index fetch deadline exceeded")),
                ms,
            ),
        ),
    ]);
    return extractL1Entries(raw, ctx.logger);
}

/**
 * 按 type 过滤 + 映射为 LtmSummary（纯内存操作，无 MCP 调用）。
 */
export function filterL1ByType(
    entries: L1IndexEntryDto[],
    typeFilter: "MEMTYPE" | "SCENE",
): LtmSummary[] {
    return entries
        .filter((e) => mapEntryType(e.type) === typeFilter)
        .map<LtmSummary>((e) => ({
            id: e.id,
            path: e.path,
            type: typeFilter,
            summary: e.summary ?? "",
            factCount: e.factCount ?? 0,
            updatedAtMs: e.updatedAtMs ?? 0,
            isPreset: e.is_preset ?? false,
        }));
}

/**
 * 通过 MCP 拉取 L1 索引并过滤为指定 type 的条目数组。
 *
 * 便捷封装：内部调用 `fetchL1Index` + `filterL1ByType`。
 * 若调用方需同时获取 SCENE 和 MEMTYPE，应改用
 * `fetchL1Index` 一次拉取后分别 `filterL1ByType`。
 */
export async function fetchL1ByType(
    client: McpClientLike,
    tenantId: string,
    userId: string,
    typeFilter: "MEMTYPE" | "SCENE",
    ctx: SafeWriteCtx & { deadlineMs?: number },
): Promise<LtmSummary[]> {
    const entries = await fetchL1Index(client, tenantId, userId, ctx);
    return filterL1ByType(entries, typeFilter);
}

/**
 * 同步 L1 场景摘要到 `<workspaceDir>/MEMORY.md` 的 MEMORY_SCENES。
 *
 * @param prefetched  可选预取数据；提供时跳过 MCP 调用，直接写入。
 *                    由 `runAllSyncs` 预取后按 type 分发，避免重复拉取。
 */
/** 固定加载侧只展示的动态场景数上限。 */
const DYNAMIC_SCENE_TOP_K = 10;

/**
 * 固定加载容量治理：preset 场景全量保留，动态场景按 factCount 取 Top-K。
 *
 * 该 helper 供独立 sync 与 hooks.ts 的批量 sync 共用，避免一条路径全量
 * 写入动态 scenes，另一条路径做 Top-K，导致同轮固定加载 token 预算失控。
 */
export function selectFixedLoadScenes(scenes: LtmSummary[]): LtmSummary[] {
    const presets = scenes.filter((s) => s.isPreset);
    const dynamic = scenes
        .filter((s) => !s.isPreset)
        .sort((a, b) => b.factCount - a.factCount)
        .slice(0, DYNAMIC_SCENE_TOP_K);
    return [...presets, ...dynamic];
}

export async function syncL1ScenesToMemoryMd(
    client: McpClientLike,
    workspaceDir: string,
    tenantId: string,
    userId: string,
    ctx: SafeWriteCtx,
    prefetched?: LtmSummary[],
): Promise<MarkerWriteResult | null> {
    let scenes: LtmSummary[];
    if (prefetched) {
        scenes = prefetched;
    } else {
        try {
            scenes = await fetchL1ByType(
                client, tenantId, userId, "SCENE", ctx);
        } catch (err) {
            ctx.logger?.warn?.(
                `memory-celia: [l1ScenesSync] trace=${ctx.traceId} ` +
                    `evt=fetch_failed err=${String(err)}`,
            );
            return null;
        }
    }

    /* ---- 容量治理：preset 全量 + 动态 top-K ---- */
    scenes = selectFixedLoadScenes(scenes);

    const targetPath = path.join(workspaceDir, "MEMORY.md");
    return await safeWriteMarker(
        targetPath,
        "MEMORY_SCENES",
        scenes,
        ctx,
    );
}
