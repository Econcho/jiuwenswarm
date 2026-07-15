"""
OpenClaw 完整备份脚本
- 打包 /home/sandbox 目录（排除指定路径）
- 上传到华为云空间
- 可选上传到 OBS
- 记录所有返回信息
"""

import argparse
import json
import os
import shutil
import sys
import zipfile
import uuid
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs
import logging
import requests
import stat

# 配置参数
BASE_DIR = "/home/sandbox"
OPENCLAW_DIR = "/home/sandbox/.openclaw"
BACKUP_DIR = os.path.join(OPENCLAW_DIR, "backup")
LOG_DIR = "/tmp/logs"
RESULT_FILE = os.path.join(OPENCLAW_DIR, "backup_result.json")

# 固定压缩包文件名
PACK_FILE_NAME = "openClawFullBackup.zip"
PACK_FILE_PATH = os.path.join(BACKUP_DIR, PACK_FILE_NAME)

# 排除的路径（相对于 /home/sandbox）- 默认值，优先使用远程配置 packup_skip_file_list
DEFAULT_EXCLUDE_PATHS = [
    ".openclaw/logs",
    ".openclaw/backup",
    ".openclaw/tmp",
    "openclaw",
    ".local",
    "chrome-linux",
    "pandoc.tar.gz",
    "requirements",
    "nodejs"
]

# 排除路径是否已初始化（用于缓存）
_EXCLUDE_PATHS_INITIALIZED = False

# OBS 配置
OSMS_PREPARE_URL = "/osms/v1/file/manager/prepare"
OSMS_COMPLETE_URL = "/osms/v1/file/manager/complete"
ENV_FILE_PATH = '/home/sandbox/.openclaw/.xiaoyienv'

# 云空间配置 - 默认值，可从 --jsonStr 参数覆盖
CONFIG_URL = "https://h5hosting-drcn.dbankcdn.cn/cch5/HAG/D1E0CiDwLKSTla6LZEqEHDzqA/import.json"

# 备份大小限制配置（从远程配置读取，有默认值）
DEFAULT_BACKUP_LIMIT_GB = 30  # 默认 30GB 上限
DEFAULT_COMPRESSION_RATIO = 0.8  # 默认压缩率

# 日志配置
os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "full_backup_upload.log")
if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, 'w') as f:
        pass
    os.chmod(LOG_FILE, 0o600)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8')]
)
logger = logging.getLogger(__name__)


class ObsUploader:
    """OBS 上传类"""

    def __init__(self):
        self.trace_id = str(uuid.uuid4())[:16]
        self.config = self._read_env()

    def _read_env(self):
        """读取环境配置"""
        env_vars = {}
        try:
            with open(ENV_FILE_PATH, 'r', encoding='utf-8') as file:
                for line in file:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    env_vars[key.strip()] = value.strip()
        except Exception as e:
            logger.error(f"读取环境配置失败: {e}")
        return env_vars

    def _get_headers(self):
        """构建请求头"""
        return {
            "Content-Type": "application/json",
            "x-request-from": "openclaw",
            "x-uid": self.config.get('PERSONAL-UID', ''),
            "x-api-key": self.config.get('PERSONAL-API-KEY', ''),
            "x-hag-trace-id": self.trace_id
        }

    def prepare(self, file_path):
        """调用 prepare 接口获取上传 URL"""
        import hashlib

        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)

        # 计算 SHA256
        sha256_hash = hashlib.sha256()
        with open(file_path, 'rb') as file:
            for chunk in iter(lambda: file.read(8192), b''):
                sha256_hash.update(chunk)
        file_sha256 = sha256_hash.hexdigest()

        obs_object_type = get_properties(CONFIG_URL, "obs_object_type")
        body = {
            "useEdge": False,
            "objectType": obs_object_type,
            "fileName": file_name,
            "fileSha256": file_sha256,
            "fileSize": file_size,
            "fileOwnerInfo": {
                "uid": "openclaw",
                "teamId": "openclaw"
            }
        }

        obs_base_url = get_properties(CONFIG_URL, "osms_host")
        try:
            response = requests.post(
                obs_base_url + OSMS_PREPARE_URL,
                headers=self._get_headers(),
                json=body,
                timeout=30
            )
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"OBS prepare 失败: {response.status_code} - {response.text}")
                return None
        except Exception as e:
            logger.error(f"OBS prepare 异常: {e}")
            return None

    def upload(self, upload_info, file_path):
        """上传文件到 OBS"""
        url = upload_info['url']
        headers = upload_info['headers']

        try:
            with open(file_path, 'rb') as file:
                response = requests.put(
                    url,
                    data=file,
                    headers=headers,
                    timeout=300
                )
                return response.status_code == 200
        except Exception as e:
            logger.error(f"OBS 上传失败: {e}")
            return False

    def complete(self, object_id, draft_id):
        """调用 complete 接口确认上传"""
        body = {
            "objectId": object_id,
            "draftId": draft_id
        }
        obs_base_url = get_properties(CONFIG_URL, "osms_host")
        try:
            response = requests.post(
                obs_base_url + OSMS_COMPLETE_URL,
                headers=self._get_headers(),
                json=body,
                timeout=30
            )
            return response.status_code == 200
        except Exception as e:
            logger.error(f"OBS complete 异常: {e}")
            return False

    def upload_file(self, file_path):
        """完整的上传流程"""
        logger.info("开始 OBS 上传流程...")

        # 1. Prepare
        prepare_resp = self.prepare(file_path)
        if not prepare_resp:
            return None

        object_id = prepare_resp.get('objectId')
        draft_id = prepare_resp.get('draftId')
        upload_infos = prepare_resp.get('uploadInfos', [])

        if not upload_infos:
            logger.error("OBS prepare 返回的 uploadInfos 为空")
            return None

        nsp_url = upload_infos[0].get('url', '')

        # 2. Upload
        logger.info(f"上传文件到 OBS...")
        if not self.upload(upload_infos[0], file_path):
            return None

        # 3. Complete
        logger.info("确认 OBS 上传完成...")
        if not self.complete(object_id, draft_id):
            return None

        logger.info("OBS 上传成功")
        return {
            "objectId": object_id,
            "nspUrl": nsp_url
        }


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


def get_exclude_paths():
    """获取排除路径列表，优先使用远程配置 packup_skip_file_list

    使用缓存机制，只在第一次调用时获取远程配置，后续直接返回缓存值。
    """
    global DEFAULT_EXCLUDE_PATHS, _EXCLUDE_PATHS_INITIALIZED

    # 如果已初始化，直接返回缓存值
    if _EXCLUDE_PATHS_INITIALIZED:
        return DEFAULT_EXCLUDE_PATHS

    # 标记为已初始化
    _EXCLUDE_PATHS_INITIALIZED = True

    # 尝试从远程配置获取
    remote_exclude_paths = get_properties(CONFIG_URL, "packup_skip_file_list")
    if remote_exclude_paths and isinstance(remote_exclude_paths, list):
        DEFAULT_EXCLUDE_PATHS = remote_exclude_paths
        logger.info(f"使用远程配置 packup_skip_file_list: {DEFAULT_EXCLUDE_PATHS}")
    else:
        logger.info(f"远程配置 packup_skip_file_list 不存在，使用默认排除路径: {DEFAULT_EXCLUDE_PATHS}")

    return DEFAULT_EXCLUDE_PATHS


def should_exclude(path):
    """检查路径是否应该被排除"""
    exclude_paths = get_exclude_paths()
    rel_path = os.path.relpath(path, BASE_DIR)
    for exclude in exclude_paths:
        if rel_path == exclude or rel_path.startswith(exclude + os.sep):
            return True
    return False


def is_excluded(rel_path, exclude_paths):
    """检查相对路径是否在排除列表中"""
    for exclude in exclude_paths:
        if rel_path == exclude or rel_path.startswith(exclude + os.sep):
            return True
    return False


def get_dir_used_size(dir_path, exclude_paths=None):
    """获取目录实际已使用大小（字节）

    递归统计目录下所有普通文件的大小，跳过符号链接避免重复计算。
    支持排除指定路径，与实际打包范围一致。
    """
    if exclude_paths is None:
        exclude_paths = []

    total_size = 0
    for dirpath, dirnames, filenames in os.walk(dir_path, followlinks=False):
        # 计算当前目录的相对路径
        rel_dirpath = os.path.relpath(dirpath, dir_path)

        # 过滤排除的目录（修改 dirnames 会影响 os.walk 的遍历）
        dirnames[:] = [d for d in dirnames if not is_excluded(os.path.join(rel_dirpath, d), exclude_paths)]

        for filename in filenames:
            filepath = os.path.join(dirpath, filename)

            # 检查是否在排除路径中
            rel_filepath = os.path.relpath(filepath, dir_path)
            if is_excluded(rel_filepath, exclude_paths):
                continue

            # 跳过符号链接，避免重复计算
            if not os.path.islink(filepath):
                try:
                    total_size += os.path.getsize(filepath)
                except OSError:
                    pass
    return total_size


def check_backup_size_limit(safety_margin=0.1, min_free_after=200*1024*1024):
    """检查备份目录大小是否在规格上限内
    从远程配置读取 backup_limit_gb 作为上限，默认 30GB。
    从远程配置读取 compression_ratio 作为压缩率，默认 0.8。
    """
    # 从远程配置读取参数
    limit_gb = get_properties(CONFIG_URL, "backup_limit_gb") or DEFAULT_BACKUP_LIMIT_GB
    compression_ratio = get_properties(CONFIG_URL, "compression_ratio") or DEFAULT_COMPRESSION_RATIO

    limit_bytes = limit_gb * 1024 * 1024 * 1024

    # 获取排除路径
    exclude_paths = get_exclude_paths()

    # 计算目录已用空间（排除指定路径，与实际打包范围一致）
    used_size = get_dir_used_size(BASE_DIR, exclude_paths=exclude_paths)
    used_gb = used_size / (1024 ** 3)
    used_kb = used_size / 1024

    logger.info(f"备份目录大小检查: 已用 {used_kb:.2f}KB ({used_gb:.2f}GB), 上限 {limit_gb}GB, 压缩率 {compression_ratio}")
    logger.info(f"排除路径: {exclude_paths}")

    # 计算预估需要的空间：压缩后大小 * (1 + 安全余量) + 最小剩余空间
    estimated_compressed_size = used_size * compression_ratio
    required_space = int(estimated_compressed_size * (1 + safety_margin)) + min_free_after

    # 计算可用空间
    free_space = limit_bytes - used_size

    if free_space >= required_space:
        logger.info(f"空间检查通过: 可用 {free_space / (1024**3):.2f}GB, 需要 {required_space / (1024**3):.2f}GB（含安全余量）")
        return True, used_size, limit_bytes
    else:
        logger.error(f"超出备份大小上限: 已用 {used_gb:.2f}GB, 上限 {limit_gb}GB")
        logger.error(f"可用空间 {free_space / (1024**3):.2f}GB < 需要空间 {required_space / (1024**3):.2f}GB")
        return False, used_size, limit_bytes


class CloudDriveUploader:
    """华为云空间上传类"""

    def __init__(self, auth, is_security=False):
        self.trace_id = str(uuid.uuid4())[:16]
        self.auth = f"Bearer {auth}" if not auth.startswith("Bearer ") else auth
        # 根据 is_security 选择不同的 base_url
        if is_security:
            self.base_url = get_properties(CONFIG_URL, "cloud_namespace_security_base_url")
        else:
            self.base_url = get_properties(CONFIG_URL, "cloud_namespace_base_url")

    def create_folder(self):
        """创建云空间目录"""
        url = f"{self.base_url}/drive/v1/files"
        data = {
            "parentFolder": ["applicationData"],
            "fileName": self.trace_id,
            "mimeType": "application/vnd.huawei-apps.folder"
        }
        headers = {
            "Authorization": self.auth,
            "x-hw-trace-id": f"{self.trace_id}_create"
        }

        try:
            response = requests.post(url, headers=headers, json=data, timeout=60)
            if response.status_code == 200:
                return response.json()
            if response.status_code == 403:
                logger.error(f"创建云空间目录 403 权限拒绝")
                return {"_errorType": "DRIVE_PROTOCOL_UNSIGNED"}
            logger.error(f"创建云空间目录失败: {response.status_code}")
        except Exception as e:
            logger.error(f"创建云空间目录异常: {e}")
        return None

    def create_resume(self, file_name, folder_id, file_size):
        """创建上传任务"""
        url = f"{self.base_url}/upload/drive/v1/files?uploadType=resume"
        data = {
            "parentFolder": [folder_id],
            "mimeType": "application/x-zip-compressed",
            "fileName": file_name
        }
        headers = {
            "Authorization": self.auth,
            "X-Upload-Content-Length": str(file_size),
            "x-hw-trace-id": f"{self.trace_id}_resume",
            "Content-Type": "application/json"
        }

        try:
            response = requests.post(url, headers=headers, json=data, timeout=60)
            if response.status_code == 200:
                result = response.json()
                result["uploadUrl"] = response.headers.get("Location")
                return result
            logger.error(f"创建上传任务失败: {response.status_code}")
        except Exception as e:
            logger.error(f"创建上传任务异常: {e}")
        return None

    def upload_chunks(self, server_id, upload_id, file_path, chunk_size):
        """分块上传"""
        file_size = os.path.getsize(file_path)
        start_byte = 0

        url = f"{self.base_url}/upload/drive/v1/{server_id}/files"

        with open(file_path, 'rb') as file:
            while start_byte < file_size:
                end_byte = min(start_byte + chunk_size - 1, file_size - 1)
                file.seek(start_byte)
                chunk_data = file.read(end_byte - start_byte + 1)

                headers = {
                    "Authorization": self.auth,
                    "Content-Type": "application/json;charset=UTF-8",
                    "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}",
                    "x-hw-trace-id": f"{self.trace_id}_upload"
                }

                try:
                    response = requests.put(
                        f"{url}?fields=*&uploadType=resume&uploadId={upload_id}",
                        headers=headers,
                        data=chunk_data,
                        timeout=120
                    )

                    if response.status_code == 308:
                        start_byte = end_byte + 1
                        continue
                    elif response.status_code == 200:
                        return response.json().get("id")
                    else:
                        logger.error(f"上传块失败: {response.status_code}")
                        return None
                except Exception as e:
                    logger.error(f"上传块异常: {e}")
                    return None

        # 完成上传
        final_headers = {
            "Authorization": self.auth,
            "Content-Type": "application/json;charset=UTF-8",
            "Content-Length": "0"
        }

        for _ in range(10):
            try:
                response = requests.put(
                    f"{url}?fields=*&uploadType=resume&uploadId={upload_id}",
                    headers=final_headers,
                    timeout=60
                )
                if response.status_code == 200:
                    return response.json().get("id")
                elif response.status_code == 308:
                    time.sleep(2)
                    continue
            except Exception as e:
                logger.error(f"完成上传异常: {e}")
                return None

        return None

    def upload_file(self, file_path):
        """完整的上传流程"""
        logger.info("开始云空间上传流程...")

        # 1. 创建目录
        folder = self.create_folder()
        if not folder:
            return None
        # 若创建目录返回了错误类型，向上透传
        if folder.get("_errorType"):
            return {"_errorType": folder["_errorType"]}
        folder_id = folder.get("id")

        # 2. 创建上传任务
        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        resume = self.create_resume(file_name, folder_id, file_size)
        if not resume:
            return None

        upload_url = resume.get("uploadUrl")
        chunk_size = resume.get("sliceSize", 67108864)

        # 解析 server_id 和 upload_id
        parsed = urlparse(upload_url)
        path_parts = parsed.path.split("/")
        server_id = path_parts[4] if len(path_parts) > 4 else None
        query_params = parse_qs(parsed.query)
        upload_id = query_params.get("uploadId", [None])[0]

        # 3. 分块上传
        logger.info(f"上传文件到云空间...")
        file_id = self.upload_chunks(server_id, upload_id, file_path, chunk_size)
        if not file_id:
            return None

        logger.info("云空间上传成功")
        return {"fileId": file_id}


def pack_sandbox(output_zip_path=None):
    """打包 /home/sandbox 目录
    在打包前会先检查目录大小是否超出限制（默认 30GB）。
    如果超出限制，将终止打包并返回 None。
    """
    # 检查备份大小是否超限
    is_ok, used_size, limit_bytes = check_backup_size_limit()

    # 北京时区
    BJ_TZ = timezone(timedelta(hours=8))
    timestamp = datetime.now(BJ_TZ).strftime("%Y%m%d%H%M%S")
    if not is_ok:
        used_gb = used_size / (1024 ** 3)
        limit_gb = limit_bytes / (1024 ** 3)
        error_msg = f"目录大小 {used_gb:.2f}GB 超出上限"
        logger.error(f"备份终止: {error_msg}")
        result = {
            "error": True,
            "errorType": "SIZE_LIMIT_EXCEEDED",
            "message": error_msg,
            "usedGB": round(used_gb, 2),
            "limitGB": round(limit_gb, 2),
            "timestamp": timestamp
        }
        save_result(result)
        return result

    exclude_paths = get_exclude_paths()
    logger.info("=" * 50)
    logger.info("开始打包 /home/sandbox")
    logger.info(f"排除路径: {exclude_paths}")
    logger.info("=" * 50)

    # 确保备份目录存在
    os.makedirs(BACKUP_DIR, exist_ok=True)

    # 使用固定文件名
    zip_path = output_zip_path or PACK_FILE_PATH

    try:
        total_files = 0
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for root, dirs, files in os.walk(BASE_DIR, followlinks=False):
                # 过滤排除的目录
                dirs[:] = [d for d in dirs if not should_exclude(os.path.join(root, d))]

                # 处理指向目录的符号链接（os.walk 把它们放在 dirs 中，但 followlinks=False 不会遍历其内容）
                for d in dirs[:]:
                    dir_path = os.path.join(root, d)
                    if os.path.islink(dir_path):
                        arcname = os.path.relpath(dir_path, BASE_DIR)
                        try:
                            link_target = os.readlink(dir_path)
                            file_stat = os.lstat(dir_path)

                            mtime = datetime.fromtimestamp(file_stat.st_mtime, tz=BJ_TZ)
                            min_zip_time = datetime(1980, 1, 1, tzinfo=BJ_TZ)
                            if mtime < min_zip_time:
                                mtime = min_zip_time

                            zip_info = zipfile.ZipInfo(arcname, date_time=mtime.timetuple()[:6])
                            zip_info.compress_type = zipfile.ZIP_DEFLATED
                            # 设置符号链接标记 (0xA << 28) | 权限
                            zip_info.external_attr = (0xA << 28) | ((file_stat.st_mode & 0xFFFF) << 16)

                            # 链接目标作为内容写入
                            zf.writestr(zip_info, link_target.encode('utf-8'))

                            total_files += 1
                            logger.info(f"打包目录符号链接: {arcname} -> {link_target}")
                        except OSError as e1:
                            logger.warning(f"跳过目录符号链接 {dir_path}: {e1}")
                        # 从 dirs 中移除，避免 os.walk 尝试进入此目录
                        dirs.remove(d)

                # 添加目录（保留原始时间戳，使用北京时间）
                rel_dir = os.path.relpath(root, BASE_DIR)
                if rel_dir != '.':
                    try:
                        dir_stat = os.lstat(root)
                        mtime = datetime.fromtimestamp(dir_stat.st_mtime, tz=BJ_TZ)

                        # ZIP 格式不支持 1980 年之前的时间戳，使用最小允许值
                        min_zip_time = datetime(1980, 1, 1, tzinfo=BJ_TZ)
                        if mtime < min_zip_time:
                            mtime = min_zip_time

                        dir_arcname = rel_dir + '/'
                        dir_info = zipfile.ZipInfo(dir_arcname, date_time=mtime.timetuple()[:6])
                        dir_info.compress_type = zipfile.ZIP_STORED
                        # 目录权限
                        dir_info.external_attr = (dir_stat.st_mode & 0xFFFF) << 16
                        zf.writestr(dir_info, '')
                    except OSError as e:
                        logger.warning(f"跳过目录 {root}: {e}")

                for file in files:
                    file_path = os.path.join(root, file)

                    # 跳过排除的文件
                    if should_exclude(file_path):
                        continue

                    # 处理符号链接 - 保存链接本身
                    if os.path.islink(file_path):
                        arcname = os.path.relpath(file_path, BASE_DIR)
                        try:
                            link_target = os.readlink(file_path)
                            file_stat = os.lstat(file_path)

                            mtime = datetime.fromtimestamp(file_stat.st_mtime, tz=BJ_TZ)
                            min_zip_time = datetime(1980, 1, 1, tzinfo=BJ_TZ)
                            if mtime < min_zip_time:
                                mtime = min_zip_time

                            zip_info = zipfile.ZipInfo(arcname, date_time=mtime.timetuple()[:6])
                            zip_info.compress_type = zipfile.ZIP_DEFLATED
                            # 设置符号链接标记 (0xA << 28) | 权限
                            zip_info.external_attr = (0xA << 28) | ((file_stat.st_mode & 0xFFFF) << 16)

                            # 链接目标作为内容写入
                            zf.writestr(zip_info, link_target.encode('utf-8'))

                            total_files += 1
                        except OSError as e:
                            logger.warning(f"跳过符号链接 {file_path}: {e}")
                        continue

                    # 跳过特殊文件
                    try:
                        file_stat = os.lstat(file_path)
                        if not stat.S_ISREG(file_stat.st_mode):
                            continue
                    except OSError:
                        continue

                    # 添加到压缩包（保留原始文件时间戳，使用北京时间）
                    arcname = os.path.relpath(file_path, BASE_DIR)
                    try:
                        mtime = datetime.fromtimestamp(file_stat.st_mtime, tz=BJ_TZ)

                        # ZIP 格式不支持 1980 年之前的时间戳，使用最小允许值
                        min_zip_time = datetime(1980, 1, 1, tzinfo=BJ_TZ)
                        if mtime < min_zip_time:
                            mtime = min_zip_time

                        # 使用手动 ZipInfo 构造，确保夹紧后的时间戳生效
                        zip_info = zipfile.ZipInfo(arcname, date_time=mtime.timetuple()[:6])
                        zip_info.compress_type = zipfile.ZIP_DEFLATED
                        with open(file_path, 'rb') as src, zf.open(zip_info, 'w', force_zip64=True) as dst:
                            shutil.copyfileobj(src, dst)

                        total_files += 1
                        if total_files % 1000 == 0:
                            logger.info(f"已打包 {total_files} 个文件...")
                    except OSError as e:
                        logger.warning(f"跳过文件 {file_path}: {e}")

        file_size = os.path.getsize(zip_path) / (1024 * 1024)
        logger.info(f"打包完成: {zip_path}")
        logger.info(f"文件数量: {total_files}")
        logger.info(f"文件大小: {file_size:.2f} MB")

        return {"success": True, "zipPath": zip_path}

    except Exception as e:
        logger.error(f"打包失败: {e}")
        if os.path.exists(zip_path):
            os.remove(zip_path)
        return {"error": True, "errorType": "PACK_ERROR", "message": str(e)}


def save_result(result):
    """保存结果到文件"""
    try:
        with open(RESULT_FILE, 'w', encoding='utf-8') as file:
            json.dump(result, file, indent=2, ensure_ascii=False)
        logger.info(f"结果已保存到: {RESULT_FILE}")
        return True
    except Exception as e:
        logger.error(f"保存结果失败: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="OpenClaw 完整备份脚本")
    parser.add_argument("--jsonStr", required=True, help="JSON 字符串参数，包含 mode、Authorization、uploadObs 等")
    parser.add_argument("--output", help="指定输出 zip 文件路径（可选，覆盖默认固定路径）")
    args = parser.parse_args()

    # 解析 JSON 参数
    try:
        params = json.loads(args.jsonStr)
        mode = params.get("mode", "packAndUpload")  # pack / upload / packAndUpload
        authorization = params.get("Authorization")
        upload_obs = params.get("uploadObs", False)
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

    # 参数校验
    if mode not in ("pack", "upload", "packAndUpload"):
        logger.error(f"无效的 mode 参数: {mode}，可选值: pack / upload / packAndUpload")
        print(json.dumps({"status": "error", "message": f"无效的 mode 参数: {mode}"}))
        sys.exit(1)

    if mode in ("upload", "packAndUpload") and not authorization:
        logger.error("upload/packAndUpload 模式需要 Authorization 参数")
        print(json.dumps({"status": "error", "message": "upload/packAndUpload 模式需要 Authorization 参数"}))
        sys.exit(1)

    # 准备结果对象
    BJ_TZ = timezone(timedelta(hours=8))
    timestamp = datetime.now(BJ_TZ).strftime("%Y%m%d%H%M%S")
    result = {
        "status": "success",
        "mode": mode,
        "timestamp": timestamp,
        "zipFile": None,
        "cloudDrive": {
            "fileId": None
        },
        "obs": {
            "uploaded": upload_obs,
            "objectId": None,
            "nspUrl": None
        }
    }

    # 确定压缩包路径
    zip_path = args.output or PACK_FILE_PATH

    # ========== pack 模式：只打包 ==========
    if mode == "pack":
        pack_result = pack_sandbox(args.output)
        if isinstance(pack_result, dict) and pack_result.get("error"):
            result["status"] = "error"
            result["errorType"] = pack_result.get("errorType", "PACK_ERROR")
            result["message"] = pack_result.get("message", "打包失败")
            if "usedGB" in pack_result:
                result["usedGB"] = pack_result["usedGB"]
            if "limitGB" in pack_result:
                result["limitGB"] = pack_result["limitGB"]
            print(json.dumps(result))
            sys.exit(1)

        zip_path = pack_result.get("zipPath") if isinstance(pack_result, dict) else pack_result
        result["zipFile"] = zip_path
        result["message"] = "仅打包完成，未上传"
        timestamp = datetime.now(BJ_TZ).strftime("%Y%m%d%H%M%S")
        result["timestamp"] = timestamp
        save_result(result)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        logger.info("流程结束（pack）")
        return

    # ========== upload 模式：只上传已有压缩包 ==========
    if mode == "upload":
        if not os.path.exists(zip_path):
            logger.error(f"压缩包不存在: {zip_path}，请先执行 pack 模式")
            result["status"] = "error"
            result["message"] = f"压缩包不存在: {zip_path}，请先执行 pack 模式"
            print(json.dumps(result))
            sys.exit(1)

        result["zipFile"] = zip_path
        logger.info(f"上传已有压缩包: {zip_path} ({os.path.getsize(zip_path) / 1024 / 1024:.2f} MB)")

        # 上传到云空间
        logger.info("=" * 50)
        logger.info("开始上传到云空间")
        logger.info("=" * 50)

        drive_uploader = CloudDriveUploader(authorization, is_security=is_security)
        drive_result = drive_uploader.upload_file(zip_path)

        if drive_result:
            error_type = drive_result.get("_errorType")
            if error_type:
                logger.error(f"云空间上传失败 (errorType={error_type})")
                result["status"] = "error"
                result["errorType"] = error_type
                result["message"] = "云空间上传失败"
            else:
                result["cloudDrive"]["fileId"] = drive_result.get("fileId")
        else:
            logger.error("云空间上传失败")
            result["status"] = "error"
            result["message"] = "云空间上传失败"

        # 上传到 OBS（可选）
        if upload_obs and result["status"] == "success":
            logger.info("\n" + "=" * 50)
            logger.info("开始上传到 OBS")
            logger.info("=" * 50)

            obs_uploader = ObsUploader()
            obs_result = obs_uploader.upload_file(zip_path)

            if obs_result:
                result["obs"]["objectId"] = obs_result.get("objectId")
                result["obs"]["nspUrl"] = obs_result.get("nspUrl")
            else:
                logger.error("OBS 上传失败")
                result["obs"]["error"] = "上传失败"
        timestamp = datetime.now(BJ_TZ).strftime("%Y%m%d%H%M%S")
        result["timestamp"] = timestamp
        save_result(result)
        print(json.dumps(result, indent=2, ensure_ascii=False))

        # 上传完成后删除压缩包
        if os.path.exists(zip_path):
            os.remove(zip_path)
            logger.info(f"已删除压缩包: {zip_path}")
        logger.info("流程结束（upload）")
        return

    # ========== packAndUpload 模式：打包 + 上传（兼容旧逻辑）==========
    pack_result = pack_sandbox(args.output)
    if isinstance(pack_result, dict) and pack_result.get("error"):
        result["status"] = "error"
        result["errorType"] = pack_result.get("errorType", "PACK_ERROR")
        result["message"] = pack_result.get("message", "打包失败")
        if "usedGB" in pack_result:
            result["usedGB"] = pack_result["usedGB"]
        if "limitGB" in pack_result:
            result["limitGB"] = pack_result["limitGB"]
        print(json.dumps(result))
        sys.exit(1)

    zip_path = pack_result.get("zipPath") if isinstance(pack_result, dict) else pack_result
    result["zipFile"] = zip_path

    # 上传到云空间
    logger.info("=" * 50)
    logger.info("开始上传到云空间")
    logger.info("=" * 50)

    drive_uploader = CloudDriveUploader(authorization, is_security=is_security)
    drive_result = drive_uploader.upload_file(zip_path)

    if drive_result:
        error_type = drive_result.get("_errorType")
        if error_type:
            logger.error(f"云空间上传失败 (errorType={error_type})")
            result["status"] = "partial_error"
            result["errorType"] = error_type
            result["message"] = "云空间上传失败"
        else:
            result["cloudDrive"]["fileId"] = drive_result.get("fileId")
    else:
        logger.error("云空间上传失败")
        result["status"] = "partial_error"
        result["message"] = "云空间上传失败"

    # 上传到 OBS（可选）
    if upload_obs and result["status"] == "success":
        logger.info("\n" + "=" * 50)
        logger.info("开始上传到 OBS")
        logger.info("=" * 50)

        obs_uploader = ObsUploader()
        obs_result = obs_uploader.upload_file(zip_path)

        if obs_result:
            result["obs"]["objectId"] = obs_result.get("objectId")
            result["obs"]["nspUrl"] = obs_result.get("nspUrl")
        else:
            logger.error("OBS 上传失败")
            result["obs"]["error"] = "上传失败"

    timestamp = datetime.now(BJ_TZ).strftime("%Y%m%d%H%M%S")
    result["timestamp"] = timestamp
    save_result(result)
    print(json.dumps(result, indent=2, ensure_ascii=False))

    # 清理压缩包
    if os.path.exists(zip_path):
        os.remove(zip_path)
        logger.info(f"已删除压缩包: {zip_path}")
    logger.info("流程结束（packAndUpload）")


if __name__ == "__main__":
    main()