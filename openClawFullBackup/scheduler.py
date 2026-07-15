from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timezone, timedelta
import subprocess
import sys
import requests

JOB_SCRIPT = "/home/sandbox/full_backup_upload.py"  # 要执行的脚本
JOB_TIMEOUT = 3600          # 超时时间（秒）

# 执行时间（24小时制）
TARGET_HOUR = 1
TARGET_MINUTE = 0
BEIJING_TZ = timezone(timedelta(hours=8))
zip_input_files_url = "https://h5hosting-drcn.dbankcdn.cn/cch5/HAG/D1E0CiDwLKSTla6LZEqEHDzqA/import.json"


def run_job():
    print(f"[{datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')} 北京时间] 开始执行任务...")

    try:
        result = subprocess.run(
            [sys.executable, JOB_SCRIPT, "--jsonStr", "{\"mode\":\"pack\"}"],
            capture_output=True,
            text=True,
            timeout=JOB_TIMEOUT
        )

        if result.returncode == 0:
            print(" 执行成功！")
            if result.stdout:
                print(result.stdout)
        else:
            print(f"执行失败！返回码: {result.returncode}")
            if result.stderr:
                print(f"错误: {result.stderr}")

    except subprocess.TimeoutExpired:
        print(f"任务超时（超过 {JOB_TIMEOUT} 秒）")
    except FileNotFoundError:
        print(f"找不到脚本: {JOB_SCRIPT}")
    except Exception as e:
        print(f"执行出错: {e}")


def get_properties(url, setting_key):
    try:
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
        # 解析 JSON 内容
        try:
            content = response.json()
        except requests.exceptions.JSONDecodeError:
            print("下载的文件不是有效的 JSON 格式。")
            return None

        # 读配置
        result = content.get(setting_key)
        print(f"json content: {result}")

        return result
    except Exception as e:
        return None

# ==================== 启动调度器 ====================

if __name__ == "__main__":
    sched = BlockingScheduler()

    target_hour = get_properties(zip_input_files_url, "backup_target_hour") or TARGET_HOUR
    target_minute = get_properties(zip_input_files_url, "backup_target_minute") or TARGET_MINUTE
    # 每天指定时间执行
    sched.add_job(
        run_job,
        CronTrigger(timezone=BEIJING_TZ, hour=target_hour, minute=target_minute),
        id='daily_job'
    )

    print(f"定时任务已启动！")
    print(f"每天 {target_hour:02d}:{target_minute:02d} 执行 {JOB_SCRIPT}")
    print(f"按 Ctrl+C 退出")

    try:
        sched.start()
    except KeyboardInterrupt:
        print("\n任务已停止")