import argparse
import json
import os
import sys
import uuid
import logging
import requests
import tempfile

# 配置文件路径
write_file_id_to_file_url = "/home/sandbox/.openclaw/fileIdObj.txt"
write_all_file_id_to_file_url = "/home/sandbox/.openclaw/fileIdObjForAll.txt"
# 云空间 API 地址
zip_input_files_url = "https://h5hosting-drcn.dbankcdn.cn/cch5/HAG/D1E0CiDwLKSTla6LZEqEHDzqA/import.json"
BASE_DIR = "/home/sandbox/.openclaw"

# 日志文件路径
LOG_DIR = "/tmp/logs"
LOG_PATH = os.path.join(LOG_DIR, f"{os.path.basename(__file__).replace('.py', '')}.log")

# 创建日志目录并设置权限
os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)

# 创建日志文件并设置权限（如果不存在）
if not os.path.exists(LOG_PATH):
    with open(LOG_PATH, 'w') as f1:
        pass
    os.chmod(LOG_PATH, 0o600)

# 配置日志
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8')
    ],
    level=logging.INFO
)
logger = logging.getLogger(__name__)



def parse_arguments():
    parser = argparse.ArgumentParser(description="读取 fileId 并验证")
    parser.add_argument("--fileId", type=str, help="文件Id")
    parser.add_argument(
        "--packScope",
        choices=["all", "normal"],
        default="normal",
        help="备份范围：all=zip_input_all_files, upload=zip_input_files"
        )
    parser.add_argument("--Authorization", required=True, type=str, help="用于云空间接口at")
    return parser.parse_args()


def read_stored_fileid(FILE_ID_PATH):
    """
    读取本地存储的 fileId

    Returns:
        str: 存储的 fileId，文件不存在或格式错误返回 None
    """
    if not os.path.exists(FILE_ID_PATH):
        logger.info(f"Warning: File not found: {FILE_ID_PATH}")
        return None

    try:
        with open(FILE_ID_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)

            # 支持新格式 {"fileId": "xxx"}
        if isinstance(data, dict):
            return data.get("fileId")

            # 兼容旧格式 [{"objectId": "xxx"}]
        elif isinstance(data, list) and len(data) > 0:
            return data[-1].get("objectId")

        else:
            logger.info(f"Warning: Invalid data format in {FILE_ID_PATH}")
            return None

    except json.JSONDecodeError:
        logger.error(f"Error: Invalid JSON format in {FILE_ID_PATH}")
        return None
    except Exception as e:
        logger.error(f"Error: Failed to read file: {e}")
        return None


def delete_cloud_file(file_id, auth_token):
    """
    调用云空间接口删除文件

    Args:
        file_id: 要删除的文件 ID
        auth_token: Authorization Token

    Returns:
        bool: 是否删除成功
    """

    trace_id = str(uuid.uuid4())[:16]
    cloud_namespace_base_url = get_properties(zip_input_files_url, "cloud_namespace_base_url")
    url = f"{cloud_namespace_base_url}/drive/v1/files/{file_id}"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "x-hw-trace-id": trace_id,
    }

    logger.info(f"Deleting cloud file: {file_id}, traceId: {trace_id}")

    try:
        response = requests.delete(url, headers=headers, timeout=60, verify=True)

        if response.status_code == 204:
            logger.info(f"Success: File {file_id} deleted from cloud.")
            return True
        else:
            logger.info(f"Warning: Failed to delete file {file_id}, status: {response.status_code}")
            logger.info(f"Response: {response.text}")
            return False

    except Exception as e:
        logger.error(f"Error: Exception while deleting file: {e}")
        return False


def get_properties(url, setting_key):
    try:
        resps = requests.get(url, stream=True, timeout=60)
        resps.raise_for_status()

        try:
            contents = resps.json()
        except requests.exceptions.JSONDecodeError:
            logger.error("下载的文件不是有效的JSON格式。")
            return None

        # 读配置
        result = contents.get(setting_key)

        return result
    except Exception as e:
        logger.error(f"获取配置失败 : {e}")
        return None


def main():
    args = parse_arguments()

    result = {
        "status": "success",
        "input_fileId": args.fileId,
        "stored_fileId": None,
        "action": None
    }

    logger.info("=" * 50)
    logger.info("FileId 验证工具")
    logger.info("=" * 50)

    stored_fileid = None
    # 读取本地存储的 fileId
    if args.packScope == "all":
        logger.info(f"\n[1/3] 读取本地 fileIdForAll: {write_all_file_id_to_file_url}")
        stored_fileid = read_stored_fileid(write_all_file_id_to_file_url)
    else:
        logger.info(f"\n[1/3] 读取本地 fileId: {write_file_id_to_file_url}")
        stored_fileid = read_stored_fileid(write_file_id_to_file_url)

    if stored_fileid is None:
        logger.error("Error: 无法读取本地 fileId")
        result["status"] = "error"
        result["message"] = "无法读取本地 fileId"
        print(json.dumps(result))
        sys.exit(0)

    logger.info(f"本地 fileId: {stored_fileid}")
    logger.info(f"入参 fileId: {args.fileId}")

    # 对比 fileId
    logger.info(f"\n[2/3] 验证 fileId...")

    if args.fileId == stored_fileid:
        result["action"] = "none"
        logger.info("✓ fileId 一致，无需操作")
    else:
        result["action"] = "delete"
        logger.info("✗ fileId 不一致")
        logger.info(f"  准备删除入参 fileId: {args.fileId}")

        # 删除云空间中的旧文件
        delete_success = delete_cloud_file(args.fileId, args.Authorization)

        # 删除失败时返回错误
        if not delete_success:
            result["status"] = "warning"
            result["message"] = "删除云空间文件失败"
            logger.info(f"\nError: 删除云空间文件失败: {args.fileId}")

    result["returned_fileId"] = stored_fileid

        # 返回本地存储的 fileId
    logger.info(f"\n[3/3] 返回结果")
    logger.info(f"=" * 50)
    logger.info(f"RESULT_FILE_ID:{stored_fileid}")
    logger.info(f"=" * 50)

    # 输出到 stdout 供其他程序捕获
    print(json.dumps(result, ensure_ascii=False))

    return stored_fileid


if __name__ == "__main__":
    main()