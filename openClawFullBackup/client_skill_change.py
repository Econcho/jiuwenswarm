#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import json
import os
import subprocess
import argparse
import datetime
import re
import shlex
from packaging import version
import logging

# ================= 配置区 =================
SKILL_DB_FILE = "/home/sandbox/client_installed_skills.json"
LOG_DIR = "/tmp/logs"

# 确保日志目录存在
os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "client_installed_skills.log")

# 初始化日志：仅写入文件，不输出到控制台
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


def load_skill_db():
    """加载技能数据库"""
    if not os.path.exists(SKILL_DB_FILE):
        logger.info("技能库文件不存在，将创建新库。")
        return {}
    try:
        with open(SKILL_DB_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"读取技能库失败，将使用空库：{e}")
        return {}


def save_skill_db(db):
    """保存技能数据库"""
    try:
        # 使用原子写入防止并发破坏
        tmp_file = SKILL_DB_FILE + ".tmp"
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(db, f, indent=4, ensure_ascii=False)
        os.replace(tmp_file, SKILL_DB_FILE)
        logger.info("技能库已更新并保存。")
    except Exception as e:
        logger.error(f"写入技能库失败：{e}")
        # 尝试清理临时文件
        if os.path.exists(tmp_file):
            try:
                os.remove(tmp_file)
            except Exception as ex:
                logger.error(f"临时文件清理失败:{ex}")
                pass
        raise


def extract_zip_path(command):
    """从命令字符串中智能提取 .zip 文件路径"""
    if not command:
        return None
    # 正则逻辑：寻找空格后以 .zip 结尾的字符串
    pattern = r'\s+([^\s]+\.zip)\b'
    match = re.search(pattern, command)
    if match:
        return match.group(1)
    return None


def get_highest_version(skill_db):
    """从技能字典中找出所有已安装设备中的最高版本号"""
    if not skill_db:
        return None, None
    max_ver = None
    max_device = None
    for dev_id, ver_str in skill_db.items():
        # 注意：这里不需要脱敏用于比较，只需要比较数值
        if not isinstance(ver_str, str):
            continue
        try:
            current_ver_obj = version.parse(ver_str)
            if max_ver is None or current_ver_obj > max_ver:
                max_ver = current_ver_obj
                max_device = dev_id
        except Exception:
            masked_dev_id = mask_id(dev_id)
            logger.error(f"警告：设备 {masked_dev_id} 的版本 {ver_str} 解析失败，跳过。")
            continue
    return max_ver, max_device


def mask_id(device_id):
    """脱敏显示设备 ID"""
    if not device_id:
        return "****"
    if len(str(device_id)) > 6:
        return str(device_id)[:3] + "***" + str(device_id)[-3:]
    return "****"


def clean_zip_file(zip_path):
    """安全删除 zip 包"""
    if not zip_path or not os.path.exists(zip_path):
        return
    try:
        os.remove(zip_path)
        logger.info(f"已清理临时文件：{zip_path}")
    except Exception as e:
        logger.error(f"警告：无法删除文件 {zip_path}，错误：{e}")


def parse_command_to_list(command):
    """
    安全地将命令字符串转换为列表。
    """
    if not command:
        raise ValueError("命令不能为空")

    try:
        cmd_list = shlex.split(command)
    except ValueError as e:
        raise ValueError(f"命令格式解析失败") from e

    if not cmd_list:
        raise ValueError("解析后的命令列表为空")

    return cmd_list


def check_and_install(skill_id, device_id, target_ver, command, db):
    zip_path = extract_zip_path(command)
    skill_db = db.get(skill_id, {})
    max_ver_obj, max_dev_id = get_highest_version(skill_db)

    masked_id = mask_id(device_id)
    logger.info(f"--- 安装校验 (Skill: {skill_id}, Dev: {masked_id}, Ver: {target_ver}) ---")

    should_run = False
    reason = ""

    try:
        target_ver_obj = version.parse(target_ver)
    except Exception:
        logger.warning(f"版本解析失败，默认执行。")
        should_run = True
        target_ver_obj = None

    if not should_run:
        if max_ver_obj is None:
            should_run = True
            reason = "全局无记录，首次安装"
        elif target_ver_obj <= max_ver_obj:
            should_run = False
            reason = f"目标版本 <= 全局最高版本 (v{max_ver_obj})，跳过"
        else:
            should_run = True
            reason = "目标版本 > 全局最高版本，允许执行"

    if should_run:
        logger.info(f"执行安装: {reason}")
        try:
            # 使用正确的函数名 parse_command_to_list
            cmd_list = parse_command_to_list(command)
            subprocess.run(cmd_list, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            logger.info("安装成功！")
        except Exception as e:
            logger.error(f"安装失败：{e}")
            raise

        if skill_id not in db:
            db[skill_id] = {}
        db[skill_id][device_id] = target_ver

        # 安装成功后清理 zip
        if zip_path:
            clean_zip_file(zip_path)

        save_skill_db(db)
        return

    # 无需安装时的强制同步 (仅更新数据库)
    logger.info(f"跳过安装: {reason}")
    if skill_id not in db:
        db[skill_id] = {}
    db[skill_id][device_id] = target_ver

    # 即使不执行安装，通常也会清理下载下来的临时包
    if zip_path:
        clean_zip_file(zip_path)

    save_skill_db(db)
    logger.info(f"配置已同步为 v{target_ver}。")


def check_and_uninstall(skill_id, device_id, command, db):
    skill_db = db.get(skill_id, {})
    device_list = list(skill_db.keys())
    masked_id = mask_id(device_id)

    logger.info(f"--- 卸载校验 (Skill: {skill_id}, Dev: {masked_id}) ---")

    if not device_list:
        logger.info("无记录，跳过。")
        return

    if device_id not in skill_db:
        logger.info(f"设备不在数据库中，跳过。")
        return

    others_count = len([d for d in device_list if d != device_id])

    should_uninstall = False
    if others_count == 0:
        should_uninstall = True
        reason = "唯一设备，执行物理卸载"
    else:
        should_uninstall = False
        reason = f"有其他 {others_count} 台设备，跳过物理卸载"

    if should_uninstall:
        logger.info(f"执行卸载: {reason}")
        try:
            # 使用正确的函数名
            cmd_list = parse_command_to_list(command)
            subprocess.run(cmd_list, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            logger.info("卸载成功！")
        except Exception as e:
            logger.error(f"卸载失败：{e}")
            raise

        del db[skill_id][device_id]
        if not db[skill_id]:
            del db[skill_id]
        save_skill_db(db)
        logger.info(f"设备 {masked_id} 已移除。")
    else:
        logger.info(f"跳过卸载: {reason}")
        del db[skill_id][device_id]
        if not db[skill_id]:
            del db[skill_id]
        save_skill_db(db)
        logger.info(f"设备 {masked_id} 记录已清除。")


def main():
    parser = argparse.ArgumentParser(description="Skill Manager - 安全加固版")
    parser.add_argument("--jsonStr", type=str, required=True, help="JSON 入参")
    args = parser.parse_args()

    try:
        input_data = json.loads(args.jsonStr)
    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析错误：{e}")
        sys.exit(1)

    skill_id = input_data.get("skillId")
    device_id = input_data.get("deviceId")
    skill_version = input_data.get("version")
    operate = input_data.get("operate")
    command = input_data.get("command")

    if not all([skill_id, device_id, operate, command]):
        logger.info("错误：缺少必要字段")
        sys.exit(1)

    db = load_skill_db()

    if operate == "Install":
        if not skill_version:
            logger.info("错误：安装操作必须提供 version 字段")
            sys.exit(1)
        try:
            check_and_install(skill_id, device_id, skill_version, command, db)
        except Exception as e:
            logger.error(f"安装异常：{e}")
            sys.exit(1)

    elif operate == "Uninstall":
        try:
            check_and_uninstall(skill_id, device_id, command, db)
        except Exception as e:
            logger.error(f"卸载异常：{e}")
            sys.exit(1)
    else:
        logger.error(f"错误：未知操作 {operate}")
        sys.exit(1)


if __name__ == "__main__":
    main()