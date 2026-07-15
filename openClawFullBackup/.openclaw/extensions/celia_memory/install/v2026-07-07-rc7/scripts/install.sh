#!/bin/bash
# install.sh — celiaclaw platform install hook
#
# 调用方：Docker 部署脚本。
# 调用方负责获取并解压 release tarball，本脚本只负责沙盒侧的
# runtime artifact 和配置落盘动作。
#
set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# ---- 路径定位 -----------------------------------------------------------
# PACKAGE_ROOT：Docker 部署流程已经解压好的 release 根目录，通常是
#   $OPENCLAW_CONFIG_DIR/extensions/celia_memory/package。
# VERSIONED_ROOT：本脚本按 manifest.toml 里的版本号创建的真实运行目录：
#   $OPENCLAW_CONFIG_DIR/extensions/celia_memory/install/<version>。
# RUNTIME_ROOT：写入 openclaw.json 的稳定入口：
#   $OPENCLAW_CONFIG_DIR/extensions/celia_memory/install/current。
# current 软链只在版本目录准备完成后发布，避免生产配置指向 package 暂存区。
PACKAGE_ROOT="${CELIA_EXTRACT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
OPENCLAW_CONFIG_DIR="${CELIA_CONFIG_DIR:-/home/sandbox/.openclaw}"
EXTENSION_ROOT="$OPENCLAW_CONFIG_DIR/extensions/celia_memory"
INSTALL_ROOT="$EXTENSION_ROOT/install"
RELEASE_VERSION=""
VERSIONED_ROOT=""
RUNTIME_ROOT="$INSTALL_ROOT/current"
SUPERVISOR_CONFIG_FILE="${CELIA_SUPERVISORD_CONF:-/home/sandbox/supervisord.conf}"
INSTALL_RUN_ID="${CELIA_INSTALL_RUN_ID:-$(date +%Y%m%d-%H%M%S)-$$}"
INSTALL_STARTED_AT="$(date +%s)"
INSTALL_LOG_FILE="${CELIA_LOG_FILE_PATH:-${OPENCLAW_CONFIG_DIR}/logs/celia_memory/install-${INSTALL_RUN_ID}.log}"
INSTALL_SUMMARY_EMITTED=0
INSTALL_CURRENT_STAGE=""
INSTALL_STAGE_STARTED_AT=""
INSTALL_FAILED_STAGE=""
INSTALL_STAGE_ERROR_EMITTED=0
OPENCLAW_CONFIG_PATCH_SKIP_REASON="not run"
export CELIA_INSTALL_RUN_ID="$INSTALL_RUN_ID"
export CELIA_CONFIG_ROOT="$OPENCLAW_CONFIG_DIR"
export CELIA_CONFIG_DIR="$OPENCLAW_CONFIG_DIR"
mkdir -p "$(dirname "$INSTALL_LOG_FILE")" 2>/dev/null || true

# ---- 日志、时间和文件摘要工具 -----------------------------------------
log_info() { echo -e "${GREEN}[INFO]${NC} $1" | tee -a "$INSTALL_LOG_FILE"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1" | tee -a "$INSTALL_LOG_FILE"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1" | tee -a "$INSTALL_LOG_FILE"; }
log_step() { echo -e "${BLUE}[STEP]${NC} $1" | tee -a "$INSTALL_LOG_FILE"; }

current_seconds() {
    date +%s
}

elapsed_seconds() {
    local started_at="$1"
    echo "$(( $(current_seconds) - started_at ))"
}

readlink_or_none() {
    local path="$1"
    if [ -L "$path" ]; then
        readlink "$path"
    elif [ -e "$path" ]; then
        echo "<not-symlink>"
    else
        echo "<none>"
    fi
}

file_size_bytes() {
    local path="$1"
    if stat -c '%s' "$path" >/dev/null 2>&1; then
        stat -c '%s' "$path"
    else
        stat -f '%z' "$path"
    fi
}

file_sha256() {
    local path="$1"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$path" | awk '{print $1}'
    else
        shasum -a 256 "$path" | awk '{print $1}'
    fi
}

# ---- 安装阶段和失败摘要 ------------------------------------------------
begin_install_stage() {
    local stage_name="$1"

    INSTALL_CURRENT_STAGE="$stage_name"
    INSTALL_STAGE_STARTED_AT="$(current_seconds)"
    log_info "install.stage begin name=$stage_name"
}

finish_install_stage() {
    local stage_name="$1"
    local elapsed

    elapsed="$(elapsed_seconds "${INSTALL_STAGE_STARTED_AT:-$INSTALL_STARTED_AT}")"
    log_info "install.stage done name=$stage_name elapsed_seconds=$elapsed"
    if [ "$INSTALL_CURRENT_STAGE" = "$stage_name" ]; then
        INSTALL_CURRENT_STAGE=""
        INSTALL_STAGE_STARTED_AT=""
    fi
}

mark_install_stage_failed() {
    local rc="${1:-1}"
    local stage_name="${INSTALL_CURRENT_STAGE:-${INSTALL_FAILED_STAGE:-unknown}}"
    local started_at="${INSTALL_STAGE_STARTED_AT:-$INSTALL_STARTED_AT}"
    local elapsed

    [ -n "$stage_name" ] || stage_name="unknown"
    INSTALL_FAILED_STAGE="$stage_name"
    if [ "$INSTALL_STAGE_ERROR_EMITTED" = "1" ]; then
        return
    fi

    elapsed="$(elapsed_seconds "$started_at")"
    log_error "install.stage error name=$stage_name rc=$rc elapsed_seconds=$elapsed"
    INSTALL_STAGE_ERROR_EMITTED=1
}

run_install_stage() {
    local stage_name="$1"
    shift

    begin_install_stage "$stage_name"
    "$@"
    finish_install_stage "$stage_name"
}

# ---- 安装上下文和路径状态日志 -----------------------------------------
log_install_context() {
    local user_name
    local user_id
    local python_version
    user_name="$(id -un 2>/dev/null || echo unknown)"
    user_id="$(id -u 2>/dev/null || echo unknown)"
    python_version="$(python3 --version 2>&1 || echo unavailable)"

    log_info "install.context run_id=$INSTALL_RUN_ID script=$0 pid=$$ user=$user_name uid=$user_id"
    log_info "install.context package_root=$PACKAGE_ROOT config_dir=$OPENCLAW_CONFIG_DIR"
    log_info "install.context extension_root=$EXTENSION_ROOT install_root=$INSTALL_ROOT"
    log_info "install.context supervisord_conf=$SUPERVISOR_CONFIG_FILE log_file=$INSTALL_LOG_FILE"
    log_info "install.context python=$python_version"
}

log_file_mode() {
    local path="$1"
    if [ ! -e "$path" ]; then
        log_warn "文件不存在，无法记录权限: $path"
        return
    fi
    if stat -c '%A %a %U:%G %s %n' "$path" >/dev/null 2>&1; then
        stat -c '[INFO] mode=%A perm=%a owner=%U:%G size=%s path=%n' "$path" \
            | tee -a "$INSTALL_LOG_FILE"
    else
        stat -f '[INFO] mode=%Sp perm=%Lp owner=%Su:%Sg size=%z path=%N' \
            "$path" | tee -a "$INSTALL_LOG_FILE"
    fi
}

log_path_summary() {
    local label="$1"
    local path="$2"

    if [ -f "$path" ]; then
        local size
        local sha
        size="$(file_size_bytes "$path" 2>/dev/null || echo unknown)"
        sha="$(file_sha256 "$path" 2>/dev/null || echo unknown)"
        log_info "install.file label=$label path=$path size=$size sha256=$sha"
    elif [ -d "$path" ]; then
        log_info "install.dir label=$label path=$path"
    else
        log_warn "install.path_missing label=$label path=$path"
    fi
}

log_install_summary() {
    local result="$1"
    local rc="${2:-0}"
    local elapsed
    local current_target
    local binary_path
    elapsed="$(elapsed_seconds "$INSTALL_STARTED_AT")"
    current_target="$(readlink_or_none "$INSTALL_ROOT/current")"
    binary_path="${VERSIONED_ROOT:-$RUNTIME_ROOT}/bin/celia_memory_mcp_server"

    if [ "$INSTALL_SUMMARY_EMITTED" = "1" ]; then
        return
    fi
    INSTALL_SUMMARY_EMITTED=1

    log_info "install.summary result=$result rc=$rc run_id=$INSTALL_RUN_ID elapsed_seconds=$elapsed failed_stage=${INSTALL_FAILED_STAGE:-none}"
    log_info "install.summary release_version=${RELEASE_VERSION:-unknown} package_root=$PACKAGE_ROOT"
    log_info "install.summary versioned_root=${VERSIONED_ROOT:-unknown} current_target=$current_target"
    log_info "install.summary log_file=$INSTALL_LOG_FILE openclaw_config=$OPENCLAW_CONFIG_DIR/openclaw.json"
    if [ -f "$binary_path" ]; then
        log_path_summary "runtime_binary" "$binary_path"
    fi
}

# ---- 错误处理 ----------------------------------------------------------
on_unhandled_error() {
    local rc="$?"
    local line_no="${BASH_LINENO[0]:-unknown}"
    local command_text="${BASH_COMMAND:-unknown}"
    trap - ERR
    log_error "install.unhandled_error run_id=$INSTALL_RUN_ID rc=$rc line=$line_no command=$command_text"
    mark_install_stage_failed "$rc"
    log_install_summary "failed" "$rc"
    exit "$rc"
}

trap on_unhandled_error ERR

error_exit() {
    log_error "$1"
    mark_install_stage_failed 1
    log_install_summary "failed" 1
    exit 1
}

# ---- release 版本解析和包完整性校验 -----------------------------------
read_manifest_package_value() {
    local manifest_file="$1"
    local key="$2"

    [ -f "$manifest_file" ] || return 1
    python3 - "$manifest_file" "$key" <<'PYEOF'
import re
import sys

manifest = sys.argv[1]
key = sys.argv[2]
section = None
value = None

for raw_line in open(manifest, encoding="utf-8"):
    line = raw_line.split("#", 1)[0].strip()
    if not line:
        continue
    if line.startswith("[") and line.endswith("]"):
        section = line.strip("[]").strip()
        continue
    if section != "package":
        continue
    match = re.match(rf"^{re.escape(key)}\s*=\s*\"([^\"]+)\"\s*$", line)
    if match:
        value = match.group(1)
        break

if value is None:
    sys.exit(1)
print(value)
PYEOF
}

detect_release_version() {
    local manifest_file="$PACKAGE_ROOT/manifest.toml"
    [ -f "$manifest_file" ] \
        || error_exit "release 包缺少 manifest.toml: $manifest_file"

    read_manifest_package_value "$manifest_file" "version"
}

setup_release_paths() {
    RELEASE_VERSION="$(detect_release_version)" \
        || error_exit "manifest.toml 缺少 package.version: $PACKAGE_ROOT/manifest.toml"

    case "$RELEASE_VERSION" in
        ""|"."|".."|*/*)
            error_exit "非法 release 版本号: $RELEASE_VERSION"
            ;;
    esac

    VERSIONED_ROOT="$INSTALL_ROOT/$RELEASE_VERSION"
    RUNTIME_ROOT="$INSTALL_ROOT/current"

    log_info "release source: $PACKAGE_ROOT"
    log_info "release version: $RELEASE_VERSION"
    log_info "versioned install root: $VERSIONED_ROOT"
    log_info "runtime current root: $RUNTIME_ROOT"
}

log_release_manifest_summary() {
    local manifest_file="$PACKAGE_ROOT/manifest.toml"
    local package_name
    local package_kind
    local package_variant
    local package_sha

    package_name="$(read_manifest_package_value "$manifest_file" "name" 2>/dev/null || true)"
    package_kind="$(read_manifest_package_value "$manifest_file" "kind" 2>/dev/null || true)"
    package_variant="$(read_manifest_package_value "$manifest_file" "variant" 2>/dev/null || true)"
    package_sha="$(read_manifest_package_value "$manifest_file" "core_bin_sha256" 2>/dev/null || true)"

    log_info "release.manifest name=${package_name:-unknown} version=$RELEASE_VERSION kind=${package_kind:-unknown} variant=${package_variant:-unknown} core_bin_sha256=${package_sha:-none}"
}

check_environment() {
    log_step "检查环境..."

    # 检查是否在容器内
    if [ ! -f "/.dockerenv" ] && [ ! -d "/home/sandbox" ]; then
        log_warn "可能不在目标容器内，请确认当前环境"
    fi

    # 检查必要目录。package/ 只是解压暂存区；运行目录统一放到 install/。
    mkdir -p \
        "$INSTALL_ROOT" \
        "$OPENCLAW_CONFIG_DIR/workspace/memory/celia_memory" \
        || error_exit "无法创建安装目录"
    log_info "install.dirs install_root=$INSTALL_ROOT memory_dir=$OPENCLAW_CONFIG_DIR/workspace/memory/celia_memory"

    # 检查必要命令
    command -v python3 >/dev/null 2>&1 || error_exit "未找到 python3"

    log_info "环境检查通过"
}

validate_release_package() {
    log_step "校验 release 包完整性..."

    local source_plugin="$PACKAGE_ROOT/openclaw/memory-plugin"
    local source_shared="$PACKAGE_ROOT/openclaw/shared"
    local source_bin="$PACKAGE_ROOT/openclaw/bin/gspd_memory_mcp_server"
    local source_config="$PACKAGE_ROOT/openclaw/config/openclaw.json"
    local overlay_config="$PACKAGE_ROOT/celiaclaw/config/openclaw.json"
    local shared_singleton="$source_shared/celia-client-singleton.js"

    [ -d "$source_plugin" ] \
        || error_exit "release 包缺少 memory-plugin: $source_plugin"
    [ -d "$source_shared" ] \
        || error_exit "release 包缺少 shared: $source_shared"
    [ -f "$source_plugin/index.js" ] \
        || error_exit "release 包缺少 memory-plugin/index.js: $source_plugin/index.js"
    [ -f "$source_plugin/package.json" ] \
        || error_exit "release 包缺少 memory-plugin/package.json: $source_plugin/package.json"
    [ -f "$shared_singleton" ] \
        || error_exit "release 包缺少 shared/celia-client-singleton.js: $shared_singleton"
    [ -f "$source_bin" ] \
        || error_exit "release 包缺少 gspd_memory_mcp_server，请使用 celiaclaw full 包"
    [ -f "$source_config" ] \
        || error_exit "release 包缺少 openclaw/config/openclaw.json"

    log_info "release.package plugin=$source_plugin shared=$source_shared"
    log_info "release.package source_config=$source_config overlay_config=$overlay_config"
    log_path_summary "release_source_binary" "$source_bin"
    log_path_summary "release_openclaw_config" "$source_config"
    if [ -f "$overlay_config" ]; then
        log_path_summary "release_celiaclaw_overlay" "$overlay_config"
    else
        log_info "release.package overlay_config_absent=$overlay_config"
    fi
    log_info "release 包完整性校验通过"
}

# ---- 版本化安装目录和 runtime artifact 铺设 ---------------------------
validate_versioned_install() {
    local root="$1"

    [ -f "$root/manifest.toml" ] \
        || error_exit "版本目录缺少 manifest.toml: $root/manifest.toml"
    [ -f "$root/openclaw/config/openclaw.json" ] \
        || error_exit "版本目录缺少 openclaw/config/openclaw.json"
    [ -f "$root/openclaw/bin/gspd_memory_mcp_server" ] \
        || error_exit "版本目录缺少 openclaw/bin/gspd_memory_mcp_server"
    [ -f "$root/memory-plugin/index.js" ] \
        || error_exit "版本目录缺少 memory-plugin/index.js"
    [ -f "$root/memory-plugin/package.json" ] \
        || error_exit "版本目录缺少 memory-plugin/package.json"
    [ -f "$root/shared/celia-client-singleton.js" ] \
        || error_exit "版本目录缺少 shared/celia-client-singleton.js"
    [ -x "$root/bin/celia_memory_mcp_server" ] \
        || error_exit "版本目录缺少可执行 mcp_server: $root/bin/celia_memory_mcp_server"
}

verify_optional_release_file_consistency() {
    local relative_path="$1"
    local source_file="$PACKAGE_ROOT/$relative_path"
    local target_file="$VERSIONED_ROOT/$relative_path"

    if [ -f "$source_file" ] && [ -f "$target_file" ]; then
        cmp -s "$source_file" "$target_file" \
            || error_exit "版本目录已存在但 $relative_path 与 package 不一致"
        return
    fi
    if [ -e "$source_file" ] || [ -e "$target_file" ]; then
        error_exit "版本目录已存在但 $relative_path 存在状态与 package 不一致"
    fi
}

verify_existing_version_matches_package() {
    local package_manifest="$PACKAGE_ROOT/manifest.toml"
    local target_manifest="$VERSIONED_ROOT/manifest.toml"
    local package_bin="$PACKAGE_ROOT/openclaw/bin/gspd_memory_mcp_server"
    local target_bin="$VERSIONED_ROOT/bin/celia_memory_mcp_server"
    local expected_sha=""
    local actual_sha=""

    validate_versioned_install "$VERSIONED_ROOT"
    cmp -s "$package_manifest" "$target_manifest" \
        || error_exit "版本目录已存在但 manifest.toml 与 package 不一致"
    verify_optional_release_file_consistency "build-info.toml"

    if [ -f "$package_bin" ] && [ -f "$target_bin" ]; then
        cmp -s "$package_bin" "$target_bin" \
            || error_exit "版本目录已存在但 mcp_server 与 package 不一致"
    fi

    expected_sha="$(
        read_manifest_package_value "$package_manifest" "core_bin_sha256" \
            2>/dev/null || true
    )"
    if [ -n "$expected_sha" ] && [ -f "$target_bin" ]; then
        actual_sha="$(file_sha256 "$target_bin")"
        [ "$actual_sha" = "$expected_sha" ] \
            || error_exit "版本目录已存在但 mcp_server sha256 与 manifest 不一致"
    fi
}

lift_versioned_install_stage() {
    local stage_root="$1"
    local source_bin="$stage_root/openclaw/bin/gspd_memory_mcp_server"
    local target_bin="$stage_root/bin/celia_memory_mcp_server"
    local started_at
    started_at="$(current_seconds)"

    log_info "install.lift begin stage_root=$stage_root"
    mkdir -p "$stage_root/bin" \
        || error_exit "无法创建版本目录 bin: $stage_root/bin"
    cp -Rp "$stage_root/openclaw/memory-plugin" "$stage_root/memory-plugin" \
        || error_exit "无法铺设 memory-plugin 到版本目录"
    cp -Rp "$stage_root/openclaw/shared" "$stage_root/shared" \
        || error_exit "无法铺设 shared 到版本目录"
    log_info "install.lift binary_source=$source_bin binary_target=$target_bin"
    install -m 0755 "$source_bin" "$target_bin" \
        || error_exit "无法铺设 celia_memory_mcp_server 到版本目录"
    log_path_summary "versioned_runtime_binary" "$target_bin"

    # 当前 celiaclaw runtime 只注册 memory-celia，不加载 context-engine 插件。
    if [ -d "$stage_root/openclaw/context-engine-plugin" ]; then
        rm -rf "$stage_root/openclaw/context-engine-plugin"
        log_info "清理未使用的 openclaw/context-engine-plugin 残留"
    fi
    log_info "install.lift done elapsed_seconds=$(elapsed_seconds "$started_at")"
}

prepare_versioned_install() {
    local stage_root="$INSTALL_ROOT/.${RELEASE_VERSION}.stage.$$"
    local started_at
    local copy_started_at
    local current_before
    started_at="$(current_seconds)"
    current_before="$(readlink_or_none "$INSTALL_ROOT/current")"

    log_info "install.current before=$current_before"

    if [ -e "$VERSIONED_ROOT" ]; then
        log_info "版本目录已存在，校验与 package 是否一致: $VERSIONED_ROOT"
        verify_existing_version_matches_package
        log_info "版本目录与 package 一致，复用: $VERSIONED_ROOT"
        log_info "install.versioned_root reused path=$VERSIONED_ROOT elapsed_seconds=$(elapsed_seconds "$started_at")"
        return
    fi

    rm -rf "$stage_root"
    mkdir -p "$stage_root" \
        || error_exit "无法创建版本目录 staging: $stage_root"
    log_info "install.staging create stage_root=$stage_root"
    trap 'rm -rf "$stage_root"' EXIT
    trap 'rm -rf "$stage_root"; exit 130' INT TERM

    copy_started_at="$(current_seconds)"
    log_info "install.copy begin source=$PACKAGE_ROOT target=$stage_root"
    cp -Rp "$PACKAGE_ROOT/." "$stage_root/" || {
        rm -rf "$stage_root"
        error_exit "无法复制 release 包到版本目录 staging"
    }
    log_info "install.copy done elapsed_seconds=$(elapsed_seconds "$copy_started_at")"

    lift_versioned_install_stage "$stage_root"
    validate_versioned_install "$stage_root"

    log_info "install.versioned_root publish source=$stage_root target=$VERSIONED_ROOT"
    if ! mv "$stage_root" "$VERSIONED_ROOT"; then
        rm -rf "$stage_root"
        error_exit "无法发布版本目录: $VERSIONED_ROOT"
    fi
    log_info "install.versioned_root published path=$VERSIONED_ROOT elapsed_seconds=$(elapsed_seconds "$started_at")"
    trap - EXIT INT TERM
}

install_runtime_artifacts() {
    log_step "准备版本化安装目录..."
    setup_release_paths
    log_release_manifest_summary
    validate_release_package
    prepare_versioned_install

    log_file_mode "$VERSIONED_ROOT/bin/celia_memory_mcp_server"

    log_info "验证文件..."
    ls -la "$VERSIONED_ROOT/" | tee -a "$INSTALL_LOG_FILE"

    log_info "文件铺设完成"
}

# ---- memory-plugin 运行时产物校验 -------------------------------------
verify_memory_plugin_runtime() {
    log_step "验证 memory-plugin 运行时产物..."

    local plugin_pkg="$VERSIONED_ROOT/memory-plugin/package.json"
    if [ ! -f "$VERSIONED_ROOT/memory-plugin/index.js" ]; then
        error_exit "memory-plugin 缺少发布期生成的 index.js，请使用新发布包重装"
    fi
    if [ ! -f "$plugin_pkg" ]; then
        error_exit "memory-plugin 缺少 package.json"
    fi

    local runtime_deps
    if ! runtime_deps=$(python3 - "$plugin_pkg" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as f:
    pkg = json.load(f)
deps = {}
for key in ("dependencies", "optionalDependencies"):
    deps.update(pkg.get(key) or {})
print(len(deps))
PY
    ); then
        error_exit "无法解析 memory-plugin package.json"
    fi
    if [ "$runtime_deps" != "0" ]; then
        error_exit "memory-plugin 仍包含运行时 npm dependencies，拒绝安装"
    fi

    log_info "memory-plugin 无运行时 npm dependencies，跳过依赖安装"
}

# ---- supervisord 和 gateway 处理 --------------------------------------
log_gateway_ca_fallback_state() {
    # 这里不再把 CURL_CA_BUNDLE 写进 supervisord.conf。mcp_server 的
    # HTTP 层会在运行时扫描同一组路径；安装阶段只做可观测性检查。
    local ca_bundle=""
    local _ca_candidates=(
        "/etc/ssl/certs/ca-certificates.crt"                # Debian / Ubuntu
        "/etc/ssl/certs/ca-bundle.crt"                      # RHEL / CentOS
        "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem" # RHEL 8+
        "/etc/pki/tls/certs/ca-bundle.crt"                  # CentOS 6/7
        "/etc/ssl/cert.pem"                                 # Alpine / macOS
        "/etc/ssl/ca-bundle.pem"                            # SUSE
        "$OPENCLAW_CONFIG_DIR/.ssl/ca.crt"
        "$OPENCLAW_CONFIG_DIR/ca.crt"
        "/home/sandbox/.ssl/ca.crt"
    )
    local _c

    for _c in "${_ca_candidates[@]}"; do
        if [ -f "$_c" ]; then
            ca_bundle="$_c"
            log_info "C 端 CA fallback 可用: $ca_bundle"
            break
        fi
    done
    if [ -z "$ca_bundle" ]; then
        log_warn "未找到 C 端 fallback CA 文件（已扫常见 distro 路径 + \$OPENCLAW_CONFIG_DIR/.ssl/）。"
        log_warn "  HTTPS 调用（embed / chat）可能因证书校验失败（curl error 60）失败。"
        log_warn "  修复：把内网 CA append 到系统 trust store，或放到上述 fallback 路径之一。"
    fi
}

cleanup_gateway_env_tokens() {
    local supervisor_update_output

    # token-aware 清理：只看 [program:openclaw-gateway] 段的 environment=
    # 行，不做全文件 grep，避免注释 / 其他 program 段 / 全局段误判。
    if ! supervisor_update_output=$(
        _SUPERVISORD_CONF="$SUPERVISOR_CONFIG_FILE" python3 - <<'PYEOF'
import datetime
import os
import shutil
import sys

conf_path = os.environ["_SUPERVISORD_CONF"]

to_remove = {
    "CURL_CA_BUNDLE",
    "CELIA_LOG_FILE",
    "CELIA_LOG_MAX_BYTES",
    "CELIA_LOG_BACKUPS",
    "OPENAI_CHAT_BASE_URL",
    "OPENAI_CHAT_API_KEY",
    "OPENAI_CHAT_MODEL",
    "CELIA_CHAT_UID",
    "OPENAI_EMBED_BASE_URL",
    "OPENAI_EMBED_API_KEY",
    "OPENAI_EMBED_MODEL",
    "OPENAI_EMBED_UID",
    "OPENAI_RERANK_BASE_URL",
    "OPENAI_RERANK_API_KEY",
    "OPENAI_RERANK_MODEL",
}

with open(conf_path) as f:
    lines = f.readlines()

section_start = None
for i, ln in enumerate(lines):
    if ln.strip() == "[program:openclaw-gateway]":
        section_start = i + 1
        break
if section_start is None:
    print("未找到 [program:openclaw-gateway] 段", file=sys.stderr)
    sys.exit(4)

section_end = len(lines)
for j in range(section_start, len(lines)):
    if lines[j].lstrip().startswith("[") and j != section_start - 1:
        section_end = j
        break

env_idx = None
for j in range(section_start, section_end):
    if lines[j].startswith("environment="):
        env_idx = j
        break
if env_idx is None:
    print("OK openclaw-gateway 段无 environment= 行，跳过")
    sys.exit(0)

# 顶层逗号切分（值用双引号包，内部逗号不算分隔）
payload = lines[env_idx].rstrip("\n")[len("environment="):]
tokens, buf, in_q = [], "", False
for ch in payload:
    if ch == '"':
        in_q = not in_q
        buf += ch
    elif ch == "," and not in_q:
        if buf:
            tokens.append(buf)
        buf = ""
    else:
        buf += ch
if buf:
    tokens.append(buf)

kept = []
removed = []
for t in tokens:
    if "=" in t:
        k = t.split("=", 1)[0].strip()
        if k in to_remove:
            removed.append(k)
            continue
    kept.append(t)

if not removed:
    print("OK no legacy gateway env tokens in supervisor")
    sys.exit(0)

backup_path = f"{conf_path}.bak.{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"
shutil.copy2(conf_path, backup_path)
lines[env_idx] = "environment=" + ",".join(kept) + "\n"
with open(conf_path, "w") as f:
    f.writelines(lines)

print(f"OK removed legacy gateway env token(s): {sorted(set(removed))}")
PYEOF
    ); then
        log_warn "supervisord env 更新失败"
        while IFS= read -r line; do
            [ -n "$line" ] && log_warn "supervisor.env $line"
        done <<< "$supervisor_update_output"
        return
    fi
    while IFS= read -r line; do
        [ -n "$line" ] && log_info "supervisor.env $line"
    done <<< "$supervisor_update_output"
}

verify_gateway_env_clean() {
    # 再验证一次：不是全文件 grep，而是严格看 openclaw-gateway 的 env=
    _SUPERVISORD_CONF="$SUPERVISOR_CONFIG_FILE" python3 - <<'PYEOF' \
        && log_info "openclaw-gateway 段遗留 env 已清理" \
        || log_warn "openclaw-gateway 段仍包含遗留 env，请手动检查"
import os, sys
managed_keys = {
    "CURL_CA_BUNDLE",
    "CELIA_LOG_FILE",
    "CELIA_LOG_MAX_BYTES",
    "CELIA_LOG_BACKUPS",
    "OPENAI_CHAT_BASE_URL",
    "OPENAI_CHAT_API_KEY",
    "OPENAI_CHAT_MODEL",
    "CELIA_CHAT_UID",
    "OPENAI_EMBED_BASE_URL",
    "OPENAI_EMBED_API_KEY",
    "OPENAI_EMBED_MODEL",
    "OPENAI_EMBED_UID",
    "OPENAI_RERANK_BASE_URL",
    "OPENAI_RERANK_API_KEY",
    "OPENAI_RERANK_MODEL",
}
with open(os.environ["_SUPERVISORD_CONF"]) as f:
    lines = f.readlines()
in_section = False
for ln in lines:
    if ln.strip() == "[program:openclaw-gateway]":
        in_section = True
        continue
    if in_section and ln.lstrip().startswith("["):
        break
    if not (in_section and ln.startswith("environment=")):
        continue
    if any(f"{key}=" in ln for key in managed_keys):
        sys.exit(1)
sys.exit(0)
PYEOF
}

cleanup_gateway_supervisor_env() {
    log_step "清理 openclaw-gateway supervisord env..."

    if [ ! -f "$SUPERVISOR_CONFIG_FILE" ]; then
        log_warn "未找到 supervisord.conf，跳过此步骤"
        log_warn "supervisor.cleanup skipped reason=missing_conf path=$SUPERVISOR_CONFIG_FILE"
        return
    fi

    log_gateway_ca_fallback_state
    cleanup_gateway_env_tokens
    verify_gateway_env_clean
    log_info "chat/embed/rerank 凭据改为运行时读取 .xiaoyienv"
}

## 异步调度 gateway 重启。**关键：必须 detached + delayed**，否则同步
## supervisorctl restart 会触发 self-kill：
##
##   当安装由 openclaw-gateway 子进程触发时，同步重启会形成：
##   install.sh → restart_gateway →
##   supervisorctl restart openclaw-gateway = 把 gateway 杀掉 =
##   把自己一起 SIGKILL。
##
## 后果（生产实测，install-20260509-155343.log 在
## "supervisorctl 点重启" 行戛然而止）：
##   - hook 后续 verify_installation 没跑
##   - 调用方后续收尾动作可能没跑
##
## 修复：用 setsid + nohup + & + disown 让 supervisorctl 调用脱离
## 当前 process group。再 sleep N 秒（CELIA_RESTART_DELAY，默认 5）让
## 调用方（hook + orchestrator）完成扫尾才真重启。
##
## 返回 0 表示调度成功（不等待重启完成）；返回 1 仅当连 supervisord
## socket 都摸不到。
restart_gateway() {
    local delay="${CELIA_RESTART_DELAY:-5}"

    # 快速路径：supervisorctl 控制 socket 可用
    if command -v supervisorctl >/dev/null 2>&1 \
        && supervisorctl status >/dev/null 2>&1; then
        log_info "异步调度 supervisorctl restart openclaw-gateway（${delay}s 后执行；避免 self-kill）..."
        # detached subprocess: 新 session、忽略 SIGHUP、后台、disown
        ( setsid nohup bash -c "
            sleep $delay
            supervisorctl restart openclaw-gateway >/dev/null 2>&1
        " </dev/null >/dev/null 2>&1 & disown 2>/dev/null || true ) >/dev/null 2>&1
        log_info "✓ gateway 重启已调度"
        return 0
    fi

    # 回退路径：pkill supervisord 然后手起；同样 detach
    log_info "supervisorctl socket 不可用；异步调度 pkill+respawn (${delay}s 后)"
    ( setsid nohup bash -c "
        sleep $delay
        pkill -f 'supervisor.supervisord' 2>/dev/null || true
        sleep 2
        if ! pgrep -f 'supervisor.supervisord' >/dev/null 2>&1; then
            python3 -m supervisor.supervisord -c '$SUPERVISOR_CONFIG_FILE' >/dev/null 2>&1 &
        fi
    " </dev/null >/dev/null 2>&1 & disown 2>/dev/null || true ) >/dev/null 2>&1
    log_info "✓ gateway 重启已调度（pkill+respawn 路径）"
    return 0
}

restart_services() {
    if [ "${CELIA_SKIP_GATEWAY_RESTART:-0}" = "1" ]; then
        log_step "跳过 openclaw-gateway 重启"
        log_info "CELIA_SKIP_GATEWAY_RESTART=1"
        log_info "部署流程将负责启动/重启 openclaw-gateway"
        return 0
    fi

    log_step "调度 openclaw-gateway 异步重启..."
    # restart_gateway 已经异步 detach + delay；它返回 0 仅表示调度成功，
    # 不等待重启完成（也不能等：等就是 self-kill）。retry 循环对异步调度
    # 没意义 —— 重启的成败要等 hook + orchestrator 退出后才显现。
    if ! restart_gateway; then
        error_exit "无法调度 gateway 重启（连 supervisord socket 都摸不到）"
    fi

    # 注意：mcp_server 进程在 hook 退出时**还没**重启。下面的 pgrep 看到
    # 的是重装场景下还没被杀的旧 mcp_server / 或 fresh install 下没进程。
    # 两种都不可靠 —— 状态以 verify_installation 的 file-system
    # 检查为准，运行时探测交给重启完成后的日志/人工状态检查。
    log_info "重启已调度，hook 退出后由 supervisord 在后台执行"
}

# ---- 最终安装校验 ------------------------------------------------------
verify_installation() {
    log_step "验证安装..."

    # 检查日志
    local log_file="/tmp/openclaw/openclaw-$(date +%Y-%m-%d).log"
    if [ -f "$log_file" ]; then
        if grep -q "celia: registered" "$log_file" 2>/dev/null; then
            log_info "✓ 日志显示 celia 插件已注册"
        else
            log_info "gateway 异步重启中；运行时注册交给 status"
        fi
    fi

    # 检查文件完整性
    log_info "文件完整性检查："
    if [ -f "$VERSIONED_ROOT/bin/celia_memory_mcp_server" ]; then
        ls -lh "$VERSIONED_ROOT/bin/celia_memory_mcp_server" | tee -a "$INSTALL_LOG_FILE"
        log_file_mode "$VERSIONED_ROOT/bin/celia_memory_mcp_server"
    else
        log_warn "未找到 celia_memory_mcp_server (期望 $VERSIONED_ROOT/bin/celia_memory_mcp_server)"
    fi
    ls -ld "$VERSIONED_ROOT/memory-plugin" "$VERSIONED_ROOT/shared" | tee -a "$INSTALL_LOG_FILE"
    log_path_summary "verify_runtime_binary" "$VERSIONED_ROOT/bin/celia_memory_mcp_server"
    log_path_summary "verify_memory_plugin" "$VERSIONED_ROOT/memory-plugin"
    log_path_summary "verify_shared" "$VERSIONED_ROOT/shared"

    verify_openclaw_config_registration \
        || error_exit "openclaw.json 未完成 memory-celia 注册"

    log_info "安装流程完成，请检查上述日志输出确认运行状态"
    log_info "实时日志：tail -f $log_file"
}

# ---- install/current 软链发布 -----------------------------------------
ensure_current_link_publishable() {
    local current_link="$INSTALL_ROOT/current"

    mkdir -p "$INSTALL_ROOT" \
        || error_exit "无法创建 install 目录: $INSTALL_ROOT"
    if [ -e "$current_link" ] && [ ! -L "$current_link" ]; then
        error_exit "install/current 已存在且不是软链: $current_link"
    fi
}

publish_current_symlink() {
    log_step "发布 install/current 软链..."

    # current 只在文件铺设和生产 openclaw.json 注册校验都成功后发布，
    # 避免指向半安装或未注册的版本目录。
    local current_link="$INSTALL_ROOT/current"
    local current_tmp="$INSTALL_ROOT/.current.tmp.$$"
    local current_before

    ensure_current_link_publishable
    current_before="$(readlink_or_none "$current_link")"
    log_info "install.current before_publish=$current_before tmp=$current_tmp"
    rm -f "$current_tmp"
    ln -s "$VERSIONED_ROOT" "$current_tmp" \
        || error_exit "无法创建 install/current 软链: $current_link"
    if ! _CURRENT_TMP="$current_tmp" _CURRENT_LINK="$current_link" python3 - <<'PYEOF'
import os

os.replace(os.environ["_CURRENT_TMP"], os.environ["_CURRENT_LINK"])
PYEOF
    then
        rm -f "$current_tmp"
        error_exit "无法原子替换 install/current 软链: $current_link"
    fi
    log_info "$current_link -> $VERSIONED_ROOT"
    log_info "install.current after_publish=$(readlink_or_none "$current_link")"

    [ -x "$RUNTIME_ROOT/bin/celia_memory_mcp_server" ] \
        || error_exit "current 软链未指向可执行 mcp_server: $RUNTIME_ROOT"
    [ -d "$RUNTIME_ROOT/memory-plugin" ] \
        || error_exit "current 软链未指向 memory-plugin: $RUNTIME_ROOT"
    [ -d "$RUNTIME_ROOT/shared" ] \
        || error_exit "current 软链未指向 shared: $RUNTIME_ROOT"
}

# ---- openclaw.json 合并与注册校验 -------------------------------------
log_openclaw_config_state() {
    local phase="$1"
    local config_file="$2"
    local config_output

    if config_output=$(
        _OPENCLAW_STATE_PHASE="$phase" \
        _OPENCLAW_STATE_FILE="$config_file" \
        _OPENCLAW_STATE_RUNTIME_ROOT="$RUNTIME_ROOT" python3 - <<'PYEOF'
import json
import os
import sys

phase = os.environ["_OPENCLAW_STATE_PHASE"]
config_file = os.environ["_OPENCLAW_STATE_FILE"]
runtime_root = os.environ["_OPENCLAW_STATE_RUNTIME_ROOT"].rstrip("/")
expected_plugin = f"{runtime_root}/memory-plugin"

try:
    with open(config_file, encoding="utf-8") as handle:
        data = json.load(handle)
except FileNotFoundError:
    print(f"openclaw.config.{phase} path={config_file!r} exists=false")
    sys.exit(0)
except json.JSONDecodeError as exc:
    print(
        f"openclaw.config.{phase} path={config_file!r} "
        f"exists=true status=invalid_json error={str(exc)!r}"
    )
    sys.exit(1)

plugins = data.get("plugins")
if not isinstance(plugins, dict):
    plugins = {}

slots = plugins.get("slots")
slot_value = slots.get("memory") if isinstance(slots, dict) else None

entries = plugins.get("entries")
entry = entries.get("memory-celia") if isinstance(entries, dict) else None
has_memory_celia = isinstance(entry, dict)
entry_enabled = entry.get("enabled") if isinstance(entry, dict) else None
cfg = entry.get("config") if isinstance(entry, dict) else None
server_path = cfg.get("serverBinaryPath") if isinstance(cfg, dict) else None

load = plugins.get("load")
paths = load.get("paths") if isinstance(load, dict) else None
path_count = len(paths) if isinstance(paths, list) else 0
load_contains_plugin = isinstance(paths, list) and expected_plugin in paths

print(
    f"openclaw.config.{phase} path={config_file!r} exists=true "
    f"slot.memory={slot_value!r} "
    f"has_memory_celia={has_memory_celia} "
    f"enabled={entry_enabled!r} "
    f"serverBinaryPath={server_path!r} "
    f"load_contains_memory_plugin={load_contains_plugin} "
    f"load_path_count={path_count}"
)
PYEOF
    ); then
        while IFS= read -r line; do
            [ -n "$line" ] && log_info "$line"
        done <<< "$config_output"
        return
    fi

    while IFS= read -r line; do
        [ -n "$line" ] && log_warn "$line"
    done <<< "$config_output"
}

verify_openclaw_config_registration() {
    local config_verify_output
    if config_verify_output=$(
        _VERIFY_OPENCLAW_CONFIG="$OPENCLAW_CONFIG_DIR/openclaw.json" \
        _VERIFY_RUNTIME_ROOT="$RUNTIME_ROOT" python3 - <<'PYEOF'
import json
import os
import sys

config_path = os.environ["_VERIFY_OPENCLAW_CONFIG"]
runtime_root = os.environ["_VERIFY_RUNTIME_ROOT"].rstrip("/")
expected_plugin = f"{runtime_root}/memory-plugin"
expected_server = f"{runtime_root}/bin/celia_memory_mcp_server"
slot_value = None
server_path = None
load_contains_plugin = False

problems = []
try:
    with open(config_path, encoding="utf-8") as handle:
        data = json.load(handle)
except FileNotFoundError:
    problems.append(f"生产配置不存在: {config_path}")
except json.JSONDecodeError as exc:
    problems.append(f"生产配置 JSON 格式错误: {exc}")
else:
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        problems.append("plugins 不是对象")
        plugins = {}

    slots = plugins.get("slots")
    slot_value = slots.get("memory") if isinstance(slots, dict) else None
    if not isinstance(slots, dict) or slots.get("memory") != "memory-celia":
        problems.append(f"plugins.slots.memory={slot_value!r}, 期望 'memory-celia'")

    entries = plugins.get("entries")
    entry = entries.get("memory-celia") if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        problems.append("plugins.entries.memory-celia 不存在")
        entry = {}
    elif entry.get("enabled") is False:
        problems.append("plugins.entries.memory-celia.enabled=false")

    cfg = entry.get("config")
    if not isinstance(cfg, dict):
        problems.append("memory-celia.config 不是对象")
        cfg = {}
    server_path = cfg.get("serverBinaryPath")
    if server_path != expected_server:
        problems.append(
            "memory-celia.config.serverBinaryPath="
            f"{server_path!r}, 期望 {expected_server!r}"
        )

    load = plugins.get("load")
    paths = load.get("paths") if isinstance(load, dict) else None
    load_contains_plugin = isinstance(paths, list) and expected_plugin in paths
    if not load_contains_plugin:
        problems.append(f"plugins.load.paths 未包含 {expected_plugin!r}")

if problems:
    print("openclaw.json 注册校验未通过:")
    for problem in problems:
        print(f"  - {problem}")
    sys.exit(1)

print("openclaw.json 注册校验通过：memory-celia 已写入生产配置")
print(
    "openclaw.config.summary "
    f"slot.memory={slot_value!r} "
    "has_memory_celia=true "
    f"serverBinaryPath={server_path!r} "
    f"load_contains_memory_plugin={load_contains_plugin}"
)
PYEOF
    ); then
        log_info "$config_verify_output"
        return 0
    fi

    while IFS= read -r line; do
        [ -n "$line" ] && log_warn "$line"
    done <<< "$config_verify_output"
    if [ -n "$OPENCLAW_CONFIG_PATCH_SKIP_REASON" ]; then
        log_warn "配置合并状态: 未合并；原因: $OPENCLAW_CONFIG_PATCH_SKIP_REASON"
    fi
    return 1
}

require_openclaw_config_registration() {
    verify_openclaw_config_registration \
        || error_exit "openclaw.json 未完成 memory-celia 注册"
}

prune_openclaw_config_backups() {
    local backup_dir="$1"
    local keep_count="${2:-5}"

    [ -d "$backup_dir" ] || return 0
    python3 - "$backup_dir" "$keep_count" <<'PYEOF'
import sys
from pathlib import Path

backup_dir = Path(sys.argv[1])
try:
    keep_count = int(sys.argv[2])
except (IndexError, ValueError):
    keep_count = 5
if keep_count < 1:
    keep_count = 5

for prefix in ("openclaw.json.bak.", "init-openclaw.json.bak."):
    files = [
        item for item in backup_dir.iterdir()
        if item.is_file() and item.name.startswith(prefix)
    ]
    files.sort(key=lambda item: (item.stat().st_mtime, item.name), reverse=True)
    for old_file in files[keep_count:]:
        old_file.unlink()
PYEOF
}

backup_openclaw_config_file() {
    local src_file="$1"
    local backup_prefix="$2"
    local backup_dir="$EXTENSION_ROOT/package/openclaw.json.backup"
    local backup_file
    local timestamp
    local suffix

    mkdir -p "$backup_dir" \
        || error_exit "无法创建 openclaw.json 备份目录: $backup_dir"

    timestamp="$(date +%Y%m%d%H%M%S)"
    backup_file="$backup_dir/${backup_prefix}.bak.$timestamp"
    suffix=1
    while [ -e "$backup_file" ]; do
        backup_file="$backup_dir/${backup_prefix}.bak.$timestamp.$suffix"
        suffix=$((suffix + 1))
    done

    cp "$src_file" "$backup_file" \
        || error_exit "无法备份 openclaw.json: $src_file"
    prune_openclaw_config_backups "$backup_dir" 5
    echo "$backup_file"
}

patch_openclaw_config() {
    log_step "合并 openclaw.json 增量配置..."

    # 路径与跳过条件。
    local src_file="$VERSIONED_ROOT/openclaw/config/openclaw.json"
    local overlay_file="$VERSIONED_ROOT/celiaclaw/config/openclaw.json"
    local dst_file="$OPENCLAW_CONFIG_DIR/openclaw.json"
    local init_template_file="${CELIA_INIT_OPENCLAW_TEMPLATE:-/home/sandbox/openclaw.json}"
    local extra_dst_files=""
    local dst_backup_file=""
    local init_backup_file=""

    log_info "openclaw.config.merge src=$src_file overlay=$overlay_file dst=$dst_file runtime_root=$RUNTIME_ROOT"
    log_openclaw_config_state "before" "$dst_file"

    if [ ! -f "$src_file" ]; then
        OPENCLAW_CONFIG_PATCH_SKIP_REASON="release 参考配置不存在: $src_file"
        log_warn "跳过 openclaw.json 合并: release 参考配置不存在: $src_file"
        log_warn "后续注册校验将失败，安装不会成功完成，请检查 release 包结构"
        return
    fi

    if [ ! -f "$dst_file" ]; then
        OPENCLAW_CONFIG_PATCH_SKIP_REASON="生产配置不存在: $dst_file"
        log_warn "跳过 openclaw.json 合并: 生产配置不存在: $dst_file"
        log_warn "后续注册校验将失败，安装不会成功完成，请由部署流程创建生产配置"
        return
    fi

    # 生产配置和初始化模板备份。
    if [ -f "$dst_file" ]; then
        dst_backup_file="$(backup_openclaw_config_file "$dst_file" "openclaw.json")"
        log_info "openclaw.config.backup path=$dst_backup_file"
    fi
    if [ -f "$init_template_file" ] && [ "$init_template_file" != "$dst_file" ]; then
        init_backup_file="$(
            backup_openclaw_config_file "$init_template_file" "init-openclaw.json"
        )"
        extra_dst_files="$init_template_file"
        log_info "openclaw.config.template_backup path=$init_backup_file"
    fi

    # openclaw.json 合并实现。
    _PATCH_SRC="$src_file" _PATCH_OVERLAY="$overlay_file" _PATCH_DST="$dst_file" \
        _PATCH_EXTRA_DSTS="$extra_dst_files" \
        _PATCH_CONFIG_ROOT="$OPENCLAW_CONFIG_DIR" \
        _PATCH_INSTALL_ROOT="$RUNTIME_ROOT" python3 - <<'PYEOF'
import copy
import json
import os

src_file = os.environ.get('_PATCH_SRC', '')
overlay_file = os.environ.get('_PATCH_OVERLAY', '')
dst_file = os.environ.get('_PATCH_DST', '')
extra_dst_files = [
    item for item in os.environ.get('_PATCH_EXTRA_DSTS', '').split(os.pathsep)
    if item
]
config_root = os.environ.get('_PATCH_CONFIG_ROOT', '') or '/home/sandbox/.openclaw'
install_root = os.environ.get('_PATCH_INSTALL_ROOT', '')
MEMORY_PLUGIN_ID = 'memory-celia'

with open(src_file, 'r') as f:
    src = json.load(f)
overlay = {}
if overlay_file and os.path.isfile(overlay_file):
    with open(overlay_file, 'r') as f:
        overlay = json.load(f)

def resolve_value(value):
    return (value.replace('${CELIA_CONFIG_ROOT}', config_root)
                 .replace('${CELIA_CONFIG_DIR}', config_root))

def resolve_placeholders(obj):
    if isinstance(obj, dict):
        return {k: resolve_placeholders(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_placeholders(v) for v in obj]
    if isinstance(obj, str):
        value = resolve_value(obj)
        if install_root:
            value = (value.replace('${CELIA_INSTALL_ROOT}', install_root)
                          .replace('${CELIA_PLUGIN_ROOT}', install_root))
        return value
    return obj

def resolve_celia_config(data):
    plugins = data.get('plugins') if isinstance(data.get('plugins'), dict) else {}
    entries = plugins.get('entries') if isinstance(plugins.get('entries'), dict) else {}
    entry = entries.get('memory-celia')
    if isinstance(entry, dict):
        entries['memory-celia'] = resolve_placeholders(entry)

    installs = plugins.get('installs') if isinstance(plugins.get('installs'), dict) else {}
    install = installs.get('memory-celia')
    if isinstance(install, dict):
        installs['memory-celia'] = resolve_placeholders(install)

    load = plugins.get('load') if isinstance(plugins.get('load'), dict) else {}
    paths = load.get('paths')
    if isinstance(paths, list):
        load['paths'] = [
            resolve_placeholders(path) if (
                isinstance(path, str) and 'memory-plugin' in path
            ) else path
            for path in paths
        ]
    return data

def deep_merge(dst_obj, src_obj):
    if not isinstance(dst_obj, dict) or not isinstance(src_obj, dict):
        return src_obj
    for key, value in src_obj.items():
        if key in dst_obj:
            dst_obj[key] = deep_merge(dst_obj[key], value)
        else:
            dst_obj[key] = value
    return dst_obj

def dedupe_load_paths(data):
    plugins = data.get('plugins')
    if not isinstance(plugins, dict):
        return
    load = plugins.get('load')
    if not isinstance(load, dict):
        return
    paths = load.get('paths')
    if not isinstance(paths, list):
        return
    deduped_paths = []
    seen_paths = set()
    for path in paths:
        if path not in seen_paths:
            deduped_paths.append(path)
            seen_paths.add(path)
    load['paths'] = deduped_paths

def is_celia_memory_plugin_path(path):
    if not isinstance(path, str):
        return False
    if install_root and path.rstrip('/') == f'{install_root}/memory-plugin':
        return True
    return (
        path.endswith('/memory-plugin')
        and (
            '/celia_memory/' in path
            or '${CELIA_INSTALL_ROOT}' in path
            or '${CELIA_PLUGIN_ROOT}' in path
        )
    )

def is_managed_memory_plugin_path(path):
    return isinstance(path, str) and path.rstrip('/').endswith('/memory-plugin')

def is_managed_memory_entry(name, entry, active_memory_id):
    if name == MEMORY_PLUGIN_ID or name == active_memory_id:
        return True
    if not isinstance(name, str) or not name.startswith('memory-'):
        return False
    if not isinstance(entry, dict):
        return False

    cfg = entry.get('config')
    if isinstance(cfg, dict):
        owned_keys = ('serverBinaryPath', 'dbPath', 'vectorDim', 'embed', 'chat')
        if any(key in cfg for key in owned_keys):
            return True

    hooks = entry.get('hooks')
    return isinstance(hooks, dict) and hooks.get('allowConversationAccess') is True

def clean_managed_memory_allow_list(plugins, stale_names):
    allow = plugins.get('allow')
    if not isinstance(allow, list):
        return

    cleaned = []
    seen = set()
    for item in allow:
        if item in stale_names:
            continue
        if item not in seen:
            cleaned.append(item)
            seen.add(item)
    if MEMORY_PLUGIN_ID not in seen:
        cleaned.append(MEMORY_PLUGIN_ID)
    plugins['allow'] = cleaned

def cleanup_managed_memory_load_paths(data, desired_paths):
    plugins = data.setdefault('plugins', {})
    load = plugins.setdefault('load', {})
    paths = load.get('paths')
    if not isinstance(paths, list):
        paths = []
    desired = [
        path for path in desired_paths
        if is_celia_memory_plugin_path(path)
    ]
    desired_set = set(desired)
    cleaned = []
    seen = set()
    for path in paths:
        if is_managed_memory_plugin_path(path) and path not in desired_set:
            continue
        if path in seen:
            continue
        cleaned.append(path)
        seen.add(path)
    for path in desired:
        if path not in seen:
            cleaned.append(path)
            seen.add(path)
    load['paths'] = cleaned

def force_memory_slot(dst_obj, src_obj):
    src_slots = (
        src_obj.get('plugins', {}).get('slots')
        if isinstance(src_obj.get('plugins'), dict)
        else {}
    )
    if not isinstance(src_slots, dict):
        return
    dst_plugins = dst_obj.setdefault('plugins', {})
    dst_slots = dst_plugins.get('slots')
    if not isinstance(dst_slots, dict):
        dst_slots = {}
        dst_plugins['slots'] = dst_slots
    for key, value in src_slots.items():
        dst_slots[key] = value
    if src_slots.get('memory') == 'memory-celia':
        dst_slots['memory'] = 'memory-celia'

def replace_managed_memory_entries(dst_obj, src_obj, active_memory_id):
    src_plugins = src_obj.get('plugins', {})
    if not isinstance(src_plugins, dict):
        return set()

    preserved_memory_config = {}
    stale_names = set()
    if isinstance(active_memory_id, str) and active_memory_id != MEMORY_PLUGIN_ID:
        stale_names.add(active_memory_id)

    src_entries = src_plugins.get('entries', {})
    if isinstance(src_entries, dict):
        dst_plugins = dst_obj.setdefault('plugins', {})
        entries = dst_plugins.setdefault('entries', {})
        existing_memory_entry = entries.get(MEMORY_PLUGIN_ID)
        if isinstance(existing_memory_entry, dict):
            existing_config = existing_memory_entry.get('config')
            if isinstance(existing_config, dict):
                preserved_memory_config = copy.deepcopy(existing_config)
        for name, entry in list(entries.items()):
            if is_managed_memory_entry(name, entry, active_memory_id):
                stale_names.add(name)
                del entries[name]

        for name, entry in src_entries.items():
            if name != MEMORY_PLUGIN_ID and name not in entries:
                entries[name] = copy.deepcopy(entry)
        src_entry = src_entries.get(MEMORY_PLUGIN_ID)
        if isinstance(src_entry, dict):
            entries[MEMORY_PLUGIN_ID] = copy.deepcopy(src_entry)
            entries[MEMORY_PLUGIN_ID]['enabled'] = True
            memory_config = entries[MEMORY_PLUGIN_ID].setdefault('config', {})
            if isinstance(memory_config, dict):
                for key, value in preserved_memory_config.items():
                    memory_config.setdefault(key, value)

    src_installs = src_plugins.get('installs', {})
    if isinstance(src_installs, dict):
        dst_plugins = dst_obj.setdefault('plugins', {})
        installs = dst_plugins.setdefault('installs', {})
        for name in stale_names:
            installs.pop(name, None)
        for name, install in src_installs.items():
            if name == MEMORY_PLUGIN_ID:
                installs[MEMORY_PLUGIN_ID] = copy.deepcopy(install)
            else:
                installs.setdefault(name, copy.deepcopy(install))

    return stale_names

src = resolve_celia_config(src)
overlay = resolve_celia_config(overlay)
desired_paths = (
    src.get('plugins', {}).get('load', {}).get('paths', [])
    if isinstance(src.get('plugins'), dict)
    else []
)

def patch_target(dst_obj):
    dst_obj = resolve_celia_config(dst_obj)
    dst_plugins = dst_obj.setdefault('plugins', {})
    existing_slots = dst_plugins.get('slots')
    active_memory_id = (
        existing_slots.get('memory')
        if isinstance(existing_slots, dict)
        else None
    )

    # 1. agents.defaults.compaction.memoryFlush
    s_agents = src.get('agents', {}).get('defaults', {}).get('compaction', {})
    if 'memoryFlush' in s_agents:
        dst_obj.setdefault('agents', {}).setdefault('defaults', {}).setdefault('compaction', {})['memoryFlush'] = s_agents['memoryFlush']

    # 2. plugins.slots
    s_plugins = src.get('plugins', {})
    if 'slots' in s_plugins:
        force_memory_slot(dst_obj, src)

    # 3. plugins.load.paths (replace managed memory paths)
    if 'load' in s_plugins and 'paths' in s_plugins['load']:
        cleanup_managed_memory_load_paths(dst_obj, desired_paths)

    # 4/5. plugins.entries + plugins.installs (replace managed memory config)
    stale_names = replace_managed_memory_entries(dst_obj, src, active_memory_id)
    clean_managed_memory_allow_list(dst_plugins, stale_names)

    # 6. celiaclaw platform overlay (deep merge after common openclaw defaults)
    if overlay:
        dst_obj = deep_merge(dst_obj, overlay)

    dst_obj = resolve_celia_config(dst_obj)
    cleanup_managed_memory_load_paths(dst_obj, desired_paths)
    dedupe_load_paths(dst_obj)
    return dst_obj

target_files = [dst_file]
for target_file in extra_dst_files:
    if os.path.isfile(target_file) and target_file not in target_files:
        target_files.append(target_file)

for target_file in target_files:
    with open(target_file, 'r') as f:
        dst = json.load(f)
    dst = patch_target(dst)

    mem = dst.get('plugins', {}).get('entries', {}).get('memory-celia', {})
    cfg = mem.get('config', {}) if isinstance(mem, dict) else {}
    db_path = cfg.get('dbPath')
    if isinstance(db_path, str) and db_path:
        db_dir = os.path.dirname(os.path.expanduser(db_path))
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    with open(target_file, 'w') as f:
        json.dump(dst, f, indent=2, ensure_ascii=False)
        f.write('\n')

PYEOF

    # 合并结果校验与状态摘要。
    if python3 -c "import json; json.load(open('$dst_file'))" 2>/dev/null; then
        OPENCLAW_CONFIG_PATCH_SKIP_REASON=""
        log_info "openclaw.json 合并完成"
        log_openclaw_config_state "after" "$dst_file"
    else
        OPENCLAW_CONFIG_PATCH_SKIP_REASON="合并后 openclaw.json JSON 格式错误"
        log_error "合并后 openclaw.json JSON 格式错误"
    fi
    if [ -n "$extra_dst_files" ]; then
        if python3 -c "import json; json.load(open('$extra_dst_files'))" 2>/dev/null; then
            log_info "初始化模板 openclaw.json 同步完成: $extra_dst_files"
        else
            log_error "初始化模板 openclaw.json JSON 格式错误: $extra_dst_files"
        fi
    fi
}

## AGENTS.md 由 Docker 镜像发布前人工审核维护。
## 安装阶段不再创建、替换或注入生产环境 AGENTS.md。
# 主流程
main() {
    log_info "install.start run_id=$INSTALL_RUN_ID started_at=$(date -Is 2>/dev/null || date)"
    log_install_context

    run_install_stage "check_environment" check_environment
    run_install_stage "install_runtime_artifacts" install_runtime_artifacts
    run_install_stage "verify_memory_plugin_runtime" verify_memory_plugin_runtime
    run_install_stage "ensure_current_link_publishable" ensure_current_link_publishable
    run_install_stage "patch_openclaw_config" patch_openclaw_config
    run_install_stage \
        "verify_openclaw_config_registration" \
        require_openclaw_config_registration
    run_install_stage "publish_current_symlink" publish_current_symlink
    run_install_stage "cleanup_gateway_supervisor_env" cleanup_gateway_supervisor_env
    run_install_stage "restart_services" restart_services
    run_install_stage "verify_installation" verify_installation

    log_install_summary "success" 0
    log_info "安装流程结束"
}


if [ "${CELIA_INSTALL_SH_NO_MAIN:-0}" != "1" ]; then
    main "$@"
fi
