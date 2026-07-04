# 短剧 SOP 飞书提醒系统 - Review Notes

## 项目用途

这是一个 Streamlit 本地后台，用来管理短剧项目排期，并通过飞书机器人发送每日项目推进提醒。项目也支持同步到 GitHub Actions，由云端按固定时间自动发送飞书提醒。

## 本地运行

1. 安装依赖：

```bash
pip install -r requirements.txt
```

2. 创建 `.env`，填写飞书配置：

```env
FEISHU_WEBHOOK_URL=
FEISHU_SECRET=
REMINDER_TIMES=09:57,16:00
```

3. 启动：

```bash
streamlit run app.py
```

4. 本地访问：

```text
http://127.0.0.1:8501/
```

## 当前主要功能

- 今日提醒：查看当天需要推进的项目和飞书推送预览。
- 智能识别新增：只手填项目名，等级和交付日期可选择；也支持粘贴完整排期识别。
- 交付确认：今天到期和已超期项目优先展示，可确认已交付，或选择延期天数并自动重算节点。
- 项目工作台：所有项目直接表格化编辑，不再需要先选择项目再编辑。
- 飞书配置测试：配置检查、测试消息、本地推送时间管理、最近推送日志。
- 数据管理：Excel 导入、Excel 导出、GitHub 云端同步合并在一个页面。

## 飞书推送格式

未超期：

```text
[{标签}]《项目名》｜等级第X天｜交付YYYY-MM-DD｜剩X天
承制方：具体产出物；制片：具体确认动作。
```

已超期：

```text
[超期]《项目名》｜等级第X天｜应交YYYY-MM-DD｜已超X天
承制方：补齐缺口产出；制片：确认交付状态。
```

## GitHub Actions

云端定时文件：

```text
.github/workflows/daily-feishu-reminder.yml
```

当前北京时间推送节点：

```text
09:57
16:00
```

GitHub Actions 使用 UTC，所以 workflow 中会换算成 UTC 时间。

## 本压缩包刻意不包含

- `.env`：飞书机器人地址和密钥不应打包。
- `.venv/`：本地虚拟环境可重新安装。
- `.git/`：版本历史不需要给外部审阅。
- `data/*.db`：本地 SQLite 数据库不打包。
- `*.log`：本地运行日志不打包。

## Review 重点建议

- 检查 `app.py` 中项目工作台和交付确认流程是否还有更顺手的交互方式。
- 检查 `scheduler.py` 和 `scripts/send_github_reminder.py` 的飞书文案是否满足业务语气。
- 检查 GitHub Actions 的定时触发策略是否适合实际延迟。
- 检查 `data/projects_for_actions.json` 是否应该继续保留在仓库里，或改成更严格的数据同步方式。
