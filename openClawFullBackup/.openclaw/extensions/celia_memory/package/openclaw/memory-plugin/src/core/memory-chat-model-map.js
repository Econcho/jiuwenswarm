/**
 * @file memory-chat-model-map.ts
 * @brief 读取 memory-celia 结构化抽取专用 chat 模型映射表。
 *
 * OpenClaw 主模型可以继续使用 thinking/reasoning 变体；memory-celia
 * 借用 chat 配置时只按显式字典切到更适合 JSON 抽取的模型。
 * 新模型不要依赖名称后缀推导，直接编辑同目录 JSON：
 *
 *   memory-chat-model-map.json
 */
import * as fs from "node:fs";
import { fileURLToPath } from "node:url";
const MAP_PATH = fileURLToPath(new URL("./memory-chat-model-map.json", import.meta.url));
function LoadMemoryChatModelMap() {
    const raw = fs.readFileSync(MAP_PATH, "utf8");
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("memory-chat-model-map.json must be a JSON object");
    }
    const out = {};
    for (const [fromModel, toModel] of Object.entries(parsed)) {
        if (typeof fromModel === "string"
            && fromModel.length > 0
            && typeof toModel === "string"
            && toModel.length > 0) {
            out[fromModel] = toModel;
        }
    }
    return out;
}
export const MEMORY_CHAT_MODEL_MAP = LoadMemoryChatModelMap();
