import { dirname, join, basename } from "node:path";
import { mkdirSync } from "node:fs";
export function deriveCeliaLogFilePath(resolvedDbPath) {
    const dbDir = dirname(resolvedDbPath);
    const parentDir = dirname(dbDir);
    const grandParentDir = dirname(parentDir);
    if (basename(dbDir) === "memory" && basename(parentDir) === "workspace") {
        return join(dirname(parentDir), "logs", "celia_memory", "celia_memory.log");
    }
    if (basename(parentDir) === "memory" &&
        basename(grandParentDir) === "workspace") {
        return join(dirname(grandParentDir), "logs", "celia_memory", "celia_memory.log");
    }
    return join(dbDir, "celia_memory.log");
}
export function ensureCeliaLogDir(logFilePath) {
    mkdirSync(dirname(logFilePath), { recursive: true });
}
