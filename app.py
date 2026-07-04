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

from database import (
    add_project,
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
from project_parser import parse_chinese_schedule_text
from scheduler import (
    build_feishu_message,
    build_today_rows,
    get_reminder_times,
    send_daily_reminder,
    send_test_message,
    start_scheduler,
    update_reminder_times,
)
from scripts.export_actions_data import export_actions_data


STATUS_OPTIONS = ["进行中", "已交付", "延期", "暂停"]
LEVEL_OPTIONS = ["自定义", "S级", "A级", "B级"]
GITHUB_REPOSITORY = "1321769104lp/sop"
WORKFLOW_PATH = ".github/workflows/daily-feishu-reminder.yml"


def find_git_executable() -> str:
    """寻找可用的 Git。"""
    bundled_git = r"C:\Users\zy-user\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe"
    return shutil.which("git") or bundled_git


def find_gh_executable() -> str:
    """寻找 GitHub CLI。"""
    local_gh = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tools",
        "gh",
        "bin",
        "gh.exe",
    )
    return shutil.which("gh") or local_gh


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

    commit_message = f"Update actions project data {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
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
    local_dt = datetime.combine(date.today(), time(int(hour_text), int(minute_text)))
    utc_dt = local_dt - timedelta(hours=8)
    return utc_dt.minute, utc_dt.hour


def build_workflow_content(reminder_times: list[str]) -> str:
    """根据本地推送时间生成 GitHub Actions 定时文件。"""
    schedule_lines = []
    for reminder_time in reminder_times:
        minute, hour = beijing_time_to_utc_cron(reminder_time)
        schedule_lines.extend(
            [
                f"    # GitHub Actions uses UTC. Beijing time {reminder_time} = UTC {hour:02d}:{minute:02d}.",
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
            "        run: python scripts/send_github_reminder.py",
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
    if sha_result.returncode != 0:
        raise RuntimeError(sha_result.stderr or sha_result.stdout)

    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "sha": sha_result.stdout.strip(),
        "branch": "main",
    }
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


def sync_everything_to_github() -> str:
    """同步本地项目数据和本地推送时间到 GitHub 云端。"""
    export_actions_data()
    reminder_times = get_reminder_times(os.getenv("REMINDER_TIMES", os.getenv("REMINDER_TIME", "09:57,16:00")))
    workflow_content = build_workflow_content(reminder_times)

    workflow_full_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), WORKFLOW_PATH)
    with open(workflow_full_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(workflow_content)

    data_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "projects_for_actions.json")
    with open(data_path, "r", encoding="utf-8") as handle:
        data_content = handle.read()

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

    return (
        "已同步到 GitHub 云端：项目数据、推送时间和 GitHub Actions 定时配置都已更新。\n"
        f"项目数据提交：{data_sha[:7]}；推送时间提交：{workflow_sha[:7]}。"
    )


def table_height(row_count: int, min_height: int = 260, max_height: int = 720) -> int:
    """根据行数给表格一个尽量够看的高度。"""
    return min(max_height, max(min_height, 90 + row_count * 44))


st.set_page_config(
    page_title="短剧SOP飞书提醒系统",
    page_icon="🎬",
    layout="wide",
)


@st.cache_resource
def bootstrap_app():
    """初始化数据库和后台定时任务。"""
    load_dotenv()
    init_db(default_reminder_time=os.getenv("REMINDER_TIMES", os.getenv("REMINDER_TIME", "09:57,16:00")))
    seed_sample_data()
    return start_scheduler()


def project_form(defaults: dict | None = None) -> dict:
    """项目表单，新增和编辑共用。"""
    defaults = defaults or {}
    start_value = defaults.get("start_date") or date.today().strftime("%Y-%m-%d")
    if isinstance(start_value, str):
        start_value = datetime.strptime(start_value, "%Y-%m-%d").date()

    col1, col2 = st.columns(2)
    with col1:
        project_name = st.text_input("项目名", value=defaults.get("project_name", ""))
        start_date = st.date_input("开始制作日期", value=start_value)
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
        "episodes": episodes,
        "project_level": project_level,
        "owner": owner.strip(),
        "status": status,
        "remark": remark.strip(),
    }


def show_today_page():
    st.title("今日项目推进表")
    rows = build_today_rows(today=date.today())

    total = len(rows)
    overdue = sum(1 for row in rows if row["stage"] == "超期")
    delivery = sum(1 for row in rows if row.get("is_delivery"))
    first_episode = sum(1 for row in rows if row.get("is_first_episode"))

    metric_cols = st.columns(4)
    metric_cols[0].metric("进行中项目", f"{total} 个")
    metric_cols[1].metric("超期项目", f"{overdue} 个")
    metric_cols[2].metric("交付节点", f"{delivery} 个")
    metric_cols[3].metric("首集关键节点", f"{first_episode} 个")

    if not rows:
        st.info("今日暂无需要提醒的项目。")
        return

    df = pd.DataFrame(rows)
    df = df.rename(
        columns={
            "project_name": "原项目名",
            "display_name": "项目名",
            "start_date": "开始日期",
            "delivery_countdown": "交付倒计时",
            "stage": "当前阶段",
            "today_focus": "今天重点",
            "risk_brief": "风险提醒",
            "status": "状态",
            "remark": "备注",
            "priority_label": "重点",
        }
    )
    df.insert(0, "序号", range(1, len(df) + 1))
    display_cols = ["序号", "重点", "项目名", "交付倒计时", "当前阶段", "今天重点", "风险提醒", "状态"]
    st.dataframe(
        df[display_cols],
        use_container_width=True,
        hide_index=True,
        height=table_height(len(df)),
        column_config={
            "序号": st.column_config.NumberColumn("序号", width="small"),
            "重点": st.column_config.TextColumn("重点", width="small"),
            "项目名": st.column_config.TextColumn("项目名", width="large"),
            "今天重点": st.column_config.TextColumn("今天重点", width="large"),
            "风险提醒": st.column_config.TextColumn("风险提醒", width="large"),
        },
    )

    with st.expander("查看今日飞书推送预览"):
        st.text(build_feishu_message(today=date.today()))


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
    display_cols = ["序号", "项目名", "开始制作日期", "集数", "项目等级", "负责人", "当前状态", "备注", "创建时间", "更新时间"]
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


def project_delivery_date(project: dict, milestones: list[dict]) -> date:
    """读取项目交付日期；没有结构化节点时用旧 10 天 SOP 兜底。"""
    if milestones:
        return parse_date_value(milestones[-1]["due_date"])
    return parse_date_value(project["start_date"]) + timedelta(days=9)


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
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    new_line = f"{timestamp} {line}"
    return f"{existing}\n{new_line}" if existing else new_line


def rebuild_project_to_delivery(project: dict, new_delivery_date: date, status: str | None = None) -> dict:
    """更新项目交付日期，并同步重算节点。"""
    milestones = get_project_milestones(project["id"])
    data = dict(project)
    if status:
        data["status"] = status

    if milestones:
        start_date, rebuilt_milestones = rebuild_milestones_by_delivery_date(milestones, new_delivery_date)
        data["start_date"] = start_date
        data["remark"] = append_remark(data.get("remark", ""), f"交付日期调整为 {new_delivery_date.strftime('%Y-%m-%d')}，节点已自动重算。")
        update_project(project["id"], data)
        replace_project_milestones(project["id"], rebuilt_milestones)
    else:
        data["start_date"] = (new_delivery_date - timedelta(days=9)).strftime("%Y-%m-%d")
        data["remark"] = append_remark(data.get("remark", ""), f"交付日期调整为 {new_delivery_date.strftime('%Y-%m-%d')}。")
        update_project(project["id"], data)

    return data


def sync_after_project_change() -> tuple[bool, str]:
    """项目改动后自动同步云端，返回可展示的结果。"""
    try:
        return True, sync_everything_to_github()
    except Exception as exc:
        return False, f"本地已保存，但同步 GitHub 云端失败：{exc}"


def set_workbench_notice(message: str, success: bool = True):
    st.session_state["workbench_notice"] = {"message": message, "success": success}


def mark_project_delivered(project_id: int):
    """确认已交付：标记为已交付并同步云端。"""
    project = get_project(project_id)
    if not project:
        set_workbench_notice("项目不存在。", success=False)
        return

    data = dict(project)
    data["status"] = "已交付"
    data["remark"] = append_remark(data.get("remark", ""), "已确认交付。")
    update_project(project_id, data)

    ok, sync_message = sync_after_project_change()
    set_workbench_notice(f"《{project['project_name']}》已确认交付。\n{sync_message}", success=ok)


def delay_project_delivery(project_id: int, delay_days: int):
    """确认未交付：按延期天数重算交付日期和节点，并同步云端。"""
    project = get_project(project_id)
    if not project:
        set_workbench_notice("项目不存在。", success=False)
        return

    milestones = get_project_milestones(project_id)
    current_delivery = project_delivery_date(project, milestones)
    base_date = max(current_delivery, date.today())
    new_delivery_date = base_date + timedelta(days=delay_days)
    rebuild_project_to_delivery(project, new_delivery_date, status="延期")

    ok, sync_message = sync_after_project_change()
    set_workbench_notice(
        f"《{project['project_name']}》已延期至 {new_delivery_date.strftime('%Y-%m-%d')}，节点已重算。\n{sync_message}",
        success=ok,
    )


def build_workbench_rows(include_delivered: bool = True) -> list[dict]:
    """生成项目工作台排序后的行数据。"""
    today = date.today()
    rows = []
    for project in list_projects(include_delivered=include_delivered):
        milestones = get_project_milestones(project["id"])
        delivery = project_delivery_date(project, milestones)
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
    today = today or date.today()
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
        milestones = get_project_milestones(project_id)
        old_delivery_date = project_delivery_date(project, milestones)
        delivery_changed = new_delivery_date != old_delivery_date

        data = dict(project)
        data.update(
            {
                "project_name": project_name,
                "episodes": episodes,
                "project_level": project_level,
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
            if milestones:
                start_date, rebuilt_milestones = rebuild_milestones_by_delivery_date(milestones, new_delivery_date)
                data["start_date"] = start_date
            else:
                data["start_date"] = (new_delivery_date - timedelta(days=9)).strftime("%Y-%m-%d")
            data["remark"] = append_remark(
                data.get("remark", ""),
                f"交付日期调整为 {new_delivery_date.strftime('%Y-%m-%d')}，节点已自动重算。",
            )
        else:
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

    today = date.today()
    for row in rows:
        delivery_text = row["delivery_date"].strftime("%Y-%m-%d")
        is_overdue = row["delivery_delta"] < 0
        tag = "超期" if is_overdue else "今天交付"
        date_label = "应交" if is_overdue else "交付"
        countdown = f"已超{abs(row['delivery_delta'])}天" if is_overdue else "今天到期"
        day_number = project_day_number(row["start_date"], today=today)

        st.subheader(f"[{tag}]《{row['project_name']}》")
        st.write(f"{row['project_level']}第{day_number}天｜{date_label}{delivery_text}｜{countdown}")
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


def show_project_workbench_page():
    st.title("项目工作台")
    show_workbench_notice()

    rows = build_workbench_rows(include_delivered=True)
    if not rows:
        st.info("还没有项目，请先用智能识别新增。")
        return

    active_rows = [row for row in rows if row.get("status") != "已交付"]
    metric_cols = st.columns(4)
    metric_cols[0].metric("全部项目", f"{len(rows)} 个")
    metric_cols[1].metric("进行中", f"{len(active_rows)} 个")
    metric_cols[2].metric("今天/超期", f"{sum(1 for row in active_rows if row['delivery_delta'] <= 0)} 个")
    metric_cols[3].metric("已交付", f"{sum(1 for row in rows if row.get('status') == '已交付')} 个")

    table_rows = []
    for row in rows:
        table_rows.append(
            {
                "ID": row["id"],
                "交付状态": row["delivery_badge"],
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
        disabled=["ID", "交付状态", "开始制作日期"],
        column_config={
            "ID": st.column_config.NumberColumn("ID", width="small"),
            "交付状态": st.column_config.TextColumn("交付状态", width="small"),
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

    if st.button("保存工作台修改并同步 GitHub 云端", type="primary"):
        try:
            changed_count, rebuilt_count, delivered_count = save_workbench_edits(edited_df)
            if changed_count == 0:
                set_workbench_notice("没有检测到需要保存的修改。")
            else:
                ok, sync_message = sync_after_project_change()
                message = f"已保存 {changed_count} 个项目；重算节点 {rebuilt_count} 个；移出提醒 {delivered_count} 个。\n{sync_message}"
                set_workbench_notice(message, success=ok)
            st.rerun()
        except Exception as exc:
            st.error(f"保存失败：{exc}")


def project_name_exists(project_name: str) -> bool:
    """检查项目名是否已存在，避免重复创建同一个项目。"""
    clean_name = project_name.strip()
    return any(row["project_name"].strip() == clean_name for row in list_projects(include_delivered=True))


def handle_smart_add():
    """智能识别新增的按钮回调：保存成功后清空粘贴框。"""
    text = st.session_state.get("smart_schedule_text", "")
    year = int(st.session_state.get("smart_schedule_year", date.today().year))

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
    st.title("智能识别新增")
    st.write("项目名手动填写，等级和交付时间直接选择；如果已有完整排期，也可以继续粘贴识别。")

    if st.session_state.get("smart_add_success"):
        message = st.session_state.pop("smart_add_success")
        st.toast(message)
        st.success(message)
    if st.session_state.get("smart_add_error"):
        st.error(st.session_state.pop("smart_add_error"))

    if "smart_schedule_text" not in st.session_state:
        st.session_state["smart_schedule_text"] = ""

    text = st.text_area(
        "粘贴排期文本",
        key="smart_schedule_text",
        height=128,
        placeholder=(
            "项目：我的妈咪是冰雪女王：My Mom Is the Ice Queen\n"
            "等级：S级\n"
            "交付时间：7月14日\n\n"
            "也支持完整节点：\n"
            "资产确定（3天）6月19日\n"
            "首集制作与修改（2天）6月21日\n"
            "一卡前制作（5天）6月26日\n"
            "全集制作（16天）7月12日\n"
            "终审与交付（2天）7月14日"
        ),
    )

    with st.form("quick_project_form"):
        st.subheader("选择新增")
        quick_col1, quick_col2, quick_col3 = st.columns([2, 1, 1])
        with quick_col1:
            quick_project_name = st.text_input("项目名", key="quick_project_name")
        with quick_col2:
            quick_project_level = st.selectbox("项目等级", ["S级", "A级", "B级"], key="quick_project_level")
        with quick_col3:
            quick_delivery_date = st.date_input("交付日期", value=date.today(), key="quick_delivery_date")

        quick_submit = st.form_submit_button("按选择新建项目", type="primary")
        if quick_submit:
            save_quick_project(quick_project_name, quick_project_level, quick_delivery_date)

    year = st.number_input(
        "年份",
        min_value=2020,
        max_value=2100,
        value=date.today().year,
        key="smart_schedule_year",
    )

    parsed = None
    duplicate = False
    if text.strip():
        try:
            parsed = parse_chinese_schedule_text(text, year=int(year))
            duplicate = project_name_exists(parsed["project_name"])
            if duplicate:
                st.warning(f"项目“{parsed['project_name']}”已存在，不能重复新建。")
            else:
                st.success("已识别成功，请确认后保存。")
            milestones = parsed.get("milestones", [])
            delivery_date = milestones[-1]["due_date"] if milestones else ""
            col1, col2, col3 = st.columns(3)
            col1.metric("开始制作日期", parsed["start_date"])
            col2.metric("交付节点", delivery_date)
            col3.metric("节点数量", f"{len(milestones)} 个")
            st.write(f"项目名：{parsed['project_name']}")
            if milestones:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "节点": item["name"],
                                "周期": f"{item['duration']} 天",
                                "节点日期": item["due_date"],
                            }
                            for item in milestones
                        ]
                    ),
                    use_container_width=True,
                    hide_index=True,
                )
            st.caption(
                f"默认：集数 {parsed['episodes']} / 等级 {parsed['project_level']} / 状态 {parsed['status']}"
            )
        except Exception as exc:
            st.warning(f"暂时无法识别：{exc}")

    st.button(
        "确认新建项目",
        type="primary",
        disabled=parsed is None or duplicate,
        on_click=handle_smart_add,
    )


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

    current_delivery_date = datetime.strptime(milestones[-1]["due_date"], "%Y-%m-%d").date()
    new_delivery_date = st.date_input("修改交付日期", value=current_delivery_date)
    st.caption("保存后会按当前各阶段耗时重新倒推全部节点，首页提醒和飞书推送会同步使用新日期。")

    if st.button("按新交付日期重算节点并保存", type="primary"):
        start_date, rebuilt_milestones = rebuild_milestones_by_delivery_date(milestones, new_delivery_date)
        data["start_date"] = start_date
        remark_lines = [
            "按修改后的交付日期重算节点：",
            *[
                f"{item['name']}（{item['duration']}天）：{item['due_date']}"
                for item in rebuilt_milestones
            ],
        ]
        data["remark"] = "\n".join(remark_lines)
        update_project(project_id, data)
        replace_project_milestones(project_id, rebuilt_milestones)
        st.success("交付日期和全部节点已同步更新。")
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


def show_feishu_page():
    st.title("飞书配置测试")
    load_dotenv()
    webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "")
    secret = os.getenv("FEISHU_SECRET", "")
    reminder_times = get_reminder_times(os.getenv("REMINDER_TIMES", os.getenv("REMINDER_TIME", "09:57,16:00")))

    st.write("Webhook URL：", "已配置" if webhook_url else "未配置")
    st.write("Secret：", "已配置" if secret else "未配置，可用于未开启签名的机器人")
    st.write("今日自动推送：", "已成功" if has_successful_auto_log() else "未看到成功记录")

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
            st.success(f"本地时间已保存：{', '.join(saved_times)}。\n{result}")
        except Exception as exc:
            st.error(f"同步失败：{exc}")

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
        file_name=f"短剧SOP项目列表_{date.today().strftime('%Y%m%d')}.xlsx",
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
            st.success(result)
        except Exception as exc:
            st.error(f"同步失败：{exc}")


def show_data_management_page():
    st.title("数据管理")
    total_projects = len(list_projects(include_delivered=True))
    active_projects = len(list_projects(include_delivered=False))
    reminder_times = get_reminder_times(os.getenv("REMINDER_TIMES", os.getenv("REMINDER_TIME", "09:57,16:00")))

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


def main():
    bootstrap_app()

    st.sidebar.title("短剧SOP")
    if st.sidebar.button("⏻ 同步 GitHub 云端", type="primary", use_container_width=True):
        try:
            result = sync_everything_to_github()
            st.sidebar.success(result)
        except Exception as exc:
            st.sidebar.error(f"同步失败：{exc}")
    st.sidebar.caption("本地项目、推送时间、云端定时一起同步。")
    st.sidebar.divider()

    page = st.sidebar.radio(
        "后台页面",
        [
            "今日提醒",
            "智能识别新增",
            "交付确认",
            "项目工作台",
            "飞书配置测试",
            "数据管理",
        ],
    )

    if page == "今日提醒":
        show_today_page()
    elif page == "智能识别新增":
        show_smart_add_page()
    elif page == "交付确认":
        show_delivery_confirmation_page()
    elif page == "项目工作台":
        show_project_workbench_page()
    elif page == "飞书配置测试":
        show_feishu_page()
    elif page == "数据管理":
        show_data_management_page()


if __name__ == "__main__":
    main()
