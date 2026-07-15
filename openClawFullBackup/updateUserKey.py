import argparse
import json
import os
import logging

ENV_FILE_PATH = "/home/sandbox/.openclaw/.xiaoyienv"
BASE_DIR = "/home/sandbox/.openclaw"

# 日志文件路径
LOG_DIR = "/tmp/logs"
LOG_DIR_FILE = os.path.join(LOG_DIR, f"{os.path.basename(__file__).replace('.py', '')}.log")


# 创建日志目录并设置权限
os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)


# 创建日志文件并设置权限（如果不存在）
if not os.path.exists(LOG_DIR_FILE):
    with open(LOG_DIR_FILE, 'w') as f1:
        pass
    os.chmod(LOG_DIR_FILE, 0o600)


# 配置日志
logging.basicConfig(
    handlers=[
        logging.FileHandler(LOG_DIR_FILE, encoding='utf-8')
    ],
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)



def update_env_file(json_input):
    """
    将 JSON 中的所有 key-value 写入 .xiaoyienv 文件
    如果 key 已存在，则更新其值；如果不存在，则追加

    :param json_input: JSON 字符串，如 '{"key1": "value1", "key2": "value2"}'
    :return: True（成功）/ False（失败）
    """
    try:
        # 解析 JSON
        data = json.loads(json_input)
        if not isinstance(data, dict):
            logger.error("JSON 必须是一个对象（key-value 形式）")
            return False

        if not data:
            logger.warning("JSON 为空，无内容需要写入")
            return True

        # 读取现有内容
        existing_lines = []
        existing_keys = set()

        if os.path.exists(ENV_FILE_PATH):
            with open(ENV_FILE_PATH, 'r', encoding='utf-8') as f:
                for line in f:
                    # 提取每行的 key（格式为 KEY=VALUE）
                    if '=' in line:
                        key = line.split('=')[0]
                        # 如果这个 key 不在新的 JSON 中，保留该行
                        if key not in data:
                            existing_lines.append(line)
                        else:
                            existing_keys.add(key)
                    else:
                        existing_lines.append(line)

        # 写入文件：保留的非更新行 + 新的 key-value
        with open(ENV_FILE_PATH, 'w', encoding='utf-8') as f:
            # 写入保留的旧行
            f.writelines(existing_lines)

            # 追加新的 key-value
            for key, value in data.items():
                f.write(f"{key}={value}\n")
        logger.info(f"完成！配置已写入 {ENV_FILE_PATH}")
        return True

    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析失败 - {e}")
        return False
    except Exception as e:
        logger.error(f"发生错误: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="将 JSON 中的 key-value 写入 .xiaoyienv 文件")
    parser.add_argument("--json_input", type=str, help="JSON 字符串，如 '{\"key1\": \"value1\"}'")
    args = parser.parse_args()

    success = update_env_file(args.json_input)
    if success:
        print(json.dumps({"status": "success"}))
    exit(0 if success else 1)


if __name__ == "__main__":
    main()