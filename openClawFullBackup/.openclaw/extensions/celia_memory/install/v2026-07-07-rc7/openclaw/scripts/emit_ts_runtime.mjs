#!/usr/bin/env node
/**
 * Emit JavaScript runtime files next to TypeScript plugin sources.
 *
 * OpenClaw production loads installed plugins as JavaScript.  The source tree
 * keeps TypeScript files with .js import specifiers, so a lightweight
 * transpile-only pass is enough for runtime packaging and avoids type-checking
 * host-provided modules such as openclaw/plugin-sdk.
 */

import { createRequire } from "node:module";
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const SKIP_DIRS = new Set([
  ".git",
  "build",
  "dist",
  "node_modules",
]);

function usage() {
  console.error(
    "Usage: emit_ts_runtime.mjs --typescript-root <package-dir> " +
      "--root <dir> [--root <dir> ...]",
  );
}

function parseArgs(argv) {
  const roots = [];
  let typescriptRoot = "";
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--typescript-root") {
      typescriptRoot = argv[++i] || "";
    } else if (arg === "--root") {
      roots.push(argv[++i] || "");
    } else {
      usage();
      throw new Error(`unknown argument: ${arg}`);
    }
  }
  if (!typescriptRoot || roots.length === 0) {
    usage();
    throw new Error("missing required arguments");
  }
  return {
    typescriptRoot: resolve(typescriptRoot),
    roots: roots.map((root) => resolve(root)),
  };
}

function loadTypescript(typescriptRoot) {
  const requireFromRoot = createRequire(join(typescriptRoot, "package.json"));
  try {
    return requireFromRoot("typescript");
  } catch (err) {
    throw new Error(
      `cannot load TypeScript from ${typescriptRoot}; run npm install first: ` +
        err.message,
    );
  }
}

function collectTsFiles(root, out) {
  let entries;
  try {
    entries = readdirSync(root, { withFileTypes: true });
  } catch (err) {
    throw new Error(`cannot read ${root}: ${err.message}`);
  }

  for (const entry of entries) {
    const path = join(root, entry.name);
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name)) {
        collectTsFiles(path, out);
      }
      continue;
    }
    if (
      entry.isFile() &&
      entry.name.endsWith(".ts") &&
      !entry.name.endsWith(".d.ts") &&
      !entry.name.endsWith(".test.ts")
    ) {
      out.push(path);
    }
  }
}

function formatDiagnostic(ts, diagnostic) {
  const msg = ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n");
  if (!diagnostic.file || diagnostic.start === undefined) {
    return msg;
  }
  const pos = diagnostic.file.getLineAndCharacterOfPosition(diagnostic.start);
  return `${diagnostic.file.fileName}:${pos.line + 1}:${pos.character + 1}: ${msg}`;
}

function emitFile(ts, file) {
  const source = readFileSync(file, "utf8");
  const result = ts.transpileModule(source, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ES2022,
      moduleResolution: ts.ModuleResolutionKind.Bundler,
      esModuleInterop: true,
      importsNotUsedAsValues: ts.ImportsNotUsedAsValues.Remove,
      inlineSourceMap: false,
      sourceMap: false,
      removeComments: false,
    },
    fileName: file,
    reportDiagnostics: true,
  });

  const errors = (result.diagnostics || []).filter(
    (diag) => diag.category === ts.DiagnosticCategory.Error,
  );
  if (errors.length > 0) {
    throw new Error(errors.map((diag) => formatDiagnostic(ts, diag)).join("\n"));
  }

  const outFile = file.slice(0, -".ts".length) + ".js";
  writeFileSync(outFile, result.outputText, "utf8");
  return outFile;
}

function ensureDirectory(path) {
  if (!statSync(path).isDirectory()) {
    throw new Error(`${path} is not a directory`);
  }
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const ts = loadTypescript(args.typescriptRoot);
  const files = [];
  for (const root of args.roots) {
    ensureDirectory(root);
    collectTsFiles(root, files);
  }

  files.sort();
  for (const file of files) {
    emitFile(ts, file);
  }
  console.error(`[emit-ts-runtime] emitted ${files.length} JavaScript file(s)`);
}

try {
  main();
} catch (err) {
  console.error(`[emit-ts-runtime] ERROR: ${err.message}`);
  process.exit(1);
}
