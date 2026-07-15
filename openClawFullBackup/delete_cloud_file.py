"""
OpenClaw 云空间文件删除脚本
- 根据 fileId 删除云空间中的指定文件
- 支持批量删除（fileId 逗号分隔）
- 入参通过 --jsonStr 传入
"""

import argparse
import json
import os
import sys
import uuid
import logging
import requests

# 配置
BASE_DIR = "/home/sandbox/.openclaw"
LOG_DIR = "/tmp/logs"
CONFIG_URL = "https://h5hosting-drcn.dbankcdn.cn/cch5/HAG/D1E0CiDwLKSTla6LZEqEHDzqA/import.json"

# 日志配置
os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "delete_cloud_file.log")
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


def get_properties(url, setting_key):
    """从远程 JSON 获取配置"""
    try:
        resp = requests.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        content = resp.json()
        respResult = content.get(setting_key)
        logger.info(f"获取配置 {setting_key}: {'成功' if respResult else '失败'}")
        return respResult
    except Exception as e:
        logger.error(f"获取配置失败: {e}")
        return None


def delete_cloud_file(file_id, auth_token, is_security=False):
    """
    调用云空间接口删除文件

    Args:
        file_id: 要删除的文件 ID
        auth_token: Authorization Token
        is_security: 是否走安全通道（True 用 cloud_namespace_security_base_url，否则用 cloud_namespace_base_url）

    Returns:
        bool: 是否删除成功
    """
    trace_id = str(uuid.uuid4())[:16]
    # 根据 is_security 选择不同的 base_url
    if is_security:
        cloud_namespace_base_url = get_properties(CONFIG_URL, "cloud_namespace_security_base_url")
    else:
        cloud_namespace_base_url = get_properties(CONFIG_URL, "cloud_namespace_base_url")
    if not cloud_namespace_base_url:
        logger.error("获取云空间基础 URL 失败")
        return False

    url = f"{cloud_namespace_base_url}/drive/v1/files/{file_id}"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "x-hw-trace-id": trace_id,
    }

    logger.info(f"删除云空间文件: {file_id}, traceId: {trace_id}")

    try:
        response = requests.delete(url, headers=headers, timeout=60, verify=True)

        if response.status_code == 204:
            logger.info(f"文件 {file_id} 删除成功")
            return True
        else:
            logger.error(f"文件 {file_id} 删除失败, status: {response.status_code}, response: {response.text}")
            return False

    except Exception as e:
        logger.error(f"删除文件异常: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="OpenClaw 云空间文件删除脚本")
    parser.add_argument("--jsonStr", required=True, help="JSON 字符串参数，包含 Authorization 和 fileId")
    args = parser.parse_args()

    # 解析 JSON 参数
    try:
        params = json.loads(args.jsonStr)
        file_id = params.get("fileId")
        authorization = params.get("Authorization")
        is_security = params.get("isSecurity", False)
    except json.JSONDecodeError as e:
        print(json.dumps({"status": "error", "message": f"JSON 解析失败: {e}"}))
        logger.error(f"JSON 解析失败: {e}")
        sys.exit(1)

    if not file_id:
        logger.error("缺少 fileId 参数")
        print(json.dumps({"status": "error", "message": "缺少 fileId 参数"}))
        sys.exit(1)

    if not authorization:
        logger.error("缺少 Authorization 参数")
        print(json.dumps({"status": "error", "message": "缺少 Authorization 参数"}))
        sys.exit(1)

    # 支持逗号分隔的多个 fileId
    file_ids = [fid.strip() for fid in file_id.split(",") if fid.strip()]

    logger.info("=" * 50)
    logger.info(f"开始删除云空间文件，共 {len(file_ids)} 个")
    logger.info("=" * 50)

    result = {
        "status": "success",
        "total": len(file_ids),
        "success_count": 0,
        "fail_count": 0,
        "details": []
    }

    for fid in file_ids:
        success = delete_cloud_file(fid, authorization, is_security=is_security)
        detail = {"fileId": fid, "deleted": success}
        if success:
            result["success_count"] += 1
        else:
            result["fail_count"] += 1
            detail["error"] = f"删除文件 {fid} 失败"
        result["details"].append(detail)

    if result["fail_count"] > 0:
        if result["success_count"] == 0:
            result["status"] = "error"
            result["message"] = "所有文件删除失败"
        else:
            result["status"] = "partial_error"
            result["message"] = f"部分文件删除失败（{result['fail_count']}/{result['total']}）"

    logger.info("=" * 50)
    logger.info(f"删除完成: 成功 {result['success_count']}, 失败 {result['fail_count']}")
    logger.info("=" * 50)

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()