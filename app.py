from datetime import date, datetime
from io import BytesIO
from datetime import time, timedelta
import base64
import json
import os
import shutil
import subprocess
import tempfile

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from delivery_rules import (
    cycle_days_for_level,
    delivery_label,
    resolve_delivery_date,
    summarize_rows,
)
from database import (
    add_project,
    add_notification_log,
    delete_project,
    get_project,
    get_project_milestones,
    get_setting,
    has_successful_auto_log,
    import_projects_from_excel,
    init_db,
    list_notification_logs,
    list_projects,
    projects_to_dataframe,
    rebuild_milestones_by_delivery_date,
    replace_project_milestones,
    seed_sample_data,
    update_project,
)
from feishu import send_feishu_message
from message_template import DEFAULT_MESSAGE_TEMPLATE, load_message_template, reset_message_template, save_message_template
from project_parser import parse_chinese_schedule_text
from scheduler import (
    build_feishu_message,
    build_today_rows,
    contractor_output,
    generate_reminder_check_times,
    get_reminder_window,
    get_reminder_times,
    reminder_window_minutes,
    producer_action,
    send_daily_reminder,
    send_test_message,
    update_reminder_window,
    update_reminder_times,
)
from scripts.export_actions_data import export_actions_data
from styles import COLORS
from ui_components import (
    build_home_table,
    inject_custom_css,
    render_empty_state,
    render_kpi_grid,
    render_page_header,
    render_project_cards,
    render_section_title,
)
from utils_time import now_beijing, today_beijing


DEFAULT_REMINDER_TIMES = "13:00"
STATUS_OPTIONS = ["进行中", "已交付", "延期", "暂停"]
LEVEL_OPTIONS = ["自定义", "S级", "A级", "B级"]
GITHUB_REPOSITORY = "1321769104lp/sop"
WORKFLOW_PATH = ".github/workflows/daily-feishu-reminder.yml"
WORKFLOW_ID = "daily-feishu-reminder.yml"
DEFAULT_REMINDER_WINDOW_START = "13:00"
DEFAULT_REMINDER_WINDOW_END = "18:00"
DEFAULT_REMINDER_CHECK_COUNT = 10


def find_git_executable() -> str:
    """寻找可用的 Git。"""
    git_exe = shutil.which("git")
    if not git_exe:
        raise RuntimeError("没有找到 Git。请先在这台电脑安装 Git。")
    return git_exe


def find_gh_executable() -> str:
    """寻找 GitHub CLI。"""
    project_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        shutil.which("gh"),
        os.path.join(project_dir, "tools", "gh", "bin", "gh.exe"),
        os.path.join(os.path.dirname(project_dir), "tools", "gh", "bin", "gh.exe"),
    ]
    return next((path for path in candidates if path and os.path.exists(path)), "gh")


def run_git_command(args: list[str]) -> subprocess.CompletedProcess:
    """在项目目录执行 Git 命令。"""
    git_exe = find_git_executable()
    return subprocess.run(
        [git_exe, *args],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        text=True,
        capture_output=True,
        check=False,
    )


def sync_actions_data_to_github() -> str:
    """导出云端数据文件，并提交推送到 GitHub。"""
    export_actions_data()

    status = run_git_command(["status", "--short", "data/projects_for_actions.json"])
    if status.returncode != 0:
        raise RuntimeError(status.stderr or status.stdout)
    if not status.stdout.strip():
        return "项目数据没有变化，GitHub 已经是最新。"

    add_result = run_git_command(["add", "data/projects_for_actions.json"])
    if add_result.returncode != 0:
        raise RuntimeError(add_result.stderr or add_result.stdout)

    commit_message = f"Update actions project data {now_beijing().strftime('%Y-%m-%d %H:%M:%S')}"
    commit_result = run_git_command(["commit", "-m", commit_message])
    if commit_result.returncode != 0:
        output = f"{commit_result.stdout}\n{commit_result.stderr}".strip()
        if "nothing to commit" not in output:
            raise RuntimeError(output)

    push_result = run_git_command(["push"])
    if push_result.returncode != 0:
        raise RuntimeError(push_result.stderr or push_result.stdout)

    return "已同步到 GitHub，云端每日推送会使用最新项目数据。"


def beijing_time_to_utc_cron(reminder_time: str) -> tuple[int, int]:
    """把北京时间 HH:MM 转成 GitHub Actions 使用的 UTC cron。"""
    hour_text, minute_text = reminder_time.split(":")
    local_dt = datetime.combine(today_beijing(), time(int(hour_text), int(minute_text)))
    utc_dt = local_dt - timedelta(hours=8)
    return utc_dt.minute, utc_dt.hour


def build_workflow_content(start_time: str, end_time: str, check_count: int = DEFAULT_REMINDER_CHECK_COUNT) -> str:
    """根据发送区间生成 GitHub Actions 定时文件。"""
    check_times = generate_reminder_check_times(start_time, end_time, check_count)
    retry_window_minutes = reminder_window_minutes(start_time, end_time)
    schedule_lines = []
    for check_time in check_times:
        minute, hour = beijing_time_to_utc_cron(check_time)
        schedule_lines.extend(
            [
                f"    # Send window {start_time}-{end_time}; check at Beijing {check_time} = UTC {hour:02d}:{minute:02d}.",
                f'    - cron: "{minute} {hour} * * *"',
            ]
        )

    return "\n".join(
        [
            "name: Daily Feishu Reminder",
            "",
            "on:",
            "  schedule:",
            *schedule_lines,
            "  workflow_dispatch:",
            "",
            "permissions:",
            "  contents: write",
            "",
            "jobs:",
            "  send-reminder:",
            "    runs-on: ubuntu-latest",
            "    steps:",
            "      - name: Checkout repository",
            "        uses: actions/checkout@v4",
            "",
            "      - name: Setup Python",
            "        uses: actions/setup-python@v5",
            "        with:",
            '          python-version: "3.11"',
            "",
            "      - name: Install dependencies",
            "        run: pip install -r requirements-actions.txt",
            "",
            "      - name: Send Feishu reminder",
            "        env:",
            "          FEISHU_WEBHOOK_URL: ${{ secrets.FEISHU_WEBHOOK_URL }}",
            "          FEISHU_SECRET: ${{ secrets.FEISHU_SECRET }}",
            f'          REMINDER_TIMES: "{start_time}"',
            f'          RETRY_WINDOW_MINUTES: "{retry_window_minutes}"',
            '          SEND_STATE_PATH: "data/github_send_state.json"',
            "        run: python scripts/send_github_reminder.py",
            "",
            "      - name: Persist send state",
            "        if: success()",
            "        run: |",
            '          git config user.name "github-actions[bot]"',
            '          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"',
            "          if [ -f data/github_send_state.json ]; then",
            "            git add data/github_send_state.json",
            "          fi",
            "          if ! git diff --cached --quiet; then",
            '            git commit -m "Record Feishu reminder send state"',
            "            git push",
            "          fi",
            "",
        ]
    )


def run_gh_api(args: list[str], payload: dict | None = None) -> subprocess.CompletedProcess:
    """执行 GitHub CLI API 请求。"""
    gh_exe = find_gh_executable()
    if not os.path.exists(gh_exe) and not shutil.which(gh_exe):
        raise RuntimeError("没有找到 GitHub CLI。请先安装或登录 gh。")

    command = [gh_exe, "api", *args]
    temp_path = None
    try:
        if payload is not None:
            fd, temp_path = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            command.extend(["--input", temp_path])

        return subprocess.run(
            command,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def get_github_auto_push_state() -> str:
    """读取 GitHub Actions 每日飞书工作流的启用状态。"""
    result = run_gh_api(
        [
            f"repos/{GITHUB_REPOSITORY}/actions/workflows/{WORKFLOW_ID}",
            "--jq",
            ".state",
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout.strip()


def set_github_auto_push_enabled(enabled: bool) -> str:
    """启用或暂停 GitHub Actions 每日自动飞书推送。"""
    action = "enable" if enabled else "disable"
    result = run_gh_api(
        [
            f"repos/{GITHUB_REPOSITORY}/actions/workflows/{WORKFLOW_ID}/{action}",
            "--method",
            "PUT",
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)
    return get_github_auto_push_state()


def render_auto_push_control():
    """在侧边栏提供云端自动推送的暂停和恢复操作。"""
    with st.sidebar.expander("自动飞书推送控制", expanded=True):
        try:
            workflow_state = get_github_auto_push_state()
        except Exception as exc:
            st.error(format_sync_error(exc))
            st.caption("无法读取云端状态时，不会自动执行任何更改。")
            return

        is_active = workflow_state == "active"
        if is_active:
            st.success("当前状态：自动推送中")
            st.caption("暂停后，GitHub 云端不再每日自动发送；页面手动发送仍可使用。")
            confirmed = st.checkbox("我确认暂停每日自动推送", key="confirm_disable_auto_push")
            if st.button(
                "暂停自动飞书推送",
                type="primary",
                use_container_width=True,
                disabled=not confirmed,
            ):
                try:
                    new_state = set_github_auto_push_enabled(False)
                    if new_state != "disabled_manually":
                        raise RuntimeError(f"GitHub 返回了未预期的工作流状态：{new_state}")
                    st.success("每日自动飞书推送已暂停。")
                    st.rerun()
                except Exception as exc:
                    st.error(format_sync_error(exc))
        elif workflow_state == "disabled_manually":
            st.warning("当前状态：自动推送已暂停")
            st.caption("恢复后，GitHub 云端会继续按现有时间和消息规则自动发送。")
            confirmed = st.checkbox("我确认恢复每日自动推送", key="confirm_enable_auto_push")
            if st.button(
                "恢复自动飞书推送",
                type="primary",
                use_container_width=True,
                disabled=not confirmed,
            ):
                try:
                    new_state = set_github_auto_push_enabled(True)
                    if new_state != "active":
                        raise RuntimeError(f"GitHub 返回了未预期的工作流状态：{new_state}")
                    st.success("每日自动飞书推送已恢复。")
                    st.rerun()
                except Exception as exc:
                    st.error(format_sync_error(exc))
        else:
            st.warning(f"当前工作流状态：{workflow_state}")
            st.caption("该状态不能在页面中直接切换，请先检查 GitHub Actions。")


def update_github_text_file(path: str, content: str, message: str) -> str:
    """通过 GitHub API 更新远端文本文件。"""
    api_path = path.replace("\\", "/")
    sha_result = run_gh_api(
        [
            f"repos/{GITHUB_REPOSITORY}/contents/{api_path}",
            "--jq",
            ".sha",
        ]
    )
    existing_sha = sha_result.stdout.strip() if sha_result.returncode == 0 else ""
    if sha_result.returncode != 0 and "Not Found" not in (sha_result.stderr or sha_result.stdout):
        raise RuntimeError(sha_result.stderr or sha_result.stdout)

    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": "main",
    }
    if existing_sha:
        payload["sha"] = existing_sha
    update_result = run_gh_api(
        [
            f"repos/{GITHUB_REPOSITORY}/contents/{api_path}",
            "--method",
            "PUT",
            "--jq",
            ".commit.sha",
        ],
        payload=payload,
    )
    if update_result.returncode != 0:
        raise RuntimeError(update_result.stderr or update_result.stdout)
    return update_result.stdout.strip()


def sync_everything_to_github() -> dict:
    """同步本地项目数据和本地推送时间到 GitHub 云端。"""
    export_actions_data()
    reminder_window = get_reminder_window()
    workflow_content = build_workflow_content(
        reminder_window["start"],
        reminder_window["end"],
        reminder_window["check_count"],
    )

    workflow_full_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), WORKFLOW_PATH)
    with open(workflow_full_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(workflow_content)

    data_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "projects_for_actions.json")
    with open(data_path, "r", encoding="utf-8") as handle:
        data_content = handle.read()

    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "feishu_message_template.txt")
    with open(template_path, "r", encoding="utf-8") as handle:
        template_content = handle.read()

    data_sha = update_github_text_file(
        "data/projects_for_actions.json",
        data_content,
        "Sync local project data",
    )
    workflow_sha = update_github_text_file(
        WORKFLOW_PATH,
        workflow_content,
        "Sync reminder schedule times",
    )
    template_sha = update_github_text_file(
        "data/feishu_message_template.txt",
        template_content,
        "Sync Feishu message template",
    )

    return {
        "title": "GitHub 云端同步完成",
        "items": [
            "项目数据同步成功",
            "每日推送时间同步成功",
            "GitHub Actions 定时配置同步成功",
            "飞书发送模板同步成功",
        ],
        "details": {
            "data": data_sha[:7],
            "workflow": workflow_sha[:7],
            "template": template_sha[:7],
        },
    }


def render_sync_result(result: dict, sidebar=False):
    """用清单展示同步结果，不把提交号或底层输出直接露给用户。"""
    target = st.sidebar if sidebar else st
    if sidebar:
        target.success("同步完成")
        return
    target.success(result.get("title", "同步完成"))
    for item in result.get("items", []):
        target.markdown(f"- {item}")


def format_sync_error(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else "未知错误"
    if "Not Found" in text:
        return "同步失败：GitHub 上没有找到对应文件或仓库权限不足。"
    if "Bad credentials" in text or "Requires authentication" in text:
        return "同步失败：GitHub 登录状态失效，请重新授权。"
    if "failed to connect" in text.lower() or "could not resolve" in text.lower():
        return "同步失败：当前网络无法连接 GitHub。"
    return f"同步失败：{text[:160]}"


def table_height(row_count: int, min_height: int = 260, max_height: int = 720) -> int:
    """根据行数给表格一个尽量够看的高度。"""
    return min(max_height, max(min_height, 90 + row_count * 44))


def latest_push_subtitle() -> str:
    """首页副标题：优先显示最近一次推送时间。"""
    try:
        logs = list_notification_logs(limit=1)
    except Exception:
        logs = []
    if logs:
        created_at = str(logs[0].get("created_at") or "")
        if len(created_at) >= 16:
            return f"北京时间｜上次推送 {created_at[11:16]}"
    return "北京时间｜飞书推送预览已生成"


def render_manual_send_button(key: str):
    """在常用页面放一个直接手动发送飞书提醒的按钮。"""
    if st.button("手动发送今日飞书提醒", type="primary", key=key):
        try:
            result = send_daily_reminder(send_type="manual")
            st.success("今日飞书提醒已手动发送。")
            st.caption(f"飞书返回：{result}")
        except Exception as exc:
            st.error(f"发送失败：{exc}")


st.set_page_config(
    page_title="短剧SOP飞书提醒系统",
    page_icon="🎬",
    layout="wide",
)


@st.cache_resource
def bootstrap_app():
    """初始化本地页面数据；自动推送统一由 GitHub Actions 负责。"""
    load_dotenv()
    init_db(default_reminder_time=os.getenv("REMINDER_TIMES", os.getenv("REMINDER_TIME", DEFAULT_REMINDER_TIMES)))
    seed_sample_data()
    return True


def project_form(defaults: dict | None = None) -> dict:
    """项目表单，新增和编辑共用。"""
    defaults = defaults or {}
    start_value = defaults.get("start_date") or today_beijing().strftime("%Y-%m-%d")
    if isinstance(start_value, str):
        start_value = datetime.strptime(start_value, "%Y-%m-%d").date()
    delivery_value = defaults.get("delivery_date") or start_value
    if isinstance(delivery_value, str):
        delivery_value = datetime.strptime(delivery_value, "%Y-%m-%d").date()

    col1, col2 = st.columns(2)
    with col1:
        project_name = st.text_input("项目名", value=defaults.get("project_name", ""))
        start_date = st.date_input("开始制作日期", value=start_value)
        delivery_date = st.date_input("交付日期", value=delivery_value)
        episodes = st.number_input("集数", min_value=1, max_value=300, value=int(defaults.get("episodes") or 30))
        project_level = st.selectbox(
            "项目等级",
            LEVEL_OPTIONS,
            index=LEVEL_OPTIONS.index(defaults.get("project_level", "自定义")) if defaults.get("project_level", "自定义") in LEVEL_OPTIONS else 0,
        )
    with col2:
        owner = st.text_input("负责人", value=defaults.get("owner", ""))
        status = st.selectbox(
            "当前状态",
            STATUS_OPTIONS,
            index=STATUS_OPTIONS.index(defaults.get("status", "进行中")) if defaults.get("status", "进行中") in STATUS_OPTIONS else 0,
        )
        remark = st.text_area("备注", value=defaults.get("remark", ""), height=132)

    return {
        "project_name": project_name.strip(),
        "start_date": start_date.strftime("%Y-%m-%d"),
        "delivery_date": delivery_date.strftime("%Y-%m-%d"),
        "episodes": episodes,
        "project_level": project_level,
        "owner": owner.strip(),
        "status": status,
        "remark": remark.strip(),
    }


def show_today_page():
    today = today_beijing()
    rows = build_today_rows(today=today)
    summary = summarize_rows(rows)

    render_page_header(
        f"今日项目提醒｜{today.strftime('%Y-%m-%d')}",
        latest_push_subtitle(),
    )
    render_manual_send_button("today_manual_send")

    render_kpi_grid(
        [
            ("全部项目", summary["total"], None),
            ("交付风险", summary["delivery_risk"], COLORS["delivery"]),
            ("超期项目", summary["overdue"], COLORS["overdue"]),
            ("今日节点", summary["today_nodes"], COLORS["today"]),
        ]
    )

    delivery_risk_rows = [
        row for row in rows if row.get("is_overdue") or row.get("is_delivery_node") or row.get("is_delivery")
    ]
    delivery_risk_ids = {id(row) for row in delivery_risk_rows}
    today_node_rows = [
        row for row in rows if row.get("is_due_today") and id(row) not in delivery_risk_ids
    ]
    highlighted_ids = delivery_risk_ids | {id(row) for row in today_node_rows}
    normal_rows = [row for row in rows if id(row) not in highlighted_ids]

    render_section_title("交付风险", "交付倒计时 2 天内或已超期的项目")
    render_project_cards(
        delivery_risk_rows,
        contractor_output,
        producer_action,
        "暂无交付风险项目",
    )

    render_section_title("今日节点", "今天进入关键 SOP 节点的项目")
    render_project_cards(
        today_node_rows,
        contractor_output,
        producer_action,
        "暂无今日节点项目",
    )

    render_section_title("正常推进", "未进入交付风险和今日关键节点的项目")
    if normal_rows:
        home_df = build_home_table(normal_rows, contractor_output, producer_action)
        st.dataframe(
            home_df,
            use_container_width=True,
            hide_index=True,
            height=table_height(len(home_df), min_height=240, max_height=560),
            column_config={
                "标签": st.column_config.TextColumn("标签", width="small"),
                "项目名": st.column_config.TextColumn("项目名", width="large"),
                "等级": st.column_config.TextColumn("等级", width="small"),
                "D天数": st.column_config.TextColumn("D天数", width="small"),
                "交付日期": st.column_config.TextColumn("交付日期", width="medium"),
                "倒计时": st.column_config.TextColumn("倒计时", width="small"),
                "承制方动作": st.column_config.TextColumn("承制方动作", width="large"),
                "制片动作": st.column_config.TextColumn("制片动作", width="large"),
                "状态": st.column_config.TextColumn("状态", width="small"),
            },
        )
    else:
        render_empty_state("暂无正常推进项目")

    with st.expander("查看飞书推送预览", expanded=False):
        st.text(build_feishu_message(today=today))


def show_project_list_page():
    st.title("项目列表")
    rows = list_projects(include_delivered=True)
    if not rows:
        st.info("还没有项目，请先新增或导入 Excel。")
        return

    df = pd.DataFrame(rows).rename(
        columns={
            "id": "ID",
            "project_name": "项目名",
            "start_date": "开始制作日期",
            "delivery_date": "交付日期",
            "episodes": "集数",
            "project_level": "项目等级",
            "owner": "负责人",
            "status": "当前状态",
            "remark": "备注",
            "created_at": "创建时间",
            "updated_at": "更新时间",
        }
    )
    df.insert(0, "序号", range(1, len(df) + 1))
    display_cols = ["序号", "项目名", "开始制作日期", "交付日期", "集数", "项目等级", "负责人", "当前状态", "备注", "创建时间", "更新时间"]
    st.dataframe(
        df[display_cols],
        use_container_width=True,
        hide_index=True,
        height=table_height(len(df)),
        column_config={
            "序号": st.column_config.NumberColumn("序号", width="small"),
            "项目名": st.column_config.TextColumn("项目名", width="large"),
            "备注": st.column_config.TextColumn("备注", width="large"),
            "创建时间": st.column_config.TextColumn("创建时间", width="medium"),
            "更新时间": st.column_config.TextColumn("更新时间", width="medium"),
        },
    )


def show_add_page():
    st.title("新增项目")
    data = project_form()
    if st.button("保存项目", type="primary"):
        if not data["project_name"]:
            st.error("请填写项目名。")
            return
        add_project(data)
        st.success("项目已保存。")
        st.rerun()


def parse_date_value(value) -> date:
    """把表格里的日期值统一转成 date。"""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def project_delivery_info(project: dict) -> dict:
    """读取正式交付日期；没有填写时只返回预估交付日。"""
    cycle_days = cycle_days_for_level(project.get("project_level") or "B级")
    return resolve_delivery_date(project, cycle_days=cycle_days)


def project_delivery_date(project: dict, milestones: list[dict] | None = None) -> date:
    """兼容旧调用：返回正式交付日或预估交付日。"""
    return project_delivery_info(project)["delivery_date"]


def delivery_badge(delivery_date: date, today: date, status: str) -> str:
    """项目工作台里的交付状态文字。"""
    if status == "已交付":
        return "已交付"
    delta = (delivery_date - today).days
    if delta < 0:
        return f"已超{abs(delta)}天"
    if delta == 0:
        return "今天交付"
    return f"剩{delta}天"


def append_remark(existing: str, line: str) -> str:
    """追加一条简短备注。"""
    existing = (existing or "").strip()
    timestamp = now_beijing().strftime("%Y-%m-%d %H:%M")
    new_line = f"{timestamp} {line}"
    return f"{existing}\n{new_line}" if existing else new_line


def rebuild_project_to_delivery(project: dict, new_delivery_date: date, status: str | None = None) -> dict:
    """更新项目正式交付日期。"""
    data = dict(project)
    if status:
        data["status"] = status
    data["delivery_date"] = new_delivery_date.strftime("%Y-%m-%d")
    data["remark"] = append_remark(data.get("remark", ""), f"交付日期调整为 {new_delivery_date.strftime('%Y-%m-%d')}。")
    update_project(project["id"], data)

    return data


def sync_after_project_change() -> tuple[bool, str]:
    """项目改动后自动同步云端，返回可展示的结果。"""
    try:
        result = sync_everything_to_github()
        return True, "；".join(result.get("items", ["GitHub 云端同步完成"]))
    except Exception as exc:
        return False, f"本地已保存，但{format_sync_error(exc)}"


def set_workbench_notice(message: str, success: bool = True):
    st.session_state["workbench_notice"] = {"message": message, "success": success}


def mark_project_delivered(project_id: int):
    """确认已交付：标记为已交付。"""
    project = get_project(project_id)
    if not project:
        set_workbench_notice("项目不存在。", success=False)
        return

    data = dict(project)
    data["status"] = "已交付"
    data["remark"] = append_remark(data.get("remark", ""), "已确认交付。")
    update_project(project_id, data)

    set_workbench_notice(f"《{project['project_name']}》已确认交付。需要同步云端时，请点击左侧“同步 GitHub 云端”。")


def delay_project_delivery(project_id: int, delay_days: int):
    """确认未交付：按延期天数更新交付日期。"""
    project = get_project(project_id)
    if not project:
        set_workbench_notice("项目不存在。", success=False)
        return

    current_delivery = project_delivery_date(project)
    base_date = max(current_delivery, today_beijing())
    new_delivery_date = base_date + timedelta(days=delay_days)
    rebuild_project_to_delivery(project, new_delivery_date, status="延期")

    set_workbench_notice(
        f"《{project['project_name']}》已延期至 {new_delivery_date.strftime('%Y-%m-%d')}。需要同步云端时，请点击左侧“同步 GitHub 云端”。"
    )


def build_workbench_rows(include_delivered: bool = True) -> list[dict]:
    """生成项目工作台排序后的行数据。"""
    today = today_beijing()
    rows = []
    for project in list_projects(include_delivered=include_delivered):
        milestones = get_project_milestones(project["id"])
        delivery_info = project_delivery_info(project)
        delivery = delivery_info["delivery_date"]
        status = project.get("status") or "进行中"
        delta = (delivery - today).days
        if status == "已交付":
            priority = 4
        elif delta < 0:
            priority = 0
        elif delta == 0:
            priority = 1
        elif delta <= 3:
            priority = 2
        else:
            priority = 3

        rows.append(
            {
                **project,
                "delivery_date": delivery,
                "delivery_delta": delta,
                "delivery_badge": delivery_badge(delivery, today, status),
                "is_estimated_delivery": delivery_info["is_estimated_delivery"],
                "sort_priority": priority,
                "has_milestones": bool(milestones),
            }
        )
    return sorted(rows, key=lambda row: (row["sort_priority"], row["delivery_date"], row["project_name"]))


def show_workbench_notice():
    """展示项目操作后的结果提示。"""
    notice = st.session_state.pop("workbench_notice", None)
    if not notice:
        return
    if notice.get("success", True):
        st.success(notice.get("message", "操作完成。"))
    else:
        st.error(notice.get("message", "操作失败。"))


def project_day_number(start_date, today: date | None = None) -> int:
    """计算当前是项目第几天。"""
    today = today or today_beijing()
    return max(1, (today - parse_date_value(start_date)).days + 1)


def clean_cell(value, default: str = "") -> str:
    """把表格单元格转成干净文本。"""
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except TypeError:
        pass
    return str(value).strip()


def clean_int_cell(value, default: int = 30) -> int:
    """把表格单元格转成整数。"""
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except TypeError:
        pass
    return max(1, int(value))


def save_workbench_edits(edited_df: pd.DataFrame) -> tuple[int, int, int]:
    """保存项目工作台里的直接编辑，并在交付日期变化时重算节点。"""
    projects = {row["id"]: row for row in list_projects(include_delivered=True)}
    pending_updates = []

    for _, row in edited_df.iterrows():
        project_id = int(row["ID"])
        project = projects.get(project_id)
        if not project:
            continue

        project_name = clean_cell(row["项目名"])
        if not project_name:
            raise ValueError(f"ID {project_id} 的项目名不能为空。")

        status = clean_cell(row["当前状态"], "进行中")
        if status not in STATUS_OPTIONS:
            status = "进行中"

        project_level = clean_cell(row["项目等级"], "自定义")
        if project_level not in LEVEL_OPTIONS:
            project_level = "自定义"

        episodes = clean_int_cell(row["集数"], int(project.get("episodes") or 30))
        owner = clean_cell(row["负责人"])
        remark = clean_cell(row["备注"])
        new_delivery_date = parse_date_value(row["交付日期"])
        old_delivery_date = project_delivery_date(project)
        delivery_changed = new_delivery_date != old_delivery_date or not (project.get("delivery_date") or "").strip()

        data = dict(project)
        data.update(
            {
                "project_name": project_name,
                "episodes": episodes,
                "project_level": project_level,
                "delivery_date": new_delivery_date.strftime("%Y-%m-%d"),
                "owner": owner,
                "status": status,
                "remark": remark,
            }
        )

        basic_changed = any(
            str(data.get(key) or "") != str(project.get(key) or "")
            for key in ["project_name", "episodes", "project_level", "owner", "status", "remark"]
        )

        if not basic_changed and not delivery_changed:
            continue

        rebuilt_milestones = None
        if delivery_changed:
            data["remark"] = append_remark(
                data.get("remark", ""),
                f"交付日期调整为 {new_delivery_date.strftime('%Y-%m-%d')}。",
            )
        data["start_date"] = project["start_date"]

        newly_delivered = status == "已交付" and project.get("status") != "已交付"
        pending_updates.append((project_id, data, rebuilt_milestones, delivery_changed, newly_delivered))

    changed_count = 0
    rebuilt_count = 0
    delivered_count = 0
    for project_id, data, rebuilt_milestones, delivery_changed, delivered in pending_updates:
        update_project(project_id, data)
        if rebuilt_milestones is not None:
            replace_project_milestones(project_id, rebuilt_milestones)
        changed_count += 1
        rebuilt_count += int(delivery_changed)
        delivered_count += int(delivered)

    return changed_count, rebuilt_count, delivered_count


def show_delivery_confirmation_page():
    st.title("交付确认")
    show_workbench_notice()

    rows = [row for row in build_workbench_rows(include_delivered=False) if row["delivery_delta"] <= 0]
    if not rows:
        st.info("今天没有到期或超期的项目。")
        return

    today = today_beijing()
    for row in rows:
        delivery_text = row["delivery_date"].strftime("%Y-%m-%d")
        is_overdue = row["delivery_delta"] < 0
        tag = "超期" if is_overdue else "今天交付"
        date_label = delivery_label(row)
        countdown = f"已超{abs(row['delivery_delta'])}天" if is_overdue else "今天交付"
        day_number = project_day_number(row["start_date"], today=today)

        st.subheader(f"[{tag}]《{row['project_name']}》")
        st.write(f"{row['project_level']}D{day_number}｜{date_label}{delivery_text}｜{countdown}")
        if row.get("owner"):
            st.caption(f"负责人：{row['owner']}")

        action_col1, action_col2, action_col3 = st.columns([1, 1, 4])
        with action_col1:
            if st.button("已交付", type="primary", key=f"delivered_{row['id']}"):
                mark_project_delivered(row["id"])
                st.rerun()
        with action_col2:
            if st.button("未交付", key=f"undelivered_{row['id']}"):
                st.session_state["delay_project_id"] = row["id"]
                st.rerun()

        if st.session_state.get("delay_project_id") == row["id"]:
            with st.form(f"delay_form_{row['id']}"):
                delay_col1, delay_col2 = st.columns([1, 1])
                with delay_col1:
                    delay_choice = st.selectbox("延期天数", ["1天", "2天", "3天", "5天", "自定义"], key=f"delay_choice_{row['id']}")
                with delay_col2:
                    custom_days = st.number_input(
                        "自定义天数",
                        min_value=1,
                        max_value=60,
                        value=1,
                        step=1,
                        disabled=delay_choice != "自定义",
                        key=f"custom_delay_{row['id']}",
                    )
                delay_days = {"1天": 1, "2天": 2, "3天": 3, "5天": 5}.get(delay_choice, int(custom_days))
                if st.form_submit_button("确认延期并同步 GitHub", type="primary"):
                    delay_project_delivery(row["id"], delay_days)
                    st.session_state.pop("delay_project_id", None)
                    st.rerun()

        st.divider()


def parse_time_value(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def render_reminder_window_settings():
    """项目工作台里的每日发送区间设置。"""
    reminder_window = get_reminder_window()
    st.subheader("每日发送区间")
    st.caption("系统会在这个区间内自动生成 10 个检查点；任意一个检查点发送成功后，当天后面的检查点都会跳过。")

    with st.form("reminder_window_form"):
        col1, col2, col3 = st.columns([1, 1, 2])
        with col1:
            start_value = st.time_input(
                "开始时间",
                value=parse_time_value(reminder_window["start"]),
                step=300,
                key="reminder_window_start",
            )
        with col2:
            end_value = st.time_input(
                "结束时间",
                value=parse_time_value(reminder_window["end"]),
                step=300,
                key="reminder_window_end",
            )
        start_text = start_value.strftime("%H:%M")
        end_text = end_value.strftime("%H:%M")

        generated_times = []
        preview_error = ""
        try:
            generated_times = generate_reminder_check_times(start_text, end_text, DEFAULT_REMINDER_CHECK_COUNT)
        except Exception as exc:
            preview_error = str(exc)

        with col3:
            if generated_times:
                st.write("自动检查点：")
                st.caption(" / ".join(generated_times))
            else:
                st.warning(preview_error or "请设置有效时间区间。")

        save_col1, save_col2 = st.columns([1, 1])
        with save_col1:
            save_local = st.form_submit_button("保存发送区间", type="primary", use_container_width=True)
        with save_col2:
            save_and_sync = st.form_submit_button("保存并同步 GitHub 云端", use_container_width=True)

        if save_local or save_and_sync:
            try:
                saved_window = update_reminder_window(start_text, end_text, DEFAULT_REMINDER_CHECK_COUNT)
                if save_and_sync:
                    result = sync_everything_to_github()
                    st.success(f"已保存发送区间：{saved_window['start']} - {saved_window['end']}。")
                    render_sync_result(result)
                else:
                    st.success(
                        f"已保存发送区间：{saved_window['start']} - {saved_window['end']}。需要云端生效时，请点击左侧同步或这里的同步按钮。"
                    )
            except Exception as exc:
                st.error(f"保存失败：{exc}")


def show_project_workbench_page():
    st.title("项目工作台")
    show_workbench_notice()

    rows = build_workbench_rows(include_delivered=True)
    if not rows:
        st.info("还没有项目，请先用智能识别新增。")
        return

    active_rows = [row for row in rows if row.get("status") != "已交付"]
    render_kpi_grid(
        [
            ("全部项目", len(rows), None),
            ("进行中", len(active_rows), COLORS["normal"]),
            ("今天/超期", sum(1 for row in active_rows if row["delivery_delta"] <= 0), COLORS["delivery"]),
            ("已交付", sum(1 for row in rows if row.get("status") == "已交付"), COLORS["first"]),
        ]
    )
    render_reminder_window_settings()

    filter_col1, filter_col2, filter_col3 = st.columns([1, 1, 1])
    with filter_col1:
        status_filter = st.multiselect("状态", STATUS_OPTIONS, placeholder="全部状态")
    with filter_col2:
        level_filter = st.multiselect("等级", LEVEL_OPTIONS, placeholder="全部等级")
    with filter_col3:
        risk_filter = st.selectbox("交付风险", ["全部项目", "只看交付风险", "只看非风险"])

    filtered_rows = rows
    if status_filter:
        filtered_rows = [row for row in filtered_rows if (row.get("status") or "进行中") in status_filter]
    if level_filter:
        filtered_rows = [row for row in filtered_rows if (row.get("project_level") or "自定义") in level_filter]
    if risk_filter == "只看交付风险":
        filtered_rows = [
            row
            for row in filtered_rows
            if row.get("status") != "已交付" and not row.get("is_estimated_delivery") and row["delivery_delta"] <= 2
        ]
    elif risk_filter == "只看非风险":
        filtered_rows = [
            row
            for row in filtered_rows
            if row.get("status") == "已交付" or row.get("is_estimated_delivery") or row["delivery_delta"] > 2
        ]

    if not filtered_rows:
        render_empty_state("当前筛选条件下暂无项目")
        return

    table_rows = []
    for row in filtered_rows:
        table_rows.append(
            {
                "删除": False,
                "ID": row["id"],
                "交付状态": row["delivery_badge"],
                "交付类型": delivery_label(row),
                "项目名": row["project_name"],
                "交付日期": row["delivery_date"],
                "开始制作日期": parse_date_value(row["start_date"]),
                "集数": int(row.get("episodes") or 30),
                "项目等级": row.get("project_level") or "自定义",
                "负责人": row.get("owner") or "",
                "当前状态": row.get("status") or "进行中",
                "备注": row.get("remark") or "",
            }
        )

    edited_df = st.data_editor(
        pd.DataFrame(table_rows),
        use_container_width=True,
        hide_index=True,
        height=table_height(len(table_rows), min_height=360, max_height=760),
        num_rows="fixed",
        disabled=["ID", "交付状态", "交付类型", "开始制作日期"],
        column_order=[
            "删除",
            "项目名",
            "交付状态",
            "交付类型",
            "交付日期",
            "项目等级",
            "当前状态",
            "集数",
            "负责人",
            "开始制作日期",
            "备注",
            "ID",
        ],
        column_config={
            "删除": st.column_config.CheckboxColumn("删除", help="勾选后可在下方删除项目", width="small"),
            "ID": st.column_config.NumberColumn("ID", width="small"),
            "交付状态": st.column_config.TextColumn("交付状态", width="small"),
            "交付类型": st.column_config.TextColumn("交付类型", width="small"),
            "项目名": st.column_config.TextColumn("项目名", width="large", required=True),
            "交付日期": st.column_config.DateColumn("交付日期", format="YYYY-MM-DD", required=True),
            "开始制作日期": st.column_config.DateColumn("开始制作日期", format="YYYY-MM-DD"),
            "集数": st.column_config.NumberColumn("集数", min_value=1, max_value=300, step=1, width="small"),
            "项目等级": st.column_config.SelectboxColumn("项目等级", options=LEVEL_OPTIONS, width="small"),
            "负责人": st.column_config.TextColumn("负责人", width="medium"),
            "当前状态": st.column_config.SelectboxColumn("当前状态", options=STATUS_OPTIONS, width="small"),
            "备注": st.column_config.TextColumn("备注", width="large"),
        },
    )

    filtered_ids = {item["id"] for item in filtered_rows}
    selected_for_delete = [
        row for _, row in edited_df.iterrows() if bool(row.get("删除")) and int(row["ID"]) in filtered_ids
    ]

    action_col1, action_col2, action_col3 = st.columns([1.2, 1.4, 1])
    with action_col1:
        if st.button("保存工作台修改", type="primary", use_container_width=True):
            try:
                changed_count, rebuilt_count, delivered_count = save_workbench_edits(edited_df)
                if changed_count == 0:
                    set_workbench_notice("没有检测到需要保存的修改。")
                else:
                    message = f"已保存 {changed_count} 个项目；更新交付日期 {rebuilt_count} 个；移出提醒 {delivered_count} 个。需要同步云端时，请点击左侧“同步 GitHub 云端”。"
                    set_workbench_notice(message)
                st.rerun()
            except Exception as exc:
                st.error(f"保存失败：{exc}")

    with action_col2:
        delete_confirm = st.checkbox(
            f"确认删除已勾选的 {len(selected_for_delete)} 个项目",
            disabled=not selected_for_delete,
            key="workbench_inline_delete_confirm",
        )

    with action_col3:
        if st.button(
            "删除勾选项目",
            disabled=not selected_for_delete or not delete_confirm,
            use_container_width=True,
        ):
            deleted_names = []
            for row in selected_for_delete:
                delete_project(int(row["ID"]))
                deleted_names.append(clean_cell(row["项目名"]))
            set_workbench_notice(
                f"已删除 {len(deleted_names)} 个项目。需要同步云端时，请点击左侧“同步 GitHub 云端”。"
            )
            st.rerun()


def project_name_exists(project_name: str) -> bool:
    """检查项目名是否已存在，避免重复创建同一个项目。"""
    clean_name = project_name.strip()
    return any(row["project_name"].strip() == clean_name for row in list_projects(include_delivered=True))


def handle_smart_add():
    """导入粘贴识别结果的按钮回调：保存成功后清空粘贴框。"""
    text = st.session_state.get("smart_schedule_text", "")
    year = int(st.session_state.get("smart_schedule_year", today_beijing().year))

    try:
        parsed = parse_chinese_schedule_text(text, year=year)
        if project_name_exists(parsed["project_name"]):
            st.session_state["smart_add_error"] = f"项目“{parsed['project_name']}”已存在，已阻止重复新建。"
            return

        add_project(parsed)
        st.session_state["smart_add_success"] = f"项目“{parsed['project_name']}”已新建成功。"
        st.session_state["smart_schedule_text"] = ""
    except Exception as exc:
        st.session_state["smart_add_error"] = f"新建失败：{exc}"


def build_quick_schedule_text(project_name: str, project_level: str, delivery_date: date) -> str:
    """把表单选择项转成智能识别可复用的排期文本。"""
    return (
        f"项目：{project_name.strip()}\n"
        f"等级：{project_level}\n"
        f"交付时间：{delivery_date.month}月{delivery_date.day}日"
    )


def save_quick_project(project_name: str, project_level: str, delivery_date: date):
    """用项目名、等级、交付日期快速创建项目。"""
    if not project_name.strip():
        st.error("请填写项目名。")
        return

    try:
        parsed = parse_chinese_schedule_text(
            build_quick_schedule_text(project_name, project_level, delivery_date),
            year=delivery_date.year,
        )
        if project_name_exists(parsed["project_name"]):
            st.error(f"项目“{parsed['project_name']}”已存在，不能重复新建。")
            return

        add_project(parsed)
        st.success(f"项目“{parsed['project_name']}”已新建成功，节点已按 {project_level} 倒推。")
    except Exception as exc:
        st.error(f"新建失败：{exc}")


def show_smart_add_page():
    st.title("新增项目")
    st.write("填写项目名，选择等级和交付日期即可新建。")

    if st.session_state.get("smart_add_success"):
        message = st.session_state.pop("smart_add_success")
        st.toast(message)
        st.success(message)
    if st.session_state.get("smart_add_error"):
        st.error(st.session_state.pop("smart_add_error"))

    with st.form("quick_project_form"):
        st.subheader("新建项目")
        quick_col1, quick_col2, quick_col3 = st.columns([2, 1, 1])
        with quick_col1:
            quick_project_name = st.text_input("项目名", key="quick_project_name")
        with quick_col2:
            quick_project_level = st.selectbox("项目等级", ["S级", "A级", "B级"], key="quick_project_level")
        with quick_col3:
            quick_delivery_date = st.date_input("交付日期", value=today_beijing(), key="quick_delivery_date")

        quick_submit = st.form_submit_button("新建项目", type="primary")
        if quick_submit:
            save_quick_project(quick_project_name, quick_project_level, quick_delivery_date)


def show_edit_page():
    st.title("编辑项目")
    rows = list_projects(include_delivered=True)
    if not rows:
        st.info("暂无可编辑项目。")
        return

    options = {f"{row['id']} - {row['project_name']}": row["id"] for row in rows}
    selected = st.selectbox("选择项目", list(options.keys()))
    project_id = options[selected]
    project = get_project(project_id)
    data = project_form(project)
    milestones = get_project_milestones(project_id)

    if st.button("保存修改", type="primary"):
        if not data["project_name"]:
            st.error("请填写项目名。")
            return
        update_project(project_id, data)
        st.success("项目已更新。")
        st.rerun()

    st.divider()
    st.subheader("节点排期")
    if not milestones:
        st.info("这个项目没有结构化节点排期。可以用“智能识别新增”创建带节点的项目。")
        return

    milestone_df = pd.DataFrame(
        [
            {
                "节点": item["name"],
                "耗时": f"{item['duration']} 天",
                "节点日期": item["due_date"],
            }
            for item in milestones
        ]
    )
    st.dataframe(milestone_df, use_container_width=True, hide_index=True)

    current_delivery_date = project_delivery_date(project)
    new_delivery_date = st.date_input("修改交付日期", value=current_delivery_date)
    st.caption("保存后会更新项目正式交付日期，首页提醒和飞书推送会同步使用新日期。")

    if st.button("保存交付日期", type="primary"):
        data["delivery_date"] = new_delivery_date.strftime("%Y-%m-%d")
        remark_lines = [
            f"交付日期调整为：{new_delivery_date.strftime('%Y-%m-%d')}",
        ]
        data["remark"] = "\n".join(remark_lines)
        update_project(project_id, data)
        st.success("交付日期已同步更新。")
        st.rerun()


def show_delete_page():
    st.title("删除项目")
    rows = list_projects(include_delivered=True)
    if not rows:
        st.info("暂无可删除项目。")
        return

    options = {f"{row['id']} - {row['project_name']}": row["id"] for row in rows}
    selected = st.selectbox("选择要删除的项目", list(options.keys()))
    confirm = st.checkbox("我确认要删除这个项目")
    if st.button("删除项目", type="primary", disabled=not confirm):
        delete_project(options[selected])
        st.success("项目已删除。")
        st.rerun()


def send_custom_feishu_message(text: str) -> dict:
    """发送用户临时编辑后的飞书文案，并记录日志。"""
    load_dotenv()
    webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "")
    secret = os.getenv("FEISHU_SECRET", "")
    message = text.strip()
    if not message:
        raise ValueError("推送文案不能为空。")

    try:
        result = send_feishu_message(webhook_url, secret, message)
        add_notification_log("manual_custom", "success", message=message)
        return result
    except Exception as exc:
        add_notification_log("manual_custom", "failed", message=message, error=str(exc))
        raise


def render_feishu_template_editor():
    st.subheader("每日推送模板")
    st.caption("这里改的是每天自动生成飞书消息的模板；保存后，本地自动推送和 GitHub 云端推送都会按这个模板生成。")

    if "feishu_message_template_draft" not in st.session_state:
        st.session_state["feishu_message_template_draft"] = load_message_template()

    template_col, preview_col = st.columns([3, 2])
    with template_col:
        template_text = st.text_area(
            "模板内容",
            key="feishu_message_template_draft",
            height=260,
            help="可用占位符：{date} 日期、{summary} 总览、{separator} 分割线、{items} 项目明细、{title} 标题。",
        )
        action_col1, action_col2, action_col3 = st.columns(3)
        with action_col1:
            if st.button("保存每日模板", type="primary", use_container_width=True):
                try:
                    save_message_template(template_text)
                    st.success("每日推送模板已保存。云端推送需要点击左侧“同步 GitHub 云端”后生效。")
                except Exception as exc:
                    st.error(f"保存失败：{exc}")
        with action_col2:
            if st.button("保存并同步云端", use_container_width=True):
                try:
                    save_message_template(template_text)
                    result = sync_everything_to_github()
                    st.success("每日推送模板已保存。")
                    render_sync_result(result)
                except Exception as exc:
                    st.error(format_sync_error(exc))
        with action_col3:
            if st.button("恢复默认模板", use_container_width=True):
                reset_message_template()
                st.session_state["feishu_message_template_draft"] = DEFAULT_MESSAGE_TEMPLATE
                st.rerun()

    with preview_col:
        st.markdown("**模板预览**")
        try:
            preview = build_feishu_message(today=today_beijing(), template_text=template_text)
            st.text_area("按当前模板生成的今日消息", value=preview, height=260, disabled=True)
        except Exception as exc:
            st.warning(f"模板暂时无法预览：{exc}")


def render_custom_feishu_editor(webhook_url: str):
    st.subheader("编辑并发送今日推送")
    st.caption("这里是临时手改发送，不会修改项目数据，也不会影响自动推送规则。")

    default_message = build_feishu_message(today=today_beijing())
    if st.session_state.get("custom_feishu_base_date") != today_beijing().strftime("%Y-%m-%d"):
        st.session_state["custom_feishu_message"] = default_message
        st.session_state["custom_feishu_base_date"] = today_beijing().strftime("%Y-%m-%d")

    edit_col, preview_col = st.columns([3, 2])
    with edit_col:
        custom_message = st.text_area(
            "今日推送文案",
            key="custom_feishu_message",
            height=420,
            help="可以直接删改文字。只影响这次手动发送，不会保存为规则。",
        )
    with preview_col:
        st.markdown("**发送前检查**")
        st.write("当前字数：", len(custom_message.strip()))
        st.write("飞书机器人：", "已配置" if webhook_url else "未配置")
        st.info("建议只在临时强调、删减项目、补充口径时手改；项目日期和节点仍回到项目工作台维护。")
        if st.button("恢复自动生成文案", use_container_width=True):
            st.session_state["custom_feishu_message"] = default_message
            st.rerun()

    confirm_send = st.checkbox("我确认发送上面这版文案到飞书", key="confirm_custom_feishu_send")
    send_disabled = (not webhook_url) or (not custom_message.strip()) or (not confirm_send)
    if st.button("发送这版文案到飞书", type="primary", disabled=send_disabled):
        try:
            result = send_custom_feishu_message(custom_message)
            st.success(f"已发送这版文案：{result}")
        except Exception as exc:
            st.error(f"发送失败：{exc}")


def show_feishu_page():
    st.title("飞书推送")
    load_dotenv()
    webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "")
    secret = os.getenv("FEISHU_SECRET", "")
    reminder_times = get_reminder_times(os.getenv("REMINDER_TIMES", os.getenv("REMINDER_TIME", DEFAULT_REMINDER_TIMES)))

    st.write("Webhook URL：", "已配置" if webhook_url else "未配置")
    st.write("Secret：", "已配置" if secret else "未配置，可用于未开启签名的机器人")
    st.write("今日自动推送：", "已成功" if has_successful_auto_log() else "未看到成功记录")

    with st.expander("每日发送模板编辑", expanded=True):
        render_feishu_template_editor()

    with st.expander("编辑并发送今日推送", expanded=True):
        render_custom_feishu_editor(webhook_url)

    st.divider()
    st.subheader("本地推送时间")
    st.caption("每行一个时间。本地后台会按这些时间推送；点左侧同步后，GitHub 云端也会使用同一组时间。")
    new_times_text = st.text_area(
        "每日推送时间",
        value="\n".join(reminder_times),
        height=112,
        help="格式：HH:MM，每行一个，例如 09:57",
    )
    col1, col2 = st.columns(2)
    with col1:
        if st.button("保存本地推送时间", type="primary"):
            try:
                saved_times = update_reminder_times(new_times_text)
                st.success(f"推送时间已保存：{', '.join(saved_times)}。当前进程内的定时任务也已刷新。")
            except Exception as exc:
                st.error(f"保存失败：{exc}")
    with col2:
        if st.button("发送测试消息到飞书"):
            try:
                result = send_test_message()
                st.success(f"测试消息已发送：{result}")
            except Exception as exc:
                st.error(f"发送失败：{exc}")

    add_col1, add_col2 = st.columns([1, 2])
    with add_col1:
        added_time = st.time_input("新增推送时间", value=time(18, 0), step=300)
    with add_col2:
        st.write("")
        st.write("")
        if st.button("新增到本地时间列表"):
            try:
                added_value = added_time.strftime("%H:%M")
                saved_times = update_reminder_times([*reminder_times, added_value])
                st.success(f"已新增：{added_value}。当前推送时间：{', '.join(saved_times)}")
                st.rerun()
            except Exception as exc:
                st.error(f"新增失败：{exc}")

    st.divider()
    if st.button("同步这些设置到 GitHub 云端", type="primary"):
        try:
            saved_times = update_reminder_times(new_times_text)
            result = sync_everything_to_github()
            st.success(f"本地时间已保存：{', '.join(saved_times)}。")
            render_sync_result(result)
        except Exception as exc:
            st.error(format_sync_error(exc))

    st.divider()
    if st.button("立即发送今日提醒"):
        try:
            result = send_daily_reminder(send_type="manual")
            st.success(f"今日提醒已发送：{result}")
        except Exception as exc:
            st.error(f"发送失败：{exc}")

    st.divider()
    st.subheader("最近推送日志")
    logs = list_notification_logs(limit=20)
    if not logs:
        st.info("暂无推送日志。")
    else:
        log_df = pd.DataFrame(logs).rename(
            columns={
                "send_date": "日期",
                "send_type": "类型",
                "status": "结果",
                "error": "错误",
                "created_at": "时间",
            }
        )
        display_cols = ["时间", "日期", "类型", "结果", "错误"]
        st.dataframe(log_df[display_cols], use_container_width=True, hide_index=True)


def render_excel_import_section():
    st.subheader("Excel 导入")
    st.write("支持列名：项目名、开始制作日期、集数、项目等级、负责人、当前状态、备注。")
    file = st.file_uploader("上传 Excel 文件", type=["xlsx", "xls"], key="data_import_file")
    if file and st.button("开始导入", type="primary", key="data_import_submit"):
        try:
            count = import_projects_from_excel(file)
            st.success(f"导入完成，共新增 {count} 个项目。")
            st.rerun()
        except Exception as exc:
            st.error(f"导入失败：{exc}")


def render_excel_export_section():
    st.subheader("Excel 导出")
    df = projects_to_dataframe()
    display_df = df.copy()
    if "ID" in display_df.columns:
        display_df = display_df.drop(columns=["ID"])
    if not display_df.empty:
        display_df.insert(0, "序号", range(1, len(display_df) + 1))
    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        height=table_height(len(display_df)),
        column_config={
            "序号": st.column_config.NumberColumn("序号", width="small"),
            "项目名": st.column_config.TextColumn("项目名", width="large"),
            "备注": st.column_config.TextColumn("备注", width="large"),
        },
    )

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="项目列表")
    output.seek(0)

    st.download_button(
        "下载 Excel",
        data=output,
        file_name=f"短剧SOP项目列表_{today_beijing().strftime('%Y%m%d')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="data_export_download",
    )


def render_cloud_sync_section():
    st.subheader("GitHub 云端同步")
    st.write("本地项目、推送时间有修改后，点击这里同步到 GitHub。云端每日推送会读取同步后的数据和时间。")
    if st.button("生成 GitHub Actions 数据文件", type="primary", key="data_generate_actions"):
        try:
            export_actions_data()
            st.success("已生成 data/projects_for_actions.json。提交并推送到 GitHub 后，云端定时推送会使用最新数据。")
        except Exception as exc:
            st.error(f"生成失败：{exc}")

    if st.button("同步全部到 GitHub 云端", key="data_sync_all"):
        try:
            result = sync_everything_to_github()
            render_sync_result(result)
        except Exception as exc:
            st.error(format_sync_error(exc))


def show_data_management_page():
    st.title("数据导入导出")
    total_projects = len(list_projects(include_delivered=True))
    active_projects = len(list_projects(include_delivered=False))
    reminder_times = get_reminder_times(os.getenv("REMINDER_TIMES", os.getenv("REMINDER_TIME", DEFAULT_REMINDER_TIMES)))

    metric_cols = st.columns(3)
    metric_cols[0].metric("全部项目", f"{total_projects} 个")
    metric_cols[1].metric("提醒中", f"{active_projects} 个")
    metric_cols[2].metric("每日推送", " / ".join(reminder_times))

    import_tab, export_tab, sync_tab = st.tabs(["导入 Excel", "导出 Excel", "云端同步"])
    with import_tab:
        render_excel_import_section()
    with export_tab:
        render_excel_export_section()
    with sync_tab:
        render_cloud_sync_section()


def show_import_page():
    st.title("Excel 导入")
    render_excel_import_section()


def show_export_page():
    st.title("Excel 导出")
    render_excel_export_section()
    st.divider()
    render_cloud_sync_section()


def show_system_settings_page():
    st.title("系统设置")
    st.info("系统设置暂时保持轻量：项目、推送时间和云端同步入口仍在对应页面管理，避免误改业务规则。")

    reminder_times = get_reminder_times(os.getenv("REMINDER_TIMES", os.getenv("REMINDER_TIME", DEFAULT_REMINDER_TIMES)))
    st.write("当前本地推送时间：", " / ".join(reminder_times))
    st.write("云端同步：请使用左侧醒目的“同步 GitHub 云端”按钮，或在“数据导入导出”页面执行。")


def main():
    bootstrap_app()
    inject_custom_css()

    st.sidebar.markdown('<div class="sop-sidebar-brand">短剧SOP</div>', unsafe_allow_html=True)
    if st.sidebar.button("同步 GitHub 云端", type="primary", use_container_width=True):
        try:
            result = sync_everything_to_github()
            render_sync_result(result, sidebar=True)
        except Exception as exc:
            st.sidebar.error(format_sync_error(exc))
    st.sidebar.markdown(
        '<div class="sop-sidebar-note">本地项目、推送时间、云端定时一起同步。</div>',
        unsafe_allow_html=True,
    )
    render_auto_push_control()
    st.sidebar.divider()

    page = st.sidebar.radio(
        "后台页面",
        [
            "今日看板",
            "项目工作台",
            "新增项目",
            "交付确认",
        ],
    )

    if page == "今日看板":
        show_today_page()
    elif page == "项目工作台":
        show_project_workbench_page()
    elif page == "新增项目":
        show_smart_add_page()
    elif page == "交付确认":
        show_delivery_confirmation_page()


if __name__ == "__main__":
    main()
