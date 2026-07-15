import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import List
import stat
import requests
import zipfile
import tarfile
import shutil
import logging
import argparse
import tempfile
import json
import time
import subprocess
import signal


# 全局配置
zip_input_files_url = "https://h5hosting-drcn.dbankcdn.cn/cch5/HAG/D1E0CiDwLKSTla6LZEqEHDzqA/import.json"
write_upload_status_to_file_url = "/home/sandbox/.openclaw/upload_status.txt"
DOWNLOAD_FILE = "downloaded_file.zip"
EXTRACTED_DIR = "extracted_folder"
BASE_DIR = "/home/sandbox/.openclaw"
tmp_base_dir = "/home/sandbox/.openclaw/tmp"
TEMP_DIR = None

# 特殊处理的文件路径
OPENCLAW_FILE = "/home/sandbox/.openclaw/openclaw.json"  # 需要合并而非覆盖
EXTENSIONS_FILE = "/home/sandbox/.openclaw/extensions"

# 日志文件路径
LOG_DIR = "/tmp/logs"
LOG_FILE = os.path.join(LOG_DIR, f"{os.path.basename(__file__).replace('.py', '')}.log")

# 创建日志目录并设置权限
os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)

# 创建日志文件并设置权限（如果不存在）
if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, 'w') as f1:
        pass
    os.chmod(LOG_FILE, 0o600)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
    ]
)
logger = logging.getLogger(__name__)


def ensure_string_field(config: dict, path: str) -> bool:
    """将指定路径的字段转换为字符串（存在时）"""
    keys = path.split('.')
    current = config
    for key in keys[:-1]:
        if not isinstance(current, dict):
            return False
        current = current.get(key)
        if current is None:
            return False
    last_key = keys[-1]
    if isinstance(current, dict) and last_key in current:
        original = current[last_key]
        if not isinstance(original, str):
            current[last_key] = str(original)
            logger.info(f"✓ 转换: {path} = {original!r} -> {current[last_key]!r}")
        else:
            logger.info(f"✓ 已是字符串: {path} = {original!r}")
        return True
    else:
        logger.info(f"✗ 跳过不存在的路径: {path}")
        return False


def fix_config_strings(file_path: str) -> None:
    """
    读取 JSON 配置文件，将预定义的字段强制转换为字符串类型，然后保存回原文件。

    Args:
        file_path: JSON 文件的路径
    """
    # 需要确保为字符串的字段路径（可以根据需要修改）
    paths_to_fix = get_properties(zip_input_files_url, "paths_to_fix") or []

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        logger.info(f"错误：文件不存在 - {file_path}")
        return
    except json.JSONDecodeError as e:
        logger.info(f"错误：JSON 解析失败 - {e}")
        return

    logger.info(f"开始处理文件: {file_path}")
    for path in paths_to_fix:
        ensure_string_field(config, path)

    # 写回原文件
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    logger.info(f"完成！已更新文件: {file_path}")


def init_temp_dirs():
    global TEMP_DIR, DOWNLOAD_FILE, EXTRACTED_DIR
    os.makedirs(tmp_base_dir, mode=0o700, exist_ok=True)
    TEMP_DIR = tempfile.mkdtemp(prefix="recover_", dir=tmp_base_dir)
    DOWNLOAD_FILE = os.path.join(TEMP_DIR, "downloaded_file.zip")
    os.chmod(TEMP_DIR, 0o700)
    EXTRACTED_DIR = os.path.join(TEMP_DIR, "extracted_folder")
    logger.info(f"创建临时目录: {TEMP_DIR}")


# 添加帮助信息
def parse_arguments():
    parser = argparse.ArgumentParser(description="从指定 URL 下载压缩文件并覆盖指定路径。")
    parser.add_argument("--Authorization", type=str, required=True, help="用于下载的认证 token")
    parser.add_argument("--fileId", type=str, help="文件的ID，下载文件使用")
    parser.add_argument(
        "--recoverStrategy",
        type=int,
        help="claw配置文件损坏情况恢复策略 1、跳过openclaw.json 恢复，2、使用openclaw.json.bak恢复"
    )
    parser.add_argument("--allFileId", type=str, help="全量文件的ID，下载文件使用")
    parser.add_argument("--no-clean", action="store_true", help="不清理临时文件")
    return parser.parse_args()


def get_properties(url, setting_key):
    try:
        response = requests.get(url, stream=True, timeout=60)
        response.raise_for_status()
        # 解析 JSON 内容
        try:
            content = response.json()
        except requests.exceptions.JSONDecodeError:
            logger.error("下载的文件不是有效的 JSON 格式。")
            return None

        # 读配置
        result = content.get(setting_key)


        return result
    except Exception as e:
        return None


def clean_skip_files(extracted_dir, skip_paths, target_base_paths):
    """删除解压目录中需要跳过的文件/目录（已通过普通备份恢复）"""
    if not skip_paths:
        return

    for skip_path in skip_paths:
        # 找到跳过路径对应的解压目录中的路径
        for target_base in target_base_paths:
            if skip_path.startswith(target_base):
                # 计算相对路径（相对于 BASE_DIR）
                rel_path = os.path.relpath(skip_path, BASE_DIR)
                # 构造解压目录中的路径
                extracted_path = os.path.join(extracted_dir, rel_path)
                if os.path.exists(extracted_path):
                    if os.path.isdir(extracted_path):
                        shutil.rmtree(extracted_path)
                        logger.info(f"删除已恢复的目录: {extracted_path}")
                    else:
                        os.remove(extracted_path)
                        logger.info(f"删除已恢复的文件: {extracted_path}")


# 下载 JSON 文件并解析获取需要覆盖的绝对路径列表
def download_json_file(url, setting_key):
    try:
        if not url.startswith("http://") and not url.startswith("https://"):
            logging.error("JSON URL 格式不正确，必须以 http:// 或 https:// 开头。")
            return None

        # 获取 zip_input_files 列表
        zip_input_files = get_properties(url, setting_key)

        # 检查 zip_input_files 是否为列表且不为空
        if zip_input_files is None or not isinstance(zip_input_files, list) or not zip_input_files:
            logging.error("zip_input_files 为空或格式不正确，脚本终止。")
            return None

        # 检查路径是否为绝对路径
        valid_paths = []
        for path in zip_input_files:
            if not os.path.isabs(path):
                logging.error("路径必须是绝对路径: {}".format(path))
                return None
            valid_paths.append(path)

        if not valid_paths:
            logging.error("没有有效的路径可以恢复，脚本终止。 ")
            return None

        logging.info("JSON 文件解析成功，获取到需要覆盖的路径列表。")
        return valid_paths

    except requests.exceptions.RequestException as e:
        logging.error("下载 JSON 文件失败（网络问题）: {}".format(e))
    except Exception as e:
        logging.error("下载 JSON 文件失败: {}".format(e))
    return None


# 下载压缩文件
def download_file_by_auth_and_fileid(authorization, fileid):
    try:
        # 构造 URL
        cloud_namespace_base_url = get_properties(zip_input_files_url, "cloud_namespace_base_url")
        url = f"{cloud_namespace_base_url}/drive/v1/files/{fileid}?form=content"

        trace_id = str(uuid.uuid4())[:16]
        # 构造请求头
        headers = {
            "Authorization": f"Bearer {authorization}",
            "x-hw-trace-id": trace_id,
            "Cache-Control": "no-cache",
            "Accept": "image/jpeg"
        }

        # 获取目标目录的剩余空间
        # 获取当前磁盘空间
        if os.name == "posix":
            stat_vfs = os.statvfs('.')
            free_space = stat_vfs.f_frsize * stat_vfs.f_bavail
        elif os.name == "nt":
            total, used, free = shutil.disk_usage('.')
            free_space = free
        else:
            logging.error("不支持的系统类型")
            return False
        # 保留 10% 的空间作为容错
        available_space = free_space * 0.9

        # 发送 GET 请求
        response = requests.get(url, headers=headers, stream=True, verify=True, timeout=60)
        response.raise_for_status()

        # 获取文件大小（如果响应头中包含 Content-Length）
        file_size = int(response.headers.get('Content-Length', 0))
        if file_size > available_space:
            logging.error(
                "目标目录空间不足，压缩文件大小为 {} 字节，可用空间为 {} 字节。".format(file_size, available_space))
            return False

        # 保存文件
        with open(DOWNLOAD_FILE, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024):
                f.write(chunk)
        logging.info("文件已下载: {}".format(DOWNLOAD_FILE))

        return True

    except requests.exceptions.RequestException as e:
        logging.error("下载文件失败（网络问题）: {}".format(e))
    except Exception as e:
        logging.error("下载文件失败: {}".format(e))
    finally:
        pass

    return False


# 解压文件
def extract_file():
    if not os.path.exists(DOWNLOAD_FILE):
        logging.error("未找到下载的文件: {}".format(DOWNLOAD_FILE))
        return False

    try:
        if not os.path.exists(EXTRACTED_DIR):
            os.makedirs(EXTRACTED_DIR)
            logging.info("已创建解压目录: {}".format(EXTRACTED_DIR))

        if os.name == "posix":
            stat_vfs = os.statvfs(EXTRACTED_DIR)  # 改名避免与 stat 模块冲突
            free_space = stat_vfs.f_frsize * stat_vfs.f_bavail
        elif os.name == "nt":
            total, used, free = shutil.disk_usage(EXTRACTED_DIR)
            free_space = free
        else:
            logging.error("不支持的系统类型")
            return False

        # 保留 10% 的空间作为容错
        available_space = free_space * 0.9

        # 预估解压后文件的大小
        total_uncompressed_size = 0
        if DOWNLOAD_FILE.endswith(".zip"):
            with zipfile.ZipFile(DOWNLOAD_FILE, "r") as zip_ref:
                total_uncompressed_size = sum(file.file_size for file in zip_ref.infolist())
        elif DOWNLOAD_FILE.endswith(".tar.gz"):
            with tarfile.open(DOWNLOAD_FILE, "r:gz") as tar_ref:
                total_uncompressed_size = sum(file.size for file in tar_ref.getmembers())
        else:
            logging.error("不支持的压缩文件格式")
            return False

        # 比较解压后文件大小和可用空间
        if available_space < total_uncompressed_size:
            logging.error("目标目录空间不足，解压后文件大小为 {} 字节，可用空间为 {} 字节。".format(total_uncompressed_size, available_space))
            return False

        # 解压文件
        if DOWNLOAD_FILE.endswith(".zip"):
            with zipfile.ZipFile(DOWNLOAD_FILE, "r") as zip_ref:
                for member in zip_ref.namelist():
                    member_path = os.path.join(EXTRACTED_DIR, member)
                       # 安全检查：防止 Zip Slip 攻击
                    if not os.path.abspath(member_path).startswith(os.path.abspath(EXTRACTED_DIR)):
                        logging.warning(f"检测到危险路径，已跳过: {member}")
                        continue

                    # 获取文件信息
                    info = zip_ref.getinfo(member)

                    # 检查是否是符号链接（external_attr 高 4 位为 0xA 表示 S_IFLNK）
                    is_symlink = (info.external_attr >> 28) == 0xA

                    if is_symlink:
                        # 符号链接：读取目标路径并创建链接
                        link_target = zip_ref.read(member).decode('utf-8')

                        # 确保父目录存在
                        parent_dir = os.path.dirname(member_path)
                        if not os.path.exists(parent_dir):
                            os.makedirs(parent_dir)

                        # 如果目标已存在，先删除
                        if os.path.exists(member_path) or os.path.islink(member_path):
                            if os.path.isdir(member_path) and not os.path.islink(member_path):
                                shutil.rmtree(member_path)
                            else:
                                os.remove(member_path)

                        # 创建符号链接
                        os.symlink(link_target, member_path)
                        logging.info(f"创建符号链接: {member} -> {link_target}")

                    elif member.endswith('/'):
                        # 目录：创建目录
                        if not os.path.exists(member_path):
                            os.makedirs(member_path)

                    else:
                        # 普通文件：直接解压
                        zip_ref.extract(member, EXTRACTED_DIR)

                        # 恢复时间戳和权限
                        try:
                            extracted_path = os.path.join(EXTRACTED_DIR, member)
                            mtime = time.mktime(info.date_time + (0, 0, -1))
                            os.utime(extracted_path, (mtime, mtime))

                            if info.external_attr > 0:
                                mode = (info.external_attr >> 16) & 0o777
                                if mode > 0:
                                    os.chmod(extracted_path, mode)
                                    logging.debug("恢复权限 %s: %s", oct(mode), extracted_path)
                        except Exception as e:
                            logging.warning(f"恢复元数据失败 {member}: {e}")

        elif DOWNLOAD_FILE.endswith(".tar.gz"):
            with tarfile.open(DOWNLOAD_FILE, "r:gz") as tar_ref:
                for member in tar_ref.getmembers():
                    member_path = os.path.join(EXTRACTED_DIR, member.name)
                    if not os.path.abspath(member_path).startswith(os.path.abspath(EXTRACTED_DIR)):
                        logging.warning(f"检测到危险路径，已跳过: {member.name}")
                        continue
                    tar_ref.extract(member, path=EXTRACTED_DIR)

        else:
            logging.error("不支持的压缩文件格式")
            return False

        logging.info("文件已解压到: {}".format(EXTRACTED_DIR))
        return True

    except zipfile.BadZipFile:
        logging.error("压缩文件损坏（zip 格式错误）")
    except tarfile.ReadError:
        logging.error("压缩文件损坏（tar.gz 格式错误）")
    except tarfile.ExtractError as e:
        logging.error("解压失败（tar.gz 提取错误）: {}".format(e))
    except OSError as e:
        logging.error("文件操作失败（OSError）: {}".format(e))
    except IOError as e:
        logging.error("文件操作失败（IOError）: {}".format(e))
    except Exception as e:
        logging.error("解压失败: {}".format(e))
    finally:
        pass

    return False


def copytree_preserve_metadata(src, dst):
    """
    递归复制目录树，保留所有元数据（时间戳、权限）
    """
    if not os.path.exists(dst):
        os.makedirs(dst)
        # 恢复目录权限（新增）← 从这里开始是新增的
        try:
            src_stat = os.stat(src)
            os.chmod(dst, stat.S_IMODE(src_stat.st_mode))
        except Exception as e:
            logging.warning(f"恢复目录权限失败 {dst}: {e}")


    for item in os.listdir(src):
        src_path = os.path.join(src, item)
        dst_path = os.path.join(dst, item)

        if os.path.isdir(src_path):
            copytree_preserve_metadata(src_path, dst_path)
        else:
            # 复制文件并保留时间戳
            shutil.copy2(src_path, dst_path)

            # 恢复权限
            try:
                src_stat = os.stat(src_path)
                os.chmod(dst_path, src_stat.st_mode)
                # 恢复时间戳（访问时间和修改时间）
                os.utime(dst_path, (src_stat.st_atime, src_stat.st_mtime))
            except Exception as e:
                logging.warning(f"恢复元数据失败 {dst_path}: {e}")

            # 恢复目录本身的时间戳
    try:
        src_stat = os.stat(src)
        os.utime(dst, (src_stat.st_atime, src_stat.st_mtime))
    except Exception as e:
        logging.warning(f"恢复目录时间戳失败 {dst}: {e}")


def deep_equals(a, b):
    """递归比较两个值是否相等（支持字典、列表、基本类型）"""
    if isinstance(a, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        for k in a:
            if not deep_equals(a[k], b[k]):
                return False
        return True
    elif isinstance(a, list):
        if len(a) != len(b):
            return False
        for i in range(len(a)):
            if not deep_equals(a[i], b[i]):
                return False
        return True
    else:
        return a == b


def merge_json(file1_path: Path, file2_path: Path):
    """
    合并两个 JSON 文件，将 file2 中的 channels、plugins.entries、plugins.installs
    合并到 file1 中，但若 file1 中已存在相同的键，则保留 file1 的值。

    扩展：支持数组合并 - 若目标字段和源字段均为数组，则将源数组中不重复的元素追加到目标数组。

    Args:
        file1_path: 目标文件路径（将被修改或作为源）
        file2_path: 源文件路径
    """
    logger.info(f"[合并配置] 开始合并 JSON 文件")
    logger.info(f"[合并配置] 目标文件(本地): {file1_path}")
    logger.info(f"[合并配置] 源文件(备份): {file2_path}")

    # 如果目标文件不存在，直接复制源文件
    if not os.path.exists(file1_path):
        shutil.copy2(file2_path, file1_path)
        logger.info(f"目标文件不存在，直接复制: {file1_path}")
        return

    # 读取 JSON
    with open(file1_path, 'r', encoding='utf-8') as file1:
        data1 = json.load(file1)

    with open(file2_path, 'r', encoding='utf-8') as file2:
        data2 = json.load(file2)

    # 定义需要合并的路径（点分隔）
    paths = get_properties(zip_input_files_url, "merge_list_v411") or []
    total_added = 0  # 记录新增的配置项数量
    for path in paths:
        parts = path.split('.')

        # 从 data2 中提取源字典
        src = data2
        for part in parts:
            if part in src:
                src = src[part]
            else:
                src = None
                break
        if src is None:
            logger.info(f"[合并配置] 路径 '{path}' 在备份中不存在，跳过")
            continue

        # 在 data1 中定位目标字典，若中间路径缺失则创建空字典
        target = data1
        for part in parts[:-1]:
            if part not in target:
                target[part] = {}
            target = target[part]
        key = parts[-1]  # 最后一级的键名

        # 情况1：目标字段不存在 → 直接使用源值
        if key not in target:
            target[key] = src
            logger.info(f"[合并配置] 路径 '{path}' 本地不存在，直接使用备份值 (新增 {len(src) if isinstance(src, (dict, list)) else 1} 项)")
            if isinstance(src, dict):
                total_added += len(src)
            elif isinstance(src, list):
                total_added += len(src)
            else:
                total_added += 1
            continue

        # 情况2：目标字段存在，且均为字典 → 递归合并字典（保留本地键）
        if isinstance(target[key], dict) and isinstance(src, dict):
            added_keys = []
            for k, v in src.items():
                if k not in target[key]:
                    target[key][k] = v
                    added_keys.append(k)
            if added_keys:
                logger.info(f"[合并配置] 路径 '{path}' 新增字典键: {added_keys}")
                total_added += len(added_keys)
            else:
                logger.info(f"[合并配置] 路径 '{path}' 无新增键，保留本地配置")
            continue

        # 情况3：目标字段存在，且均为列表 → 数组合并（去重追加）
        if isinstance(target[key], list) and isinstance(src, list):
            added_items = []
            for item_src in src:
                # 检查 target 列表中是否已存在与 item_src 相等的元素
                exists = any(deep_equals(item, item_src) for item in target[key])
                if not exists:
                    target[key].append(item_src)
                    added_items.append(item_src)
            if added_items:
                logger.info(f"[合并配置] 路径 '{path}' 新增数组元素 {len(added_items)} 个")
                total_added += len(added_items)
            else:
                logger.info(f"[合并配置] 路径 '{path}' 无新增数组元素，保留本地配置")
            continue

        # 情况4：其他类型不匹配或非容器类型 → 保留本地值
        logger.info(f"[合并配置] 路径 '{path}' 类型不匹配或非字典/列表，保留本地值")

    # 输出结果
    out_path = file1_path
    with open(out_path, 'w', encoding='utf-8') as f_out:
        json.dump(data1, f_out, indent=2, ensure_ascii=False)

    logger.info(f"[合并配置] 合并完成，共新增 {total_added} 项配置")
    logger.info(f"[合并配置] 输出文件: {out_path}")


def clean_black_list_files(extracted_dir, black_list, target_base_paths):
    """删除解压目录中黑名单里的文件/目录"""
    if not black_list:
        return

    for black_path in black_list:
        # 找到黑名单路径对应的解压目录中的路径
        for target_base in target_base_paths:
            if black_path.startswith(target_base):
                # 计算相对路径（相对于 BASE_DIR，而不是 target_base）
                rel_path = os.path.relpath(black_path, BASE_DIR)
                # 构造解压目录中的路径
                extracted_path = os.path.join(extracted_dir, rel_path)
                if os.path.exists(extracted_path):
                    if os.path.isdir(extracted_path):
                        shutil.rmtree(extracted_path)
                        logger.info(f"删除黑名单目录: {extracted_path}")
                    else:
                        os.remove(extracted_path)
                        logger.info(f"删除黑名单文件: {extracted_path}")


def fix_permissions_recursive(path, tag="权限修复"):
    """
    递归修复权限：目录 755，文件 644
    """
    if os.path.islink(path):
        return  # 跳过符号链接

    if os.path.isdir(path):
        os.chmod(path, 0o755)
        logger.debug("[%s] 设置文件权限 755: %s", tag, path)
        for item in os.listdir(path):
            fix_permissions_recursive(os.path.join(path, item), tag)
    elif os.path.isfile(path):
        os.chmod(path, 0o644)
        logger.debug("[%s] 设置文件权限 644: %s", tag, path)


def merge_dirs_preserve_metadata(src, dst, _is_extensions_root=None):
    """
    将 src 目录下的内容合并到 dst 目录中，保留元数据。
    """
    # 确保目标目录存在
    if not os.path.exists(dst):
        os.makedirs(dst)
        shutil.copystat(src, dst)   # 复制源目录的权限等元数据

    # 判断是否是 extensions 根目录（只在第一次调用时判断，递归时传递）
    if _is_extensions_root is None:
        # 获取绝对路径并标准化
        abs_dst = os.path.abspath(dst)
        expected_extensions = os.path.abspath('/home/sandbox/.openclaw/extensions')
        _is_extensions_root = (abs_dst == expected_extensions)

    for item in os.listdir(src):
        # 如果是 extensions 根目录，跳过以 .openclaw-install-stage 开头的文件/目录
        if _is_extensions_root and item.startswith(".openclaw-install-stage"):
            logger.info(f"[extensions] 跳过安装阶段文件: {item}")
            continue

        src_path = os.path.join(src, item)
        dst_path = os.path.join(dst, item)

        if os.path.islink(src_path):
            # 符号链接：复制链接本身
            link_target = os.readlink(src_path)
            if os.path.lexists(dst_path):
                os.remove(dst_path)
            os.symlink(link_target, dst_path)
        elif os.path.isfile(src_path):
            shutil.copy2(src_path, dst_path)   # 复制文件并保留元数据
        elif os.path.isdir(src_path):
            # 递归处理子目录（传递 _is_extensions_root=False，只在根目录排除）
            merge_dirs_preserve_metadata(src_path, dst_path, _is_extensions_root=False)


def recover_special_path(special_path, tag="特殊处理", fix_permissions=False):
    target_abs = os.path.abspath(special_path)
    target_name = os.path.relpath(target_abs, BASE_DIR)
    source_path = os.path.join(EXTRACTED_DIR, target_name)

    if not os.path.exists(source_path):
        logger.info(f"[{tag}] 压缩包中不存在，跳过: {special_path}")
        return False




    logger.info(f"[{tag}] 开始恢复: {special_path}")

    # 先删除目标（处理符号链接情况）
    if os.path.islink(target_abs):
        os.unlink(target_abs)  # 删除符号链接本身
        logger.info(f"[{tag}] 已删除符号链接: {target_abs}")
    elif os.path.exists(target_abs):
        if os.path.isdir(target_abs) and os.path.isdir(source_path):
            pass  # 不删除，直接进入合并逻辑
        else:
            # 类型不匹配或其他情况，删除目标
            if os.path.isdir(target_abs):
                shutil.rmtree(target_abs)
            else:
                os.remove(target_abs)
            logger.info(f"[{tag}] 已删除: {target_abs}")

    if os.path.isfile(source_path):
        target_dir = os.path.dirname(target_abs)
        if not os.path.exists(target_dir):
            os.makedirs(target_dir)
        shutil.copy2(source_path, target_abs)
        logger.info(f"[{tag}] 已覆盖文件: {target_abs}")
    elif os.path.isdir(source_path):
        if not os.path.exists(target_abs):
            os.makedirs(target_abs)
        # 使用 copytree_preserve_metadata 保留权限
        merge_dirs_preserve_metadata(source_path, target_abs)
        logger.info(f"[{tag}] 已覆盖目录: {target_abs}")

    # 修复权限
    if fix_permissions:
        fix_permissions_recursive(target_abs, tag)


    return True



def overwrite_file_and_dir_special(recover_strategy=0):
    # 特殊处理文件
    # 1. 合并 openclaw.json（需要合并逻辑）
    if recover_strategy == 1:
        logger.info("恢复策略1 跳过合并 openclaw.json")
    else:
        logger.info("合并openclaw.json文件")
        target_openclaw = os.path.abspath(OPENCLAW_FILE)
        target_openclaw_name = os.path.relpath(target_openclaw, BASE_DIR)
        source_openclaw_path = os.path.join(EXTRACTED_DIR, target_openclaw_name)
        # 检查压缩包中是否存在该文件
        if os.path.exists(source_openclaw_path):
            logger.info(f"检测到特殊配置文件，使用合并逻辑: {target_openclaw}")
            logger.info(f"本地文件: {target_openclaw}")
            logger.info(f"备份文件: {source_openclaw_path}")
            merge_json(Path(target_openclaw), Path(source_openclaw_path))
            fix_config_strings(target_openclaw)
        else:
            logger.info(f"压缩包中不存在 {OPENCLAW_FILE}，跳过合并逻辑")

    # 2. 恢复 extensions 目录（直接覆盖）
    recover_special_path(EXTENSIONS_FILE, "extensions", fix_permissions=True)

    special_list = get_properties(zip_input_files_url, "special_list") or []
    for special_file in special_list:
        recover_special_path(special_file, special_file)


def overwrite_target_paths(target_paths, black_list_for_recover_files, recover_strategy=0):
    if not os.path.exists(EXTRACTED_DIR):
        logging.error("未找到解压后的文件夹: {}".format(EXTRACTED_DIR))
        return False

    for target_path in target_paths:
        target_abs = os.path.abspath(target_path)
        target_name = os.path.relpath(target_abs, BASE_DIR)
        source_path = os.path.join(EXTRACTED_DIR, target_name)

        # 检查压缩包里是否存在
        if not os.path.exists(source_path):
            logging.warning("解压后的内容中没有找到: {}".format(target_name))
            continue

        # 判断是目录还是文件（以压缩包里的为准）
        if os.path.isdir(source_path):
            # 目录：只遍历一层，替换压缩包里有的文件/目录
            # 如果磁盘上不存在目标目录，先创建
            if not os.path.exists(target_abs):
                os.makedirs(target_abs)
                logging.info("已创建目标目录: {}".format(target_abs))

            # 遍历压缩包里的目录（只一层）
            for item in os.listdir(source_path):
                src_item = os.path.join(source_path, item)
                dst_item = os.path.join(target_abs, item)

                if os.path.isdir(src_item):
                    # 子目录：递归合并，只覆盖存在的文件，保留目标目录中其他文件
                    if os.path.exists(dst_item):
                        # 递归合并目录
                        copytree_preserve_metadata(src_item, dst_item)
                    else:
                        shutil.copytree(src_item, dst_item)
                    logging.info("已替换目录: {}".format(dst_item))
                else:
                    # 文件：直接替换
                    dst_dir = os.path.dirname(dst_item)
                    if not os.path.exists(dst_dir):
                        os.makedirs(dst_dir)
                    shutil.copy2(src_item, dst_item)
                    logging.info("已替换文件: {}".format(dst_item))
        else:
            # 文件：直接覆盖
            target_dir = os.path.dirname(target_abs)
            if not os.path.exists(target_dir):
                os.makedirs(target_dir)
                logging.info("已创建目标目录: {}".format(target_dir))
            shutil.copy2(source_path, target_abs)
            logging.info("已覆盖: {}".format(target_abs))

    special_list_recover_switch = get_properties(zip_input_files_url, "special_list_recover_switch") or False
    if special_list_recover_switch:
        overwrite_file_and_dir_special(recover_strategy)
    return True


# 清理临时文件
def clean_up():
    if TEMP_DIR and os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)
    logging.info(f"已删除临时目录: {TEMP_DIR}")


def recover_sessions_dir():
    """
    恢复 sessions 目录（普通备份中额外打包的当天 sessions）
    sessions 目录结构：agents/main/sessions/<session_id>/
    """
    SESSIONS_DIR_NAME = "agents/main/sessions"
    source_sessions_dir = os.path.join(EXTRACTED_DIR, SESSIONS_DIR_NAME)
    target_sessions_dir = os.path.join(BASE_DIR, SESSIONS_DIR_NAME)

    if not os.path.exists(source_sessions_dir):
        logger.info(f"[Sessions恢复] 压缩包中无 sessions 目录，跳过")
        return True

    logger.info(f"[Sessions恢复] 开始恢复 sessions 目录")

    # 确保目标目录存在
    if not os.path.exists(target_sessions_dir):
        os.makedirs(target_sessions_dir)
        logger.info(f"[Sessions恢复] 创建目标目录: {target_sessions_dir}")

    # 遍历压缩包中的 sessions 子目录
    for item in os.listdir(source_sessions_dir):
        src_item = os.path.join(source_sessions_dir, item)
        dst_item = os.path.join(target_sessions_dir, item)

        if os.path.isdir(src_item):
            # session 子目录：直接覆盖（合并）
            if os.path.exists(dst_item):
                copytree_preserve_metadata(src_item, dst_item)
                logger.info(f"[Sessions恢复] 已合并目录: {dst_item}")
            else:
                shutil.copytree(src_item, dst_item)
                logger.info(f"[Sessions恢复] 已恢复目录: {dst_item}")
        else:
            # 文件：直接覆盖
            shutil.copy2(src_item, dst_item)
            logger.info(f"[Sessions恢复] 已恢复文件: {dst_item}")

    logger.info(f"[Sessions恢复] sessions 目录恢复完成")
    return True


def do_recover_by_config(authorization, file_id, config_key, skip_switch=False, recover_strategy=0):
    """
    根据配置恢复文件
    :param file_id: 云空间文件ID
    :param config_key: 配置键名 (zip_input_files 或 zip_input_all_files)
    :return: True/False
    """
    # 1. 获取恢复目录配置
    target_paths = download_json_file(zip_input_files_url, config_key)
    if not target_paths:
        logger.error(f"错误：获取 {config_key} 配置失败")
        return False

    # 2. 下载文件
    if not download_file_by_auth_and_fileid(authorization, file_id):
        logger.error(f"错误：下载文件失败")
        return False

    # 3. 解压
    if not extract_file():
        logger.error(f"错误：解压失败")
        return False

    # 4. 清理黑名单文件
    black_list_for_recover_files = get_properties(zip_input_files_url, "black_list_for_recover_files") or []
    clean_black_list_files(EXTRACTED_DIR, black_list_for_recover_files, target_paths)
    logger.info("黑名单文件清理完成")

    # 5. 清理已恢复的文件（全量+普通场景）
    if skip_switch:
        skip_paths = get_properties(zip_input_files_url, "zip_input_files") or []
        clean_skip_files(EXTRACTED_DIR, skip_paths, target_paths)
        logger.info("已恢复文件清理完成")

    # 6. 恢复文件
    if not overwrite_target_paths(target_paths, black_list_for_recover_files, recover_strategy):
        logger.error(f"错误：恢复文件失败")
        return False

    # 7. 恢复 sessions 目录（仅普通备份）
    if config_key == "zip_input_files":
        if not recover_sessions_dir():
            logger.warning(f"警告：sessions 目录恢复失败，但继续执行")

    return True


def write_status_to_file():
    """
    将上传完成状态写入文件，格式为JSON对象 {"status": "success"}
    :return: True（成功）/ False（失败）
    """
    try:

        # 获取当前UTC时间
        current_utc_time = datetime.utcnow()
        beijing_time = current_utc_time + timedelta(hours=8)

        # 将时间格式化为字符串，例如："2024-07-23T12:34:56Z"
        formatted_utc_time = beijing_time.strftime("%Y%m%d%H%M%S")
        # 写入文件
        with open(write_upload_status_to_file_url, 'w', encoding='utf-8') as file:
            file.write(formatted_utc_time)

        logger.info(f"Success: Data written to upload_status success")
        return True

    except Exception as e:
        logger.error(f"An error occurred: {e}")
        return False


# 停止supervisor 进程
def stop_supervisor():
    """停止 supervisor 进程"""
    try:
        subprocess.run(
            ["python3", "-m", "supervisor.supervisorctl", "-c", "/home/sandbox/supervisord.conf", "stop", "all"],
            check=False,
            timeout=5,
            capture_output=True
            )
        logger.info("已通过 supervisorctl 停止所有子进程")
        return True
    except Exception as e:
        logger.warning(f"supervisorctl stop all 失败: {e}")
        return False


# 检查gateway进程关闭
def check_gateway_stopped():
    """确认 openclaw-gateway 进程已停止，如果还在运行则尝试杀死"""
    try:
        for i in range(10):
            result = subprocess.run(["pgrep", "-f", "openclaw-gateway"], capture_output=True, text=True, timeout=5)
            if not result.stdout.strip():
                logger.info("openclaw-gateway 进程已停止")
                return True

            # 主动杀死进程
            pids = [p for p in result.stdout.strip().split('\n') if p]
            if pids and i == 0:
                logger.info(f"发现 openclaw-gateway 进程 PIDs: {pids}，尝试停止...")
                for pid in pids:
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                    except Exception as e:
                        logger.error("停止 openclaw-gateway 进程失败...")
                        pass

            logger.info(f"等待 openclaw-gateway 进程停止... ({i+1}/10)")
            time.sleep(1)

        # 超时后 SIGKILL 强杀
        result = subprocess.run(["pgrep", "-f", "openclaw-gateway"], capture_output=True, text=True, timeout=5)
        pids = [p for p in result.stdout.strip().split('\n') if p]
        if pids:
            logger.warning("openclaw-gateway 进程仍在运行，尝试 SIGKILL")
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except Exception as e:
                    logger.error("停止 openclaw-gateway 进程失败...")
                    pass
            time.sleep(2)

        # 最终检查
        result = subprocess.run(["pgrep", "-f", "openclaw-gateway"], capture_output=True, text=True, timeout=5)
        if not result.stdout.strip():
            logger.info("openclaw-gateway 进程已停止")
            return True

        logger.warning("openclaw-gateway 进程仍在运行")
        return False
    except Exception as e:
        logger.error(f"检查 openclaw-gateway 进程失败: {e}")
        return False


# 检查gateway进程关闭
def check_openclaw_stopped():
    """确认 openclaw 进程已停止，如果还在运行则尝试杀死"""
    try:
        for i in range(10):
            result = subprocess.run(["pgrep", "-x", "openclaw"], capture_output=True, text=True, timeout=5)
            if not result.stdout.strip():
                logger.info("openclaw 进程已经停止")
                return True

            # 主动杀死进程
            pids = [p for p in result.stdout.strip().split('\n') if p]
            if pids and i == 0:
                logger.info(f"发现 openclaw 进程 PIDs: {pids}，尝试停止...")
                for pid in pids:
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                    except Exception as e:
                        logger.error("停止 openclaw进程失败...")
                        pass

            logger.info(f"等待 openclaw 进程停止中... ({i+1}/10)")
            time.sleep(1)

        # 超时后 SIGKILL 强杀
        result = subprocess.run(["pgrep", "-x", "openclaw"], capture_output=True, text=True, timeout=5)
        pids = [p for p in result.stdout.strip().split('\n') if p]
        if pids:
            logger.warning("openclaw 进程仍在运行中，尝试 SIGKILL")
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except Exception as e:
                    logger.error("停止 openclaw 进程失败了...")
                    pass
            time.sleep(2)

        # 最终检查
        result = subprocess.run(["pgrep", "-x", "openclaw"], capture_output=True, text=True, timeout=5)
        if not result.stdout.strip():
            logger.info("openclaw 进程已经停止")
            return True

        logger.warning("openclaw 进程仍在运行中")
        return False
    except Exception as e:
        logger.error(f"检查 openclaw 进程失败: {e}")
        return False


# 重启supervisor
def start_supervisor():
    """启动 supervisor 进程"""
    try:
        logger.info("正在启动 supervisord 进程...")
        subprocess.Popen(
            ["python3", "-m", "supervisor.supervisorctl", "-c", "/home/sandbox/supervisord.conf", "start", "all"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd="/home/sandbox"
        )
        # 等待进程启动（最多10秒）
        for _ in range(10):
            time.sleep(1)
            result = os.popen("ps aux | grep 'supervisor.supervisord' | grep -v grep").read()
            if result.strip():
                logger.info("supervisord 进程已启动")
                return True
        logger.warning("supervisord 进程启动超时")
        return False
    except Exception as e:
        logger.error(f"启动 supervisord 进程失败: {e}")
        return False


# 主函数
def main():
    args = parse_arguments()
    # 参数校验
    if not args.fileId and not args.allFileId:
        logger.error("错误：必须提供 --fileId 或 --allFileId 至少一个参数")
        print(json.dumps({"status": "error", "message": "必须提供 --fileId 或 --allFileId 至少一个参数"}))
        sys.exit(1)

    # 步骤1: 停止 supervisor 进程
    stop_success = stop_supervisor()

    # 步骤2: 确认 openclaw-gateway 进程已停止
    gateway_stopped = check_gateway_stopped()
    check_openclaw_stopped()

    need_restart = stop_success and gateway_stopped

    recover_strategy = args.recoverStrategy or 0

    init_temp_dirs()
    result = {
        "status": "success",
        "mode": "recover",
        "file_id": args.fileId,
        "all_file_id": args.allFileId
    }
    # 步骤3: 执行恢复
    if args.allFileId and args.fileId:
        # 场景 3：两个都有，先恢复普通，再恢复全量中不包含普通的部分
        try:
            try:
                logger.info("开始恢复普通备份...")
                if not do_recover_by_config(
                    args.Authorization,
                    args.fileId,
                    "zip_input_files",
                    False,
                    recover_strategy
                ):
                    logger.error("普通备份恢复失败")
                    result["status"] = "error"
                    result["message"] = "普通备份恢复失败"
                    print(json.dumps(result))

                # 写入上传结果到文件
                write_status_to_file()
            finally:
                logger.info("end")
                # 步骤4: 重启 supervisor 进程
                if need_restart:
                    start_supervisor()
                else:
                    logger.info("无需重新启动supervisor")

            logger.info("开始恢复全量备份...")
            if not do_recover_by_config(
                args.Authorization,
                args.allFileId,
                "zip_input_all_files",
                True,
                recover_strategy
            ):
                logger.error("全量备份恢复失败")
                result["status"] = "error"
                result["message"] = "全量备份恢复失败"
                print(json.dumps(result))
                sys.exit(1)
        finally:
            # 清理临时文件（除非指定 --no-clean）
            clean_up()

    elif args.allFileId:
        # 场景 2：只有全量
        try:
            logger.info("开始恢复全量备份...")
            if not do_recover_by_config(
                args.Authorization,
                args.allFileId,
                "zip_input_all_files",
                False,
                recover_strategy
            ):
                logger.error("恢复失败")
                result["status"] = "error"
                result["message"] = "恢复失败"
                print(json.dumps(result))
                sys.exit(1)
            # 写入上传结果到文件
            write_status_to_file()
        finally:
            logger.info("end")
            # 步骤4: 重启 supervisor 进程
            if need_restart:
                start_supervisor()
            else:
                logger.info("无需重新启动supervisor")
            # 清理临时文件（除非指定 --no-clean）
            clean_up()

    else:
        # 场景 1：只有普通
        try:
            logger.info("开始恢复普通备份...")
            if not do_recover_by_config(
                args.Authorization,
                args.fileId,
                "zip_input_files",
                False,
                recover_strategy
            ):
                logger.error("恢复失败")
                result["status"] = "error"
                result["message"] = "恢复失败"
                print(json.dumps(result))
                sys.exit(1)
            # 写入上传结果到文件
            write_status_to_file()
        finally:
            logger.info("end")
            # 步骤4: 重启 supervisor 进程
            if need_restart:
                start_supervisor()
            else:
                logger.info("无需重新启动supervisor")
            # 清理临时文件（除非指定 --no-clean）
            clean_up()

    print(json.dumps(result))
    logger.info("恢复完成！")


if __name__ == "__main__":
    main()