/**
 * xiaoyiruntime 状态读取工具。
 *
 * `.xiaoyiruntime` 是 shell 风格的 KEY=VALUE 文件；当前只读取
 * MEMORYSTATE。协议层使用 true/false，插件内部转换为 1/0。
 * 文件缺失、格式异常或值非法时默认关闭，避免未感知运行时状态的
 * 数据触发 LLM 抽取。
 */

import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

export type MemoryState = 0 | 1;

const MEMORY_STATE_KEY = "MEMORYSTATE";
const DEFAULT_MEMORY_STATE: MemoryState = 0;

function stripExportPrefix(line: string): string {
  return line.replace(/^export\s+/, "");
}

function unquoteValue(value: string): string {
  const trimmed = value.trim();
  if (trimmed.length >= 2) {
    const first = trimmed[0];
    const last = trimmed[trimmed.length - 1];
    if ((first === "\"" && last === "\"")
        || (first === "'" && last === "'")) {
      return trimmed.slice(1, -1);
    }
  }
  return trimmed;
}

function parseMemoryStateValue(value: string): MemoryState | null {
  const normalized = unquoteValue(value).toLowerCase();
  if (normalized === "true" || normalized === "1") {
    return 1;
  }
  if (normalized === "false" || normalized === "0") {
    return 0;
  }
  return null;
}

export function resolveXiaoyiRuntimePath(
  env: NodeJS.ProcessEnv = process.env,
): string {
  const explicitPath = env.CELIA_XIAOYI_RUNTIME_PATH;
  if (explicitPath && explicitPath.trim()) {
    return explicitPath;
  }
  const configDir = env.CELIA_CONFIG_DIR && env.CELIA_CONFIG_DIR.trim()
    ? env.CELIA_CONFIG_DIR
    : join(homedir(), ".openclaw");
  return join(configDir, ".xiaoyiruntime");
}

export function parseXiaoyiMemoryState(text: string): MemoryState {
  let state = DEFAULT_MEMORY_STATE;
  for (const rawLine of text.split(/\r?\n/)) {
    const line = stripExportPrefix(rawLine.trim());
    if (!line || line.startsWith("#") || !line.includes("=")) {
      continue;
    }
    const eqIdx = line.indexOf("=");
    const key = line.slice(0, eqIdx).trim();
    if (key !== MEMORY_STATE_KEY) {
      continue;
    }
    const parsed = parseMemoryStateValue(line.slice(eqIdx + 1));
    if (parsed !== null) {
      state = parsed;
    }
  }
  return state;
}

export async function readXiaoyiMemoryState(
  env: NodeJS.ProcessEnv = process.env,
): Promise<MemoryState> {
  try {
    const runtimePath = resolveXiaoyiRuntimePath(env);
    const text = await readFile(runtimePath, "utf8");
    return parseXiaoyiMemoryState(text);
  } catch {
    return DEFAULT_MEMORY_STATE;
  }
}
