#!/usr/bin/env node
/**
 * @file run-tests.mjs
 * @brief 串行用本地 `tsx` 跑插件下所有 *.test.ts；任一失败即整体非零退出。
 *
 * 设计意图（deep review FIX-5）：
 *   - 把 vitest-style 自定义 test() runner 各文件接入 npm test 入口，
 *     使 pre-commit / CI 能"npm run test"统一调用。
 *   - tsx 必须在 devDependencies 声明，避免依赖全局安装。
 *
 * 使用：
 *   cd integration/openclaw/memory-plugin
 *   npm install
 *   npm test
 */

import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = fileURLToPath(new URL(".", import.meta.url));
const PLUGIN_ROOT = join(__dirname, "..");
const requireFromPlugin = createRequire(join(PLUGIN_ROOT, "package.json"));

/** 递归收集 *.test.ts 文件。 */
function collectTestFiles(dir) {
    const out = [];
    for (const entry of readdirSync(dir)) {
        if (entry === "node_modules" || entry === "dist") continue;
        const full = join(dir, entry);
        if (statSync(full).isDirectory()) {
            out.push(...collectTestFiles(full));
        } else if (entry.endsWith(".test.ts")) {
            out.push(full);
        }
    }
    return out;
}

function resolveLocalTsxLoader() {
    try {
        return requireFromPlugin.resolve("tsx");
    } catch {
        return null;
    }
}

const tsxLoader = resolveLocalTsxLoader();
if (!tsxLoader) {
    console.error(
        "[run-tests] Missing local dev dependency: tsx. " +
        "Run `npm install` in integration/openclaw/memory-plugin before `npm test`.",
    );
    process.exit(1);
}

const testFiles = collectTestFiles(join(PLUGIN_ROOT, "src")).sort();
console.log(`[run-tests] discovered ${testFiles.length} test file(s)`);

let failed = 0;
for (const file of testFiles) {
    const rel = relative(PLUGIN_ROOT, file);
    process.stdout.write(`[run-tests] >>> ${rel}\n`);
    const result = spawnSync(
        process.execPath,
        ["--import", tsxLoader, file],
        { cwd: PLUGIN_ROOT, stdio: "inherit", shell: false },
    );
    if (result.status !== 0) {
        failed++;
        process.stderr.write(
            `[run-tests] FAIL ${rel} (exit=${result.status})\n`,
        );
    }
}

if (failed > 0) {
    console.error(`[run-tests] ${failed} test file(s) failed`);
    process.exit(1);
}
console.log(`[run-tests] all ${testFiles.length} test file(s) passed`);
