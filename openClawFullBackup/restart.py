#!/usr/bin/env python3
import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
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

BASE_DIR = "/home/sandbox/.openclaw"
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


# 停止supervisor 进程
def stop_supervisor():
    """停止 supervisor 进程"""

    # 方法1: 先用 supervisorctl 优雅停止子进程
    try:
        subprocess.run(
            ["python3", "-m", "supervisor.supervisorctl", "-c", "/home/sandbox/supervisord.conf", "stop", "all"],
            check=False,
            timeout=5,
            capture_output=True
            )
        logger.info("正在通过 supervisorctl 停止所有子进程")
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
                logger.info("openclaw-gateway 进程已经停止")
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

            logger.info(f"等待 openclaw-gateway 进程停止中... ({i+1}/10)")
            time.sleep(1)

        # 超时后 SIGKILL 强杀
        result = subprocess.run(["pgrep", "-f", "openclaw-gateway"], capture_output=True, text=True, timeout=5)
        pids = [p for p in result.stdout.strip().split('\n') if p]
        if pids:
            logger.warning("openclaw-gateway 进程仍在运行中，尝试 SIGKILL")
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except Exception as e:
                    logger.error("停止 openclaw-gateway 进程失败了...")
                    pass
            time.sleep(2)

        # 最终检查
        result = subprocess.run(["pgrep", "-f", "openclaw-gateway"], capture_output=True, text=True, timeout=5)
        if not result.stdout.strip():
            logger.info("openclaw-gateway 进程已经停止")
            return True

        logger.warning("openclaw-gateway 进程仍在运行中")
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
        logger.info("正在启动 supervisord 进程中...")
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
                logger.info("supervisord 进程已启动了")
                return True
        logger.warning("supervisord 进程启动超时了")
        return False
    except Exception as e:
        logger.error(f"启动 supervisord 进程失败: {e}")
        return False


def restart_supervisor():
    # 步骤1: 停止 supervisor 进程
    stop_success = stop_supervisor()

    # 步骤2: 确认 openclaw-gateway 进程已停止
    gateway_stopped = check_gateway_stopped()
    check_openclaw_stopped()

    start_supervisor()


def extract_filename_after_slash(filepath: str) -> str:
    """提取路径中最后一个 '/' 之后的内容（即文件名）"""
    return os.path.basename(filepath)


def main():
    parser = argparse.ArgumentParser(description="Parse JSON input to extract fileName and super flag.")
    parser.add_argument(
        "--input",
        required=True,
        help="JSON string containing 'fileName' (string) and 'super' (boolean)"
    )
    args = parser.parse_args()

    json_string = args.input

    try:
        data = json.loads(json_string)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON: {e}")
        sys.exit(1)

    # 提取 fileName
    file_name = data.get("fileName")
    # 提取 super (布尔值)
    super_val = data.get("super")

    if super_val is not None and super_val:
        restart_supervisor()
        sys.exit(0)

    if file_name is not None:
        if file_name.endswith("openclaw.json") and not file_name.endswith("back.openclaw.json"):
            try:
                shutil.copy2(file_name, file_name.replace("openclaw.json", "back.openclaw.json"))  # copy2 会保留元数据
                logger.info(f"成功复制 '{file_name}' -> 'back.openclaw.json'")
            except Exception as e:
                logger.error(f"复制失败: {e}")
                sys.exit(1)
        else:
            try:
                bak_name = extract_filename_after_slash(file_name)
                # 使用.bak替换openclaw.json
                shutil.copy2(file_name, file_name.replace(bak_name, "openclaw.json"))  # copy2 会保留元数据
                logger.info(f"成功复制 '{file_name}' -> 'openclaw.json'")
            except Exception as e:
                logger.error(f"复制失败: {e}")
                sys.exit(1)
        restart_supervisor()


if __name__ == "__main__":
    main()