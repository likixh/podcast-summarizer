# 🎙️ 播客精华自动提取器

自动监控网易云音乐播客，转录全文并用 AI 提取核心认知，存入飞书多维表格。

## 技术栈

| 环节 | 工具 | 费用 |
|------|------|------|
| RSS 监控 | feedparser + RSSHub | 免费 |
| 音频下载 | yt-dlp | 免费 |
| 语音转文字 | Faster-Whisper large-v3 | 完全免费（本地运行）|
| AI 总结 | OpenRouter 免费模型 | 完全免费 |
| 数据存储 | 飞书多维表格 | 免费 |
| 自动运行 | GitHub Actions | 免费（公开仓库无限分钟）|

## 配置步骤

### 1. Fork 或 Clone 本仓库

### 2. 在 GitHub 仓库设置 Secrets

进入仓库 → Settings → Secrets and variables → Actions → New repository secret

需要添加以下 5 个 Secrets：

| Secret 名称 | 说明 |
|------------|------|
| `FEISHU_APP_ID` | 飞书自建应用的 App ID |
| `FEISHU_APP_SECRET` | 飞书自建应用的 App Secret |
| `FEISHU_APP_TOKEN` | 多维表格 URL 中的 token（/base/ 后面那段）|
| `FEISHU_TABLE_ID` | 多维表格 URL 中的 table 参数（tbl 开头）|
| `OPENROUTER_API_KEY` | OpenRouter 的 API Key（sk-or-v1-...）|

### 3. 飞书多维表格列结构

表格需要包含以下列（均为文本类型）：

- 拆解书名
- 标题
- 发布日期
- 原链接
- 核心认知
- 金句
- 书单
- 完整转录
- 处理状态

### 4. 触发运行

- **自动触发**：每天北京时间早上 9 点自动检查更新
- **手动触发**：GitHub 仓库 → Actions → 选择 workflow → Run workflow

## 注意事项

- 首次运行会下载 Whisper large-v3 模型（约 3GB），之后从缓存读取
- 85 分钟音频完整处理约需 30-90 分钟
- 已处理过的集数会自动跳过，不会重复写入
