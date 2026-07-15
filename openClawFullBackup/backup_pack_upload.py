import argparse
import json
import os
import shutil
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
import logging
import requests
import sys
import zipfile
import time
import tempfile

write_file_id_to_file_url = "/home/sandbox/.openclaw/fileIdObj.txt"
write_all_file_id_to_file_url = "/home/sandbox/.openclaw/fileIdObjForAll.txt"
zip_input_files_url = "https://h5hosting-drcn.dbankcdn.cn/cch5/HAG/D1E0CiDwLKSTla6LZEqEHDzqA/import.json"
pack_file_path = "/home/sandbox/.openclaw/backup/openClawPack.zip"
pack_all_file_path = "/home/sandbox/.openclaw/backup/openClawPackAll.zip"
BASE_DIR = "/home/sandbox/.openclaw"
# 特殊处理的文件路径（需要额外打包到压缩包中）
OPENCLAW_FILE = "/home/sandbox/.openclaw/openclaw.json"
EXTENSIONS_FILE = "/home/sandbox/.openclaw/extensions"
SESSIONS_DIR = "/home/sandbox/.openclaw/agents/main/sessions"
ENV_FILE_PATH = '/home/sandbox/.openclaw/.xiaoyienv'


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
        logging.FileHandler(LOG_FILE, encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


class UploadFileHwDrive(object):
    def __init__(self, auth):
        self.trace_id = str(uuid.uuid4())[:16]
        self.auth = f"Bearer {auth}"

    # 创建云空间目录
    def create_file_dir(self):
        """
        调用 /drive/v1/files 接口创建文件夹
        :return: 创建的文件夹信息
        """
        cloud_namespace_base_url = get_properties(zip_input_files_url, "cloud_namespace_base_url")
        url = f"{cloud_namespace_base_url}/drive/v1/files"
        data = {
            "parentFolder": ["applicationData"],
            "fileName": self.trace_id,
            "mimeType": "application/vnd.huawei-apps.folder"
        }
        create_trace_id = f"{self.trace_id}_create"
        headers = {
            "Authorization": self.auth,
            "x-hw-trace-id": create_trace_id
        }
        logger.info("create dir trace_id: {}".format(create_trace_id))

        try:
            response = requests.post(url, headers=headers, json=data, verify=True, timeout=60)
            if response.status_code == 200:
                result = response.json()
                return result
            else:
                logger.error(f"Error: Failed to create directory, status code: {response.status_code}")
                return None
        except Exception as e:
            logger.error(f"Error: An error occurred while creating directory: {e}")
            return None

    # 创建云空间文件
    def create_file_resume(self, file_name, file_id, file_size):
        cloud_namespace_base_url = get_properties(zip_input_files_url, "cloud_namespace_base_url")
        url = f"{cloud_namespace_base_url}/upload/drive/v1/files?uploadType=resume"
        data = {
            "parentFolder": [file_id],
            "mimeType": "application/x-zip-compressed",
            "fileName": file_name
        }
        create_trace_id = f"{self.trace_id}_create_resume"
        headers = {
            "Authorization": self.auth,
            "X-Upload-Content-Length": file_size,
            "x-hw-trace-id": create_trace_id,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        logger.info("create trace_id: {}".format(create_trace_id))

        try:
            response = requests.post(url, headers=headers, json=data, verify=True, timeout=60)
            if response.status_code == 200:
                result = response.json()
                headers = response.headers

                logger.info(f"headers = {headers}")

                resultHeader = response.headers.get("Location")
                logger.info(f"resultHeader = {resultHeader}")
                result["uploadUrl"] = resultHeader
                return result
            else:
                logger.error(f"Error: Failed to create resume, status code: {response.status_code}")
                logger.info(f"Response: {response.text}")
                return None
        except Exception as e:
            logger.error(f"Error: An error occurred while creating resume: {e}")
            return None

    def upload_file_in_chunks(self, server_id, upload_id, file_path, chunk_size=67108864):
        """
        循环上传 ZIP 文件，每次上传固定大小的块
        :param server_id: 服务器 ID
        :param upload_id: 上传 ID
        :param file_path: ZIP 文件路径
        :param chunk_size: 每次上传的字节数（默认 64MB）
        :return: 上传结果
        """
        if not os.path.exists(file_path):
            logger.info("文件不存在")
            return None

        file_size = os.path.getsize(file_path)
        logger.info(f"文件大小: {file_size} 字节")
        cloud_namespace_base_url = get_properties(zip_input_files_url, "cloud_namespace_base_url")

        upload_trace_id = f"{self.trace_id}_upload"

        start_byte = 0
        while start_byte < file_size:
            end_byte = min(start_byte + chunk_size - 1, file_size - 1)
            content_range = f"bytes {start_byte}-{end_byte}/{file_size}"
            logger.info(f"上传范围: {content_range}")

            # 分块读取文件内容，避免一次性加载大文件到内存
            with open(file_path, "rb") as f:
                f.seek(start_byte)
                chunk_data = f.read(end_byte - start_byte + 1)

            url = f"{cloud_namespace_base_url}/upload/drive/v1/{server_id}/files"
            request_url = f"{url}?fields=*&uploadType=resume&uploadId={upload_id}"

            headers = {
                "Authorization": self.auth,
                "Content-Type": "application/json;charset=UTF-8",
                "Content-Range": content_range,
                "x-hw-trace-id": upload_trace_id,
            }
            try:
                response = requests.put(
                    request_url,
                    headers=headers,
                    data=chunk_data,
                    verify=True,
                    timeout=60
                )
                if response.status_code == 308:
                    start_byte = end_byte + 1
                    continue
                if response.status_code == 200:
                    logger.info("上传成功")
                else:
                    logger.info(f"响应内容: {response.text}")
                    return None
            except Exception as e:
                logger.error(f"请求异常: {e}")
                return None

        # 文件上传完成后，调用一次接口，不传Content-Range，传Content-Length为0
        final_url = f"{cloud_namespace_base_url}/upload/drive/v1/{server_id}/files"
        final_request_url = f"{final_url}?fields=*&uploadType=resume&uploadId={upload_id}"
        final_headers = {
            "Authorization": self.auth,
            "Content-Type": "application/json;charset=UTF-8",
            "Content-Length": "0"
        }
        max_retries = 10
        retry_count = 0
        while retry_count < max_retries:
            try:
                response = requests.put(
                    final_request_url,
                    headers=final_headers,
                    data=b"",
                    verify=True,
                    timeout=60
                )
                if response.status_code == 200:
                    response_text = response.text
                    response_json = json.loads(response_text)
                    logger.info(f"上传完成接口调用成功，返回的文件ID : {str(response_json.get('id'))}")
                    return response_json.get("id")
                elif response.status_code == 308:
                    retry_count += 1
                    logger.info(f"上传完成接口返回308，等待2秒后重试（第{retry_count}次）")
                    time.sleep(2)
                    continue
                else:
                    logger.error(f"上传完成接口调用失败，状态码: {response.status_code}")
                    logger.error(f"响应内容: {response.text}")
                    return None
            except Exception as e:
                logger.info(f"上传完成接口调用异常: {e}")
                return None

        logger.error(f"上传完成接口重试{max_retries}次后仍返回308，放弃")
        return None


# 添加帮助信息
def parse_arguments():
    parser = argparse.ArgumentParser(description="备份openclaw数据")
    parser.add_argument("--mode", required=True, choices=["pack", "upload"], help="操作模式: pack=只打包, upload=上传指定文件")
    parser.add_argument(
        "--packScope",
        choices=["all", "normal"],
        default="normal",
        help="备份范围：all=zip_input_all_files, upload=zip_input_files"
        )
    parser.add_argument("--Authorization", type=str, help="用于云空间接口at")
    return parser.parse_args()


def format_date():
    time_stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    time_stamp = time_stamp[:len(time_stamp) - 3]
    return time_stamp


def write_url_to_file(prepare_response, filename):
    """
    将fileId写入文件，格式为JSON对象 {"fileId": "xxxxxx"}
    :param driveObj: UploadFileHwDrive 实例
    :param prepare_response: 新的文件ID
    :param filename: 要写入的文件路径
    :return: True（成功）/ False（失败）
    """
    try:
        new_file_id = prepare_response

        # 构造新数据对象
        data = {
            "fileId": new_file_id
        }

        # 写入文件
        with open(filename, 'w', encoding='utf-8') as file:
            json.dump(data, file, indent=4, ensure_ascii=False)

        logger.info(f"Success: Data written to {filename}, fileId: {new_file_id}")
        return True

    except Exception as e:
        logger.error(f"An error occurred: {e}")
        return False


def parse_url(url):
    """
    解析 URL，提取 server_id 和 uploadId
    :param url: 完整的 URL
    :return: server_id 和 uploadId
    """
    parsed_url = urlparse(url)
    path_parts = parsed_url.path.split("/")
    query_params = parse_qs(parsed_url.query)

    server_id = path_parts[4] if len(path_parts) > 4 else None
    upload_id = query_params.get("uploadId", [None])[0]

    return server_id, upload_id


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
        logger.error(f"获取配置失败: {e}")
        return None


def get_today_session_dirs():
    """获取 sessions 目录下当天修改过的子目录列表"""
    today_dirs = []
    if not os.path.exists(SESSIONS_DIR):
        logger.warning(f"[Sessions备份] sessions 目录不存在: {SESSIONS_DIR}")
        return today_dirs

    now_utc = datetime.utcnow()                         # datetime 对象
    beijing_now = now_utc + timedelta(hours=8)          # 正确偏移得到北京时间
    beijing_today = beijing_now.date()

    for item in os.listdir(SESSIONS_DIR):
        item_path = os.path.join(SESSIONS_DIR, item)
        mtime = os.path.getmtime(item_path)
        mtime_utc = datetime.utcfromtimestamp(mtime)
        mtime_beijing = mtime_utc + timedelta(hours=8)
        mtime_date = mtime_beijing.date()
        if mtime_date == beijing_today:
            today_dirs.append(item_path)
            logger.info(f"[Sessions备份] 发现当天修改的目录: {item}")

    logger.info(f"[Sessions备份] 共发现 {len(today_dirs)} 个当天修改的目录")
    return today_dirs


def pack_special_path(zf, special_path, tag="特殊处理", exclude_dirs=None, skip_hidden=False):
    """
    将指定路径打包到 ZIP 文件中（公共方法）
    支持文件和目录，自动处理符号链接

    :param zf: zipfile.ZipFile 实例
    :param special_path: 要打包的路径（文件或目录）
    :param tag: 日志标签，用于区分不同类型的特殊处理
    :param exclude_dirs: 要排除的子目录名称列表（如 ['xiaoyi-channel']）
    :param skip_hidden: 是否跳过打包隐藏文件
    :return: True（成功）/ False（路径不存在）
    """
    # 检查路径是否存在（包括断开的符号链接）
    if not os.path.exists(special_path) and not os.path.islink(special_path):
        logger.warning(f"[{tag}] 路径不存在，跳过: {special_path}")
        return False

    arcname = os.path.relpath(special_path, BASE_DIR)

    # 处理顶层符号链接
    if os.path.islink(special_path):
        try:
            link_target = os.readlink(special_path)
            zip_info = zipfile.ZipInfo(arcname)
            zip_info.create_system = 3  # Unix
            zip_info.external_attr = (0o120777 << 16)  # 符号链接权限
            zf.writestr(zip_info, link_target)
            logger.info(f"[{tag}] 打包符号链接: {arcname} -> {link_target}")
            return True
        except Exception as e:
            logger.error(f"[{tag}] 无法添加符号链接 {special_path}: {e}")
            return False

    if os.path.isfile(special_path):
        # 单个文件
        if skip_hidden and os.path.basename(special_path).startswith('.'):
            logger.info(f"[{tag}] 跳过隐藏文件: {arcname}")
            return False
        zf.write(special_path, arcname)
        logger.info(f"[{tag}] 打包文件: {arcname}")
    elif os.path.isdir(special_path):
        # 目录，递归打包内容
        for root, dirs, files in os.walk(special_path, followlinks=False):
            if exclude_dirs:
                dirs[:] = [d for d in dirs if d not in exclude_dirs]
            # 过滤隐藏目录（以 '.' 开头）
            if skip_hidden:
                dirs[:] = [d for d in dirs if not d.startswith('.')]
            # 过滤隐藏文件（以 '.' 开头）
            if skip_hidden:
                files[:] = [f for f in files if not f.startswith('.')]
            # 处理目录中的符号链接目录
            for d in dirs:
                dir_path = os.path.join(root, d)
                if os.path.islink(dir_path):
                    rel_path = os.path.relpath(dir_path, start=special_path)
                    file_arcname = os.path.join(arcname, rel_path)
                    try:
                        link_target = os.readlink(dir_path)
                        zip_info = zipfile.ZipInfo(file_arcname)
                        zip_info.create_system = 3
                        zip_info.external_attr = (0o120755 << 16)  # 目录权限
                        zf.writestr(zip_info, link_target)
                        logger.info(f"[{tag}] 打包目录链接: {file_arcname} -> {link_target}")
                    except Exception as e:
                        logger.error(f"[{tag}] 无法添加目录链接 {dir_path}: {e}")

            # 处理普通文件和符号链接文件
            for file in files:
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, start=special_path)
                file_arcname = os.path.join(arcname, rel_path)

                if os.path.islink(file_path):
                    # 符号链接：保存链接本身
                    try:
                        link_target = os.readlink(file_path)
                        zip_info = zipfile.ZipInfo(file_arcname)
                        zip_info.create_system = 3
                        zip_info.external_attr = (0o120777 << 16)
                        zf.writestr(zip_info, link_target)
                        logger.info(f"[{tag}] 打包符号链接: {file_arcname} -> {link_target}")
                    except Exception as e:
                        logger.error(f"[{tag}] 无法添加符号链接 {file_path}: {e}")
                else:
                    zf.write(file_path, file_arcname)
        logger.info(f"[{tag}] 打包目录: {arcname}")

    return True


def zip_dirs(output_zip_path, dir_paths, prefix="result", extra_dirs=None, pack_scope=None):
    # 1. 基础校验：判断output_zip_path是目录还是文件路径
    if os.path.isdir(output_zip_path):
        # 若传入的是目录，自动生成带时间戳的文件名（兼容旧逻辑）
        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        safe_ts = ts.replace(":", "").replace("\\", "").replace("/", "")
        zip_filename = f"{prefix}{safe_ts}.zip"
        output_zip_abs = os.path.abspath(os.path.join(output_zip_path, zip_filename))
    else:
        # 核心优化：若传入的是文件路径，直接使用（无多余目录层级）
        output_zip_abs = os.path.abspath(output_zip_path)
        # 确保文件所在目录存在
        zip_dir = os.path.dirname(output_zip_abs)
        os.makedirs(zip_dir, exist_ok=True)

    # 2. 待打包路径校验（支持文件和目录）

    if not dir_paths:
        logger.info("错误：待打包路径列表不能为空")
        return None
    valid_paths = [p for p in dir_paths if os.path.exists(p)]
    if not valid_paths:
        logger.info("错误：无有效待打包路径")
        return None

    # 分离文件和目录
    valid_files = [p for p in valid_paths if os.path.isfile(p)]
    valid_dirs = [p for p in valid_paths if os.path.isdir(p)]

    logger.info(f"待打包文件: {valid_files}")
    logger.info(f"待打包目录: {valid_dirs}")

    # 3. 磁盘空间检测
    total_size = get_total_size_of_paths(valid_paths)
    if total_size == 0:
        logger.info("警告：待打包文件总大小为0，仍将生成空压缩包")

    enough, free, required = check_space_within_limit(os.path.dirname(output_zip_abs), total_size)
    if not enough:
        logger.error(f"错误：磁盘空间不足！可用 {free / 1024 / 1024:.2f} MB，需要 {required / 1024 / 1024:.2f} MB（含10%安全余量）")
        return None

    # 4. 执行打包（跨系统路径兼容）
    try:
        with zipfile.ZipFile(output_zip_abs, 'w', zipfile.ZIP_DEFLATED) as zf:
            # 先打包单个文件
            for file_path in valid_files:
                file_abs = os.path.abspath(file_path)
                arcname = os.path.relpath(file_abs, BASE_DIR)
                logger.info(f"打包文件: {arcname}")
                zf.write(file_abs, arcname)

            # 再打包目录
            for dir_path in valid_dirs:
                dir_abs = os.path.abspath(dir_path)
                # 获取待打包目录的外层名称（如 /a → a）
                dir_name = os.path.relpath(dir_abs, BASE_DIR)
                # 遍历目录并添加文件和空目录
                for root, dirs, files in os.walk(dir_abs, followlinks=False):
                    # 处理符号链接（文件链接）
                    for file in files:
                        file_path = os.path.join(root, file)
                        if os.path.islink(file_path):
                            rel_path = os.path.relpath(file_path, start=dir_abs)
                            arcname = os.path.join(dir_name, rel_path)
                            try:
                                # 获取链接指向的目标
                                link_target = os.readlink(file_path)
                                # 在ZIP中创建符号链接（需要Python 3.6+）
                                zip_info = zipfile.ZipInfo(arcname)
                                zip_info.create_system = 3  # Unix
                                zip_info.external_attr = (
                                            0o120777 << 16)  # 符号链接权限
                                zf.writestr(zip_info, link_target)
                                logger.info(f"信息：添加符号链接 {arcname} -> {link_target}")
                            except Exception as e:
                                logger.error(f"警告：无法添加符号链接 {file_path}: {e}")

                    # 处理符号链接（目录链接）
                    for d in dirs:
                        dir_path = os.path.join(root, d)
                        if os.path.islink(dir_path):
                            rel_path = os.path.relpath(dir_path, start=dir_abs)
                            arcname = os.path.join(dir_name, rel_path)
                            try:
                                link_target = os.readlink(dir_path)
                                zip_info = zipfile.ZipInfo(arcname)
                                zip_info.create_system = 3  # Unix
                                zip_info.external_attr = (
                                            0o120755 << 16)  # 目录权限
                                zf.writestr(zip_info, link_target)
                                logger.info(f"信息：添加目录链接 {arcname} -> {link_target}")
                            except Exception as e:
                                logger.error(f"警告：无法添加目录链接 {dir_path}: {e}")
                    # 添加普通文件
                    for file in files:
                        file_path = os.path.join(root, file)

                        # 跳过符号链接（已单独处理）
                        if os.path.islink(file_path):
                            continue

                            # Windows长路径兼容
                        if sys.platform.startswith('win32') and len(file_path) > 255:
                            file_path = f"\\\\?\\{file_path}"

                            # 检查文件类型，跳过特殊文件（socket、设备文件、管道等）
                        try:
                            file_stat = os.lstat(
                                file_path)  # 使用lstat不跟随链接
                            import stat
                            if not stat.S_ISREG(file_stat.st_mode):
                                logger.info(f"警告：跳过特殊文件 {file_path} (非普通文件)")
                                continue
                        except OSError as e:
                            logger.error(f"警告：无法获取文件信息 {file_path}: {e}")
                            continue

                            # 核心修改：保留外层目录名 + 内部相对路径
                        rel_path = os.path.relpath(file_path, start=dir_abs)
                        arcname = os.path.join(dir_name, rel_path)
                        try:
                            zf.write(file_path, arcname)
                        except OSError as e:
                            logger.error(f"警告：跳过文件 {file_path}: {e}")
                            continue

                            # 修复：添加空目录（如果没有文件且是空目录）
                    real_dirs = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
                    if not files and not real_dirs:
                        # 这是一个空目录
                        rel_path = os.path.relpath(root, start=dir_abs)
                        if rel_path != '.':
                            arcname = os.path.join(dir_name, rel_path) + '/'
                        else:
                            arcname = dir_name + '/'
                        zf.writestr(arcname, '')


            # 特殊处理：额外打包 openclaw.json 和 extensions（不在配置路径中）
            pack_special_path(zf, OPENCLAW_FILE, "特殊处理-配置文件")
            pack_special_path(zf, EXTENSIONS_FILE, "特殊处理-extensions", exclude_dirs=['xiaoyi-channel'], skip_hidden=True)

            special_list = get_properties(zip_input_files_url, "special_list") or []
            for special_file in special_list:
                pack_special_path(zf, special_file, special_file)


            # 特殊处理：打包当天修改的 sessions 目录（仅普通备份）
            if extra_dirs:
                for extra_dir in extra_dirs:
                    if os.path.exists(extra_dir):
                        extra_dir_name = os.path.relpath(extra_dir, BASE_DIR)

                        if os.path.isfile(extra_dir):
                            # 单个文件
                            zf.write(extra_dir, extra_dir_name)
                            logger.info(f"[Sessions备份] 打包文件: {extra_dir_name}")
                        elif os.path.isdir(extra_dir):
                            # 目录，递归打包内容
                            for root, _, files in os.walk(extra_dir, followlinks=False):
                                for file in files:
                                    file_path = os.path.join(root, file)
                                    rel_path = os.path.relpath(file_path, start=extra_dir)
                                    arcname = os.path.join(extra_dir_name, rel_path)

                                    if os.path.islink(file_path):
                                        # 符号链接：保存链接本身
                                        try:
                                            link_target = os.readlink(file_path)
                                            zip_info = zipfile.ZipInfo(arcname)
                                            zip_info.create_system = 3
                                            zip_info.external_attr = (0o120777 << 16)
                                            zf.writestr(zip_info, link_target)
                                            logger.info(f"[Sessions备份] 打包符号链接: {arcname} -> {link_target}")
                                        except Exception as e:
                                            logger.error(f"[Sessions备份] 无法添加符号链接 {file_path}: {e}")
                                    else:
                                        zf.write(file_path, arcname)
                            logger.info(f"[Sessions备份] 打包目录: {extra_dir_name}")
                    else:
                        logger.warning(f"[Sessions备份] 路径不存在: {extra_dir}")

        # 验证打包结果
        if not os.path.exists(output_zip_abs):
            logger.error(f"错误：打包完成但未找到文件 {output_zip_abs}")
            return None
        file_size = os.path.getsize(output_zip_abs) / 1024 / 1024
        logger.info(f"打包成功：{output_zip_abs}（大小：{file_size:.2f} MB）")
        return output_zip_abs

    except Exception as e:
        logger.error(f"打包失败：{e}")
        # 清理不完整文件
        if os.path.exists(output_zip_abs):
            os.remove(output_zip_abs)
        return None


def get_total_size_of_paths(paths):
    """计算指定路径列表的总大小（字节），递归统计目录"""
    total = 0
    import stat
    for path in paths:
        if not os.path.exists(path):
            continue
        if os.path.isfile(path) and not os.path.islink(path):
            # 只统计普通文件，跳过符号链接
            try:
                total += os.path.getsize(path)
            except OSError:
                continue
        elif os.path.isdir(path):
            for root, _, files in os.walk(path, followlinks=False):
                for file in files:
                    file_path = os.path.join(root, file)
                    try:
                        # 使用lstat不跟随符号链接，只统计普通文件
                        file_stat = os.lstat(file_path)
                        if stat.S_ISREG(file_stat.st_mode):
                            total += file_stat.st_size
                    except OSError:
                        continue
    return total


def get_dir_used_size(dir_path):
    """获取目录实际已使用大小（字节）"""
    total_size = 0
    for dirpath, _, filenames in os.walk(dir_path):
        for filename in filenames:
            filepath = os.path.join(dirpath, filename)
            # 跳过符号链接，避免重复计算
            if not os.path.islink(filepath):
                try:
                    total_size += os.path.getsize(filepath)
                except OSError:
                    pass
    return total_size


def check_space_within_limit(target_path, required_size, safety_margin=0.1, min_free_after=200*1024*1024):
    """检查目录已用空间是否在规格上限内"""
    used_size = get_dir_used_size(BASE_DIR)
    limit_gb = get_properties(zip_input_files_url, "backup_limit_gb") or 30
    compression_ratio = get_properties(zip_input_files_url, "compression_ratio") or 0.8
    limit_bytes = limit_gb * 1024 * 1024 * 1024  # 30G 转 字节

    used_gb = used_size / (1024 ** 3)
    logger.info(f"目录已用: {used_size:.2f}KB, 上限: {limit_gb}G")
    required = int((required_size * compression_ratio) * (1 + safety_margin)) + min_free_after
    free = limit_bytes - used_size
    if free >= required:
        return True, 0, required
    else:
        logger.warning(f"超出规格上限: 已用 {used_gb:.2f}G > {limit_gb}G")
        return False, free, required


def check_disk_space_enough(target_path, required_size, safety_margin=0.1, min_free_after=200*1024*1024):
    """跨系统磁盘空间检测（兼容Windows/Linux）"""
    try:
        # 提取磁盘根目录（Windows取盘符，Linux取/）
        target_abs = os.path.abspath(target_path)
        if sys.platform.startswith('win32'):
            drive, _ = os.path.splitdrive(target_abs)
            disk_root = f"{drive}\\" if drive else os.path.splitdrive(os.getcwd())[0] + "\\"
        else:
            disk_root = "/"

        usage = shutil.disk_usage(disk_root)
        required = int(required_size * (1 + safety_margin)) + min_free_after
        logger.info(f"获取磁盘空间：{usage}")
        return usage.free >= required, usage.free, required
    except Exception as e:
        logger.error(f"获取磁盘空间失败：{e}")
        logger.error("警告：磁盘空间检测异常，将继续执行打包（请确保磁盘空间充足）")
        return True, 0, required_size


def delete_zip_file(zip_file_path):
    """
    删除指定的压缩包文件（增加完善的校验）
    :param zip_file_path: 压缩包绝对路径
    :return: True（删除成功）/ False（删除失败）
    """
    # 校验路径是否有效
    if not zip_file_path or not os.path.exists(zip_file_path):
        logger.info(f"警告：压缩包 {zip_file_path} 不存在，无需删除！")
        return False

    # 校验是否是文件（避免误删目录）
    if not os.path.isfile(zip_file_path):
        logger.error(f"错误：{zip_file_path} 不是文件，无法删除！")
        return False

    # 执行删除
    try:
        os.remove(zip_file_path)
        # 二次验证是否删除成功
        if not os.path.exists(zip_file_path):
            logger.info(f"成功删除压缩包：{zip_file_path}")
            return True
        else:
            logger.error(f"错误：执行删除后，压缩包 {zip_file_path} 仍存在！")
            return False
    except PermissionError:
        logger.error(f"错误：权限不足，无法删除 {zip_file_path}（可尝试 sudo 运行脚本）")
        return False
    except Exception as e:
        logger.error(f"删除压缩包失败：{str(e)}")
        return False


def do_pack(zip_path, zip_files, extra_dirs=None, pack_scope=None):
    """
    只打包，不上传
    """
    logger.info("=" * 50)
    logger.info("OpenClaw 数据打包")
    logger.info("=" * 50)
    result = {
        "status": "success",
        "mode": "pack",
        "file_path": None,
        "file_size_mb": None
    }
    if not zip_files:
        logger.error("错误：获取打包目录列表失败")
        result["status"] = "error"
        result["message"] = "获取打包目录列表失败"
        print(json.dumps(result))
        return None
    # 检查 zip_files 是否为列表且不为空
    if not isinstance(zip_files, list) or not zip_files:
        logger.error("错误：获取打包目录列表失败")
        result["status"] = "error"
        result["message"] = "获取打包目录列表失败"
        print(json.dumps(result))
        return None

    # 打包
    # 指定要上传的文件路径
    output_temp_file = zip_path
    logger.info(f"\n开始打包文件到 {output_temp_file} ...")
    file_path = zip_dirs(output_temp_file, zip_files, extra_dirs=extra_dirs, pack_scope=pack_scope)  # 替换为实际文件路径
    if file_path is None:
        logger.error("错误：文件打包失败")
        result["status"] = "error"
        result["message"] = "文件打包失败"
        print(json.dumps(result))
        return None

    logger.info(f"\n打包完成: {file_path}")
    logger.info(f"文件大小: {os.path.getsize(file_path) / 1024 / 1024:.2f} MB")
    result["file_path"] = file_path
    result["file_size_mb"] = round(os.path.getsize(file_path) / 1024 / 1024, 2)
    print(json.dumps(result))
    return file_path


def do_upload(auth, file_path, zip_files, file_id_file_url):
    """
    上传指定的压缩包到云空间
    """

    result = {
        "status": "success",
        "mode": "upload",
        "file_id": None,
        "file_path": file_path
    }

    if not os.path.exists(file_path):
        logger.info(f"错误：备份文件不存在: {file_path}，请先执行 pack 模式")
        result["status"] = "error"
        result["message"] = f"错误：备份文件不存在: {file_path}，请先执行 pack 模式"
        print(json.dumps(result))
        return


    logger.info("=" * 50)
    logger.info("OpenClaw 备份上传")
    logger.info("=" * 50)
    logger.info(f"上传文件: {file_path}")
    logger.info(f"文件大小: {os.path.getsize(file_path) / 1024 / 1024:.2f} MB")

    hwDrive = UploadFileHwDrive(auth)

    try:
        # 创建云空间目录
        create_result = hwDrive.create_file_dir()
        if not create_result:
            logger.error("错误：创建云空间目录失败")
            result["status"] = "error"
            result["message"] = "错误：创建云空间目录失败"
            print(json.dumps(result))
            return
        logger.info(f"目录创建成功: {create_result['id']}")

        # 创建云空间文件
        file_name = os.path.basename(file_path)
        file_size = str(os.path.getsize(file_path))
        create_resume_result = hwDrive.create_file_resume(
            f"openclaw_{file_name}",
            create_result["id"],
            file_size
        )
        if not create_resume_result:
            logger.error("错误：创建上传任务失败")
            result["status"] = "error"
            result["message"] = "错误：创建上传任务失败"
            print(json.dumps(result))
            return

        slice_size = create_resume_result["sliceSize"]
        upload_url = create_resume_result["uploadUrl"]
        server_id, upload_id = parse_url(upload_url)

        # 上传文件
        resume_result = hwDrive.upload_file_in_chunks(
           server_id, upload_id, file_path, slice_size
        )
        if not resume_result:
            logger.error("错误：文件上传失败")
            result["status"] = "error"
            result["message"] = "错误：文件上传失败"
            print(json.dumps(result))
            return

        logger.info(f"上传成功，文件ID: {resume_result}")

        # 保存记录
        write_url_to_file(resume_result, file_id_file_url)
        result["file_id"] = resume_result
        print(json.dumps(result))
        logger.info("备份记录已保存")
        delete_zip_file(file_path)
        logger.info(f"已删除本地压缩包: {file_path}")
        return

    except Exception as e:
        logger.error(f"上传过程发生错误: {str(e)}")
        result["status"] = "error"
        result["message"] = f"上传过程发生错误: {str(e)}"
        print(json.dumps(result))
        return


def main():
    args = parse_arguments()
    if args.mode == "pack":
        # 打包模式
        if args.packScope == "all":
            logger.info("开始备份openclaw全量数据")
            # 下载配置文件获取待打包目录
            zip_files = get_properties(zip_input_files_url, "zip_input_all_files")
            do_pack(pack_all_file_path, zip_files, pack_scope=args.packScope)
        else:
            logger.info("开始备份openclaw数据")
            # 下载配置文件获取待打包目录
            today_session_dirs = get_today_session_dirs()
            zip_files = get_properties(zip_input_files_url, "zip_input_files")
            do_pack(pack_file_path, zip_files, extra_dirs=today_session_dirs, pack_scope=args.packScope)
    elif args.mode == "upload":
        # 上传模式
        if not args.Authorization:
            logger.error("错误：upload模式需要提供 --Authorization 参数")
            print(json.dumps({"status": "error", "message": "upload模式需要提供 --Authorization 参数"}))
            sys.exit(1)
        if args.packScope == "all":
            logger.info("开始上传openclaw全量数据")
            # 下载配置文件获取待打包目录
            zip_files = get_properties(zip_input_files_url, "zip_input_all_files")
            do_upload(args.Authorization, pack_all_file_path, zip_files, write_all_file_id_to_file_url)
        else:
            logger.info("开始上传openclaw数据")
            # 下载配置文件获取待打包目录
            zip_files = get_properties(zip_input_files_url, "zip_input_files")
            do_upload(args.Authorization, pack_file_path, zip_files, write_file_id_to_file_url)


if __name__ == "__main__":
    main()