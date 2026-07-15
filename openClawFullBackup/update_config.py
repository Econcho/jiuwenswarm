import json
import argparse
import re
from pathlib import Path

# 1. 配置模板（带占位符）
config_template = {
    "channels": {
        "xiaoyi-channel": {
            "wsUrl1": "${wsUrl1}",
            "wsUrl2": "${wsUrl2}",
            "apiKey": "${apiKey}",
            "agentId": "${agentId}",
            "apiId": "${apiId}",
            "pushId": 123456,
            "uid": "${uid}",
            "enabled": True,
            "fileUploadUrl": "${fileUploadUrl}",
            "pushUrl": "${pushUrl}"
        }
    }
}


# 2. 替换占位符函数（支持嵌套结构）
def replace_placeholders(data, replacements):
    """
    递归替换数据中的占位符，支持多种格式：
    - ${KEY}
    - {{KEY}}
    - $KEY (可选，如果需要)

    注意：会保留布尔值、数字等非字符串类型的原始类型
    同时也会处理字典的键

    :param data: 要处理的数据（dict/list/str）
    :param replacements: 替换映射字典，如 {"KEY": "value"}
    :return: 替换后的数据
    """
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            # 处理字典的键
            new_key = replace_placeholders(k, replacements)
            # 处理字典的值
            result[new_key] = replace_placeholders(v, replacements)
        return result
    elif isinstance(data, list):
        return [replace_placeholders(item, replacements) for item in data]
    elif isinstance(data, str):
        # 支持多种格式：${KEY}，{{KEY}}，$KEY
        patterns = [
            r"\$\{([^}]+)\}",  # ${KEY}
            r"{{\s*([^}]+)\s*}}",  # {{KEY}}（支持空格）
            r"\$([^$]+)",  # $KEY（可选，谨慎使用）
        ]

        # 逐个应用所有正则模式
        for pattern in patterns:
            data = re.sub(
                pattern,
                lambda m: str(replacements.get(m.group(1).strip(), m.group(0))),
                data  # 第三个参数：要被替换的字符串
            )
        return data
    else:
        # 对于非字符串类型（如布尔值、数字），直接返回
        # 这样可以保留原始类型
        return data


# 3. 将字符串转换为布尔值
def to_bool(value):
    """
    将字符串转换为布尔值
    支持: true/false, True/False, TRUE/FALSE, 1/0
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ('true', '1', 'yes', 'y')
    return bool(value)


def is_numeric_string(s):
    """判断字符串是否为有效数字（整数或浮点数），不支持科学计数法"""
    if not isinstance(s, str):
        return False
    # 匹配：可选负号 + 整数部分 + 可选小数点 + 可选小数部分
    pattern = r'^-?\d+(\.\d+)?$'
    return bool(re.match(pattern, s))


# 3.5. 转换数据类型（将字符串值转换回正确的类型）
def convert_types(data, type_mapping=None, parent_key=None):
    """
    递归转换数据类型，将字符串值转换回正确的类型（布尔值、数字等）

    :param data: 要处理的数据
    :param type_mapping: 类型映射字典，格式为 {"key_path": type}
                       例如: {"models.providers.xiaoyiprovider.models.reasoning": bool}
    :param parent_key: 父级键名，用于判断上下文（如区分 models[].input 和 models[].cost.input）
    :return: 转换后的数据
    """
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            # 跳过 "channels" 键，不进行类型转换
            if key == "channels" or key == "memorySearch":
                result[key] = value
            # 只在 models 数组中的对象顶层处理 "input" 键
            # 通过 parent_key == "models" 来判断当前是否在 models 数组的元素中
            elif key == "input" and parent_key == "models" and not is_numeric_string(value):
                result[key] = value.split(",")
            else:
                result[key] = convert_types(value, type_mapping, key)
        return result
    elif isinstance(data, list):
        # 对于列表，传递当前列表的父级键名
        return [convert_types(item, type_mapping, parent_key) for item in data]
    elif isinstance(data, str):
        # 尝试将字符串转换为布尔值
        lower_val = data.lower()
        if lower_val in ('true', 'false'):
            return lower_val == 'true'
        # 尝试将字符串转换为整数
        try:
            return int(data)
        except ValueError:
            pass

        # 尝试将字符串转换为浮点数
        try:
            return float(data)
        except ValueError:
            pass
        # 保持原样
        return data
    else:
        return data


# 4. 合并两个 JSON 结构（深度合并）
def merge_dicts(target, source):
    """
    深度合并两个字典
    优先使用 source 中的值
    """
    for key, value in source.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            merge_dicts(target[key], value)
        else:
            target[key] = value
    return target


# 5. 解析命令行参数
def parse_args():
    parser = argparse.ArgumentParser(description="将配置模板合并到已有 config.json 文件中")

    # 所有占位符字段（必须传参）
    parser.add_argument("--wsUrl1", required=True, help="WebSocket URL 1")
    parser.add_argument("--wsUrl2", required=True, help="WebSocket URL 2")
    parser.add_argument("--apiKey", required=True, help="API Key")
    parser.add_argument("--agentId", required=True, help="Agent ID")
    parser.add_argument("--apiId", required=True, help="API ID")
    parser.add_argument("--uid", required=True, help="User ID")
    parser.add_argument("--model", required=True, help="model (格式: model|reasoning(bool)|input|cost_input"
                                                       "|cost_output|cacheRead|cacheWrite|contextWindow|maxTokens)")
    parser.add_argument("--fileUploadUrl", required=True, help="File Upload URL")
    parser.add_argument("--pushUrl", required=True, help="Push URL (optional)")

    # 可选参数
    parser.add_argument("--input", default="config.json", help="输入配置文件路径（已有内容）")
    parser.add_argument("--output", default="config.json", help="输出文件路径（合并后内容）")
    parser.add_argument("--overwrite", action="store_true", help="是否覆盖已有输出文件")

    return parser.parse_args()


# 6. 主函数
def main():
    args = parse_args()

    # 分割 model 参数并验证长度
    model_config = args.model.split("|")
    if len(model_config) < 9:
        print(f"❌ model 参数格式错误，需要9个字段，当前只有 {len(model_config)} 个")
        print("   正确格式: model|reasoning|input|cost_input|"
              "cost_output|cacheRead|cacheWrite|"
              "contextWindow|maxTokens")
        return

    # 构建替换映射
    replacements = {
        "wsUrl1": args.wsUrl1,
        "wsUrl2": args.wsUrl2,
        "apiKey": args.apiKey,
        "agentId": args.agentId,
        "apiId": args.apiId,
        "uid": str(args.uid),
        "fileUploadUrl": args.fileUploadUrl,
        "pushUrl": args.pushUrl,
        "model": model_config[0],
        "reasoning": to_bool(model_config[1]),  # 转换为布尔值
        "input": model_config[2],
        "cost_input": model_config[3],
        "cost_output": model_config[4],
        "cacheRead": model_config[5],
        "cacheWrite": model_config[6],
        "contextWindow": model_config[7],
        "maxTokens": model_config[8]
    }

    # 读取输入文件
    input_path = Path(args.input)
    input_data = {}
    if not input_path.exists():
        print(f"⚠️ 输入文件不存在：{args.input}，将从空开始")
    else:
        try:
            with open(input_path, "r", encoding="utf-8") as f:
                input_data = json.load(f)
        except Exception as e:
            print(f"❌ 读取输入文件失败：{str(e)}")
            return

    # 读取输出文件中已有的内容（如果存在）
    output_path = Path(args.output)
    output_existing_data = {}
    if output_path.exists():
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                output_existing_data = json.load(f)
            print(f"📖 已读取输出文件中的现有内容：{args.output}")
        except Exception as e:
            print(f"⚠️ 读取输出文件失败：{str(e)}，将从空开始")
            output_existing_data = {}

    # 先对输入文件中的所有内容进行占位符替换
    try:
        input_data_replaced = replace_placeholders(input_data, replacements)
    except Exception as e:
        print(f"❌ 输入文件占位符替换失败：{str(e)}")
        return

    # 替换模板中的占位符
    try:
        template_with_values = replace_placeholders(config_template, replacements)
    except Exception as e:
        print(f"❌ 模板占位符替换失败：{str(e)}")
        return

    # 合并配置：输出文件已有内容 -> 替换后的输入文件内容 -> 替换后的模板内容
    try:
        # 先合并输出文件已有内容和替换后的输入文件内容
        merged_data = merge_dicts(output_existing_data, input_data_replaced)
        # 再合并模板内容
        final_data = merge_dicts(merged_data, template_with_values)
    except Exception as e:
        print(f"❌ 合并配置失败：{str(e)}")
        return

    # 转换数据类型（将字符串值转换回正确的类型，如布尔值、数字等）
    try:
        final_data = convert_types(final_data)
    except Exception as e:
        print(f"❌ 数据类型转换失败：{str(e)}")
        return

    # 确保输出目录存在
    output_dir = output_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    # 写入最终合并后的配置
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(final_data, f, ensure_ascii=False, indent=4)
        print(f"✅ 配置已成功合并并写入：{args.output}")
    except Exception as e:
        print(f"❌ 写入输出文件失败：{str(e)}")
        return


# 入口点
if __name__ == "__main__":
    main()