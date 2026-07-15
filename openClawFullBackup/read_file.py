import os
import sys
import json
import argparse
import logging

# 全局配置
BASE_DIR = "/home/sandbox/.openclaw"
CONFIG_URL = "https://h5hosting-drcn.dbankcdn.cn/cch5/HAG/D1E0CiDwLKSTla6LZEqEHDzqA/import.json"

# 日志配置
LOG_DIR = "/tmp/logs"
LOG_FILE_PATH = os.path.join(LOG_DIR, "read_file.log")

os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)

if not os.path.exists(LOG_FILE_PATH):
    with open(LOG_FILE_PATH, 'w') as f:
        pass
    os.chmod(LOG_FILE_PATH, 0o600)

logging.basicConfig(
    level=logging.INFO,
    handlers=[
        logging.FileHandler(LOG_FILE_PATH, encoding='utf-8'),
    ],
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def parse_arguments():
    parser = argparse.ArgumentParser(description="读取文件内容并返回镜像版本")
    parser.add_argument("--file", type=str, required=True, help="要读取的文件路径")
    return parser.parse_args()


def get_version_from_config():
    """从配置文件获取镜像版本"""
    try:
        import requests
        response = requests.get(CONFIG_URL, stream=True, timeout=60)
        response.raise_for_status()
        content = response.json()
        version = content.get("version", "unknown")
        logger.info(f"获取版本: {version}")
        return version
    except Exception as e:
        logger.error(f"获取版本失败: {e}")
        return "unknown"


def read_file_content(file_path):
    """
    读取文件内容

    :param file_path: 文件路径
    :return: (success, content) 元组
    """
    # 检查文件是否存在
    if not os.path.exists(file_path):
        logger.warning(f"文件不存在: {file_path}")
        return False, None

    # 检查是否是文件
    if not os.path.isfile(file_path):
        logger.warning(f"路径不是文件: {file_path}")
        return False, None

    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as file:
            content = file.read()
        logger.info(f"读取文件成功: {file_path}, 大小: {len(content)} 字节")
        return True, content
    except Exception as e:
        logger.error(f"读取文件失败: {file_path}, 错误: {e}")
        return False, None


def main():
    args = parse_arguments()
    file_path = args.file

    # 获取镜像版本
    version = get_version_from_config()

    # 读取文件内容
    success, content = read_file_content(file_path)

    # 构造返回结果
    if success:
        result = {
            "status": "success",
            "version": version,
            "file": file_path,
            "content": content
        }
    else:
        result = {
            "status": "error",
            "version": version,
            "file": file_path,
            "content": None,
            "message": "文件不存在"
        }

    print(json.dumps(result, ensure_ascii=False))
    logger.info(f"返回结果: status={result['status']}, version={version}")


if __name__ == "__main__":
    main()