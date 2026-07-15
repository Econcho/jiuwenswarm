---
name: xiaoyi-image-understanding
description: 使用小艺图像理解API进行图像内容识别和描述，获取图像的详细文本描述。当用户需要分析图片内容、识别图像中的物体、场景理解、提取图像信息、图像问答、OCR文字识别、图表分析等任务时使用此技能。
---

# 小艺图像理解

传入图片（本地路径或公网 URL），返回图像的详细文本描述。最多 10 张。

## 核心功能

- **图片描述**：详细描述图片中的场景、人物、活动
- **OCR 文字识别**：识别图中文字，保持原始排版，支持手写、印刷、表格
- **物体识别**：识别并列举图中物体，标注位置
- **图片问答**：针对图片内容提问，获取精准回答
- **图表分析**：提取图表数据点、趋势和关键信息

## 用法

```bash
# 本地单图
python3 scripts/image_understanding.py --image "photo.jpg" --prompt "详细描述图片中的场景"

# URL 图片
python3 scripts/image_understanding.py --image "https://example.com/image.jpg" --prompt "提取图中所有文字"

# 多图（最多 10 张）
python3 scripts/image_understanding.py --image img1.jpg img2.jpg img3.jpg --prompt "对比这些图片的差异"
```

## 参数

| 参数 | 必填 | 说明 |
|------|------|------|
| --image / -i | ✅ | 本地路径或 http/https URL，最多 10 张 |
| --prompt / -p | ✅ | 提示词 |

## 输出格式

```json
{"images": ["photo.jpg"], "caption": "图片描述文本..."}
```

多图时 images 为数组，caption 为整体描述。部分图片上传失败时会有 `failed` 字段。

## Prompt 要点

- **说清楚要什么**：`提取图中所有文字` > `图中讲了什么`
- **限定范围**：`忽略背景，只识别表格中的数字`、`仅描述人物动作`
- **多图对比**：`对比图1和图2的差异`

## 故障排除

| 问题 | 解决方案 |
|------|----------|
| 配置缺失 | 检查 `~/.openclaw/.xiaoyienv` 是否包含 PERSONAL-API-KEY、PERSONAL-UID、SERVICE_URL |
| 文件不存在 | 确认图片路径正确 |
| 上传失败 | 检查网络连接，确认图片未损坏 |
| 识别不准确 | 使用更具体的 prompt |
| 请求超时 | 默认 120 秒，大图可能需要更长时间 |
| 图片过大 | 超过 2048×2048 自动等比例缩放 |