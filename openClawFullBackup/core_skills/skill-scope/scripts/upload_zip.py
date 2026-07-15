import hashlib
import json
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
import requests
from tqdm import tqdm
import sys
import zipfile
import warnings
warnings.filterwarnings('ignore', category=DeprecationWarning)
warnings.filterwarnings('ignore', category=requests.packages.urllib3.exceptions.InsecureRequestWarning)

MAX_TIMES = 3
SUCCESS_STATUS_CODE = 200
EXPIRE_TIME = 259200
# 超时时间：连接超时15秒，上传/读取超时300秒（5分钟）
connect_timeout = 15
read_timeout = 300
ENV_FILE_PATH = '/home/sandbox/.openclaw/.xiaoyienv'
class EnvConfig:
    def __init__(self):
        self.apiKey = ''
        self.uid = ''
        self.serviceUrl = ''


def load_env_config() -> EnvConfig:
    config = EnvConfig()

    try:
        with open(ENV_FILE_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                trimmed = line.strip()
                if not trimmed or trimmed.startswith('#'):
                    continue
                parts = trimmed.split('=', 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    value = parts[1].strip()
                    if key == 'PERSONAL-API-KEY':
                        config.apiKey = value
                    elif key == 'PERSONAL-UID':
                        config.uid = value
                    elif key == 'SERVICE_URL':
                        config.serviceUrl = value
    except:
        pass

    return config
# 项目配置参数
osms_prepare_URL_SUFFIX = '/osms/v1/file/manager/prepare'
osms_complete_URL_SUFFIX = '/osms/v1/file/manager/completeAndQuery'
env_config = load_env_config()
if not env_config.serviceUrl:
    raise Exception("SERVICE_URL is not configured")

osms_prepare_url = env_config.serviceUrl + osms_prepare_URL_SUFFIX
osms_complete_url = env_config.serviceUrl + osms_complete_URL_SUFFIX

api_key = env_config.apiKey
x_uid = env_config.uid

write_url_to_file_file_name = "url.txt"
zip_input_files_url = "https://h5hosting-drcn.dbankcdn.cn/cch5/HAG/D1E0CiDwLKSTla6LZEqEHDzqA/import.json"

def parse_args():
    """解析命令行参数"""
    if len(sys.argv) < 2:
        print("Usage: python upload_zip.py <skill_directory>")
        sys.exit(1)
    return sys.argv[1]

zip_input_files = None  # 将由命令行参数决定




class UploadFileOSMS(object):
    def __init__(self):
        self.trace_id = str(uuid.uuid4())[:16]
        self.isPermanentStoragePublic = False
        self.x_uid = x_uid
        self.api_key = api_key

    def invoking_osms_prepare(self, file_path):
        """ 上传 osms prepare """
        # print(f"Invoke the osms prepare interface, trace_id: {self.trace_id}")
        for times in range(0, MAX_TIMES):
            time_stamp, headers = self.build_osms_headers()
            body = {
                "useEdge": False,
                "objectType": "TEMPORARY_MATERIAL_PACKAGE",
                "fileName": os.path.basename(file_path),
                "fileSha256": self.calculate_file_sha256(file_path),
                "fileSize": os.path.getsize(file_path),
                "fileOwnerInfo": {
                    "uid": "openclaw",
                    "teamId": "openclaw"
                }
            }

            if self.isPermanentStoragePublic:  # 只用于保存结果件中的图片链接
                body["objectType"] = "TEMPORARY_MATERIAL_PACKAGE"

            try:
                response = requests.post(url=osms_prepare_url, headers=headers, json=body, timeout=(connect_timeout, read_timeout), verify=False)

                if response.status_code != SUCCESS_STATUS_CODE:
                    continue
                resp = response.json()
                if 'objectId' not in resp.keys() \
                        or 'draftId' not in resp.keys() or 'uploadInfos' not in resp.keys():
                    error = "The hag osms prepare interface returns an exception"
                    raise error
                if not resp["uploadInfos"]:
                    error = "The hag osms prepare interface uploadInfos returns is empty"
                    raise error
                if 'url' not in resp["uploadInfos"][0].keys() or \
                        'headers' not in resp["uploadInfos"][0].keys():
                    error = ("The hag osms prepare interface url and headers for uploadInfos "
                             "map returns is empty")
                    raise error

                return resp
            except Exception as e:
                print(
                    "{}st invoking Hag_OSMS_Prepare interface throws the exception: {}.".format(times + 1, str(e)))
                if times == MAX_TIMES - 1:
                    raise e
        return {}

    def read_file_as_bytes(self, file_path):
        # 读取任意文件的原始二进制内容
        result = {
            "success": False,
            "file_name": os.path.basename(file_path),
            "bytes": None,
            "error": None
        }

        try:
            with open(file_path, 'rb') as f:
                byte_content = f.read()
                result.update({
                    "success": True,
                    "bytes": byte_content
                })

        except PermissionError:
            result["error"] = "没有读取权限"
        except Exception as e:
            result["error"] = f"读取失败: {str(e)}"

        return result

    def upload_file_to_obs(self, file_info, file_bytes):
        retry_delay = 1  # 初始重试延迟(秒)
        retry_time = 1  # 当前块重试次数
        while True:
            try:
                response = requests.put(file_info["url"], data=file_bytes, headers=file_info["headers"],
                                        timeout=(connect_timeout, read_timeout), verify=False)
                # print("response", response)
                response.raise_for_status()
                return True
            except Exception as e:
                retry_time += 1
                time.sleep(retry_delay * (2 ** retry_time))
                if retry_time > MAX_TIMES:
                    print("{}上传文件到 obs 报错{}".format(file_info["url"], str(e)))
                    return False

    def invoking_osms_complete(self, file_info):
        for times in range(0, MAX_TIMES):

            headers = {
                "Content-Type": "application/json",
                "x-request-from": "openclaw",
                "x-uid": self.x_uid,
                "x-api-key": self.api_key,
                "x-hag-trace-id": self.trace_id
            }

            body = {
                "objectId": file_info["objectId"],
                "draftId": file_info["draftId"],
                "expireTime": EXPIRE_TIME,
            }

            try:
                response = requests.post(url=osms_complete_url, headers=headers, json=body, timeout=(connect_timeout, read_timeout), verify=False)

                if response.status_code != SUCCESS_STATUS_CODE:
                    continue
                return (True, response.json())
            except Exception as e:
                print(
                    f"{times + 1}st invoking Hag_OSMS_Prepare interface throws the exception: {str(e)}.")
                if times == MAX_TIMES - 1:
                    raise e
        return (False, None)

    def build_osms_headers(self) -> tuple[str, dict]:
        time_stamp = self.format_date()
        # todo
        file_path = ENV_FILE_PATH
        config = self.read_env_file(file_path)
        self.x_uid = config.get('PERSONAL-UID')
        self.api_key = config.get('PERSONAL-API-KEY')
        headers = {
            "Content-Type": "application/json",
            "x-request-from": "openclaw",
            "x-uid": self.x_uid,
            "x-api-key": self.api_key,
            "x-hag-trace-id": self.trace_id
        }
        return time_stamp, headers

    def format_date(self):
        time_stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        time_stamp = time_stamp[:len(time_stamp) - 3]
        return time_stamp

    def read_env_file(self,filepath):
        """读取 key=value 格式的文件，返回字典"""
        env_vars = {}
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    # 跳过空行和注释
                    if not line or line.startswith('#'):
                        continue
                    if '=' in line:
                        key, value = line.split('=', 1)
                        env_vars[key.strip()] = value.strip()
        except FileNotFoundError:
            print(f"文件 {filepath} 不存在")
        except Exception as e:
            print(f"读取文件出错: {e}")
        return env_vars

    def calculate_file_sha256(self, file_path):
        sha256 = hashlib.sha256()
        file_size = os.path.getsize(file_path)

        with open(file_path, 'rb') as f, tqdm(total=file_size, unit='B', unit_scale=True,
                                              desc="Calculating SHA256") as pbar:
            while chunk := f.read(4096):  # 分块4kb
                sha256.update(chunk)
                pbar.update(len(chunk))

        return sha256.hexdigest()


def write_url_to_file(prepare_response, filename):
    """
    将objectId和url写入文件，格式为指定JSON数组，最多保留3条数据
    :param prepare_response: 包含uploadInfos和objectId的字典
    :param filename: 要写入的文件路径
    :return: True（成功）/ False（失败）
    """
    try:
        # ========== 1. 校验prepare_response数据合法性 ==========
        # 检查uploadInfos是否存在且为非空列表
        if 'uploadInfos' not in prepare_response or not isinstance(prepare_response['uploadInfos'], list):
            print("Error: 'uploadInfos' is not present or not a list.")
            return False
        if len(prepare_response['uploadInfos']) == 0:
            print("Error: 'uploadInfos' list is empty.")
            return False

        # 提取objectId和nspUrl（url）
        object_id = prepare_response.get('objectId', '')
        first_obj = prepare_response['uploadInfos'][0]
        nsp_url = first_obj.get('url', '')
        if not nsp_url:
            print("Error: 'url' not found in the first object of uploadInfos.")
            return False

        # ========== 2. 读取现有文件（若存在） ==========
        existing_data = []
        if os.path.exists(filename):
            try:
                with open(filename, 'r', encoding='utf-8') as file:
                    # 读取现有JSON数据
                    existing_data = json.load(file)
                    # 校验现有数据是否为列表
                    if not isinstance(existing_data, list):
                        print("Warning: Existing file content is not a list, will overwrite.")
                        existing_data = []
            except json.JSONDecodeError:
                print("Warning: Existing file content is invalid JSON, will overwrite.")
                existing_data = []
            except Exception as e:
                print(f"Warning: Failed to read existing file: {e}, will overwrite.")
                existing_data = []

        # ========== 3. 控制数据条数（最多保留3条） ==========
        # 构造新数据项
        new_item = {
            "objectId": object_id,
            "nspUrl": nsp_url
        }

        # 添加新数据到列表末尾
        existing_data.append(new_item)

        # 若超过3条，删除第一条（最早的）
        if len(existing_data) > 3:
            existing_data.pop(0)
            print(f"Notice: Data exceeds 3 items, deleted the first old item.")

        # ========== 4. 写入文件（格式化JSON） ==========
        with open(filename, 'w', encoding='utf-8') as file:
            # indent=4 格式化输出，ensure_ascii=False 支持中文
            json.dump(existing_data, file, indent=4, ensure_ascii=False)


        return True

    except Exception as e:
        print(f"An error occurred: {e}")
        return False

def upload_skill_with_size(skill_dir: str) -> tuple:
    """上传 Skill 目录，返回下载 URL 和文件大小"""
    zip_input_files = [skill_dir]
    
    uploader = UploadFileOSMS()
    skill_name = os.path.basename(skill_dir)
    time_stamp = uploader.format_date()
    output_temp_file = f"/tmp/{skill_name}_{time_stamp}.zip"
    
    file_path = zip_dirs(output_temp_file, zip_input_files)
    file_size = os.path.getsize(file_path)
    
    try:
        prepare_response = uploader.invoking_osms_prepare(file_path)
        if not prepare_response:
            raise Exception("Failed to get a valid response from the prepare interface.")
        
        file_bytes_result = uploader.read_file_as_bytes(file_path)
        if not file_bytes_result["success"]:
            delete_zip_file(file_path)
            raise Exception(f"Failed to read file: {file_bytes_result['error']}")
        
        file_bytes = file_bytes_result["bytes"]
        
        upload_success = uploader.upload_file_to_obs(prepare_response["uploadInfos"][0], file_bytes)
        if not upload_success:
            delete_zip_file(file_path)
            raise Exception("Failed to upload file to OBS.")
        
        complete_success, complete_response = uploader.invoking_osms_complete(prepare_response)
        if not complete_success:
            delete_zip_file(file_path)
            raise Exception("Failed to complete the file upload.")
        
        nsp_url = complete_response.get("fileDetailInfo").get("url", "")
        delete_zip_file(file_path)
        return nsp_url, file_size
    except Exception as e:
        delete_zip_file(file_path)
        raise Exception(f"Upload failed: {str(e)}")

def upload_skill(skill_dir: str) -> str:
    """上传 Skill 目录，返回下载 URL (兼容旧接口)"""
    url, _ = upload_skill_with_size(skill_dir)
    return url


def main():
    skill_dir = parse_args()
    download_url, file_size = upload_skill_with_size(skill_dir)
    print(f"DOWNLOAD_URL={download_url}")
    print(f"FILE_SIZE={file_size}")


def download_file(url):
    try:
        response = requests.get(url, stream=True, verify=False, timeout=30)
        response.raise_for_status()
        # 解析 JSON 内容
        try:
            content = response.json()
        except requests.exceptions.JSONDecodeError:
            return None

        # 获取 zip_input_files 列表
        result = content.get("zip_input_files")
        # print(f"json content: {result}")
        # 检查 zip_input_files 是否为列表且不为空
        if not isinstance(result, list) or not result:
            return None

        return result
    except Exception as e:
        return None


def zip_dirs(output_zip_path, dir_paths, prefix="result"):
    # 1. 基础校验：判断output_zip_path是目录还是文件路径
    if os.path.isdir(output_zip_path):
        # 若传入的是目录，自动生成带时间戳的文件名（兼容旧逻辑）
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        safe_ts = ts.replace(":", "").replace("\\", "").replace("/", "")
        zip_filename = f"{prefix}{safe_ts}.zip"
        output_zip_abs = os.path.abspath(os.path.join(output_zip_path, zip_filename))
    else:
        # 核心优化：若传入的是文件路径，直接使用（无多余目录层级）
        output_zip_abs = os.path.abspath(output_zip_path)
        # 确保文件所在目录存在
        zip_dir = os.path.dirname(output_zip_abs)
        os.makedirs(zip_dir, exist_ok=True)

    # 2. 待打包目录校验
    if not dir_paths:
        print("错误：待打包目录列表不能为空")
        return None
    valid_dirs = [d for d in dir_paths if os.path.exists(d) and os.path.isdir(d)]
    if not valid_dirs:
        print("错误：无有效待打包目录")
        return None

    # 3. 磁盘空间检测
    total_size = get_total_size_of_paths(valid_dirs)
    if total_size == 0:
        print("警告：待打包文件总大小为0，仍将生成空压缩包")
    enough, free, required = check_disk_space_enough(os.path.dirname(output_zip_abs), total_size)
    if not enough:
        print(f"错误：磁盘空间不足！可用 {free / 1024 / 1024:.2f} MB，需要 {required / 1024 / 1024:.2f} MB（含10%安全余量）")
        return None

    # 4. 执行打包（跨系统路径兼容）
    try:
        with zipfile.ZipFile(output_zip_abs, 'w', zipfile.ZIP_DEFLATED) as zf:
            for dir_path in valid_dirs:
                dir_abs = os.path.abspath(dir_path)
                # 遍历目录并添加文件（压缩包内保留文件相对路径，无多余层级）
                for root, _, files in os.walk(dir_abs):
                    for file in files:
                        file_path = os.path.join(root, file)
                        # Windows长路径兼容
                        if sys.platform.startswith('win32') and len(file_path) > 255:
                            file_path = f"\\\\?\\{file_path}"
                        # 核心：压缩包内以待打包目录为根，保留子层级（无多余空目录）
                        arcname = os.path.relpath(file_path, start=dir_abs)
                        zf.write(file_path, arcname)

        # 验证打包结果
        if not os.path.exists(output_zip_abs):
            print(f"错误：打包完成但未找到文件 {output_zip_abs}")
            return None
        file_size = os.path.getsize(output_zip_abs) / 1024 / 1024
        # print(f"打包成功：{output_zip_abs}（大小：{file_size:.2f} MB）")
        return output_zip_abs

    except Exception as e:
        print(f"打包失败：{e}")
        # 清理不完整文件
        if os.path.exists(output_zip_abs):
            os.remove(output_zip_abs)
        return None


def get_total_size_of_paths(paths):
    """计算指定路径列表的总大小（字节），递归统计目录"""
    total = 0
    for path in paths:
        if not os.path.exists(path):
            continue
        if os.path.isfile(path):
            total += os.path.getsize(path)
        else:
            for root, _, files in os.walk(path):
                for file in files:
                    try:
                        total += os.path.getsize(os.path.join(root, file))
                    except (PermissionError, FileNotFoundError):
                        continue
    return total


def check_disk_space_enough(target_path, required_size, safety_margin=0.1):
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
        required = int(required_size * (1 + safety_margin))
        # print(f"获取磁盘空间：{usage}")
        return usage.free >= required, usage.free, required
    except Exception as e:
        print(f"获取磁盘空间失败：{e}")
        print("警告：磁盘空间检测异常，将继续执行打包（请确保磁盘空间充足）")
        return True, 0, required_size


def delete_zip_file(zip_file_path):
    """
    删除指定的压缩包文件（增加完善的校验）
    :param zip_file_path: 压缩包绝对路径
    :return: True（删除成功）/ False（删除失败）
    """
    # 校验路径是否有效
    if not zip_file_path or not os.path.exists(zip_file_path):
        print(f"警告：压缩包 {zip_file_path} 不存在，无需删除！")
        return False

    # 校验是否是文件（避免误删目录）
    if not os.path.isfile(zip_file_path):
        print(f"错误：{zip_file_path} 不是文件，无法删除！")
        return False

    # 执行删除
    try:
        os.remove(zip_file_path)
        # 二次验证是否删除成功
        if not os.path.exists(zip_file_path):
            # print(f"成功删除压缩包：{zip_file_path}")
            return True
        else:
            print(f"错误：执行删除后，压缩包 {zip_file_path} 仍存在！")
            return False
    except PermissionError:
        print(f"错误：权限不足，无法删除 {zip_file_path}（可尝试 sudo 运行脚本）")
        return False
    except Exception as e:
        print(f"删除压缩包失败：{str(e)}")
        return False


if __name__ == "__main__":
    main()
