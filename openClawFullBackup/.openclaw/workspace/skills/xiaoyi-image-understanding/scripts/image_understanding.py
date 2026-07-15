#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小艺图像理解 - 支持本地图片和公网 URL

用法:
  python3 image_understanding.py --image "图片URL或本地路径" --prompt "图中讲了什么"
  python3 image_understanding.py --image "图片路径" --prompt "描述图中人物的表情和动作"
  python3 image_understanding.py --image img1.jpg img2.jpg img3.jpg --prompt "描述这些图片"
  python3 image_understanding.py --image img1.jpg --image img2.jpg --prompt "对比这两张图"
"""

import uuid
import json
import os
import ssl
import hashlib
import argparse
import tempfile
import requests
import urllib3
from PIL import Image
from datetime import datetime

MAX_RESOLUTION = 2048  # 最大边长 2K

# 禁用 SSL 验证（华为内网需要）
ssl._create_default_https_context = ssl._create_unverified_context

# 禁用 urllib3 的 InsecureRequestWarning 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── 配置与工具方法 ────────────────────────────────────────────

def read_xiaoyienv():
    """
    读取 ~/.openclaw/.xiaoyienv 文件并解析为键值对象

    Returns:
        dict: 解析后的配置
    """
    file_path = os.path.expanduser("~/.openclaw/.xiaoyienv")
    result = {}

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or line.startswith('!'):
                    continue
                if '=' in line:
                    key, value = line.split('=', 1)
                    result[key.strip()] = value.strip()
    except FileNotFoundError:
        print(f'❌ 配置文件不存在: {file_path}')
    except Exception as err:
        print(f'❌ 读取配置文件失败: {err}')

    return result


def check_config(config, required_keys=None):
    """
    校验配置是否包含必需的 key，不打印 API Key 原文

    Returns:
        bool: 是否全部存在
    """
    if required_keys is None:
        required_keys = ['PERSONAL-API-KEY', 'PERSONAL-UID', 'SERVICE_URL']

    missing = [k for k in required_keys if k not in config]
    if missing:
        print(f'❌ 配置缺失: {", ".join(missing)}')
        return False

    return True


def calculate_sha256(file_path):
    """计算文件的 SHA256 哈希值"""
    sha256 = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            sha256.update(chunk)
    return sha256.hexdigest()


# ── 文件上传 ──────────────────────────────────────────────────

def upload_file(file_path, config, object_type="TEMPORARY_MATERIAL_DOC"):
    """
    将本地文件上传到小艺文件存储服务（三阶段上传：prepare → upload → complete）

    Args:
        file_path: 本地文件路径
        config: 已读取的配置 dict
        object_type: 文件类型（默认 TEMPORARY_MATERIAL_DOC）

    Returns:
        str: fileUrl，失败返回 None
    """
    try:
        if not os.path.isfile(file_path):
            print(f'❌ 文件不存在: {file_path}')
            return None

        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        file_sha256 = calculate_sha256(file_path)
        uid = config['PERSONAL-UID']
        base_url = config['SERVICE_URL']

        common_headers = {
            'Content-Type': 'application/json',
            'x-uid': uid,
            'x-api-key': config['PERSONAL-API-KEY'],
            'x-request-from': 'openclaw',
        }

        # ── 阶段 1: Prepare ──
        prepare_url = f'{base_url}/osms/v1/file/manager/prepare'
        prepare_payload = {
            'objectType': object_type,
            'fileName': file_name,
            'fileSha256': file_sha256,
            'fileSize': file_size,
            'fileOwnerInfo': {'uid': uid, 'teamId': uid},
            'useEdge': False,
        }

        prepare_resp = requests.post(
            prepare_url, headers=common_headers, json=prepare_payload,
            timeout=30, verify=False
        )

        if prepare_resp.status_code != 200:
            print(f'❌ Prepare 失败: HTTP {prepare_resp.status_code}')
            return None

        prepare_data = prepare_resp.json()
        if 'code' in prepare_data and prepare_data['code'] != '0':
            print(f'❌ Prepare 失败: {prepare_data.get("desc", "未知错误")}')
            return None

        object_id = prepare_data.get('objectId')
        draft_id = prepare_data.get('draftId')
        upload_infos = prepare_data.get('uploadInfos', [])

        if not object_id or not draft_id or not upload_infos:
            print('❌ Prepare 响应缺少必要字段')
            return None

        upload_info = upload_infos[0]
        upload_url = upload_info['url']
        upload_method = upload_info.get('method', 'PUT').upper()
        upload_headers = upload_info.get('headers', {'Content-Type': 'application/octet-stream'})

        # ── 阶段 2: Upload ──
        with open(file_path, 'rb') as f:
            file_data = f.read()

        upload_resp = requests.request(
            method=upload_method, url=upload_url, headers=upload_headers,
            data=file_data, timeout=120, verify=False
        )

        if upload_resp.status_code not in (200, 204):
            print(f'❌ 文件上传失败: HTTP {upload_resp.status_code}')
            return None

        # ── 阶段 3: Complete ──
        complete_url = f'{base_url}/osms/v1/file/manager/completeAndQuery'
        complete_payload = {'objectId': object_id, 'draftId': draft_id}

        complete_resp = requests.post(
            complete_url, headers=common_headers, json=complete_payload,
            timeout=30, verify=False
        )

        if complete_resp.status_code != 200:
            print(f'❌ Complete 失败: HTTP {complete_resp.status_code}')
            return None

        complete_data = complete_resp.json()
        file_url = complete_data.get('fileDetailInfo', {}).get('url', '')

        if not file_url:
            print('❌ Complete 响应中未获取到文件 URL')
            return None

        print(f'✅ 图片上传成功 (objectId={object_id})')
        return file_url

    except requests.exceptions.Timeout:
        print('❌ 上传超时')
        return None
    except requests.exceptions.ConnectionError as e:
        print(f'❌ 连接失败: {e}')
        return None
    except Exception as e:
        print(f'❌ 上传异常: {e}')
        return None


# ── 图像理解核心逻辑 ──────────────────────────────────────────

def _resize_if_needed(file_path):
    """
    如果图片分辨率超过 MAX_RESOLUTION×MAX_RESOLUTION，等比例缩放并保存到临时文件

    Returns:
        str: 实际上传的文件路径（原文件或缩放后的临时文件）
    """
    try:
        img = Image.open(file_path)
        w, h = img.size
        if w <= MAX_RESOLUTION and h <= MAX_RESOLUTION:
            return file_path

        # 等比例缩放，长边对齐 MAX_RESOLUTION
        ratio = min(MAX_RESOLUTION / w, MAX_RESOLUTION / h)
        new_w = int(w * ratio)
        new_h = int(h * ratio)
        img_resized = img.resize((new_w, new_h), Image.LANCZOS)

        # 保存到临时文件，保留原格式
        fmt = img.format or 'PNG'
        suffix = os.path.splitext(file_path)[1] or '.png'
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        save_kwargs = {'format': fmt}
        if fmt == 'JPEG':
            save_kwargs['quality'] = 95
        img_resized.save(tmp, **save_kwargs)
        tmp.close()

        print(f'📐 缩放: {w}×{h} → {new_w}×{new_h}')
        return tmp.name
    except Exception as e:
        print(f'⚠️ 缩放失败，使用原图: {e}')
        return file_path


def _resolve_image_url(image_path, config):
    """
    将图片路径解析为公网 URL
    - 已经是 http/https URL → 直接返回
    - 本地文件路径 → 缩放（如需）→ 上传后返回 URL

    Args:
        image_path: 图片路径
        config: 已读取的配置 dict

    Returns:
        str: 公网 URL，失败返回 None
    """
    if image_path.startswith(('http://', 'https://')):
        return image_path

    if not os.path.isfile(image_path):
        print(f'❌ 文件不存在: {image_path}')
        return None

    upload_path = _resize_if_needed(image_path)
    file_url = upload_file(upload_path, config)

    # 清理临时文件
    if upload_path != image_path:
        try:
            os.unlink(upload_path)
        except OSError:
            pass

    return file_url


def image_understanding(image_urls, text, config):
    """
    执行图像理解

    Args:
        image_urls: 图片 URL 列表（公网地址）
        text: 提示文本
        config: 已读取的配置 dict

    Returns:
        dict: {"caption": "图像描述文本"} 或 None
    """
    try:
        service_url = config['SERVICE_URL']
        api_url = f"{service_url}/celia-claw/v1/sse-api/skill/execute"
        uid = config['PERSONAL-UID']
        api_key = config['PERSONAL-API-KEY']
        trace_id = f"{hashlib.sha256(uid.encode('utf-8')).hexdigest()[:32]}-{datetime.now().strftime("%Y%m%d%H%M%S")}"

        headers = {
            'Content-Type': 'application/json',
            'Accept': 'text/event-stream',
            'x-hag-trace-id': trace_id,
            'x-api-key': api_key,
            'x-request-from': 'openclaw',
            'x-uid': uid,
            'x-skill-id': 'xiaoyi_image_comprehension',
            'x-prd-pkg-name': 'com.huawei.hag'
        }

        # 单图用 imageUrl，多图用 imageUrls 数组
        if len(image_urls) == 1:
            content = {"imageUrl": image_urls[0], "text": text}
        else:
            content = {"imageUrls": image_urls, "text": text}

        payload = {
            "version": "1.0",
            "session": {
                "isNew": True,
                "sessionId": str(uuid.uuid4()),
                "interactionId": 0
            },
            "endpoint": {
                "device": {
                    "sid": uuid.uuid4().hex,
                    "prdVer": "99.0.64.303",
                    "phoneType": "WLZ-AL10",
                    "sysVer": "HarmonyOS_2.0.0",
                    "deviceType": 0,
                    "timezone": "GMT+08:00"
                },
                "locale": "zh-CN",
                "sysLocale": "zh",
                "countryCode": "CN"
            },
            "utterance": {
                "type": "text",
                "original": text
            },
            "actions": [
                {
                    "actionSn": str(uuid.uuid4()),
                    "actionExecutorTask": {
                        "agentState": "OnShelf",
                        "actionName": "imageComprehensionStream",
                        "content": content
                    }
                }
            ]
        }

        response = requests.post(
            api_url, headers=headers, json=payload,
            stream=True, timeout=120, verify=False
        )

        if response.status_code != 200:
            print(f'❌ 请求失败: HTTP {response.status_code}')
            print(f'🔍 响应: {response.text[:500]}')
            return None

        # 接收 SSE 流式数据
        last_caption = ""
        buffer = ""
        print('📡 SSE 流已建立')

        for chunk in response.iter_content(chunk_size=1024, decode_unicode=True):
            if chunk is None:
                continue

            buffer += chunk
            lines = buffer.split('\n')
            buffer = lines.pop()

            for line in lines:
                line = line.rstrip('\r')
                if not line or not line.startswith("data:"):
                    continue

                data_content = line[5:].strip()
                if not data_content or data_content == "[DONE]":
                    continue

                try:
                    data_json = json.loads(data_content)

                    # 检查顶层错误响应（如 backendError）
                    top_code = data_json.get('code')
                    if top_code and str(top_code) != '200':
                        top_desc = data_json.get('desc', '未知错误')
                        print(f'❌ 服务端返回错误: [{top_code}] {top_desc}')
                        # 标记为错误并提前退出
                        last_caption = ""
                        break

                    if 'abilityInfos' in data_json:
                        for info in data_json['abilityInfos']:
                            result = info.get('actionExecutorResult', {})
                            # 检查 abilityInfos 内的错误
                            error_code = result.get('errorCode') or result.get('code')
                            if error_code and str(error_code) != '0':
                                error_desc = result.get('desc', '未知错误')
                                print(f'❌ 服务端返回错误: [{error_code}] {error_desc}')
                                last_caption = ""
                                break
                            stream_info = result.get('reply', {}).get('streamInfo', {})
                            stream_content = stream_info.get('streamContent', '')
                            if stream_content:
                                last_caption = stream_content
                except json.JSONDecodeError:
                    pass

        return {"caption": last_caption} if last_caption else None

    except requests.exceptions.Timeout:
        print('❌ 请求超时')
        return None
    except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError):
        # 连接提前关闭，但可能已收到部分数据
        if last_caption:
            print(f'⚠️ 流中断，已收到 {len(last_caption)} 字')
            return {"caption": last_caption}
        return None
    except Exception as e:
        print(f'❌ 请求异常: {e}')
        return None


def main():
    parser = argparse.ArgumentParser(
        description='小艺图像理解 - 支持本地图片和公网 URL'
    )
    parser.add_argument(
        '--image', '-i',
        nargs='+',
        action='append',
        required=True,
        metavar='PATH',
        help='图片路径（本地路径或 http/https URL），支持多张。'
             '可用空格分隔: --image a.jpg b.jpg，'
             '也可多次指定: --image a.jpg --image b.jpg'
    )
    parser.add_argument(
        '--prompt', '-p',
        required=True,
        metavar='TEXT',
        help='提示词（必填）'
    )
    args = parser.parse_args()
    text = args.prompt
    # action='append' + nargs='+' 产生嵌套列表 [[img1, img2], [img3]]，需展平
    image_paths = [p for group in args.image for p in group]

    if len(image_paths) > 10:
        print('❌ 最多支持 10 张图片')
        return

    config = read_xiaoyienv()
    if not check_config(config):
        return

    # 解析所有图片 URL（本地文件自动上传）
    image_urls = []
    for img_path in image_paths:
        url = _resolve_image_url(img_path, config)
        if not url:
            print(f'❌ 失败: {img_path}')
            return
        image_urls.append(url)

    # 一次请求处理所有图片
    result = image_understanding(image_urls, text, config)
    if result:
        result["images"] = image_paths
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(json.dumps(None))


if __name__ == '__main__':
    main()