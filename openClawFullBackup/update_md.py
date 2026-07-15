import zipfile
import shutil
import os
from pathlib import Path
from tqdm import tqdm
import datetime
import argparse
import requests
import logging

# ==================== 日志配置（已升级） ====================
LOG_DIR = Path("/home/sandbox/.openclaw/workspace/logs/")
LOG_FILE = LOG_DIR / "update_md.txt"

# 自动创建日志目录
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 配置日志
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 避免重复添加处理器
if not logger.handlers:
    # 文件处理器（输出到指定文件）
    file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
    file_handler.setFormatter(
        logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    )

    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    )

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


# ==================== 主函数 ====================
def download_zip(url, output_path):
    """下载 ZIP 文件，支持断点续传（流式下载）"""
    logger.info(f"📥 正在下载 ZIP: {url}")
    try:
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()

        total_size = int(response.headers.get("content-length", 0))
        downloaded = 0

        with open(output_path, "wb") as f:
            for chunk in tqdm(
                    response.iter_content(chunk_size=8192),
                    total=total_size // 8192 + 1,
                    desc="下载进度",
                    unit="KB"
            ):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)

        logger.info(f"✅ 下载完成: {output_path} ({downloaded / 1024:.1f} KB)")
        return True
    except Exception as e:
        logger.error(f"❌ 下载失败: {url} | 错误: {e}")
        return False


def extract_zip_with_backup(zip_path, extract_to, backup_dir=None):
    """
    解压 ZIP 文件，同名文件先备份再覆盖。
    """
    zip_path = Path(zip_path)
    extract_path = Path(extract_to)

    # 自动创建备份目录
    if backup_dir is None:
        backup_dir = extract_path / ".backup"
    backup_path = Path(backup_dir)
    backup_path.mkdir(parents=True, exist_ok=True)

    # 生成时间戳
    timestamp = datetime.datetime.now()

    logger.info(f"📦 正在解压: {zip_path}")
    logger.info(f"📁 目标路径: {extract_path}")
    logger.info(f"💾 备份路径: {backup_path}")
    logger.info(f"🔁 同名文件将先备份，再覆盖...\n")

    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            file_list = zip_ref.namelist()

            for file_info in tqdm(file_list, desc="处理文件", unit="文件"):
                target_file = extract_path / file_info

                # 如果是目录，直接创建
                if file_info.endswith('/'):
                    target_file.mkdir(parents=True, exist_ok=True)
                    continue

                # 如果目标文件已存在，先备份
                if target_file.exists():
                    backup_name = f"{target_file.name}.{timestamp}"
                    backup_file = backup_path / backup_name

                    try:
                        shutil.copy2(target_file, backup_file)
                        logger.info(f"✅ 备份: {target_file} → {backup_file}")
                    except Exception as e:
                        logger.error(f"❌ 备份失败: {target_file} → {backup_file} | 错误: {e}")
                        continue  # 跳过该文件

                # 确保目标目录存在
                target_file.parent.mkdir(parents=True, exist_ok=True)

                # 从 ZIP 中读取并写入目标文件（覆盖）
                try:
                    with zip_ref.open(file_info) as src, open(target_file, 'wb') as dst:
                        dst.write(src.read())
                    logger.info(f"🔄 覆盖: {target_file}")
                except Exception as e:
                    logger.error(f"❌ 写入失败: {target_file} | 错误: {e}")
                    continue

        logger.info(f"\n✅ 解压完成！同名文件已备份至: {backup_path}")
        return True

    except Exception as e:
        logger.error(f"❌ 解压失败: {zip_path} | 错误: {e}")
        return False


# ==================== 主入口 ====================
def main():
    parser = argparse.ArgumentParser(description="远程下载并解压 ZIP，同名文件先备份再覆盖")
    parser.add_argument("--url", required=True, help="ZIP 文件的远程 URL")
    args = parser.parse_args()

    # 下载 ZIP
    if not download_zip(args.url, "/home/sandbox/"):
        logger.error("❌ 下载失败，程序终止。")
        return

    # 解压 ZIP（带备份覆盖）
    if not extract_zip_with_backup(args.output, "/home/sandbox/.openclaw/workspace/",
                                   "/home/sandbox/.openclaw/workspace/"):
        logger.error("❌ 解压失败，程序终止。")
        return
    # 查找当前目录下所有 .zip 文件
    zip_files = glob.glob("*.zip")

    # 删除每个 zip 文件
    for file in zip_files:
        try:
            os.remove(file)
            logger.info(f"✅ 已删除: {file}")
        except Exception as e:
            logger.info(f"❌ 删除失败 {file}: {e}")
    logger.info("🎉 所有任务完成！")


# ==================== 入口点 ====================
if __name__ == "__main__":
    main()