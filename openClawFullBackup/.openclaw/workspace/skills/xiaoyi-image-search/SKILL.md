---
name: xiaoyi-image-search
description: 图片搜索技能。支持搜索图片并直接下载到本地临时目录，返回本地路径而非URL链接。适用于为文档、PPT、报告等场景查找配图素材。
---

# Image-Search: 多引擎图片搜索器

## 简介

图片搜索工具，支持两种模式：
- **下载模式（默认，推荐）**：搜索图片并逐张下载到本地临时目录 `/tmp/xiaoyi-image-search/{sn}`，下载完一张立即输出该图片信息。**不返回原图URL，避免模型篡改链接的风险。**
- **URL模式（向后兼容）**：返回原图链接（OSMS 预签名 URL）、缩略图、标题、尺寸等信息。

## 触发条件

当用户表达以下意图时，请激活此技能：

### 1. 直接指令型 (Direct Commands)

- "搜索一张关于XXX的图片"
- "帮我找几张XXX的图"
- "搜图 XXX"
- "执行 image-search"
- "运行图片搜索技能"

### 2. 素材需求型 (Asset Gathering)

- "帮我找一些配图素材"
- "我需要几张关于XXX的图片用于PPT"
- "给报告找些插图"
- "搜索一些XXX相关的图片素材"

### 3. 自然语言型 (Natural Language Intent)

- "有没有XXX的图片？"
- "能不能帮我搜一下XXX的图？"
- "我想要一些XXX的图片"

## 文件结构

```
xiaoyi-image-search/
    ├── scripts            # 程序文件夹
    │ ├── index.js         # 主程序（CLI入口，支持下载模式）
    │ ├── env_loader.js    # 加载环境变量
    │ ├── image_search.js  # 请求服务（执行搜索逻辑）
    │ └── package.json     # node依赖
    └── SKILL.md           # 使用说明（本文档）
```

## 参数说明

| 参数 | 必填 | 默认值 | 说明 |
|:-----|:-----|:------|:-----|
| `query` | 是 | - | 搜索关键词 |
| `num_results` | 否 | 5 | 返回结果数量 |
| `store` | 否 | `osms` | 图片存储方式 |
| `--download <dir>` | 否 | `/tmp/xiaoyi-image-search/{sn}` | 下载模式：逐张下载到指定目录，默认自动创建临时目录 |
| `--url` | 否 | - | URL模式：返回原图链接（向后兼容，不使用下载模式） |

## 输出格式

### 下载模式（`--download` 指定目录）

```
| 序号 | 标题 | 尺寸 | 本地路径 | 来源页面 |
|:---|:---|:---|:---|:---|
| 1 | XXX | 1920 x 1080 | `/tmp/xiaoyi-image-search/a1b2c3d4/image_01.jpg` | https://example.com |
| 2 | YYY | 800 x 600 | `/tmp/xiaoyi-image-search/a1b2c3d4/image_02.jpg` | https://example2.com |
```

**注意**：下载模式下不输出 `原图URL` 和 `缩略图`，避免AI模型输出时篡改链接。

### URL模式（无 `--download`）

```
| 序号 | 标题 | 尺寸 | 原图URL | 缩略图 | 来源页面 |
|:---|:---|:---|:---|:---|:---|
| 1 | XXX | 1920 x 1080 | https://... | https://... | https://example.com |
| 2 | YYY | 800 x 600 | https://... | https://... | https://example2.com |
```

## 核心逻辑
1. **前置检查（每次调用前必须执行）**：先检查依赖是否已安装，未安装则自动安装，确保后续执行不因缺少依赖而失败。

   ```bash
   # 必须在调用 index.js 前执行
    cd /path/to/current/skill/scripts
    npm install
   ```

   依赖安装成功后，才继续后续步骤。
2. **接收参数**：解析搜索关键词及可选参数。
3. **发送请求**：调用内部 ImageSearch 接口（`mcp_server_name: browser-use`, `mcp_function_name: image_search`）。
4. **解析响应**：`result` 字段为 JSON 字符串，需二次解析获取图片列表。
5. **下载（可选）**：逐张下载图片到指定目录，每下载完一张立即输出该图片信息（用 `<<<END_OF_IMAGE>>>` 分隔）。
6. **格式化输出**：下载模式下输出标题、尺寸、本地路径；URL模式下输出标题、尺寸、原图URL等。

## 使用方法

### 依赖安装（初次使用）

首次使用此技能前，需要安装 Node.js 依赖：

```bash
cd /path/to/current/skill/scripts
npm install
```

### 命令行调用

```bash
# 用法1：默认下载模式（自动保存到 /tmp/xiaoyi-image-search/{sn}，逐张下载并输出）
node /path/to/current/skill/scripts/index.js "黄山"
node /path/to/current/skill/scripts/index.js "黄山" 4

# 用法2：指定下载目录
node /path/to/current/skill/scripts/index.js "黄山" --download /tmp/images

# 用法3：指定存储方式
node /path/to/current/skill/scripts/index.js "黄山" 5 osms --download /tmp/images

# 用法4：URL模式（向后兼容）
node /path/to/current/skill/scripts/index.js "黄山" --url
```

### 执行流程
> **⚠️ 前置依赖检查（必须首个步骤）：**
> ```bash
> SKILL_DIR=~/.openclaw/workspace/skills/xiaoyi-image-search/scripts
> [ -d "$SKILL_DIR/node_modules" ] || (cd "$SKILL_DIR" && npm install)
> ```
#### 1. 搜索并下载图片（默认自动创建目录 /tmp/xiaoyi-image-search/{sn}）

```bash
# 先确保依赖就位
SKILL_DIR=~/.openclaw/workspace/skills/xiaoyi-image-search/scripts
[ -d "$SKILL_DIR/node_modules" ] || (cd "$SKILL_DIR" && npm install)

# 然后执行搜索
node /path/to/current/skill/scripts/index.js "黄山"
```

#### 2. 解析输出，获取本地文件路径

```
输出格式：

<<<DOWNLOAD_DIR:/tmp/xiaoyi-image-search/a1b2c3d4>>>

| 序号 | 标题 | 尺寸 | 本地路径 | 来源页面 |
|:---|:---|:---|:---|:---|
| 1 | XXX | 1920 x 1080 | `/tmp/xiaoyi-image-search/a1b2c3d4/image_01.jpg` | https://example.com |
| 2 | YYY | 800 x 600 | `/tmp/xiaoyi-image-search/a1b2c3d4/image_02.jpg` | https://example2.com |
```

#### 3. 发送图片给用户（按顺序返回）

按顺序逐张发送图片

```javascript
// 按序号顺序发送图片（1→2→3→...）
send_file_to_user(fileLocalUrls=["/tmp/xiaoyi-image-search/a1b2c3d4/image_01.jpg"])
// 等待上一张发送完成
send_file_to_user(fileLocalUrls=["/tmp/xiaoyi-image-search/a1b2c3d4/image_02.jpg"])
// 等待上一张发送完成
send_file_to_user(fileLocalUrls=["/tmp/xiaoyi-image-search/a1b2c3d4/image_03.jpg"])
```

**注意**：
1. 图片按照表格中的序号顺序返回给用户。
2. 每次发送前确认该图片是否已发送，禁止重复发送

### 环境变量要求

需要在 `~/.xiaoyienv` 或系统环境变量中配置：

- `SERVICE_URL` — 服务基础地址
- `PERSONAL_UID` — 鉴权 UID
- `PERSONAL_API_KEY` — 鉴权 API Key

**必须严格遵守以下规则**

1. 禁止自动修正链接。
2. 禁止替换域名后缀，所有域名后缀必须严格保留原样：.cn 永远是 .cn，绝对不能改成 .com。
3. **禁止拆分关键词**：当搜索结果数量不满足用户要求时，严禁将关键词拆分成多个更宽泛的词进行多次搜索。例如：用户搜索"黄山日出"要求10张但只返回5张时，禁止分别搜索"黄山"和"日出"来凑数。必须保持原关键词不变，返回实际搜索到的结果即可。
