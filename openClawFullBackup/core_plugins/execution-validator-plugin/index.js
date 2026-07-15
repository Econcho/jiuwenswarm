import fs from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PLUGIN_DIR = path.dirname(fileURLToPath(import.meta.url));

// 功能开关。
// 放在文件顶部，后续只改这里就能控制插件行为，不需要进入 hook 主逻辑里找分支。
const ENABLE_DANGEROUS_COMMAND_APPROVAL = true;
const ENABLE_POST_FILE_OUTBOUND_GUARD = true;
const ENABLE_SEND_FILE_TO_USER_GUARD = true;

// 默认工具与运行时配置。
const DEFAULT_EXEC_TOOLS = ["exec", "process", "bash"];
const DEFAULT_COMMAND_PARAM_NAMES = ["command", "cmd"];
const DEFAULT_CONFIRM_TTL_MS = 10 * 60 * 1000;
const DEFAULT_VALIDATOR_ROOT = PLUGIN_DIR;
const MAX_INSPECT_FILE_BYTES = 2 * 1024 * 1024;
// 压缩文件审计限制：
// 1. 只检查 20MB 以内的压缩文件
// 2. 最多检查 200 个压缩包内文件
// 3. 最多递归 3 层嵌套压缩文件
const MAX_ARCHIVE_INSPECT_FILE_BYTES = 20 * 1024 * 1024;
const MAX_ARCHIVE_ENTRY_COUNT = 200;
const MAX_ARCHIVE_NESTED_DEPTH = 3;

// 当前支持的压缩文件类型。
const ARCHIVE_EXTENSIONS = [".zip", ".tar", ".tar.gz", ".tgz", ".gz"];

// 用 Python 标准库做压缩文件检查：
// - zipfile 处理 zip
// - tarfile 处理 tar / tar.gz / tgz
// - gzip 处理 gz
// 这样不需要在插件里再额外引入三方解压依赖。
const ARCHIVE_INSPECTOR_SCRIPT = String.raw`
import gzip
import io
import json
import os
import sys
import tarfile
import zipfile

file_path = sys.argv[1]
sensitive_values = [str(value) for value in json.loads(sys.argv[2]) if value]
max_bytes = int(sys.argv[3])
max_entries = int(sys.argv[4])
max_depth = int(sys.argv[5])

state = {"entries": 0, "stop": False}
matched_entries = []
errors = []

def add_error(message):
    if message not in errors:
        errors.append(message)

def lower_contains_count(text):
    lower_text = text.lower()
    count = 0
    for value in sensitive_values:
        if value.lower() in lower_text:
            count += 1
    return count

def decode_text(raw_bytes):
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return raw_bytes.decode("utf-8", "ignore")

def is_archive_name(name):
    lower_name = (name or "").lower()
    return (
        lower_name.endswith(".zip")
        or lower_name.endswith(".tar")
        or lower_name.endswith(".tar.gz")
        or lower_name.endswith(".tgz")
        or lower_name.endswith(".gz")
    )

def register_entry(entry_name):
    state["entries"] += 1
    if state["entries"] > max_entries:
        add_error("Archive entry count exceeded the inspection limit.")
        state["stop"] = True
        return False
    return True

def inspect_plain_bytes(raw_bytes, entry_name):
    if len(raw_bytes) > max_bytes:
        add_error('Archive entry "' + entry_name + '" exceeds the 20MB inspection limit.')
        return

    count = lower_contains_count(decode_text(raw_bytes))
    if count > 0:
        matched_entries.append({"entry": entry_name, "count": count})

def inspect_gzip_stream(stream, entry_name, depth):
    try:
        with gzip.GzipFile(fileobj=stream) as gz_file:
            raw_bytes = gz_file.read(max_bytes + 1)
    except Exception as exc:
        add_error('Archive "' + entry_name + '" could not be decompressed: ' + str(exc))
        return

    if len(raw_bytes) > max_bytes:
        add_error('Archive entry "' + entry_name + '" exceeds the 20MB inspection limit after decompression.')
        return

    inner_name = entry_name[:-3] if entry_name.lower().endswith(".gz") else entry_name + ".out"
    if is_archive_name(inner_name) and depth < max_depth:
        inspect_bytes(io.BytesIO(raw_bytes), inner_name, depth + 1)
    else:
        inspect_plain_bytes(raw_bytes, inner_name)

def inspect_zip_stream(stream, entry_name, depth):
    try:
        with zipfile.ZipFile(stream) as archive:
            for info in archive.infolist():
                if state["stop"]:
                    return
                if info.is_dir():
                    continue
                if not register_entry(info.filename):
                    return
                if info.file_size > max_bytes:
                    add_error('Archive entry "' + info.filename + '" exceeds the 20MB inspection limit.')
                    continue
                try:
                    with archive.open(info) as entry_stream:
                        raw_bytes = entry_stream.read(max_bytes + 1)
                except Exception as exc:
                    add_error('Archive entry "' + info.filename + '" could not be read: ' + str(exc))
                    continue
                if len(raw_bytes) > max_bytes:
                    add_error('Archive entry "' + info.filename + '" exceeds the 20MB inspection limit.')
                    continue
                if is_archive_name(info.filename) and depth < max_depth:
                    inspect_bytes(io.BytesIO(raw_bytes), info.filename, depth + 1)
                else:
                    inspect_plain_bytes(raw_bytes, info.filename)
    except Exception as exc:
        add_error('Archive "' + entry_name + '" could not be opened: ' + str(exc))

def inspect_tar_stream(stream, entry_name, depth):
    try:
        with tarfile.open(fileobj=stream, mode="r:*") as archive:
            for member in archive.getmembers():
                if state["stop"]:
                    return
                if not member.isfile():
                    continue
                if not register_entry(member.name):
                    return
                if member.size > max_bytes:
                    add_error('Archive entry "' + member.name + '" exceeds the 20MB inspection limit.')
                    continue
                try:
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        continue
                    raw_bytes = extracted.read(max_bytes + 1)
                except Exception as exc:
                    add_error('Archive entry "' + member.name + '" could not be read: ' + str(exc))
                    continue
                if len(raw_bytes) > max_bytes:
                    add_error('Archive entry "' + member.name + '" exceeds the 20MB inspection limit.')
                    continue
                if is_archive_name(member.name) and depth < max_depth:
                    inspect_bytes(io.BytesIO(raw_bytes), member.name, depth + 1)
                else:
                    inspect_plain_bytes(raw_bytes, member.name)
    except Exception as exc:
        add_error('Archive "' + entry_name + '" could not be opened: ' + str(exc))

def inspect_bytes(stream_or_bytes, entry_name, depth):
    if state["stop"]:
        return
    if depth > max_depth:
        add_error('Archive nested depth exceeded the inspection limit for "' + entry_name + '".')
        state["stop"] = True
        return

    lower_name = entry_name.lower()
    if lower_name.endswith(".zip"):
        inspect_zip_stream(stream_or_bytes if hasattr(stream_or_bytes, "read") else io.BytesIO(stream_or_bytes), entry_name, depth)
        return
    if lower_name.endswith(".tar") or lower_name.endswith(".tar.gz") or lower_name.endswith(".tgz"):
        inspect_tar_stream(stream_or_bytes if hasattr(stream_or_bytes, "read") else io.BytesIO(stream_or_bytes), entry_name, depth)
        return
    if lower_name.endswith(".gz"):
        inspect_gzip_stream(stream_or_bytes if hasattr(stream_or_bytes, "read") else io.BytesIO(stream_or_bytes), entry_name, depth)
        return

    raw_bytes = stream_or_bytes.read(max_bytes + 1) if hasattr(stream_or_bytes, "read") else stream_or_bytes
    inspect_plain_bytes(raw_bytes, entry_name)

root_size = os.path.getsize(file_path)
if root_size > max_bytes:
    print(json.dumps({
        "matchedEntries": [],
        "errors": ['Archive "' + file_path + '" exceeds the 20MB inspection limit.']
    }))
    sys.exit(0)

inspect_bytes(open(file_path, "rb"), os.path.basename(file_path), 0)
print(json.dumps({
    "matchedEntries": matched_entries,
    "errors": errors
}))
`;

// 第一版 curl 文件外发识别规则。
// 当前先只覆盖最常见的“把本地文件 POST 到远程 URL”形式，
// 保持识别范围收敛，先把最小可用能力做稳定，后面再继续扩展。
const CURL_FORM_FILE_RE =
  /(?:^|\s)(?:-F|--form)\s+(?:"[^"]*=@([^";\s]+)[^"]*"|'[^']*=@([^';\s]+)[^']*'|[^\s"']*=@([^\s;"']+))/gi;
const CURL_DATA_FILE_RE =
  /(?:^|\s)(?:--data-binary|--data|--data-raw)\s+@(?:"([^"]+)"|'([^']+)'|([^\s"';]+))/gi;

// 这里沿用 Xiaoyi channel 脱敏模块的核心思路：
// 不是只看关键词，而是先从配置里提取“真实敏感值”，再拿这些值去匹配文件内容。
// 关键词只用于判断某个配置项“是否值得收集”，不直接拿关键词本身做拦截依据。
const SECRET_KEYWORDS = [
  "apikey",
  "token",
  "secret",
  "password",
  "passwd",
  "pwd",
  "authorization",
  "cookie",
  "session",
  "signature",
  "sign"
];

const ID_KEYWORDS = [
  "agentid",
  "apiid",
  "uid",
  "pushid",
  "userid",
  "accountid",
  "clientid",
  "appid"
];

const URL_KEYWORDS = ["url", "endpoint"];

const PLACEHOLDER_VALUES = new Set([
  "apikey",
  "api-key",
  "api_key",
  "token",
  "secret",
  "password",
  "passwd",
  "pwd",
  "authorization",
  "bearer",
  "openclaw",
  "text/event-stream"
]);

function toStringList(value, fallback = []) {
  if (!Array.isArray(value)) {
    return fallback.slice();
  }

  const items = value
    .map((item) => (typeof item === "string" ? item.trim() : ""))
    .filter(Boolean);

  return items.length > 0 ? items : fallback.slice();
}

function safeReadJson(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return null;
  }
}

function logGuardFailure(logger, message, meta = undefined) {
  if (logger && typeof logger.warn === "function") {
    logger.warn(message, meta);
    return;
  }

  if (logger && typeof logger.info === "function") {
    logger.info(message, meta);
    return;
  }

  if (meta === undefined) {
    console.warn(message);
    return;
  }

  console.warn(message, meta);
}

// Linux 环境下的 HOME 解析。
// 这个插件默认和 Xiaoyi channel 部署在同类 Linux 环境里，所以路径约定直接和它对齐。
function getLinuxHomeDir() {
  return process.env.HOME || "/home/sandbox";
}

function loadRulesFromJson(filePath) {
  const parsed = safeReadJson(filePath);
  if (!parsed || !Array.isArray(parsed.rules)) {
    return [];
  }

  return parsed.rules
    .filter((item) => item && typeof item === "object")
    .map((item) => ({
      pattern: typeof item.pattern === "string" ? item.pattern.trim() : "",
      severity: item.severity === "critical" ? "critical" : "warning"
    }))
    .filter((item) => item.pattern);
}

function isArchivePath(filePath) {
  const normalizedPath = String(filePath ?? "").toLowerCase();
  return ARCHIVE_EXTENSIONS.some((extension) => normalizedPath.endsWith(extension));
}

function compileRules(rules) {
  const compiled = [];
  for (const rule of rules) {
    try {
      compiled.push({
        pattern: new RegExp(rule.pattern, "i"),
        severity: rule.severity
      });
    } catch {
      continue;
    }
  }
  return compiled;
}

// 命令提取保持轻量。
// 当前这版只需要拿到最终的 shell 字符串，就足够做危险命令匹配和第一版 POST 文件外发识别。
function getCommandFromParams(params, commandParamNames) {
  for (const name of commandParamNames) {
    if (typeof params[name] === "string" && params[name].trim()) {
      return params[name];
    }
  }

  if (Array.isArray(params.args)) {
    return params.args.map((item) => String(item)).join(" ");
  }

  return "";
}

function evaluateCommandRisk(command, rules) {
  const reasons = [];
  let severity = "warning";

  for (const rule of rules) {
    if (!rule.pattern.test(command)) {
      continue;
    }

    reasons.push(`Matched ${rule.severity} rule "${rule.pattern.source}"`);
    if (rule.severity === "critical") {
      severity = "critical";
    }
  }

  if (reasons.length === 0) {
    return null;
  }

  return { reasons, severity };
}

function buildDangerousCommandApprovalRequest(command, reasons, timeoutMs, severity) {
  const reasonLines = reasons.map((item) => `- ${item}`).join("\n");
  return {
    requireApproval: {
      title: "Execution Validator: dangerous command approval required",
      description: `Command:\n${command}\n\nReasons:\n${reasonLines}`,
      severity,
      timeoutMs,
      timeoutBehavior: "deny"
    }
  };
}

// 对于 POST 文件外发泄露，这里不走审批。
// 只要文件内容里包含已知的 Xiaoyi/OpenClaw 敏感值，就直接拦截，并返回可读的拦截理由。
function buildPostFileBlockResult(targetUrl, filePaths, reasons) {
  const reasonLines = reasons.map((item) => `- ${item}`).join("\n");
  const filesText = filePaths.map((item) => `- ${item}`).join("\n");

  return {
    block: true,
    blockReason:
      `Blocked outbound POST file request.\n\n` +
      `Target URL:\n${targetUrl}\n\n` +
      `Files:\n${filesText}\n\n` +
      `Reasons:\n${reasonLines}`
  };
}

function buildResolvedConfig(pluginConfig) {
  const validatorRoot =
    typeof pluginConfig.validatorRoot === "string" && pluginConfig.validatorRoot.trim()
      ? path.resolve(pluginConfig.validatorRoot)
      : DEFAULT_VALIDATOR_ROOT;

  return {
    commandRulesPath: path.join(validatorRoot, "config", "command-rules.json"),
    openclawConfigPath: path.join(getLinuxHomeDir(), ".openclaw", "openclaw.json"),
    xiaoyiEnvPath: path.join(getLinuxHomeDir(), ".openclaw", ".xiaoyienv"),
    confirmTtlMs:
      typeof pluginConfig.confirmTtlMs === "number" && pluginConfig.confirmTtlMs > 0
        ? pluginConfig.confirmTtlMs
        : DEFAULT_CONFIRM_TTL_MS,
    logAllowedCalls: pluginConfig.logAllowedCalls === true
  };
}

// 先把配置 key 统一归一化，再做关键词判断。
// 这样 "api-key"、"API_KEY"、"apiKey" 会被视为同一种语义。
function normalizeKeyName(key) {
  return String(key ?? "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

function normalizeValue(value) {
  return String(value ?? "").trim();
}

function matchesKeyword(normalizedKey, keywords) {
  return keywords.some((keyword) => normalizedKey.includes(keyword));
}

// 过滤占位词，避免把 "apiKey"、"openclaw" 这种普通字符串误当成真实敏感值。
function isPlaceholderValue(value) {
  const normalized = normalizeValue(value).toLowerCase();
  if (!normalized) {
    return true;
  }

  if (PLACEHOLDER_VALUES.has(normalized)) {
    return true;
  }

  return /^(my|your|demo|test)?(apikey|token|secret|password)$/.test(normalized);
}

// 这套启发式判断和 Xiaoyi channel 的 sensitive-redactor 保持同方向：
// 只收集“长得像真实 secret/id/url”的值，而不是把所有配置字符串一股脑收进来。
function looksLikeToken(value) {
  const trimmed = normalizeValue(value);
  if (trimmed.length < 8 || isPlaceholderValue(trimmed)) {
    return false;
  }

  if (/^(sk-|sk_)[a-z0-9]/i.test(trimmed)) {
    return true;
  }

  if (/^[a-f0-9]{24,}$/i.test(trimmed)) {
    return true;
  }

  return /^[A-Za-z0-9._/+\\=-]{16,}$/.test(trimmed) && /[A-Za-z]/.test(trimmed) && /\d/.test(trimmed);
}

function looksLikeIdentifier(value) {
  const trimmed = normalizeValue(value);
  if (trimmed.length < 6 || isPlaceholderValue(trimmed)) {
    return false;
  }

  if (/^agent[a-z0-9]{8,}$/i.test(trimmed)) {
    return true;
  }

  if (/^webhook[a-z0-9]{6,}$/i.test(trimmed)) {
    return true;
  }

  if (/^\d{6,}$/.test(trimmed)) {
    return true;
  }

  return /^[A-Za-z][A-Za-z0-9_-]{10,}$/.test(trimmed) && /\d/.test(trimmed);
}

function looksLikeSensitiveUrl(value) {
  const trimmed = normalizeValue(value);

  try {
    const parsed = new URL(trimmed);
    if (parsed.username || parsed.password) {
      return true;
    }

    for (const [name, paramValue] of parsed.searchParams.entries()) {
      const normalizedName = normalizeKeyName(name);
      if (
        (matchesKeyword(normalizedName, SECRET_KEYWORDS) || matchesKeyword(normalizedName, ID_KEYWORDS)) &&
        normalizeValue(paramValue).length > 5
      ) {
        return true;
      }
    }

    const segments = parsed.pathname.split("/").filter(Boolean);
    return segments.some((segment) => looksLikeToken(segment));
  } catch {
    return false;
  }
}

// 决定某个配置项是否值得当作敏感值收集。
// key 名提供语义，value 形态提供置信度，两者一起决定是否保留。
function shouldKeepSensitiveValue(key, value) {
  const trimmed = normalizeValue(value);
  if (trimmed.length <= 5 || isPlaceholderValue(trimmed)) {
    return false;
  }

  const normalizedKey = normalizeKeyName(key);

  if (matchesKeyword(normalizedKey, SECRET_KEYWORDS)) {
    return looksLikeToken(trimmed) || looksLikeSensitiveUrl(trimmed);
  }

  if (matchesKeyword(normalizedKey, ID_KEYWORDS)) {
    return looksLikeIdentifier(trimmed) || looksLikeToken(trimmed);
  }

  if (matchesKeyword(normalizedKey, URL_KEYWORDS)) {
    return looksLikeSensitiveUrl(trimmed);
  }

  return looksLikeToken(trimmed);
}

function addSensitiveValue(values, key, value) {
  if (shouldKeepSensitiveValue(key, value)) {
    values.push(normalizeValue(value));
  }
}

// 从和 Xiaoyi channel 一样的主配置文件里提取敏感值。
// 这样 POST 文件外发防护和 channel 自身脱敏逻辑就保持一致了。
function loadSensitiveValuesFromOpenClawConfig(filePath) {
  const config = safeReadJson(filePath);
  if (!config) {
    return [];
  }

  const values = [];

  if (config.channels && typeof config.channels === "object") {
    for (const channel of Object.values(config.channels)) {
      if (!channel || typeof channel !== "object") {
        continue;
      }

      addSensitiveValue(values, "apiKey", channel.apiKey);
      addSensitiveValue(values, "agentId", channel.agentId);
      addSensitiveValue(values, "apiId", channel.apiId);
      addSensitiveValue(values, "uid", channel.uid);
      addSensitiveValue(values, "pushId", channel.pushId);
      addSensitiveValue(values, "wsUrl1", channel.wsUrl1);
      addSensitiveValue(values, "wsUrl2", channel.wsUrl2);
    }
  }

  if (config.models?.providers && typeof config.models.providers === "object") {
    for (const provider of Object.values(config.models.providers)) {
      if (!provider || typeof provider !== "object") {
        continue;
      }

      addSensitiveValue(values, "apiKey", provider.apiKey);
      addSensitiveValue(values, "baseUrl", provider.baseUrl);

      if (provider.headers && typeof provider.headers === "object") {
        for (const [headerName, headerValue] of Object.entries(provider.headers)) {
          addSensitiveValue(values, headerName, headerValue);
        }
      }
    }
  }

  addSensitiveValue(values, "token", config.gateway?.auth?.token);

  return [...new Set(values)].sort((a, b) => b.length - a.length);
}

// 同时读取 .xiaoyienv。
// Xiaoyi channel 也把它视为运行时敏感信息来源，所以这里保持一致。
// 内容同时兼容 JSON 结构和 KEY=VALUE 的 dotenv 风格。
function loadSensitiveValuesFromXiaoyiEnv(filePath) {
  if (!fs.existsSync(filePath)) {
    return [];
  }

  try {
    const content = fs.readFileSync(filePath, "utf8");

    try {
      const parsed = JSON.parse(content);
      if (!parsed || typeof parsed !== "object") {
        return [];
      }

      const values = [];
      for (const [key, value] of Object.entries(parsed)) {
        addSensitiveValue(values, key, value);
      }
      return [...new Set(values)].sort((a, b) => b.length - a.length);
    } catch {
      const values = [];
      for (const line of content.split("\n")) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith("#")) {
          continue;
        }

        const eqIndex = trimmed.indexOf("=");
        if (eqIndex <= 0) {
          continue;
        }

        const key = trimmed.slice(0, eqIndex).trim();
        const value = trimmed.slice(eqIndex + 1).trim();
        addSensitiveValue(values, key, value);
      }
      return [...new Set(values)].sort((a, b) => b.length - a.length);
    }
  } catch {
    return [];
  }
}

// 调用外部 Python 检查脚本解析压缩文件内容。
// 只要压缩包里命中敏感值，或者因为超限/解压失败导致无法安全审计，
// 就返回 reasons，交给上层直接拦截本次 POST 外发。
function runArchiveInspector(filePath, sensitiveValues, options = {}) {
  const { logger = null } = options;
  const pythonCandidates = ["python3", "python"];
  let lastFailureReason = "";

  for (const pythonCommand of pythonCandidates) {
    const result = spawnSync(
      pythonCommand,
      [
        "-c",
        ARCHIVE_INSPECTOR_SCRIPT,
        filePath,
        JSON.stringify(sensitiveValues),
        String(MAX_ARCHIVE_INSPECT_FILE_BYTES),
        String(MAX_ARCHIVE_ENTRY_COUNT),
        String(MAX_ARCHIVE_NESTED_DEPTH)
      ],
      {
        encoding: "utf8",
        timeout: 15000
      }
    );

    if (result.error && result.error.code === "ENOENT") {
      lastFailureReason = `command "${pythonCommand}" was not found`;
      continue;
    }

    if (result.error) {
      lastFailureReason = String(result.error.message || result.error);
      continue;
    }

    if (result.status !== 0) {
      const stderr = (result.stderr || "").trim();
      lastFailureReason = stderr || `${pythonCommand} exited with status ${result.status}`;
      continue;
    }

    try {
      // 压缩文件检查脚本返回结构化 JSON：
      // 1. matchedEntries: 命中敏感值的压缩包内部文件
      // 2. errors: 超限、读取失败、解压失败等无法安全审计的情况
      const parsed = JSON.parse(result.stdout || "{}");
      const reasons = [];

      if (Array.isArray(parsed.matchedEntries) && parsed.matchedEntries.length > 0) {
        const preview = parsed.matchedEntries
          .slice(0, 5)
          .map((item) => `${item.entry} (${item.count} matches)`)
          .join(", ");
        reasons.push(
          `Sensitive values from Xiaoyi channel config were found in archive "${filePath}" (${parsed.matchedEntries.length} matched entries: ${preview}).`
        );
      }

      if (Array.isArray(parsed.errors) && parsed.errors.length > 0) {
        logGuardFailure(
          logger,
          "[execution-validator-plugin] Archive inspection reported non-fatal audit errors; request will continue.",
          {
            filePath,
            errors: parsed.errors
          }
        );
      }

      if (Array.isArray(parsed.errors)) {
        for (const message of parsed.errors) {
          if (typeof message === "string" && message.trim()) {
            continue;
          }
        }
      }

      return reasons.length > 0 ? { reasons } : null;
    } catch {
      lastFailureReason = `command "${pythonCommand}" returned an unreadable inspection result`;
      continue;
    }
  }

  logGuardFailure(
    logger,
    "[execution-validator-plugin] Archive inspection failed open; request will continue.",
    {
      filePath,
      reason: lastFailureReason || "neither python3 nor python is available"
    }
  );
  return null;
}

// shell 工具可能会带 cwd/workdir。
// 这里优先使用这些信息，保证 curl 里的相对路径和真实执行时的路径解析保持一致。
function getCommandCwd(params) {
  if (typeof params.cwd === "string" && params.cwd.trim()) {
    return params.cwd.trim();
  }

  if (typeof params.workdir === "string" && params.workdir.trim()) {
    return params.workdir.trim();
  }

  return process.cwd();
}

function resolveLocalPath(rawPath, cwd) {
  const candidate = String(rawPath ?? "").trim();
  if (!candidate || candidate === "-") {
    return "";
  }

  // Linux 路径语义：
  // 1. 支持 file:// 本地文件 URL
  // 2. 支持 ~/path 的 home 展开
  // 3. 其余路径按 Linux 下的绝对/相对路径规则解析
  if (candidate.startsWith("file://")) {
    try {
      return fileURLToPath(candidate);
    } catch {
      return "";
    }
  }

  if (candidate === "~") {
    return getLinuxHomeDir();
  }

  if (candidate.startsWith("~/")) {
    return path.join(getLinuxHomeDir(), candidate.slice(2));
  }

  return path.isAbsolute(candidate) ? path.normalize(candidate) : path.resolve(cwd, candidate);
}

function collectCapturedPaths(text, expression, cwd) {
  const matches = [];
  for (const match of text.matchAll(expression)) {
    const rawPath = match.slice(1).find(Boolean);
    if (!rawPath) {
      continue;
    }

    const resolvedPath = resolveLocalPath(rawPath, cwd);
    if (resolvedPath) {
      matches.push(resolvedPath);
    }
  }
  return matches;
}

function extractTargetUrl(command) {
  const urlMatches = command.match(/https?:\/\/[^\s"'`]+/gi);
  return Array.isArray(urlMatches) && urlMatches.length > 0 ? urlMatches[urlMatches.length - 1] : "";
}

// 当前识别范围：
// 1. 只处理 curl
// 2. 只处理 POST 语义
// 3. 只处理 @file 这种本地文件引用
// 故意先收窄范围，避免第一版看起来“支持很多”，实际却不稳定。
function detectPostedFiles(command, cwd) {
  if (!/\bcurl\b/i.test(command)) {
    return null;
  }

  const hasPostSemantics =
    /(?:^|\s)(?:-X|--request)\s+POST(?:$|\s)/i.test(command) ||
    /(?:^|\s)(?:-F|--form|--data|--data-binary|--data-raw)(?:$|\s)/i.test(command);

  if (!hasPostSemantics) {
    return null;
  }

  const targetUrl = extractTargetUrl(command);
  if (!targetUrl) {
    return null;
  }

  const filePaths = [
    ...collectCapturedPaths(command, CURL_FORM_FILE_RE, cwd),
    ...collectCapturedPaths(command, CURL_DATA_FILE_RE, cwd)
  ];

  const uniqueFilePaths = [...new Set(filePaths)];
  if (uniqueFilePaths.length === 0) {
    return null;
  }

  return {
    targetUrl,
    filePaths: uniqueFilePaths
  };
}

// 对提取出来的真实敏感值做大小写不敏感匹配。
// 这里和 Xiaoyi channel 一样，优先使用“具体已知的值”去命中，而不是继续做关键词猜测。
function findSensitiveMatches(text, sensitiveValues) {
  const normalizedText = String(text).toLowerCase();
  const matches = [];

  for (const sensitiveValue of sensitiveValues) {
    if (!sensitiveValue) {
      continue;
    }

    if (normalizedText.includes(String(sensitiveValue).toLowerCase())) {
      matches.push(sensitiveValue);
    }
  }

  return matches;
}

function normalizeToStringArray(param) {
  if (Array.isArray(param)) {
    return param.map((item) => String(item).trim()).filter(Boolean);
  }

  if (typeof param === "string" && param.trim()) {
    try {
      const parsed = JSON.parse(param);
      if (Array.isArray(parsed)) {
        return parsed.map((item) => String(item).trim()).filter(Boolean);
      }
    } catch {
      return [];
    }
  }

  return [];
}

// 检查 curl POST 请求里引用的每一个本地文件。
// 当前策略刻意保持简单：
// 1. 只检查普通文件
// 2. 超过大小上限的文件不做全文扫描
// 3. 先按 utf8 文本读取
// 4. 在文本里查找已知敏感值
// 后续如果这版稳定，再继续扩压缩包、二进制格式和更多文件类型。
function inspectPostedFiles(filePaths, sensitiveValues, options = {}) {
  const { logger = null } = options;
  const reasons = [];

  for (const filePath of filePaths) {
    let stats;
    try {
      stats = fs.statSync(filePath);
    } catch {
      continue;
    }

    if (!stats.isFile()) {
      continue;
    }

    // 压缩文件走单独的“解压 + 内容审计”链路。
    // 普通文件继续走现有的直接读取文本内容的逻辑。
    if (isArchivePath(filePath)) {
      const archiveInspection = runArchiveInspector(filePath, sensitiveValues, { logger });
      if (archiveInspection?.reasons?.length) {
        reasons.push(...archiveInspection.reasons);
      }
      continue;
    }

    if (stats.size > MAX_INSPECT_FILE_BYTES) {
      // reasons.push(`File "${filePath}" exceeds the ${MAX_INSPECT_FILE_BYTES} byte inspection limit.`);
      continue;
    }

    let text;
    try {
      text = fs.readFileSync(filePath, "utf8");
    } catch {
      continue;
    }

    const matches = findSensitiveMatches(text, sensitiveValues);
    if (matches.length > 0) {
      reasons.push(`Sensitive values from Xiaoyi channel config were found in "${filePath}" (${matches.length} matches).`);
    }
  }

  if (reasons.length === 0) {
    return null;
  }

  return { reasons };
}

function buildSendFileToUserBlockResult(filePaths, reasons) {
  const reasonLines = reasons.map((item) => `- ${item}`).join("\n");
  const filesText = filePaths.map((item) => `- ${item}`).join("\n");

  return {
    block: true,
    blockReason:
      `Blocked send_file_to_user request.\n\n` +
      `Files:\n${filesText}\n\n` +
      `Reasons:\n${reasonLines}`
  };
}

function evaluateSendFileToUserRisk(params, sensitiveValues, options = {}) {
  const { logger = null, logEnabled = false } = options;

  if (!ENABLE_SEND_FILE_TO_USER_GUARD || sensitiveValues.length === 0) {
    return null;
  }

  const cwd = getCommandCwd(params);
  const fileLocalUrls = normalizeToStringArray(params.fileLocalUrls);
  if (fileLocalUrls.length === 0) {
    return null;
  }

  const filePaths = fileLocalUrls
    .map((item) => resolveLocalPath(item, cwd))
    .filter(Boolean);

  if (filePaths.length === 0) {
    return null;
  }

  if (logEnabled && logger) {
    logger.info("[execution-validator-plugin] Detected send_file_to_user local files", {
      filePaths
    });
  }

  const inspection = inspectPostedFiles(filePaths, sensitiveValues, { logger });
  if (!inspection) {
    if (logEnabled && logger) {
      logger.info("[execution-validator-plugin] send_file_to_user passed content inspection", {
        filePaths
      });
    }
    return null;
  }

  return {
    filePaths,
    reasons: inspection.reasons
  };
}

function evaluatePostFileOutboundRisk(command, params, sensitiveValues, options = {}) {
  const { logger = null, logEnabled = false } = options;

  if (!ENABLE_POST_FILE_OUTBOUND_GUARD || sensitiveValues.length === 0) {
    return null;
  }

  const cwd = getCommandCwd(params);
  const postedFiles = detectPostedFiles(command, cwd);
  if (!postedFiles) {
    if (logEnabled && logger) {
      logger.info("[execution-validator-plugin] No POST file outbound pattern detected", {
        command
      });
    }
    return null;
  }

  if (logEnabled && logger) {
    logger.info("[execution-validator-plugin] Detected POST file outbound", {
      targetUrl: postedFiles.targetUrl,
      filePaths: postedFiles.filePaths
    });
  }

  const inspection = inspectPostedFiles(postedFiles.filePaths, sensitiveValues, { logger });
  if (!inspection) {
    if (logEnabled && logger) {
      logger.info("[execution-validator-plugin] POST file outbound passed content inspection", {
        filePaths: postedFiles.filePaths
      });
    }
    return null;
  }

  return {
    targetUrl: postedFiles.targetUrl,
    filePaths: postedFiles.filePaths,
    reasons: inspection.reasons
  };
}

export default function register(api) {
  // 如果所有防护都关掉了，就不注册任何 hook。
  if (!ENABLE_DANGEROUS_COMMAND_APPROVAL && !ENABLE_POST_FILE_OUTBOUND_GUARD && !ENABLE_SEND_FILE_TO_USER_GUARD) {
    api.logger.info("[execution-validator-plugin] All command guard features are disabled by code flags.");
    return;
  }

  const pluginConfig = api.pluginConfig ?? {};
  const resolvedConfig = buildResolvedConfig(pluginConfig);

  const enabledTools = new Set(toStringList(pluginConfig.enabledTools, []));
  const execTools = new Set(toStringList(pluginConfig.execTools, DEFAULT_EXEC_TOOLS));
  const commandParamNames = toStringList(pluginConfig.commandParamNames, DEFAULT_COMMAND_PARAM_NAMES);

  const commandRules = compileRules(loadRulesFromJson(resolvedConfig.commandRulesPath));
  const sensitiveValues = [
    ...loadSensitiveValuesFromOpenClawConfig(resolvedConfig.openclawConfigPath),
    ...loadSensitiveValuesFromXiaoyiEnv(resolvedConfig.xiaoyiEnvPath)
  ]
    .filter(Boolean)
    .filter((value, index, values) => values.indexOf(value) === index)
    .sort((a, b) => b.length - a.length);

  if (resolvedConfig.logAllowedCalls) {
    api.logger.info("[execution-validator-plugin] Loaded POST outbound sensitive values", {
      count: sensitiveValues.length,
      openclawConfigPath: resolvedConfig.openclawConfigPath,
      xiaoyiEnvPath: resolvedConfig.xiaoyiEnvPath
    });
  }

  api.on(
    "before_tool_call",
    (event) => {
      try {
        // 可选工具白名单。
        if (enabledTools.size > 0 && !enabledTools.has(event.toolName)) {
          return;
        }

        const params = event.params ?? {};

        // 防护 0：结构化文件外发工具 send_file_to_user。
        // 这里只检查 fileLocalUrls，本地文件会复用普通文件/压缩文件审计逻辑。
        if (event.toolName === "send_file_to_user") {
          const sendFileRisk = evaluateSendFileToUserRisk(params, sensitiveValues, {
            logger: api.logger,
            logEnabled: resolvedConfig.logAllowedCalls
          });

          if (sendFileRisk) {
            return buildSendFileToUserBlockResult(sendFileRisk.filePaths, sendFileRisk.reasons);
          }

          if (resolvedConfig.logAllowedCalls) {
            api.logger.info("[execution-validator-plugin] Allowed send_file_to_user", {
              fileLocalUrls: normalizeToStringArray(params.fileLocalUrls)
            });
          }

          return;
        }

        // 当前版本只检查 exec 类工具。
        if (!execTools.has(event.toolName)) {
          return;
        }

        const command = getCommandFromParams(params, commandParamNames);
        if (!command) {
          return;
        }

        // 防护 1：危险命令模式 -> 走 OpenClaw 原生审批。
        if (ENABLE_DANGEROUS_COMMAND_APPROVAL) {
          const commandRisk = evaluateCommandRisk(command, commandRules);
          if (commandRisk) {
            return buildDangerousCommandApprovalRequest(
              command,
              commandRisk.reasons,
              resolvedConfig.confirmTtlMs,
              commandRisk.severity
            );
          }
        }

        // 防护 2：POST 本地文件到远程 URL -> 检查文件内容。
        // 只要文件里含有 Xiaoyi/OpenClaw 配置里的真实敏感值，就直接拦截。
        const outboundRisk = evaluatePostFileOutboundRisk(command, params, sensitiveValues, {
          logger: api.logger,
          logEnabled: resolvedConfig.logAllowedCalls
        });
        if (outboundRisk) {
          return buildPostFileBlockResult(
            outboundRisk.targetUrl,
            outboundRisk.filePaths,
            outboundRisk.reasons
          );
        }

        if (resolvedConfig.logAllowedCalls) {
          api.logger.info("[execution-validator-plugin] Allowed command", {
            toolName: event.toolName,
            command
          });
        }

        return;
      } catch (error) {
        logGuardFailure(
          api.logger,
          "[execution-validator-plugin] before_tool_call guard failed open; request will continue.",
          {
            toolName: event.toolName,
            error: String(error)
          }
        );
        return;
      }
    },
    { priority: 100 }
  );
}
