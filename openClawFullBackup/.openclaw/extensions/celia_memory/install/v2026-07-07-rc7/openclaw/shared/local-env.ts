/**
 * 本地 / 通用 OpenClaw 集成方的 env 加载钩子。
 *
 * 与 celiaclaw/shared/env-loader.ts 同签名，但不启用 xiaoyi 沙盒专属 alias。
 * openclaw/scripts/install.sh 会从最终 openclaw.json 生成
 * `$OPENCLAW_CONFIG_DIR/celia_env.json`，这里读取它作为本地兜底。
 */

import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

let loaded = false;

function configDir(): string {
  return process.env.CELIA_CONFIG_DIR
    || process.env.OPENCLAW_CONFIG_DIR
    || join(homedir(), ".openclaw");
}

function envFileCandidates(): string[] {
  const explicit = process.env.CELIA_OPENCLAW_ENV_FILE;
  const dir = configDir();
  return [
    ...(explicit ? [explicit] : []),
    join(dir, "celia_env.json"),
  ];
}

function loadJsonEnv(path: string): void {
  const raw = readFileSync(path, "utf8");
  const data = JSON.parse(raw) as Record<string, unknown>;
  for (const [key, value] of Object.entries(data)) {
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) continue;
    if (process.env[key] !== undefined) continue;
    if (typeof value === "string" && value.length > 0) {
      process.env[key] = value;
    }
  }
}

/**
 * 本地模式下的 env 加载器。
 *
 * - 只读取 OpenClaw 安装脚本生成的 `celia_env.json`
 * - 不覆盖调用方已经 export 的 env
 * - 文件缺失时静默跳过，保持纯 openclaw.json / process.env 配置方式可用
 */
export function loadCeliaEnv(): void {
  if (loaded) return;
  loaded = true;
  for (const p of envFileCandidates()) {
    if (!existsSync(p)) continue;
    try {
      loadJsonEnv(p);
    } catch (err) {
      console.warn(
        `[celia] openclaw env file ignored: ${p} (${(err as Error).message})`,
      );
    }
    return;
  }
}
