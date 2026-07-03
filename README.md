# 短剧SOP飞书提醒系统

这是一个给海外短剧制片使用的轻量后台。你可以粘贴项目制作节点，系统会按项目自己的节点日期自动计算每天应该推进到哪一步，并通过飞书群机器人发送每日提醒。

## 功能

- Streamlit 网页后台
- SQLite 本地数据库
- APScheduler 每日定时推送
- 飞书自定义机器人 Webhook
- 支持飞书机器人签名 Secret
- Webhook URL 和 Secret 从 `.env` 读取
- 项目新增、编辑、删除、列表
- 今日项目推进表
- 飞书测试消息
- Excel 导入和导出
- Docker / docker-compose 部署

## 项目结构

```text
.
├── app.py                # Streamlit 后台页面
├── database.py           # SQLite 数据库逻辑
├── sop.py                # SOP 规则和风险排序
├── feishu.py             # 飞书签名和发送逻辑
├── scheduler.py          # 定时任务和每日消息生成
├── requirements.txt      # Python 依赖
├── .env.example          # 环境变量示例
├── Dockerfile            # Docker 镜像
├── docker-compose.yml    # Docker Compose 部署
└── data/                 # SQLite 数据库目录
```

## 本地运行

1. 安装 Python 3.11 或更高版本。
2. 安装依赖：

```bash
pip install -r requirements.txt
```

3. 复制环境变量文件：

```bash
cp .env.example .env
```

Windows PowerShell 可以使用：

```powershell
Copy-Item .env.example .env
```

4. 编辑 `.env`：

```env
FEISHU_WEBHOOK_URL=你的飞书机器人Webhook
FEISHU_SECRET=你的飞书机器人签名Secret
REMINDER_TIME=10:00
```

如果飞书机器人没有开启签名，`FEISHU_SECRET` 可以留空。

5. 启动后台：

```bash
streamlit run app.py
```

打开浏览器访问：

```text
http://localhost:8501
```

首次启动会自动创建 SQLite 数据库，并写入几条示例项目。

## Docker 部署

1. 复制 `.env`：

```bash
cp .env.example .env
```

2. 填写飞书配置：

```env
FEISHU_WEBHOOK_URL=你的飞书机器人Webhook
FEISHU_SECRET=你的飞书机器人签名Secret
REMINDER_TIME=10:00
```

3. 启动：

```bash
docker-compose up -d --build
```

4. 查看日志：

```bash
docker-compose logs -f
```

5. 访问后台：

```text
http://服务器IP:8501
```

SQLite 数据库会保存在宿主机的 `data/` 目录中，容器重启不会丢失数据。

## 云服务器建议

你的需求更适合云服务器运行，因为本地电脑关机后无法定时推送。

最低配置即可：

- 1 核 CPU
- 1G 内存
- 20G 硬盘
- Ubuntu 系统

大概流程：

```text
买云服务器
上传项目代码
配置 .env 里的飞书 Webhook 和 Secret
docker-compose up -d --build
每天自动推送飞书
```

## Excel 导入格式

支持 `.xlsx` 或 `.xls` 文件，推荐列名如下：

| 项目名 | 开始制作日期 | 集数 | 项目等级 | 负责人 | 当前状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| 逆袭女王 | 2026-06-23 | 30 | B级 | Alice | 进行中 | 示例备注 |

其中必填列：

- 项目名
- 开始制作日期

其他字段为空时会使用默认值。

## 智能识别新增

后台左侧点击“智能识别新增”，可以直接粘贴制作时间节点，系统会自动识别项目名，并从第一条节点反推开始制作日期。

示例格式：

```text
制作时间截点：
项目：困于深渊：狼王的契约娇妻：Caged by the Alpha: My Human Soulmate
资产确定（3天）6月24日
首集制作与修改（2天）6月26日
一卡前制作（3天）6月29日
全集制作（10天）7月9日
终审与交付（2天）7月11日
```

如果第一条是“资产确定（3天）6月24日”，系统会认为 6月24日是第 3 天，所以自动反推开始制作日期为 6月22日。

## SOP 和节点规则

智能识别新增的项目会优先使用自己的节点排期，例如“全集制作（16天）7月12日”“终审与交付（2天）7月14日”，不会被固定 10 天周期限制。

没有结构化节点的旧项目，会继续使用 `sop.py` 的 10 天 SOP 作为兜底。

兜底 SOP 规则集中在 `sop.py` 的 `SOP_RULES` 中。

例如要修改第 4 天任务，只需要改：

```python
4: {
    "stage": "首集关键节点",
    "task": "第一集制作、修改、确认首集成片。",
    "urge": "导演 / 剪辑 / 制片 / 甲方确认人",
    "risk": "首集风格决定后续批量制作方向，属于关键节点。",
    "risk_level": 3,
},
```

排序规则也在 `sop.py` 里，通过 `risk_level` 控制：

1. 超期项目
2. 第 9-10 天交付项目
3. 第 4 天首集关键节点
4. 第 5-8 天批量制作项目
5. 第 1-3 天资产项目
6. 已交付项目不显示

## 飞书推送格式

系统每天按后台设置的时间自动推送，默认 `10:00`。

推送内容类似：

```text
【今日B级短剧SOP推进提醒】

日期：YYYY-MM-DD
进行中项目：X 个
超期项目：X 个
交付节点：X 个
首集关键节点：X 个

━━━━━━━━━━━━━━

《项目名》
开始日期：YYYY-MM-DD
今天是第X天
当前阶段：XXX
今日任务：XXX
你要催：XXX
风险提醒：XXX
```

## 注意事项

- `.env` 里不要提交真实飞书 Webhook 和 Secret。
- 云服务器需要确保 8501 端口已放行。
- 如果使用本地电脑运行，电脑关机或程序关闭后不会推送提醒。

## 示例数据

系统首次启动会自动创建 `data/sop_projects.db`，并写入 5 条示例项目。

另外提供了 `data/sample_projects.xlsx`，可以在“Excel 导入”页面测试导入流程。

### 只给等级和交付时间时自动倒推

智能识别新增支持简写格式：

```text
项目：我的妈咪是冰雪女王：My Mom Is the Ice Queen
等级：S级
交付时间：7月14日
```

系统会按 S/A/B 对应 SOP 周期自动倒推全部节点：

- S级：3天资产确定 + 2天首集制作与修改 + 5天一卡前制作 + 16天全集制作 + 2天终审与交付，共 28 天
- A级：3天资产确定 + 2天首集制作与修改 + 3天一卡前制作 + 10天全集制作 + 2天终审与交付，共 20 天
- B级：3天资产确定 + 1天首集制作与修改 + 2天2-10集制作 + 2天全集制作 + 2天终审与交付，共 10 天

## GitHub Actions 云端自动推送

如果希望电脑关机或睡眠后仍然每天自动推送，可以使用 GitHub Actions。

本项目已经包含：

```text
.github/workflows/daily-feishu-reminder.yml
scripts/send_github_reminder.py
scripts/export_actions_data.py
data/projects_for_actions.json
```

### 推送时间

GitHub Actions 使用 UTC 时间。当前 workflow 设置为：

```yaml
cron: "7 3 * * *"
```

这表示北京时间每天 11:07 自动发送。避开整点可以减少 GitHub 排队延迟。

### GitHub Secrets

进入 GitHub 仓库：

```text
Settings → Secrets and variables → Actions → New repository secret
```

添加两个 Secret：

```text
FEISHU_WEBHOOK_URL=你的飞书机器人 Webhook
FEISHU_SECRET=你的飞书机器人 Secret，没有开启签名可留空或不填
```

### 本地修改项目后如何同步到云端

本地 Streamlit 后台仍然用于新增、编辑、删除项目。

每次改完项目后：

1. 打开后台“Excel 导出”页面。
2. 点击“生成 GitHub Actions 数据文件”。
3. 提交并推送 `data/projects_for_actions.json` 到 GitHub。

GitHub Actions 每天会读取这个 JSON 文件并推送飞书。

### 手动测试 GitHub Actions

进入 GitHub 仓库：

```text
Actions → Daily Feishu Reminder → Run workflow
```

手动运行一次，如果飞书收到消息，就说明云端推送配置成功。
