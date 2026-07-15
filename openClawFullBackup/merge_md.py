"""
Markdown 文件合并工具（改写版 v3）
- 从远程配置读取覆盖 Map
- 接收一个 --jsonStr 参数包含 userMdFile 和 imageMdFile
- 将 imageMdFile 的内容按规则 merge 到 userMdFile（原地修改）
"""

import re
import os
import sys
import json
import logging
import argparse

# 全局配置
BASE_DIR = "/home/sandbox/.openclaw"
CONFIG_URL = "https://h5hosting-drcn.dbankcdn.cn/cch5/HAG/D1E0CiDwLKSTla6LZEqEHDzqA/import.json"

# 日志配置
LOG_DIR = "/tmp/logs"
LOG_FILE_PATH = os.path.join(LOG_DIR, "merge_md.log")

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
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="Markdown 文件合并工具（将 imageMdFile 的内容 merge 到 userMdFile）")
    parser.add_argument(
        "--jsonStr",
        required=True,
        help='JSON 字符串，包含 userMdFile 和 imageMdFile，如 \'{"userMdFile": "a.md", "imageMdFile": "b.md"}\''
        )
    return parser.parse_args()


def load_json_str(json_str):
    """解析 JSON 字符串参数"""
    try:
        params = json.loads(json_str)
        logger.info(f"解析 jsonStr 成功: {json.dumps(params, ensure_ascii=False)}")
        return params
    except json.JSONDecodeError as e:
        logger.error(f"解析 jsonStr 失败: {e}")
        print(json.dumps({"status": "error", "message": f"jsonStr 解析失败: {e}"}, ensure_ascii=False))
        return None


def get_override_map_from_config():
    """从远程配置获取覆盖 Map

    返回 dict: {userMdFile中的标题: imageMdFile中的标题}
    获取失败时返回空 dict
    """
    config_key = "md_merge_override_map"
    try:
        import requests
        response = requests.get(CONFIG_URL, stream=True, timeout=60)
        response.raise_for_status()
        content = response.json()
        override_data = content.get(config_key, {})

        if override_data and isinstance(override_data, dict):
            logger.info(f"从配置获取覆盖 Map 成功: {config_key}, 共 {len(override_data)} 项")
            logger.info(f"覆盖 Map 内容: {override_data}")
            return override_data
        else:
            logger.info(f"配置中无覆盖 Map 或数据为空: {config_key}")
            return {}
    except Exception as e:
        logger.error(f"获取配置失败: {e}")
        return {}


def get_delete_list_from_config():
    """从远程配置获取删除标题列表

    返回 list: 需要在合并后从 userMdFile 中删除的标题列表
    获取失败时返回空 list
    """
    config_key = "md_merge_delete_list"
    try:
        import requests
        response = requests.get(CONFIG_URL, stream=True, timeout=60)
        response.raise_for_status()
        content = response.json()
        delete_list = content.get(config_key, [])

        if delete_list and isinstance(delete_list, list):
            logger.info(f"从配置获取删除列表成功: {config_key}, 共 {len(delete_list)} 项")
            logger.info(f"删除列表内容: {delete_list}")
            return delete_list
        else:
            logger.info(f"配置中无删除列表或数据为空: {config_key}")
            return []
    except Exception as e:
        logger.error(f"获取配置失败: {e}")
        return []


def clean_html_tags(text):
    """移除 HTML 标签"""
    text = re.sub(r'<div[^>]*></div>', '', text)
    text = re.sub(r'<[^>]+>', '', text)
    return text


def normalize_title(title):
    """标准化标题（移除多余空格）"""
    return re.sub(r'\s+', ' ', title.strip())


def extract_sections(content):
    """从 Markdown 内容中提取章节

    Args:
        content: Markdown 文本内容

    Returns:
        dict: {标题: 章节内容} 的字典
    """
    sections = {}
    current_title = None
    current_content = []

    content = clean_html_tags(content)

    for line in content.split('\n'):
        match = re.match(r'^(#{1,6})\s+(.+)$', line)
        if match:
            if current_title is not None:
                sections[current_title] = '\n'.join(current_content).strip()
            current_title = normalize_title(match.group(2))
            current_content = [line]
        else:
            if current_title is not None:
                current_content.append(line)

    if current_title is not None:
        sections[current_title] = '\n'.join(current_content).strip()

    return sections


def sections_to_markdown(sections, original_order):
    """将章节字典按指定顺序转回 Markdown 文本"""
    lines = []
    for title in original_order:
        content = sections.get(title, '')
        if content:
            lines.append(content)
    return '\n\n'.join(lines) + '\n'


def merge_md(file1_path, file2_path, override_map, delete_list):
    """将 file2 的内容 merge 到 file1（原地修改 file1）

    覆盖规则（override_map: {userMdFile标题: imageMdFile标题}）：
    - 如 {"A": "B"}:
      - 若 userMdFile 存在标题 A → 用 imageMdFile 的标题 B 内容替换 userMdFile 的标题 A
      - 若 userMdFile 不存在标题 A → 用 imageMdFile 的标题 B 内容替换 userMdFile 的标题 B

    合并完成后，会删除 delete_list 中指定的标题及其内容

    Args:
        file1_path: userMdFile（目标文件，会被原地修改）
        file2_path: imageMdFile（来源文件）
        override_map: 覆盖 Map
        delete_list: 合并后需要删除的标题列表
    """
    if override_map is None:
        override_map = {}
    if delete_list is None:
        delete_list = []

    logger.info(f"开始将 file2 merge 到 file1")
    logger.info(f"userMdFile（目标，将被原地修改）: {file1_path}")
    logger.info(f"imageMdFile（来源）: {file2_path}")
    logger.info(f"覆盖 Map: {override_map}")

    # 读取文件
    try:
        with open(file1_path, 'r', encoding='utf-8') as f1:
            content1 = f1.read()
        logger.info(f"读取 userMdFile 成功: {len(content1)} 字节")
    except Exception as e:
        logger.error(f"读取 userMdFile 失败: {file1_path}, 错误: {e}")
        return False

    try:
        with open(file2_path, 'r', encoding='utf-8') as f2:
            content2 = f2.read()
        logger.info(f"读取 imageMdFile 成功: {len(content2)} 字节")
    except Exception as e:
        logger.error(f"读取 imageMdFile 失败: {file2_path}, 错误: {e}")
        return False

    # 提取章节
    sections1 = extract_sections(content1)
    sections2 = extract_sections(content2)

    logger.info(f"userMdFile 章节数: {len(sections1)}")
    logger.info(f"imageMdFile 章节数: {len(sections2)}")
    logger.info(f"userMdFile 标题: {list(sections1.keys())}")
    logger.info(f"imageMdFile 标题: {list(sections2.keys())}")

    # 保留 userMdFile 的原始标题顺序
    original_order = list(sections1.keys())

    # 合并 imageMdFile 中有但 userMdFile 中没有的标题
    new_titles = []
    for title in sections2.keys():
        if title not in sections1:
            new_titles.append(title)

    # 追加新标题到末尾
    if new_titles:
        original_order.extend(new_titles)
        logger.info(f"来自 imageMdFile 的新增标题: {new_titles}")

    # 构建实际的替换映射（将覆盖 Map 解析为实际的标题替换关系）
    # override_map 格式: {key: value}
    # 如果 userMdFile 有 key → 用 imageMdFile 的 value 替换 userMdFile 的 key
    # 如果 userMdFile 没有 key → 用 imageMdFile 的 value 替换 userMdFile 的 value
    actual_replacements = {}  # {userMdFile中的标题: imageMdFile中的标题（用于取内容）}
    for key, value in override_map.items():
        if key in sections1:
            # userMdFile 存在 key → 替换 key
            actual_replacements[key] = value
            logger.info(f"[替换映射] userMdFile 存在标题 '{key}' → 用 imageMdFile 的 '{value}' 替换")
        else:
            # userMdFile 不存在 key → 替换 value
            if value in sections1:
                actual_replacements[value] = value
                logger.info(f"[替换映射] userMdFile 不存在 '{key}' → 回退：用 imageMdFile 的 '{value}' 替换自身")
            else:
                logger.info(f"[替换映射] userMdFile 中不存在 '{key}' 也不存在 '{value}'，跳过")

    # 收集被用作替换来源的 imageMdFile 标题，排除它们不被追加
    source_titles_used = set(actual_replacements.values())
    if source_titles_used:
        # 从 new_titles 中排除已被用作替换源的标题
        for st in source_titles_used:
            if st in new_titles:
                new_titles.remove(st)
                logger.info(f"[排除追加] 标题 '{st}' 已被用作替换来源，不再追加到文件末尾")
        # 同步更新 original_order
        original_order = list(sections1.keys())
        if new_titles:
            original_order.extend(new_titles)

    # 合并章节
    merged = {}
    override_count = 0
    file1_count = 0
    file2_only_count = 0

    for title in original_order:
        sec1 = sections1.get(title, '')
        sec2 = sections2.get(title, '')

        if title in actual_replacements:
            # 需要被替换的标题
            source_title = actual_replacements[title]
            source_content = sections2.get(source_title, '')
            if source_content and source_content != sec1:
                merged[title] = source_content
                override_count += 1
                logger.info(f"[覆盖] 标题 '{title}': 使用 imageMdFile 的 '{source_title}' 内容替换")
            elif source_content == sec1:
                merged[title] = sec1
                logger.info(f"[覆盖跳过] 标题 '{title}': imageMdFile 内容与 userMdFile 相同，跳过")
            else:
                merged[title] = sec1
                logger.info(f"[覆盖跳过] 标题 '{title}': imageMdFile 中无标题 '{source_title}' 的内容，保留原有")
        elif sec1 and sec2:
            # 两个文件都有但不在替换列表中 → 保留 userMdFile
            merged[title] = sec1
            file1_count += 1
        elif sec1:
            merged[title] = sec1
            file1_count += 1
            logger.info(f"[仅 userMdFile] 标题 '{title}'")
        elif sec2:
            merged[title] = sec2
            file2_only_count += 1
            logger.info(f"[仅 imageMdFile] 标题 '{title}'")

    # 删除标题列表中的标题
    delete_count = 0
    for title in delete_list:
        if title in merged:
            del merged[title]
            # 同时从 original_order 中移除
            if title in original_order:
                original_order.remove(title)
            delete_count += 1
            logger.info(f"[删除] 标题 '{title}': 已从合并结果中删除")
        else:
            logger.info(f"[删除跳过] 标题 '{title}': 合并结果中不存在，跳过")

    final_order = list(sections2.keys())
    for title in merged.keys():
        if title not in sections2:
            final_order.append(title)
    # 将合并结果写回 userMdFile（原地修改）
    merged_content = sections_to_markdown(merged, final_order)
    try:
        with open(file1_path, 'w', encoding='utf-8') as f1:
            f1.write(merged_content)
        logger.info(f"已将合并结果写入 userMdFile（原地修改）: {file1_path}")
    except Exception as e:
        logger.error(f"写入文件失败: {file1_path}, 错误: {e}")
        return False

    # 统计日志
    logger.info(f"合并完成统计:")
    logger.info(f"  - 总章节数: {len(merged)}")
    logger.info(f"  - 覆盖章节数: {override_count}")
    logger.info(f"  - 保留章节数: {file1_count}")
    logger.info(f"  - 仅 imageMdFile 新增章节: {file2_only_count}")
    logger.info(f"  - 删除章节数: {delete_count}")

    return True


def main():
    args = parse_arguments()
    params = load_json_str(args.jsonStr)

    if params is None:
        sys.exit(1)

    logger.info("=" * 50)
    logger.info("Markdown 文件合并工具启动（改写版 v3）")
    logger.info("=" * 50)

    # 从参数中读取 CONFIG_URL，如果没有则使用默认值
    config_url = params.get("configUrl")
    if config_url:
        global CONFIG_URL
        CONFIG_URL = config_url
        logger.info(f"使用自定义配置 URL: {CONFIG_URL}")

    # 从参数中提取字段（userMdFile 和 imageMdFile 为必填）
    file1 = params.get("userMdFile")
    file2 = params.get("imageMdFile")

    if not file1 or not file2:
        logger.error("userMdFile 和 imageMdFile 为必填参数")
        print(json.dumps({"status": "error", "message": "userMdFile 和 imageMdFile 为必填参数"}, ensure_ascii=False))
        sys.exit(1)

    # 从远程配置获取覆盖 Map 和删除列表（固定使用 md_merge_override_map 和 md_merge_delete_list）
    override_map = get_override_map_from_config()
    delete_list = get_delete_list_from_config()

    # 执行合并（原地修改 userMdFile）
    success = merge_md(file1, file2, override_map, delete_list)

    # 构造返回结果
    result = {
        "status": "success" if success else "error",
        "userMdFile": file1,
        "imageMdFile": file2,
        "override_map": override_map,
        "delete_list": delete_list,
        "message": "合并完成" if success else "合并失败，请查看日志"
    }

    print(json.dumps(result, ensure_ascii=False))

    if success:
        logger.info("合并任务成功完成")
    else:
        logger.error("合并任务失败")
        sys.exit(1)


if __name__ == '__main__':
    main()