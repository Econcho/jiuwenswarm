"""
OpenClaw 选择性恢复脚本
- 从云空间下载全量备份
- 按配置规则有选择性地恢复文件
- 支持层级配置和 exclude 规则
"""

import argparse
import json
import os
import sys
import zipfile
import tarfile
import shutil
import uuid
import tempfile
import logging
import requests
import signal
import subprocess
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List
import stat

# 全局配置
CONFIG_URL = "https://h5hosting-drcn.dbankcdn.cn/cch5/HAG/D1E0CiDwLKSTla6LZEqEHDzqA/import.json"
BASE_DIR = "/home/sandbox"
OPENCLAW_DIR = "/home/sandbox/.openclaw"
LOG_DIR = "/tmp/logs"
LOG_FILE = os.path.join(LOG_DIR, "selective_recover.log")
SUPERVISOR_CONF = "/home/sandbox/supervisord.conf"
RESULT_FILE = os.path.join(OPENCLAW_DIR, "recover_result.json")

# 临时目录
TEMP_DIR = None
DOWNLOAD_FILE = None
EXTRACTED_DIR = None

# merge_md.py 脚本路径
MERGE_SCRIPT_PATH = "/home/sandbox/merge_md.py"

# workspace 目录（md_merge_files 中的路径相对于此目录）
WORKSPACE_DIR = "/home/sandbox/.openclaw/workspace"

# 创建日志目录并配置日志
os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, 'w') as f:
        pass
    os.chmod(LOG_FILE, 0o600)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8')
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
    paths_to_fix = get_properties(CONFIG_URL, "paths_to_fix") or []

    try:
        with open(file_path, "r", encoding="utf-8") as file1:
            config = json.load(file1)
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
    with open(file_path, "w", encoding="utf-8") as file1:
        json.dump(config, file1, ensure_ascii=False, indent=2)
    logger.info(f"完成！已更新文件: {file_path}")


def parse_arguments():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="OpenClaw 选择性恢复脚本")
    parser.add_argument("--jsonStr", type=str, required=True, help="JSON 字符串参数，包含 Authorization 和 fileId")
    parser.add_argument("--no-clean", action="store_true", help="不清理临时文件")
    return parser.parse_args()


def init_temp_dirs():
    """初始化临时目录"""
    global TEMP_DIR, DOWNLOAD_FILE, EXTRACTED_DIR

    tmp_base_dir = os.path.join(OPENCLAW_DIR, "tmp")
    os.makedirs(tmp_base_dir, mode=0o700, exist_ok=True)

    TEMP_DIR = tempfile.mkdtemp(prefix="selective_recover_", dir=tmp_base_dir)
    os.chmod(TEMP_DIR, 0o700)

    DOWNLOAD_FILE = os.path.join(TEMP_DIR, "backup.zip")
    EXTRACTED_DIR = os.path.join(TEMP_DIR, "extracted")

    logger.info(f"创建临时目录: {TEMP_DIR}")


def get_properties(url, settingKey):
    """从远程 JSON 获取配置"""
    try:
        responseObj = requests.get(url, stream=True, timeout=60)
        responseObj.raise_for_status()
        contentJson = responseObj.json()
        resultObj = contentJson.get(settingKey)
        logger.info(f"获取配置 {settingKey}: {'成功' if resultObj else '失败'}")
        return resultObj
    except Exception as e:
        logger.error(f"获取配置失败: {e}")
        return None


def stop_supervisor():
    """停止 supervisor 进程"""
    # 方法1: 先用 supervisorctl 优雅停止子进程
    try:
        subprocess.run(
            ["python3", "-m", "supervisor.supervisorctl", "-c", SUPERVISOR_CONF, "stop", "all"],
            check=False, timeout=5, capture_output=True
        )
        logger.info("已通过 supervisorctl 停止所有子进程")
        return True
    except Exception as e:
        logger.warning(f"supervisorctl stop all 失败: {e}")
        return False


def check_gateway_stopped():
    """确认 openclaw-gateway 进程已停止"""
    try:
        for i in range(10):
            result = subprocess.run(
                ["pgrep", "-f", "openclaw-gateway"],
                capture_output=True, text=True, timeout=5
            )
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
                    except Exception:
                        logger.error("停止 openclaw-gateway 进程失败...")
                        pass

            logger.info(f"等待 openclaw-gateway 进程停止... ({i+1}/10)")
            time.sleep(1)

        # 超时后 SIGKILL 强杀
        result = subprocess.run(
            ["pgrep", "-f", "openclaw-gateway"],
            capture_output=True, text=True, timeout=5
        )
        pids = [p for p in result.stdout.strip().split('\n') if p]
        if pids:
            logger.warning("openclaw-gateway 进程仍在运行，尝试 SIGKILL")
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except Exception:
                    logger.error("停止 openclaw-gateway 进程失败...")
                    pass
            time.sleep(2)

        # 最终检查
        result = subprocess.run(
            ["pgrep", "-f", "openclaw-gateway"],
            capture_output=True, text=True, timeout=5
        )
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
            result = subprocess.run(
                ["pgrep", "-f", "supervisor.supervisord"],
                capture_output=True, text=True, timeout=5
            )
            if result.stdout.strip():
                logger.info("supervisord 进程已启动")
                return True
        logger.warning("supervisord 进程启动超时")
        return False
    except Exception as e:
        logger.error(f"启动 supervisord 进程失败: {e}")
        return False


def download_file(authorization, file_id, is_security=False):
    """从云空间下载文件"""
    try:
        # 获取云空间基础 URL（根据 is_security 选择不同的配置项）
        if is_security:
            cloud_namespace_base_url = get_properties(CONFIG_URL, "cloud_namespace_security_base_url")
        else:
            cloud_namespace_base_url = get_properties(CONFIG_URL, "cloud_namespace_base_url")
        if not cloud_namespace_base_url:
            logger.error("获取云空间基础 URL 失败")
            return False

        url = f"{cloud_namespace_base_url}/drive/v1/files/{file_id}?form=content"
        trace_id = str(uuid.uuid4())[:16]

        headers = {
            "Authorization": f"Bearer {authorization}",
            "x-hw-trace-id": trace_id,
            "Cache-Control": "no-cache"
        }

        # 检查磁盘空间
        if os.name == "posix":
            stat_vfs = os.statvfs('.')
            free_space = stat_vfs.f_frsize * stat_vfs.f_bavail
        else:
            total, used, free = shutil.disk_usage('.')
            free_space = free

        available_space = free_space * 0.9

        # 下载文件
        response = requests.get(url, headers=headers, stream=True, verify=True, timeout=60)
        response.raise_for_status()

        file_size = int(response.headers.get('Content-Length', 0))
        if file_size > available_space:
            logger.error(f"磁盘空间不足，需要 {file_size} 字节，可用 {available_space} 字节")
            return False

        with open(DOWNLOAD_FILE, "wb") as file:
            for chunk in response.iter_content(chunk_size=8192):
                file.write(chunk)

        # 获取实际下载的文件大小
        actual_size = os.path.getsize(DOWNLOAD_FILE)
        logger.info(f"文件下载成功: {DOWNLOAD_FILE} ({actual_size} 字节)")
        return True

    except Exception as e:
        logger.error(f"下载文件失败: {e}")
        return False


def extract_file():
    """解压下载的文件"""
    if not os.path.exists(DOWNLOAD_FILE):
        logger.error(f"下载文件不存在: {DOWNLOAD_FILE}")
        return False

    try:
        os.makedirs(EXTRACTED_DIR, exist_ok=True)

        # 检查磁盘空间
        if os.name == "posix":
            stat_vfs = os.statvfs(EXTRACTED_DIR)
            free_space = stat_vfs.f_frsize * stat_vfs.f_bavail
        else:
            total, used, free = shutil.disk_usage(EXTRACTED_DIR)
            free_space = free

        available_space = free_space * 0.9

        # 估算解压后大小 - 添加错误处理，跳过损坏的条目
        total_uncompressed_size = 0
        if DOWNLOAD_FILE.endswith(".zip"):
            try:
                with zipfile.ZipFile(DOWNLOAD_FILE, "r") as zip_ref:
                    for info in zip_ref.infolist():
                        try:
                            total_uncompressed_size += info.file_size
                        except Exception:
                            pass  # 跳过损坏的条目
            except Exception as e:
                logger.warning(f"估算解压大小时遇到问题: {e}，将跳过空间检查")
                total_uncompressed_size = 0  # 无法估算时跳过空间检查
        else:
            logger.error("不支持的压缩格式")
            return False

        if total_uncompressed_size > 0 and available_space < total_uncompressed_size:
            logger.error(f"磁盘空间不足，解压需要 {total_uncompressed_size} 字节")
            return False

        # 解压文件 - 添加错误处理，跳过损坏的文件
        extracted_count = 0
        skipped_count = 0
        with zipfile.ZipFile(DOWNLOAD_FILE, "r") as zip_ref:
            for member in zip_ref.namelist():
                try:
                    member_paths = os.path.join(EXTRACTED_DIR, member)

                    # Zip Slip 防护
                    if not os.path.abspath(member_paths).startswith(os.path.abspath(EXTRACTED_DIR)):
                        logger.warning(f"检测到危险路径，已跳过: {member}")
                        skipped_count += 1
                        continue

                    info = zip_ref.getinfo(member)

                    # 检查是否是符号链接
                    is_symlink = (info.external_attr >> 28) == 0xA

                    if is_symlink:
                        symlink_target = zip_ref.read(member).decode('utf-8')
                        parent_dir_obj = os.path.dirname(member_paths)
                        if not os.path.exists(parent_dir_obj):
                            os.makedirs(parent_dir_obj)
                        if os.path.exists(member_paths) or os.path.islink(member_paths):
                            if os.path.isdir(member_paths) and not os.path.islink(member_paths):
                                shutil.rmtree(member_paths)
                            else:
                                os.remove(member_paths)
                        os.symlink(symlink_target, member_paths)
                        logger.debug("创建符号链接: %s -> %s", member, symlink_target)
                        extracted_count += 1
                    elif member.endswith('/'):
                        if not os.path.exists(member_paths):
                            os.makedirs(member_paths)
                        # 恢复目录权限和时间戳
                        try:
                            if info.external_attr > 0:
                                mode = (info.external_attr >> 16) & 0o777
                                if mode > 0:
                                    os.chmod(member_paths, mode)
                            # 使用北京时区转换时间戳
                            BJ_TZ = timezone(timedelta(hours=8))
                            dt = datetime(*info.date_time, tzinfo=BJ_TZ)
                            mtime = dt.timestamp()
                            os.utime(member_paths, (mtime, mtime))
                        except Exception as e:
                            logger.warning(f"恢复目录元数据失败 {member}: {e}")
                        extracted_count += 1
                    else:
                        zip_ref.extract(member, EXTRACTED_DIR)
                        extracted_path = os.path.join(EXTRACTED_DIR, member)
                        # 恢复权限
                        try:
                            if info.external_attr > 0:
                                mode = (info.external_attr >> 16) & 0o777
                                if mode > 0:
                                    os.chmod(extracted_path, mode)
                        except Exception as e:
                            logger.warning(f"恢复权限失败 {member}: {e}")
                        # 恢复时间戳（使用北京时区）
                        try:
                            BJ_TZ = timezone(timedelta(hours=8))
                            dt = datetime(*info.date_time, tzinfo=BJ_TZ)
                            mtime = dt.timestamp()
                            os.utime(extracted_path, (mtime, mtime))
                        except Exception as e:
                            logger.warning(f"恢复时间戳失败 {member}: {e}")
                        extracted_count += 1
                except Exception as e:
                    logger.warning(f"解压文件 {member} 失败: {e}，跳过")
                    skipped_count += 1
                    continue

        logger.info(f"文件解压成功: {EXTRACTED_DIR} (解压 {extracted_count} 个文件，跳过 {skipped_count} 个损坏文件)")

        # 解压完成后删除压缩包释放空间
        try:
            os.remove(DOWNLOAD_FILE)
            logger.info(f"已删除压缩包释放空间: {DOWNLOAD_FILE}")
        except Exception as e:
            logger.warning(f"删除压缩包失败: {e}")

        return True

    except Exception as e:
        logger.error(f"解压文件失败: {e}")
        return False


def should_exclude(path, exclude_file_list, exclude_prefix_list):
    """检查路径是否在排除列表中
       配置中有两种排除，一种全路径排除，一种前缀模糊排除
    """
    for exclude_path in exclude_file_list:
        if path == exclude_path or path.startswith(exclude_path + os.sep):
            return True

    for exclude_item in exclude_prefix_list:
        # 支持字典格式，支持模糊匹配
        if path.startswith(exclude_item):
            return True
    return False


def fix_permissions_recursive(path, tag="权限修复"):
    """
    递归修复权限：目录 755，文件 644
    """
    if os.path.islink(path):
        return  # 跳过符号链接

    if os.path.isdir(path):
        os.chmod(path, 0o755)
        logger.debug("[%s] 设置目录权限 755: %s", tag, path)
        for item in os.listdir(path):
            fix_permissions_recursive(os.path.join(path, item), tag)
    elif os.path.isfile(path):
        os.chmod(path, 0o644)
        logger.debug("[%s] 设置文件权限 644: %s", tag, path)


def copy_with_metadata(src, dst):
    """复制文件或目录，保留元数据"""
    if os.path.islink(src):
        # 符号链接
        link_target = os.readlink(src)
        if os.path.exists(dst) or os.path.islink(dst):
            if os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            else:
                os.remove(dst)
        os.symlink(link_target, dst)
        logger.debug("创建符号链接: %s -> %s", dst, link_target)
    elif os.path.isfile(src):
        # 普通文件
        dst_dir = os.path.dirname(dst)
        if not os.path.exists(dst_dir):
            os.makedirs(dst_dir, exist_ok=True)
        shutil.copy2(src, dst)
        logger.debug("复制文件: %s -> %s", src, dst)
    elif os.path.isdir(src):
        # 目录 - 递归复制目录及其所有内容
        logger.debug("递归复制目录: %s -> %s", src, dst)
        # 如果目标目录已存在，需要先删除
        if os.path.exists(dst):
            if os.path.islink(dst):
                os.remove(dst)
            else:
                shutil.rmtree(dst)
        # 使用 copytree 递归复制，保留权限和元数据
        shutil.copytree(src, dst, symlinks=True, copy_function=shutil.copy2)
        # 递归恢复所有目录的时间戳（从 src 复制到 dst）
        try:
            for src_root, _, _ in os.walk(src):
                # 计算对应的 dst 路径
                rel_path = os.path.relpath(src_root, src)
                dst_root = os.path.join(dst, rel_path) if rel_path != '.' else dst

                # 恢复目录时间戳
                src_stat = os.stat(src_root)
                os.utime(dst_root, (src_stat.st_atime, src_stat.st_mtime))
        except Exception as e:
            logger.warning(f"恢复目录时间戳失败 {dst}: {e}")
        logger.debug("目录复制完成: %s -> %s", src, dst)


def process_recover_config(config_item, extracted_base):
    """处理单个恢复配置项"""
    root = config_item.get("root", "")
    exclude_file_list = config_item.get("exclude", [])
    exclude_prefix_list = config_item.get("exclude_prefix", [])

    logger.info(f"处理配置: root={root}, exclude数量={len(exclude_file_list)}")
    logger.info(f"处理配置: root={root}, exclude_prefix数量={len(exclude_prefix_list)}")

    # 计算在解压目录中的路径
    extracted_root = os.path.join(extracted_base, root.lstrip('/'))

    if not os.path.exists(extracted_root):
        logger.warning(f"解压目录中不存在: {extracted_root}")
        return 0

    # 打印当前目录下的内容
    try:
        items = os.listdir(extracted_root)
        dirs = []
        files = []
        symlinks = []

        for item in items:
            item_path = os.path.join(extracted_root, item)
            if os.path.islink(item_path):
                symlinks.append(item)
            elif os.path.isdir(item_path):
                dirs.append(item)
            else:
                files.append(item)

        logger.info(f"当前目录内容: 子目录={sorted(dirs)}, 文件={sorted(files)}, 符号链接={sorted(symlinks)}")
    except Exception as e:
        logger.warning(f"获取目录内容失败: {e}")

    processed_count = 0

    # 遍历 root 目录下的所有内容
    for item in os.listdir(extracted_root):
        src_path = os.path.join(extracted_root, item)
        # 计算相对路径（相对于 root）
        rel_path = os.path.relpath(src_path, extracted_root)
        # 计算完整路径（相对于系统根目录）
        full_path = os.path.join(root, rel_path)

        # 检查是否在排除列表中
        if should_exclude(full_path, exclude_file_list, exclude_prefix_list):
            logger.info(f"跳过排除项: {full_path}")
            continue

        # 计算目标路径
        dst_path = os.path.join(BASE_DIR, full_path)
        logger.info(dst_path)

        # 执行复制
        try:
            copy_with_metadata(src_path, dst_path)
            processed_count += 1
            logger.info(f"已恢复: {full_path}")
        except Exception as e:
            logger.error(f"恢复失败 {full_path}: {e}")

    logger.info(f"配置项处理完成: 恢复了 {processed_count} 项")
    return processed_count


def deep_equals(obj1, obj2):
    """递归比较两个值是否相等（支持字典、列表、基本类型）"""
    if isinstance(obj1, dict):
        if set(obj1.keys()) != set(obj2.keys()):
            return False
        for k in obj1:
            if not deep_equals(obj1[k], obj2[k]):
                return False
        return True
    elif isinstance(obj1, list):
        if len(obj1) != len(obj2):
            return False
        for i in range(len(obj1)):
            if not deep_equals(obj1[i], obj2[i]):
                return False
        return True
    else:
        return obj1 == obj2


def _split_path(path):
    """拆分点分隔路径，识别 [...] 为原子单元，避免值中的点被误切断"""
    parts = []
    current = ""
    in_bracket = 0
    for ch in path:
        if ch == '[':
            in_bracket += 1
            current += ch
        elif ch == ']':
            in_bracket -= 1
            current += ch
        elif ch == '.' and in_bracket == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    if current:
        parts.append(current)
    return parts


def merge_json(file_path1: Path, file_path2: Path):
    """
    合并两个 JSON 文件，将 file2 中的配置合并到 file1 中。
    若 file1 中已存在相同的键，则保留 file1 的值。
    支持数组合并 - 若目标字段和源字段均为数组，则将源数组中不重复的元素追加到目标数组。
    """
    logger.info(f"[合并配置]  开始合并 JSON 文件")
    logger.info(f"[合并配置]  目标文件(本地): {file_path1}")
    logger.info(f"[合并配置]  源文件(备份): {file_path2}")

    # 如果目标文件不存在，直接复制源文件
    if not os.path.exists(file_path1):
        shutil.copy2(file_path2, file_path1)
        logger.info(f"目标文件不存在，直接复制 : {file_path1}")
        return

    # 读取 JSON
    with open(file_path2, 'r', encoding='utf-8') as file2:
        data2 = json.load(file2)

    with open(file_path1, 'r', encoding='utf-8') as file1:
        data1 = json.load(file1)

    # 定义需要合并的路径（点分隔）
    paths = get_properties(CONFIG_URL, "merge_list_v411") or []
    # 定义需要强制替换的路径（点分隔）? 这些路径将直接用备份值覆盖本地值
    replace_paths = get_properties(CONFIG_URL, "replace_list_v411") or []
    # 定义合并完成后需要删除的路径（点分隔）
    delete_paths = get_properties(CONFIG_URL, "delete_list_v411") or []
    total_add = 0  # 记录新增的配置项数量

    for path in paths:
        partList = _split_path(path)

        # 从 data2 中提取源字典
        src = data2
        for part in partList:
            if part in src:
                src = src[part]
            else:
                src = None
                break
        if src is None:
            logger.info(f"[合并配置]  路径 '{path}' 在备份中不存在，跳过")
            continue

        # 在 data1 中定位目标字典，若中间路径缺失则创建空字典
        target = data1
        for part in partList[:-1]:
            if part not in target:
                target[part] = {}
            target = target[part]
        key = partList[-1]  # 最后一级的键名

        # 情况0：若该路径在替换列表中 → 强制用备份值覆盖本地值
        if path in replace_paths:
            target[key] = src
            logger.info(f"[合并配置]  路径 '{path}' 在替换列表中，强制替换为备份值")
            total_add += 1
            continue

        # 情况1：目标字段不存在 → 直接使用源值
        if key not in target:
            target[key] = src
            logger.info(
                f"[合并配置]  路径 '{path}' 本地不存在，直接使用备份值 (新增 {len(src) if isinstance(src, (dict, list)) else 1} 项)")
            if isinstance(src, dict):
                total_add += len(src)
            elif isinstance(src, list):
                total_add += len(src)
            else:
                total_add += 1
            continue

        # 情况2：目标字段存在，且均为字典 → 递归合并字典（保留本地键）
        if isinstance(target[key], dict) and isinstance(src, dict):
            added_keys = []
            for k, v in src.items():
                if k not in target[key]:
                    target[key][k] = v
                    added_keys.append(k)
            if added_keys:
                logger.info(f"[合并配置]  路径 '{path}' 新增字典键: {added_keys}")
                total_add += len(added_keys)
            else:
                logger.info(f"[合并配置]  路径 '{path}' 无新增键，保留本地配置")
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
                logger.info(f"[合并配置]  路径 '{path}' 新增数组元素 {len(added_items)} 个")
                total_add += len(added_items)
            else:
                logger.info(f"[合并配置]  路径 '{path}' 无新增数组元素，保留本地配置")
            continue

        # 情况4：其他类型不匹配或非容器类型 → 保留本地值
        logger.info(f"[合并配置]  路径 '{path}' 类型不匹配或非字典/列表，保留本地值")

    # 合并完成后删除指定路径
    for del_path in delete_paths:
        parts = _split_path(del_path)
        last = parts[-1]

        # 检查最后一段是否包含数组值匹配，如 load.paths[/path/to/xxx]
        m = re.match(r'^(.+?)\[(.+)\]$', last)
        if m:
            # 数组元素按值匹配删除模式
            array_key = m.group(1)
            match_value = m.group(2)
            parent_parts = parts[:-1]
            target = data1
            for part in parent_parts:
                if isinstance(target, dict) and part in target:
                    target = target[part]
                else:
                    target = None
                    break
            if target is not None and isinstance(target, dict) and array_key in target:
                arr = target[array_key]
                if isinstance(arr, list):
                    removed = False
                    for i, item in enumerate(arr):
                        if isinstance(item, str) and item == match_value:
                            arr.pop(i)
                            removed = True
                            logger.info(f"[合并配置]  已删除数组元素 '{del_path}' (匹配值: {match_value})")
                            break
                    if not removed:
                        logger.info(f"[合并配置]  未找到匹配值 '{match_value}' 在路径 '{array_key}' 中，跳过删除")
                else:
                    logger.info(f"[合并配置]  路径 '{del_path}' 不是数组，跳过删除")
            else:
                logger.info(f"[合并配置]  路径 '{del_path}' 不存在，跳过删除")
        else:
            # 原字典键删除逻辑
            target = data1
            for part in parts[:-1]:
                if isinstance(target, dict) and part in target:
                    target = target[part]
                else:
                    target = None
                    break
            if isinstance(target, dict) and last in target:
                target.pop(last)
                logger.info(f"[合并配置]  已删除路径 '{del_path}'")
            else:
                logger.info(f"[合并配置]  路径 '{del_path}' 不存在，跳过删除")

    # 输出结果
    out_path = file_path1
    with open(out_path, 'w', encoding='utf-8') as f_out:
        json.dump(data1, f_out, indent=2, ensure_ascii=False)

    logger.info(f"[合并配置]  合并完成，共新增 {total_add} 项配置")
    logger.info(f"[合并配置]  输出文件: {out_path}")


def merge_openclaw_json():
    """
    合并 openclaw.json 文件
    将备份中的配置合并到本地配置中
    """
    OPENCLAW_FILE = os.path.join(OPENCLAW_DIR, "openclaw.json")
    # 备份文件在解压目录中的路径
    backup_openclaw = os.path.join(EXTRACTED_DIR, ".openclaw", "openclaw.json")

    if not os.path.exists(backup_openclaw):
        logger.info(f"[合并openclaw.json] 备份中不存在 openclaw.json，跳过合并")
        return

    logger.info(f"[合并openclaw.json] 开始合并 openclaw.json")
    merge_json(Path(OPENCLAW_FILE), Path(backup_openclaw))
    fix_config_strings(OPENCLAW_FILE)


def clean_up():
    """清理临时文件"""
    if TEMP_DIR and os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)
        logger.info(f"已清理临时目录: {TEMP_DIR}")


def merge_md_file(user_md_path, image_md_path):
    """调用 merge_md.py 合并单个 md 文件

    userMdFile 是用户备份中的文件（会被原地修改），
    imageMdFile 是镜像预制的版本。

    Args:
        user_md_path: userMdFile 路径
        image_md_path: imageMdFile 路径
    Returns:
        bool: 是否成功
    """
    merge_script = MERGE_SCRIPT_PATH
    if not os.path.exists(merge_script):
        logger.warning(f"merge_md.py 脚本不存在: {merge_script}，跳过合并")
        return False
    if not os.path.exists(user_md_path):
        logger.warning(f"userMdFile 不存在: {user_md_path}，跳过合并")
        return False
    if not os.path.exists(image_md_path):
        logger.warning(f"imageMdFile 不存在: {image_md_path}，跳过合并")
        return False

    json_str = json.dumps({"userMdFile": user_md_path, "imageMdFile": image_md_path, "configUrl": CONFIG_URL})
    try:
        result = subprocess.run(
            ["python3", merge_script, f"--jsonStr={json_str}"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            logger.info(f"合并成功: {user_md_path}")
            if result.stdout:
                try:
                    out_json = json.loads(result.stdout)
                    logger.info(f"合并结果: {json.dumps(out_json, ensure_ascii=False)}")
                except json.JSONDecodeError:
                    logger.info(f"合并输出: {result.stdout.strip()}")
            return True
        else:
            logger.error(f"合并失败, returncode={result.returncode}")
            logger.error(f"stderr: {result.stderr}")
            return False
    except Exception as e:
        logger.error(f"调用 merge_md.py 失败: {e}")
        return False


# 三方channel插件备份升级
# !/usr/bin/env python3
"""
OpenClaw Extensions Plugin Version Monitor
==========================================
扫描 ~/.openclaw/extensions/ 目录，对比插件版本，不匹配时备份旧目录并重装。
不使用 openclaw plugins uninstall，避免 openclaw.json 配置被删。
修复流程:
  1. npm pack 预下载包 (npm 源)
  2. 备份旧插件目录 -> .bak
  3. 移走旧目录
  4. openclaw plugins install 安装
  5. 安装失败则恢复备份
  6. 有修复才重启 gateway
用法:
  python3 openclaw_plugin_monitor.py           # 检查一次
  python3 openclaw_plugin_monitor.py --fix     # 检查并自动修复
  python3 openclaw_plugin_monitor.py --watch   # 持续监控 (每5分钟)
  python3 openclaw_plugin_monitor.py --watch --fix  # 持续监控并自动修复
"""

# ============================================================
#  配置区
# ============================================================

OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", "~/.openclaw")).expanduser()
OPENCLAW_JSON = Path(os.environ.get("OPENCLAW_CONFIG", "~/.openclaw/openclaw.json")).expanduser()
EXTENSIONS_DIR = OPENCLAW_HOME / "extensions"
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN", "openclaw")
LOG_FILE = Path(os.environ.get("OPENCLAW_MONITOR_LOG", "/tmp/openclaw_plugin_monitor.log"))

# npm pack 下载缓存目录
NPM_PACK_CACHE = Path(os.environ.get("NPM_PACK_CACHE", "/tmp/openclaw_npm_cache"))

# 备份目录 (放在 openclaw home 下，避免 /tmp 空间不足)
BACKUP_DIR = OPENCLAW_HOME / "plugin_backup"

# 期望版本映射表 (只保留微信 channel)
PLUGIN_VERSION_MAP = {
    "openclaw-weixin": {
        "version": "2.4.3",
        "source": "@tencent-weixin/openclaw-weixin@latest",
    }
}

# 监控间隔 (秒)
WATCH_INTERVAL = int(os.environ.get("WATCH_INTERVAL", "300"))

# 超时 (秒)
CMD_TIMEOUT = 300

# gateway 重启命令
RESTART_CMD = ["python3", "-m", "supervisor.supervisorctl", "restart", "openclaw-gateway"]


# ============================================================
#  日志
# ============================================================

def log(msg: str, level: str = "INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] [{level}] {msg}"
    logger.info(line)


# ============================================================
#  计时器
# ============================================================

class Timer:
    def __init__(self, label: str):
        self.label = label
        self.start = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def log_elapsed(self, prefix: str = ""):
        log(f"{prefix}{self.label}: {self.elapsed():.2f}s")


# ============================================================
#  插件目录查找
# ============================================================

def find_plugin_dirs(dir_name: str) -> list[Path]:
    """查找该插件在 extensions / npm node_modules 下的所有目录。"""
    found = []
    if not EXTENSIONS_DIR.is_dir():
        return found

    # 直接匹配
    direct = EXTENSIONS_DIR / dir_name
    if direct.is_dir():
        found.append(direct)

    # scoped: extensions/@scope/dir_name
    for scope_dir in EXTENSIONS_DIR.iterdir():
        if scope_dir.is_dir() and scope_dir.name.startswith("@"):
            candidate = scope_dir / dir_name
            if candidate.is_dir():
                found.append(candidate)

    # npm node_modules
    nm_dir = OPENCLAW_HOME / "npm" / "node_modules"
    if nm_dir.is_dir():
        direct_nm = nm_dir / dir_name
        if direct_nm.is_dir():
            found.append(direct_nm)
        for scope_dir in nm_dir.iterdir():
            if scope_dir.is_dir() and scope_dir.name.startswith("@"):
                candidate = scope_dir / dir_name
                if candidate.is_dir():
                    found.append(candidate)

    return found


# ============================================================
#  备份与恢复
# ============================================================

def backup_plugin_dir(plugin_dir: Path) -> Path | None:
    """将插件目录备份到 BACKUP_DIR，返回备份路径。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_name = f"{plugin_dir.name}.{ts}"
    # 保留 scoped 结构
    if plugin_dir.parent.name.startswith("@"):
        scope_backup = BACKUP_DIR / plugin_dir.parent.name
        scope_backup.mkdir(parents=True, exist_ok=True)
        backup_path = scope_backup / backup_name
    else:
        backup_path = BACKUP_DIR / backup_name

    try:
        shutil.copytree(plugin_dir, backup_path, symlinks=True)
        log(f"  Backed up: {plugin_dir} -> {backup_path}")
        return backup_path
    except Exception as e1:
        log(f"  Backup FAILED for {plugin_dir}: {e1}", "ERROR")
        return None


def restore_from_backup(backup_path: Path, original_dir: Path) -> bool:
    """从备份恢复插件目录到原位。"""
    try:
        if original_dir.exists():
            shutil.rmtree(original_dir)
        original_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(backup_path, original_dir, symlinks=True)
        log(f"  Restored: {backup_path} -> {original_dir}")
        return True
    except Exception as e1:
        log(f"  Restore FAILED: {backup_path} -> {original_dir}: {e1}", "ERROR")
        return False


def remove_dir_safe(d: Path) -> bool:
    """安全删除目录。"""
    try:
        shutil.rmtree(d)
        log(f"  Removed: {d}")
        return True
    except Exception as e1:
        log(f"  Failed to remove {d}: {e1}", "ERROR")
        return False


# ============================================================
#  npm pack 下载
# ============================================================

def is_npm_source(source: str) -> bool:
    if source.startswith("/") or source.startswith("./") or source.startswith(".."):
        return False
    if source.startswith("git+") or source.startswith("git://"):
        return False
    if source.startswith("file:"):
        return False
    return True


def npm_pack_download(source: str, retries: int = 2, delay: int = 5) -> str | None:
    """
    npm pack 下载，带重试。
    retries 表示最多尝试次数。
    例如 retries=2，即最多执行 2 次。
    """
    NPM_PACK_CACHE.mkdir(parents=True, exist_ok=True)
    last_error = None
    for attempt in range(1, retries + 1):
        log(f"  npm pack attempt {attempt}/{retries}: {source}")
        try:
            result = subprocess.run(
                ["npm", "pack", source],
                capture_output=True,
                text=True,
                timeout=CMD_TIMEOUT,
                cwd=str(NPM_PACK_CACHE),
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                last_error = stderr
                log(
                    f"  npm pack FAILED attempt {attempt}/{retries}: {stderr}",
                    "",
                )
                if attempt < retries:
                    sleep_seconds = delay * attempt
                    log(
                        f"  npm pack retry after {sleep_seconds}s "
                        f"({attempt}/{retries})",
                        "WARN",
                    )
                    time.sleep(sleep_seconds)
                continue
            filename = result.stdout.strip().split("\n")[-1].strip()
            tgz_path = NPM_PACK_CACHE / filename
            if tgz_path.exists():
                log(f"  Downloaded: {tgz_path}")
                return str(tgz_path)
            last_error = f"npm pack output file not found: {filename}"
            log(f"  {last_error}", "ERROR")
            if attempt < retries:
                sleep_seconds = delay * attempt
                log(
                    f"  npm pack retry after {sleep_seconds}s "
                    f"({attempt}/{retries})",
                    "WARN",
                )
                time.sleep(sleep_seconds)
        except subprocess.TimeoutExpired:
            last_error = f"npm pack TIMEOUT ({CMD_TIMEOUT}s)"
            log(f"  {last_error} attempt {attempt}/{retries}", "ERROR")
            if attempt < retries:
                sleep_seconds = delay * attempt
                log(
                    f"  npm pack retry after {sleep_seconds}s "
                    f"({attempt}/{retries})",
                    "WARN",
                )
                time.sleep(sleep_seconds)
        except FileNotFoundError:
            log(f"  npm command not found", "ERROR")
            return None
        except Exception as e:
            last_error = str(e)
            log(f"  npm pack ERROR attempt {attempt}/{retries}: {e}", "ERROR")
            if attempt < retries:
                sleep_seconds = delay * attempt
                log(
                    f"  npm pack retry after {sleep_seconds}s "
                    f"({attempt}/{retries})",
                    "WARN",
                )
                time.sleep(sleep_seconds)
    log(f"  npm pack failed after {retries} attempts: {source}, last_error={last_error}", "ERROR")
    return None


# ============================================================
#  插件扫描
# ============================================================

def scan_extensions_dir() -> dict[str, dict]:
    """扫描 extensions 目录，返回 {目录名: {version, path, package_json}}"""
    plugins = {}
    if not EXTENSIONS_DIR.is_dir():
        log(f"Extensions directory not found: {EXTENSIONS_DIR}", "WARN")
        return plugins

    for entry in EXTENSIONS_DIR.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name == "node_modules":
            continue

        pkg_path = entry / "package.json"
        if not pkg_path.exists():
            for sub in entry.iterdir():
                if sub.is_dir() and (sub / "package.json").exists():
                    info = read_plugin_info(sub)
                    if info:
                        plugins[sub.name] = info
            continue

        info = read_plugin_info(entry)
        if info:
            plugins[entry.name] = info

    return plugins


def read_plugin_info(plugin_dir: Path) -> dict | None:
    pkg_path = plugin_dir / "package.json"
    if not pkg_path.exists():
        return None
    try:
        with open(pkg_path, "r", encoding="utf-8") as package_f:
            pkg = json.load(package_f)
        return {
            "version": pkg.get("version", "0.0.0"),
            "name": pkg.get("name", plugin_dir.name),
            "path": str(plugin_dir),
            "package_json": pkg,
        }
    except Exception as e1:
        log(f"Failed to read {pkg_path}: {e1}", "WARN")
        return None


# ============================================================
#  openclaw.json 读取
# ============================================================

def read_openclaw_json() -> dict:
    if not OPENCLAW_JSON.exists():
        return {}
    try:
        with open(OPENCLAW_JSON, "r", encoding="utf-8") as openclaw_f:
            return json.load(openclaw_f)
    except Exception as e1:
        log(f"Failed to read openclaw.json: {e1}", "ERROR")
        return {}


def get_configured_channels(config: dict) -> set[str]:
    """获取 openclaw.json channels 中已配置的 channel ID。"""
    channels = config.get("channels", {})
    return {k for k in channels if k not in ("defaults", "modelByChannel")}


# ============================================================
#  版本比较
# ============================================================

def parse_version(ver: str) -> tuple:
    """将版本字符串 (如 '2.4.3') 转为可比较的整数元组"""
    return tuple(int(x) for x in ver.split('.'))


def check_plugins() -> list[dict]:
    installed = scan_extensions_dir()
    config = read_openclaw_json()
    configured_channels = get_configured_channels(config)
    results = []

    for dir_name, expected in PLUGIN_VERSION_MAP.items():
        expected_ver = expected["version"]
        source = expected["source"]

        # 检查 channels 中是否存在
        channel_ok = dir_name in configured_channels

        if dir_name not in installed:
            results.append({
                "dir_name": dir_name,
                "status": "missing",
                "expected_version": expected_ver,
                "installed_version": None,
                "plugin_path": None,
                "source": source,
                "channel_ok": channel_ok,
            })
            continue

        info = installed[dir_name]
        installed_ver = info["version"]
        version_ok = parse_version(installed_ver) >= parse_version(expected_ver)

        if not version_ok and not channel_ok:
            status = "version_mismatch_and_channel_missing"
        elif not version_ok:
            status = "version_mismatch"
        elif not channel_ok:
            status = "channel_missing"
        else:
            status = "ok"

        results.append({
            "dir_name": dir_name,
            "status": status,
            "expected_version": expected_ver,
            "installed_version": installed_ver,
            "plugin_path": info["path"],
            "source": source,
            "channel_ok": channel_ok,
        })

    for dir_name in installed:
        if dir_name not in PLUGIN_VERSION_MAP:
            results.append({
                "dir_name": dir_name,
                "status": "unregistered",
                "expected_version": None,
                "installed_version": installed[dir_name]["version"],
                "plugin_path": installed[dir_name]["path"],
                "source": None,
            })

    return results


# ============================================================
#  修复操作
# ============================================================

def run_cmd(cmd: list[str], desc: str) -> tuple[bool, str]:
    """执行命令，返回 (成功与否, stdout 内容)。"""
    cmd_str = " ".join(cmd)
    resolved = shutil.which(cmd[0])
    log(f"  [{desc}] START: {cmd_str}")
    log(f"  [{desc}] resolved: {resolved}")
    t = Timer(desc)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=CMD_TIMEOUT,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if stdout:
            for line in stdout.split("\n")[:20]:
                log(f"    | {line}")
        if result.returncode != 0:
            log(f"  [{desc}] FAILED rc={result.returncode}", "ERROR")
            if stderr:
                for line in stderr.split("\n")[:10]:
                    log(f"    | {line}", "ERROR")
            t.log_elapsed(f"  [{desc}] ")
            return False, stdout
        t.log_elapsed(f"  [{desc}] ")
        return True, stdout
    except subprocess.TimeoutExpired:
        log(f"  [{desc}] TIMEOUT ({CMD_TIMEOUT}s)", "ERROR")
        return False, ""
    except FileNotFoundError:
        log(f"  [{desc}] command not found: {cmd[0]}  resolved={resolved}", "ERROR")
        return False, ""
    except PermissionError as e2:
        log(f"  [{desc}] PERMISSION DENIED: {e2}", "ERROR")
        log(f"  [{desc}] cmd={cmd}  resolved={resolved}", "ERROR")
        return False, ""
    except Exception as e1:
        log(f"  [{desc}] ERROR: {e1}", "ERROR")
        return False, ""


def run_cmd_with_env(cmd: list[str], desc: str, env: dict) -> tuple[bool, str]:
    """
    同 run_cmd，但使用自定义环境变量。
    """
    cmd_str = " ".join(cmd)
    resolved = shutil.which(cmd[0])
    log(f"  [{desc}] START: {cmd_str}")
    log(f"  [{desc}] resolved: {resolved}")
    t = Timer(desc)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=CMD_TIMEOUT, env=env,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if stdout:
            for line in stdout.split("\n")[:20]:
                log(f"    | {line}")
        if result.returncode != 0:
            log(f"  [{desc}] FAILED rc={result.returncode}", "ERROR")
            if stderr:
                for line in stderr.split("\n")[:10]:
                    log(f"    | {line}", "ERROR")
            t.log_elapsed(f"  [{desc}] ")
            return False, stdout
        t.log_elapsed(f"  [{desc}] ")
        return True, stdout
    except subprocess.TimeoutExpired:
        log(f"  [{desc}] TIMEOUT ({CMD_TIMEOUT}s)", "ERROR")
        return False, ""
    except FileNotFoundError:
        log(f"  [{desc}] command not found: {cmd[0]}  resolved={resolved}", "ERROR")
        return False, ""
    except PermissionError as e:
        log(f"  [{desc}] PERMISSION DENIED: {e}", "ERROR")
        log(f"  [{desc}] cmd={cmd}  resolved={resolved}", "ERROR")
        for tb_line in traceback.format_exc().split("\n"):
            if tb_line.strip():
                log(f"    {tb_line}", "ERROR")
        return False, ""
    except Exception as e:
        log(f"  [{desc}] ERROR: {e}", "ERROR")
        for tb_line in traceback.format_exc().split("\n"):
            if tb_line.strip():
                log(f"    {tb_line}", "ERROR")
        return False, ""


def run_install_with_retry(cmd, desc: str, env: dict, retries: int = 2, delay: int = 5):
    """
    带重试的安装命令。
    retries 表示最多尝试次数，不是失败后的额外次数。
    例如 retries=3，即最多执行 3 次。
    """
    last_result = (False, None)

    for attempt in range(1, retries + 1):
        log(f"  Install attempt {attempt}/{retries}: {desc}")

        success, output = run_cmd_with_env(
            cmd,
            f"{desc} attempt {attempt}/{retries}",
            env,
        )

        if success:
            if attempt > 1:
                log(f"  Install succeeded after retry {attempt}/{retries}")
            return True, output

        last_result = (success, output)

        if attempt < retries:
            sleep_seconds = delay * attempt
            log(
                f"  Install failed, retry after {sleep_seconds}s "
                f"({attempt}/{retries})",
                "WARN",
            )
            time.sleep(sleep_seconds)

    log(f"  Install failed after {retries} attempts: {desc}", "ERROR")
    return last_result


def fix_plugin(result: dict) -> bool:
    """
    备份旧目录，重装插件，失败则恢复备份。不动 openclaw.json。
    """
    dir_name = result["dir_name"]
    source = result["source"]
    expected_ver = result["expected_version"]

    if not source:
        log(f"  No source for {dir_name}, skip", "WARN")
        return False

    t = Timer(f"fix {dir_name}")
    log(f"  >>> Fixing {dir_name}: {result['status']}")

    # --- Step 1: npm pack 预下载 (仅 npm 源) ---
    install_source = source
    tgz_file = None
    if is_npm_source(source):
        tgz_file = npm_pack_download(source)
        if tgz_file:
            install_source = tgz_file
        else:
            log(f"  npm pack failed, try install directly from: {source}", "WARN")

    # --- Step 2: 备份旧目录 ---
    targets = find_plugin_dirs(dir_name)
    backups: list[tuple[Path, Path]] = []  # [(备份路径, 原始路径), ...]
    if targets:
        log(f"  Found existing directories: {[str(t) for t in targets]}")
        for d in targets:
            bp = backup_plugin_dir(d)
            if bp:
                backups.append((bp, d))
            else:
                # 备份失败，中止修复
                log(f"  Backup failed for {d}, abort fix to keep data safe", "ERROR")
                tgz_file and os.path.isfile(tgz_file) and os.remove(tgz_file)
                return False
    else:
        log(f"  No existing directory to backup")

    # --- Step 3: 移走旧目录 ---
    for _, original in backups:
        remove_dir_safe(original)

    # --- Step 4: 安装 ---
    install_env = os.environ.copy()
    install_env["NPM_CONFIG_REGISTRY"] = "https://registry.npmmirror.com"
    log(f"  NPM_CONFIG_REGISTRY={install_env['NPM_CONFIG_REGISTRY']}")
    success, _ = run_install_with_retry(
        [OPENCLAW_BIN, "plugins", "install", install_source],
        f"Install {dir_name}@{expected_ver}",
        install_env,
        retries=2,
        delay=5,
    )

    # 安装失败，尝试本地复制兜底
    if not success and os.path.isdir(source):
        dest = EXTENSIONS_DIR / dir_name
        try:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(source, dest)
            log(f"  Fallback: copied {source} -> {dest}")
            success = True
        except Exception as e1:
            log(f"  Fallback copy failed: {e1}", "ERROR")

    # --- Step 5: 安装失败则恢复备份 ---
    if not success:
        log(f"  Install failed, restoring backups...", "WARN")
        for backup_path, original_path in backups:
            restore_from_backup(backup_path, original_path)
        t.log_elapsed(f"  [{dir_name}] FAILED, ")
        # 清理 tgz
        if tgz_file and os.path.isfile(tgz_file):
            os.remove(tgz_file)
        return False

    # --- Step 6: 安装成功，清理备份和 tgz ---
    for backup_path, _ in backups:
        try:
            shutil.rmtree(backup_path)
            log(f"  Cleaned backup: {backup_path}")
        except Exception as e1:
            log(f" Cleaned backup error")
            pass
    if tgz_file and os.path.isfile(tgz_file):
        os.remove(tgz_file)

    t.log_elapsed(f"  [{dir_name}] DONE, ")
    return True


# ============================================================
#  主流程
# ============================================================

def run_once(fix: bool = False) -> list[dict]:
    t_total = Timer("Total")
    results = check_plugins()
    fixed_any = False

    log("=" * 60)
    log(f"Extensions dir: {EXTENSIONS_DIR}")
    log(f"Registered plugins: {len(PLUGIN_VERSION_MAP)}")
    log("-" * 60)

    for r in results:
        status = r["status"]
        icon = {
            "ok": "OK",
            "version_mismatch": "VER_MISMATCH",
            "missing": "MISSING",
            "channel_missing": "CH_MISSING",
            "version_mismatch_and_channel_missing": "BOTH_MISSING",
            "unregistered": "???",
        }.get(status, status)

        msg = f"  [{icon}] {r['dir_name']}"
        if status == "ok":
            msg += f"  v{r['installed_version']}  channel=OK"
        elif status == "version_mismatch":
            msg += f"  v{r['installed_version']} -> {r['expected_version']}  channel=OK"
        elif status == "channel_missing":
            msg += f"  v{r['installed_version']}  channel=MISSING"
        elif status == "version_mismatch_and_channel_missing":
            msg += f"  v{r['installed_version']} -> {r['expected_version']}  channel=MISSING"
        elif status == "missing":
            msg += f"  (expected v{r['expected_version']})"
        elif status == "unregistered":
            msg += f"  v{r['installed_version']} (not in VERSION_MAP)"

        log(msg)

        if fix and status == "version_mismatch":
            fixed = fix_plugin(r)
            if fixed:
                fixed_any = True

    ok_count = sum(1 for r in results if r["status"] == "ok")
    bad_count = sum(1 for r in results if r["status"] != "ok" and r["status"] != "unregistered")
    log(f"Summary: {ok_count} OK, {bad_count} need fix")
    if fixed_any:
        log("Plugins were fixed")
    else:
        log("No plugins fixed, skip gateway restart")

    t_total.log_elapsed("Run_once ")
    return results


# 当前只更新微信的版本
def update_channel():
    update_channel_flag = get_properties(CONFIG_URL, "update_channel_flag") == "True"
    if not update_channel_flag:
        return
    fix = True
    watch = False

    log("OpenClaw Extension Plugin Monitor")
    log(f"  mode: {'watch' if watch else 'once'}  fix: {fix}")
    log(f"  config: {OPENCLAW_HOME}")

    if watch:
        log(f"  interval: {WATCH_INTERVAL}s")
        while True:
            try:
                run_once(fix=fix)
            except Exception as e1:
                log(f"Watch loop error: {e1}", "ERROR")
            log(f"Next check in {WATCH_INTERVAL}s...")
            time.sleep(WATCH_INTERVAL)
    else:
        results = run_once(fix=fix)
        bad = sum(1 for r in results if r["status"] in ("version_mismatch", "missing"))
        if bad > 0 and not fix:
            log("Run with --fix to auto-repair")


def clean_html_tags(text):
    """移除 HTML 标签"""
    text = re.sub(r'<div[^>]*></div>', '', text)
    text = re.sub(r'<[^>]+>', '', text)
    return text


def normalize_title(title):
    """标准化标题（移除多余空格）"""
    return re.sub(r'\s+', ' ', title.strip())


def extract_sections(content):
    """从 Markdown 内容中提取章节

    Args:
        content: Markdown 文本内容

    Returns:
        dict: {标题: 章节内容} 的字典
    """
    sections = {}
    current_title = None
    current_content = []

    content = clean_html_tags(content)

    for line in content.split('\n'):
        match = re.match(r'^(#{1,6})\s+(.+)$', line)
        if match:
            if current_title is not None:
                sections[current_title] = '\n'.join(current_content).strip()
            current_title = normalize_title(match.group(2))
            current_content = [line]
        else:
            if current_title is not None:
                current_content.append(line)

    if current_title is not None:
        sections[current_title] = '\n'.join(current_content).strip()

    return sections


def sections_to_markdown(sections, original_order):
    """将章节字典按指定顺序转回 Markdown 文本"""
    lines = []
    for title in original_order:
        content = sections.get(title, '')
        if content:
            lines.append(content)
    return '\n\n'.join(lines) + '\n'


# {"a.md":[{"fileName":"xx/xx.md","keyMap":{"originalKey":"deleteKey"}}]} 代表如果xx.md有originalKey就要把a.md的deleteKey删掉
def delete_moved_md():
    delete_moved_title_map = get_properties(CONFIG_URL, "delete_moved_title_map") or {}

    for target_file, entries in delete_moved_title_map.items():
        need_delete_list = []

        # 读取文件
        try:
            with open(target_file, 'r', encoding='utf-8') as f1:
                content1 = f1.read()
            logger.info(f"读取 userMdFile 成功: {len(content1)} 字节")
        except Exception as e:
            logger.error(f"读取 userMdFile 失败: {target_file}, 错误: {e}")
            continue

        for entry in entries:
            md_file = entry.get('fileName')
            key_map = entry.get('keyMap', {})

            try:
                with open(md_file, 'r', encoding='utf-8') as f2:
                    content2 = f2.read()
                logger.info(f"读取 imageMdFile 成功: {len(content2)} 字节")
            except Exception as e:
                logger.error(f"读取 imageMdFile 失败: {md_file}, 错误: {e}")
                continue

            sections1 = extract_sections(content1)
            sections2 = extract_sections(content2)
            for check_title, delete_title in key_map.items():
                if check_title in sections2 and delete_title in sections1:
                    logger.info(f"删除key: {delete_title}")
                    del sections1[delete_title]

            # 将合并结果写回 userMdFile（原地修改）
            merged_content = sections_to_markdown(sections1, list(sections1.keys()))
            try:
                with open(target_file, 'w', encoding='utf-8') as f1:
                    f1.write(merged_content)
                logger.info(f"已将合并结果写入 userMdFile（原地修改）: {target_file}")
            except Exception as e:
                logger.error(f"写入文件失败: {target_file}, 错误: {e}")


def main():
    args = parse_arguments()

    # 解析 JSON 参数
    try:
        params = json.loads(args.jsonStr)
        authorization = params.get("Authorization")
        file_id = params.get("fileId")
        is_security = params.get("isSecurity", False)
        # 从参数中读取 CONFIG_URL，如果没有则使用默认值
        config_url = params.get("configUrl")
        if config_url:
            global CONFIG_URL
            CONFIG_URL = config_url
            logger.info(f"使用自定义配置 URL: {CONFIG_URL}")
    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析失败: {e}")
        print(json.dumps({"status": "error", "message": f"JSON 解析失败: {e}"}))
        sys.exit(1)

    if not authorization:
        logger.error("缺少 Authorization 参数")
        print(json.dumps({"status": "error", "message": "缺少 Authorization 参数"}))
        sys.exit(1)

    if not file_id:
        logger.error("缺少 fileId 参数")
        print(json.dumps({"status": "error", "message": "缺少 fileId 参数"}))
        sys.exit(1)

    result = {
        "status": "success",
        "mode": "selective_recover",
        "file_id": file_id
    }

    # 1. 停止 supervisor
    logger.info("=" * 50)
    logger.info("步骤1: 停止 supervisor 进程")
    logger.info("=" * 50)
    stop_success = stop_supervisor()
    gateway_stopped = check_gateway_stopped()
    check_openclaw_stopped()
    need_restart = stop_success and gateway_stopped

    # 1.5 提前保存需要 merge 的 md 文件列表（镜像预制的版本），用于后续合并
    # md_merge_files 配置示例：["AGENTS.md", "TOOLS.md"]
    # 文件路径基于 WORKSPACE_DIR (/home/sandbox/.openclaw/workspace)
    md_merge_files = get_properties(CONFIG_URL, "md_merge_files") or []
    saved_image_md_files = {}  # {userMdFile完整路径: imageMdFile完整路径}
    if md_merge_files and isinstance(md_merge_files, list):
        logger.info(f"从配置获取需要 merge 的 md 文件列表: {md_merge_files}")
        for md_file in md_merge_files:
            md_path = os.path.join(WORKSPACE_DIR, md_file)
            if os.path.exists(md_path):
                # 临时备份文件放在 workspace 目录下，文件名带 .image_backup 后缀
                backup_path = os.path.join(WORKSPACE_DIR, f".{md_file}.image_backup")
                try:
                    shutil.copy2(md_path, backup_path)
                    saved_image_md_files[md_path] = backup_path
                    logger.info(f"已保存 {md_file} 作为镜像预制版本: {backup_path}")
                except Exception as e:
                    logger.warning(f"保存镜像 {md_file} 失败: {e}")
            else:
                logger.warning(f"当前不存在 {md_file}，跳过备份")
    else:
        logger.info("配置中无 md_merge_files 或列表为空，跳过备份")

    # 2. 初始化临时目录
    logger.info("=" * 50)
    logger.info("步骤2: 初始化临时目录")
    logger.info("=" * 50)
    init_temp_dirs()

    try:
        # 3. 获取恢复配置
        logger.info("=" * 50)
        logger.info("步骤3: 获取恢复配置")
        logger.info("=" * 50)
        recover_config = get_properties(CONFIG_URL, "recover_file_list_and_exclude")
        if not recover_config:
            logger.error("获取恢复配置失败")
            result["status"] = "error"
            result["message"] = "获取恢复配置失败"
            print(json.dumps(result))
            sys.exit(1)

        # 4. 下载备份文件
        logger.info("=" * 50)
        logger.info("步骤4: 下载备份文件")
        logger.info("=" * 50)
        if not download_file(authorization, file_id, is_security=is_security):
            logger.error("下载备份文件失败")
            result["status"] = "error"
            result["message"] = "下载备份文件失败"
            print(json.dumps(result))
            sys.exit(1)

        # 5. 解压备份文件
        logger.info("=" * 50)
        logger.info("步骤5: 解压备份文件")
        logger.info("=" * 50)
        if not extract_file():
            logger.error("解压备份文件失败")
            result["status"] = "error"
            result["message"] = "解压备份文件失败"
            print(json.dumps(result))
            sys.exit(1)

        # 6. 按配置恢复文件
        logger.info("=" * 50)
        logger.info("步骤6: 按配置恢复文件")
        logger.info("=" * 50)
        total_recovered = 0
        for i, config_item in enumerate(recover_config, 1):
            logger.info(f"处理配置 {i}/{len(recover_config)}...")
            count = process_recover_config(config_item, EXTRACTED_DIR)
            total_recovered += count

        logger.info(f"总共恢复了 {total_recovered} 项")
        result["recovered_count"] = total_recovered

        # 7. 合并 openclaw.json 文件
        logger.info("=" * 50)
        logger.info("步骤7: 合并 openclaw.json 文件")
        logger.info("=" * 50)
        merge_openclaw_json()

        # 7.2 合并 md 文件（将用户备份的 md 与镜像预制的 md 合并）
        logger.info("=" * 50)
        logger.info("步骤7.2: 合并 md 文件")
        logger.info("=" * 50)
        if saved_image_md_files:
            all_success = True
            for user_md_path, image_md_path in saved_image_md_files.items():
                md_file_name = os.path.basename(user_md_path)
                if os.path.exists(user_md_path):
                    # 此时 user_md_path 已是用户备份中的文件（userMdFile）
                    # image_md_path 是镜像预制的版本（imageMdFile）
                    merge_result = merge_md_file(user_md_path, image_md_path)
                    if merge_result:
                        logger.info(f"{md_file_name} 合并完成")
                    else:
                        logger.warning(f"{md_file_name} 合并未完成，将保留用户备份版本")
                        all_success = False
                    # 清理临时备份的镜像预制文件
                    try:
                        os.remove(image_md_path)
                        logger.info(f"已清理临时镜像备份: {image_md_path}")
                    except Exception as e:
                        logger.warning(f"清理临时镜像备份失败: {e}")
                else:
                    logger.warning(f"用户备份中不存在 {md_file_name}，跳过合并")
                    # 清理无用的镜像备份
                    try:
                        os.remove(image_md_path)
                    except Exception:
                        logger.error("移除临时备份失败...")
                        pass
            if all_success:
                logger.info("所有 md 文件合并完成")
            else:
                logger.warning("部分 md 文件合并未完成")
        else:
            logger.info("无 md 文件需要合并，跳过")


        # 7.3 删除需要去掉的md标题
        delete_moved_md()

        # 7.5 修复 extensions 目录权限
        logger.info("=" * 50)
        logger.info("步骤7.5: 修复 extensions 目录权限")
        logger.info("=" * 50)
        extensions_dir = os.path.join(OPENCLAW_DIR, "extensions")
        if os.path.exists(extensions_dir):
            fix_permissions_recursive(extensions_dir, "extensions权限修复")
            logger.info(f"extensions 目录权限修复完成: {extensions_dir}")
        else:
            logger.info(f"extensions 目录不存在，跳过权限修复")

        try:
            logger.info(f"开始插件升级校验")
            update_channel()
            logger.info(f"插件升级校验完成")
        except Exception as e:
            logger.error(f"插件升级异常: {e}")

    finally:
        # 8. 清理临时文件
        if not args.no_clean:
            logger.info("=" * 50)
            logger.info("步骤8: 清理临时文件")
            logger.info("=" * 50)
            clean_up()

        # 步骤9: 写入文件表示恢复完成
        try:
            BJ_TZ = timezone(timedelta(hours=8))
            timestamp = datetime.now(BJ_TZ).strftime("%Y%m%d%H%M%S")
            result = {
                "status": "success",
                "timestamp": timestamp,
                "timestamp_unix": int(time.time())
            }
            with open(RESULT_FILE, 'w', encoding='utf-8') as file:
                json.dump(result, file, indent=2, ensure_ascii=False)
            logger.info(f"结果已保存到: {RESULT_FILE}")
        except Exception as e:
            logger.error(f"保存结果失败: {e}")

        # 10. 重启 supervisor
        logger.info("=" * 50)
        logger.info("步骤10: 重启 supervisor")
        logger.info("=" * 50)
        if need_restart:
            start_supervisor()
        else:
            logger.info("无需重启 supervisor")

    logger.info("=" * 50)
    logger.info("恢复完成!")
    logger.info("=" * 50)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()