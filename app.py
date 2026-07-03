from datetime import date, datetime
from io import BytesIO
import os
import shutil
import subprocess

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
    send_daily_reminder,
    send_test_message,
    start_scheduler,
    update_reminder_time,
)
from scripts.export_actions_data import export_actions_data


STATUS_OPTIONS = ["进行中", "已交付", "延期", "暂停"]
LEVEL_OPTIONS = ["自定义", "B级", "A级", "C级"]


def find_git_executable() -> str:
    """寻找可用的 Git。"""
    bundled_git = r"C:\Users\zy-user\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe"
    return shutil.which("git") or bundled_git


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
    init_db(default_reminder_time=os.getenv("REMINDER_TIME", "10:00"))
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
            "project_name": "项目名",
            "start_date": "开始日期",
            "day": "今天第几天",
            "stage": "当前阶段",
            "task": "今日任务",
            "urge": "需要催办的人",
            "risk": "风险提醒",
            "status": "状态",
            "remark": "备注",
            "priority_label": "重点",
        }
    )
    df.insert(0, "序号", range(1, len(df) + 1))
    display_cols = ["序号", "重点", "项目名", "开始日期", "今天第几天", "当前阶段", "今日任务", "需要催办的人", "风险提醒", "状态", "备注"]
    st.dataframe(
        df[display_cols],
        use_container_width=True,
        hide_index=True,
        height=table_height(len(df)),
        column_config={
            "序号": st.column_config.NumberColumn("序号", width="small"),
            "重点": st.column_config.TextColumn("重点", width="small"),
            "项目名": st.column_config.TextColumn("项目名", width="large"),
            "今日任务": st.column_config.TextColumn("今日任务", width="large"),
            "风险提醒": st.column_config.TextColumn("风险提醒", width="large"),
            "备注": st.column_config.TextColumn("备注", width="large"),
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


def show_smart_add_page():
    st.title("智能识别新增")
    st.write("粘贴项目排期即可。可以给完整节点；也可以只给项目名、S/A/B级和交付时间，系统会自动倒推全部节点。")

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
        height=220,
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
    reminder_time = get_setting("reminder_time", os.getenv("REMINDER_TIME", "10:00"))

    st.write("Webhook URL：", "已配置" if webhook_url else "未配置")
    st.write("Secret：", "已配置" if secret else "未配置，可用于未开启签名的机器人")
    st.write("今日自动推送：", "已成功" if has_successful_auto_log() else "未看到成功记录")

    new_time = st.text_input("每日推送时间", value=reminder_time, help="格式：HH:MM，例如 10:00")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("保存推送时间", type="primary"):
            try:
                update_reminder_time(new_time)
                st.success("推送时间已保存。当前进程内的定时任务也已刷新。")
            except Exception as exc:
                st.error(f"保存失败：{exc}")
    with col2:
        if st.button("发送测试消息到飞书"):
            try:
                result = send_test_message()
                st.success(f"测试消息已发送：{result}")
            except Exception as exc:
                st.error(f"发送失败：{exc}")

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


def show_import_page():
    st.title("Excel 导入")
    st.write("支持列名：项目名、开始制作日期、集数、项目等级、负责人、当前状态、备注。")
    file = st.file_uploader("上传 Excel 文件", type=["xlsx", "xls"])
    if file and st.button("开始导入", type="primary"):
        try:
            count = import_projects_from_excel(file)
            st.success(f"导入完成，共新增 {count} 个项目。")
            st.rerun()
        except Exception as exc:
            st.error(f"导入失败：{exc}")


def show_export_page():
    st.title("Excel 导出")
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
    )

    st.divider()
    st.subheader("GitHub Actions 数据")
    st.write("本地项目有修改后，点击这里同步到 GitHub。云端每日推送会读取同步后的数据。")
    if st.button("生成 GitHub Actions 数据文件", type="primary"):
        try:
            export_actions_data()
            st.success("已生成 data/projects_for_actions.json。提交并推送到 GitHub 后，云端定时推送会使用最新数据。")
        except Exception as exc:
            st.error(f"生成失败：{exc}")

    if st.button("一键同步到 GitHub"):
        try:
            result = sync_actions_data_to_github()
            st.success(result)
        except Exception as exc:
            st.error(f"同步失败：{exc}")


def main():
    bootstrap_app()

    st.sidebar.title("短剧SOP")
    page = st.sidebar.radio(
        "后台页面",
        [
            "今日提醒",
            "项目列表",
            "新增项目",
            "智能识别新增",
            "编辑项目",
            "删除项目",
            "飞书配置测试",
            "Excel 导入",
            "Excel 导出",
        ],
    )

    if page == "今日提醒":
        show_today_page()
    elif page == "项目列表":
        show_project_list_page()
    elif page == "新增项目":
        show_add_page()
    elif page == "智能识别新增":
        show_smart_add_page()
    elif page == "编辑项目":
        show_edit_page()
    elif page == "删除项目":
        show_delete_page()
    elif page == "飞书配置测试":
        show_feishu_page()
    elif page == "Excel 导入":
        show_import_page()
    elif page == "Excel 导出":
        show_export_page()


if __name__ == "__main__":
    main()
