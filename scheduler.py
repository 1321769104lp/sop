import os
import re
from datetime import date, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from database import (
    add_notification_log,
    get_project_milestones,
    get_setting,
    has_successful_auto_log,
    list_projects,
    set_setting,
)
from feishu import send_feishu_message
from sop import get_sop_info, production_day
from delivery_rules import (
    apply_delivery_state,
    cycle_days_for_level,
    delivery_label,
    format_delivery_countdown,
    format_summary_line,
    summarize_rows,
)
from utils_time import now_beijing, today_beijing


_scheduler = None


def chinese_project_name(name: str) -> str:
    """飞书里只显示中文项目名，去掉英文名和附加标注。"""
    clean = (name or "").strip()
    if not clean:
        return clean

    if not re.search(r"[\u4e00-\u9fff]", clean):
        return clean

    # 英文名在前、中文名在括号里时，优先取括号里的中文。
    if re.match(r"^[A-Za-z0-9 :,'’!-]+[（(]", clean):
        match = re.search(r"[（(]([^（）()]*[\u4e00-\u9fff][^（）()]*)[）)]", clean)
        if match:
            clean = match.group(1)

    for separator in ["+", "：", ":"]:
        if separator in clean:
            left, right = clean.split(separator, 1)
            if re.search(r"[A-Za-z]", right) and re.search(r"[\u4e00-\u9fff]", left):
                clean = left
                break

    clean = re.sub(r"[（(][^（）()]*[A-Za-z][^（）()]*[）)]", "", clean)
    clean = re.sub(r"[（(][^（）()]*组[^（）()]*[）)]", "", clean)
    clean = re.sub(r"[（(][^（）()]*[）)]", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip(" ：:+-")
    return clean


def delivery_countdown(delivery_date: str | None, today: date) -> str:
    """格式化交付倒计时。"""
    if not delivery_date:
        return "未配置"
    delta = (date.fromisoformat(delivery_date) - today).days
    if delta > 0:
        return f"剩{delta}天"
    if delta == 0:
        return "今天交付"
    return f"已超{abs(delta)}天"


def concise_task(row: dict) -> str:
    """把系统任务压缩成制片视角的短句。"""
    stage = row.get("stage", "")
    if row.get("is_overdue"):
        return "确认交付状态或延期方案"
    if row.get("is_due_today"):
        return f"确认{stage}产出"
    return f"推进{stage}"


def concise_risk(row: dict) -> str:
    """把风险压缩到一行。"""
    if row.get("is_overdue"):
        return "已过交付日，需立即确认"
    if row.get("is_delivery_node"):
        return "交付节点，注意验收和上传"
    if row.get("is_first_episode"):
        return "首集方向会影响后续批量制作"
    if row.get("is_due_today"):
        return "今天是节点日，必须收口"
    if row.get("days_to_due") is not None and row["days_to_due"] <= 2:
        return "节点临近，提前催产出"
    return "按节点推进"


def urge_owner_for_stage(row: dict) -> str:
    """根据当前节点给出更贴近实际协作对象的催办人。"""
    stage = row.get("stage", "")
    if row.get("is_overdue"):
        return "项目负责人 / 制片 / 承制方"
    if row.get("is_delivery_node") or "终审" in stage or "交付" in stage:
        return "制片 / 审核负责人 / 承制方"
    if row.get("is_first_episode") or "首集" in stage:
        return "导演 / 剪辑 / 承制方"
    if row.get("is_asset") or "资产" in stage:
        return "制片 / 版权 / 资产负责人"
    if row.get("is_batch") or "一卡前" in stage or "2-10" in stage or "全集" in stage or "制作" in stage:
        return "承制方 / 制片"
    if "未开始" in stage or "等待" in stage:
        return "项目负责人 / 制片"
    return row.get("urge") or "项目负责人 / 制片"


def clean_priority_label(label: str) -> str:
    """把首页标签转成飞书用的方括号标签。"""
    return (label or "常规").strip("【】")


def delivery_delta(row: dict, today: date) -> int | None:
    """计算距离最终交付日的天数。"""
    delivery_date = row.get("delivery_date")
    if not delivery_date:
        return None
    return (date.fromisoformat(delivery_date) - today).days


def contractor_output(row: dict) -> str:
    """承制方这一侧要补的具体产出物。"""
    stage = row.get("stage", "")
    if row.get("is_overdue"):
        return "补齐缺口产出"
    if row.get("is_delivery_node") or "终审" in stage or "交付" in stage:
        return "交付成片、工程文件和验收材料"
    if row.get("is_first_episode") or "首集" in stage:
        return "首集成片和修改反馈版"
    if row.get("is_asset") or "资产" in stage:
        return "角色、场景、道具等资产包"
    if row.get("is_batch") or "一卡前" in stage or "2-10" in stage or "全集" in stage or "制作" in stage:
        return f"{stage}阶段成片"
    if "未开始" in stage or "等待" in stage:
        return "准备下一节点所需素材"
    return f"{stage}产出物"


def producer_action(row: dict) -> str:
    """制片这一侧要做的具体确认动作。"""
    stage = row.get("stage", "")
    if row.get("is_overdue"):
        return "确认交付状态"
    if row.get("is_delivery_node") or "终审" in stage or "交付" in stage:
        return "确认验收、上传和交付状态"
    if row.get("is_first_episode") or "首集" in stage:
        return "确认首集方向和修改意见"
    if row.get("is_asset") or "资产" in stage:
        return "确认资产是否齐套可开工"
    if row.get("is_batch") or "一卡前" in stage or "2-10" in stage or "全集" in stage or "制作" in stage:
        return "确认产出进度和反馈收口"
    if "未开始" in stage or "等待" in stage:
        return "确认下一节点启动时间"
    return f"确认{stage}进度"


def build_project_reminder_lines(row: dict, today: date) -> list[str]:
    """按固定格式生成单个项目的飞书提醒。"""
    level = row.get("project_level") or "自定义"
    day = row.get("day") or 0
    delivery_date = row.get("delivery_date") or "未配置"
    countdown = row.get("delivery_countdown") or format_delivery_countdown(row.get("delivery_remaining_days"))
    overdue = row.get("is_overdue", False)

    if overdue:
        title = f"[超期]《{row['display_name']}》｜{level}D{day}｜{delivery_label(row)}{delivery_date}｜{countdown}"
        detail = "承制方：补齐缺口产出；制片：确认交付状态。"
        return [title, detail]

    label = clean_priority_label(row.get("priority_label", "常规"))
    title = f"[{label}]《{row['display_name']}》｜{level}D{day}｜{delivery_label(row)}{delivery_date}｜{countdown}"
    detail = f"承制方：{contractor_output(row)}；制片：{producer_action(row)}。"
    return [title, detail]


def calculate_importance(info: dict) -> int:
    """数字越小越重要，飞书和首页都按这个排序。"""
    if info.get("is_overdue"):
        return 1
    if info.get("is_delivery_node"):
        return 2
    if info.get("is_due_today"):
        return 3
    if info.get("is_first_episode"):
        return 4
    if info.get("days_to_due") is not None and info["days_to_due"] <= 2:
        return 5
    if info.get("is_batch"):
        return 6
    if info.get("is_asset"):
        return 7
    return 8


def build_priority_label(info: dict) -> str:
    """生成醒目的重点标签。"""
    if info.get("is_overdue"):
        return "【重点｜超期】"
    if info.get("delivery_remaining_days") == 0:
        return "【重点｜今日交付】"
    if info.get("is_delivery_node"):
        return "【交付风险】"
    if info.get("is_due_today"):
        return "【重点｜今日节点】"
    if info.get("is_first_episode"):
        return "【首集】"
    if info.get("days_to_due") is not None and info["days_to_due"] <= 2:
        return "【临近节点】"
    if info.get("is_batch"):
        return "【批量制作】"
    if info.get("is_asset"):
        return "【资产】"
    return "【常规】"


def reminder_sort_key(row: dict) -> tuple:
    """提醒排序：重要性优先，同级别再看距离节点日期远近。"""
    days_to_due = row.get("days_to_due")
    if days_to_due is None:
        days_to_due = 999
    return (
        row.get("importance_rank", 99),
        days_to_due,
        row.get("start_date", ""),
        row.get("project_name", ""),
    )


def build_custom_schedule_info(project: dict, milestones: list[dict], today=None) -> dict:
    """按项目自己的节点排期计算今日阶段。"""
    today = today or today_beijing()
    day = production_day(project["start_date"], today=today)

    normalized = []
    for item in milestones:
        due_date = date.fromisoformat(item["due_date"])
        duration = int(item["duration"] or 1)
        start_date = due_date - timedelta(days=duration - 1)
        normalized.append(
            {
                "name": item["name"],
                "duration": duration,
                "due_date": due_date,
                "start_date": start_date,
            }
        )

    first = normalized[0]
    final = normalized[-1]
    owner = project.get("owner") or "项目负责人 / 制片"

    if today < first["start_date"]:
        info = {
            "day": day,
            "stage": "未开始",
            "task": f"项目尚未到第一个节点“{first['name']}”的启动日。",
            "urge": owner,
            "risk": f"第一个节点计划 {first['start_date']} 启动，{first['due_date']} 完成。",
            "days_to_due": (first["due_date"] - today).days,
            "is_due_today": False,
            "is_delivery": False,
            "is_first_episode": False,
            "is_batch": False,
            "is_asset": False,
            "delivery_date": final["due_date"].strftime("%Y-%m-%d"),
        }
        info["importance_rank"] = calculate_importance(info)
        return info

    if today > final["due_date"]:
        info = {
            "day": day,
            "stage": "超期",
            "task": "项目已超过排期交付日，请确认是否已交付或需要延期。",
            "urge": owner,
            "risk": f"排期交付日是 {final['due_date']}，当前已超期。",
            "days_to_due": -((today - final["due_date"]).days),
            "is_due_today": False,
            "is_delivery": True,
            "is_first_episode": False,
            "is_batch": False,
            "is_asset": False,
            "delivery_date": final["due_date"].strftime("%Y-%m-%d"),
        }
        info["importance_rank"] = calculate_importance(info)
        return info

    active = None
    next_item = None
    for item in normalized:
        if item["start_date"] <= today <= item["due_date"]:
            active = item
            break
        if today < item["start_date"]:
            next_item = item
            break

    if active is None and next_item:
        info = {
            "day": day,
            "stage": f"等待{next_item['name']}",
            "task": f"上一节点已结束，准备进入“{next_item['name']}”。",
            "urge": owner,
            "risk": f"下一节点计划 {next_item['start_date']} 启动，{next_item['due_date']} 完成。",
            "days_to_due": (next_item["due_date"] - today).days,
            "is_due_today": False,
            "is_delivery": "交付" in next_item["name"],
            "is_first_episode": "首集" in next_item["name"],
            "is_batch": "全集" in next_item["name"] or "一卡" in next_item["name"] or "制作" in next_item["name"],
            "is_asset": "资产" in next_item["name"],
            "delivery_date": final["due_date"].strftime("%Y-%m-%d"),
        }
        info["importance_rank"] = calculate_importance(info)
        return info

    remaining_days = (active["due_date"] - today).days
    if remaining_days == 0:
        task = f"今日应完成“{active['name']}”。"
        risk = f"今天是“{active['name']}”节点日，请确认产出和反馈。"
    else:
        task = f"推进“{active['name']}”，计划 {active['duration']} 天，节点日期 {active['due_date']}。"
        risk = f"距离“{active['name']}”节点还有 {remaining_days} 天。"

    stage_name = active["name"]
    info = {
        "day": day,
        "stage": stage_name,
        "task": task,
        "urge": owner,
        "risk": risk,
        "days_to_due": remaining_days,
        "is_due_today": remaining_days == 0,
        "is_delivery": active == final or "交付" in stage_name,
        "is_first_episode": "首集" in stage_name,
        "is_batch": "全集" in stage_name or "一卡" in stage_name or "2-10" in stage_name,
        "is_asset": "资产" in stage_name,
        "delivery_date": final["due_date"].strftime("%Y-%m-%d"),
    }
    info["importance_rank"] = calculate_importance(info)
    return info


def build_today_rows(today=None) -> list[dict]:
    """生成今日推进表数据，已交付项目不展示。"""
    today = today or today_beijing()
    rows = []
    for project in list_projects(include_delivered=False):
        project_level = project.get("project_level") or "B级"
        cycle_days = cycle_days_for_level(project_level)
        info = get_sop_info(project["start_date"], today=today, project_level=project_level)
        info = apply_delivery_state(info, project, today=today, cycle_days=cycle_days)
        info["days_to_due"] = info.get("delivery_remaining_days")
        info["importance_rank"] = calculate_importance(info)

        rows.append(
            {
                "priority_label": build_priority_label(info),
                "project_name": project["project_name"],
                "display_name": chinese_project_name(project["project_name"]),
                "project_level": project_level,
                "start_date": project["start_date"],
                "day": info["day"],
                "delivery_date": info.get("delivery_date"),
                "delivery_countdown": format_delivery_countdown(info.get("delivery_remaining_days")),
                "delivery_remaining_days": info.get("delivery_remaining_days"),
                "is_overdue": info.get("is_overdue", False),
                "is_delivery_node": info.get("is_delivery_node", False),
                "is_estimated_delivery": info.get("is_estimated_delivery", False),
                "stage": info["stage"],
                "task": info["task"],
                "today_focus": "",
                "urge": info.get("urge", ""),
                "risk": info["risk"],
                "risk_brief": "",
                "status": project["status"],
                "remark": project.get("remark", ""),
                "importance_rank": info.get("importance_rank", 99),
                "days_to_due": info.get("days_to_due"),
                "is_delivery": info.get("is_delivery_node", False),
                "is_first_episode": info.get("is_first_episode", False),
                "is_due_today": info.get("is_due_today", False),
                "is_batch": info.get("is_batch", False),
                "is_asset": info.get("is_asset", False),
            }
        )
        rows[-1]["urge"] = urge_owner_for_stage(rows[-1])
        rows[-1]["today_focus"] = concise_task(rows[-1])
        rows[-1]["risk_brief"] = concise_risk(rows[-1])
    return sorted(rows, key=reminder_sort_key)


def build_feishu_message(today=None) -> str:
    """构造飞书每日提醒文本。"""
    today = today or today_beijing()
    rows = build_today_rows(today=today)
    summary = summarize_rows(rows)

    lines = [
        f"【今日短剧SOP｜{today.strftime('%Y-%m-%d')}】",
        format_summary_line(summary),
        "",
        "━━━━━━━━━━━━━━",
    ]

    if not rows:
        lines.append("今日暂无需要提醒的项目。")
        return "\n".join(lines)

    for row in rows:
        title, detail = build_project_reminder_lines(row, today)
        lines.extend(
            [
                "",
                title,
                detail,
            ]
        )
    return "\n".join(lines)


def send_daily_reminder(send_type: str = "auto") -> dict:
    """读取 .env 并发送每日提醒，同时记录成功或失败日志。"""
    load_dotenv()
    webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "")
    secret = os.getenv("FEISHU_SECRET", "")
    message = build_feishu_message()
    try:
        result = send_feishu_message(webhook_url, secret, message)
        add_notification_log(send_type, "success", message=message)
        return result
    except Exception as exc:
        add_notification_log(send_type, "failed", message=message, error=str(exc))
        raise


def send_test_message() -> dict:
    """后台按钮使用的飞书测试消息。"""
    load_dotenv()
    webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "")
    secret = os.getenv("FEISHU_SECRET", "")
    message = "短剧SOP提醒系统测试成功。"
    try:
        result = send_feishu_message(webhook_url, secret, message)
        add_notification_log("test", "success", message=message)
        return result
    except Exception as exc:
        add_notification_log("test", "failed", message=message, error=str(exc))
        raise


def parse_reminder_time(value: str) -> tuple[int, int]:
    """解析 HH:MM 格式的推送时间。"""
    hour_text, minute_text = value.strip().split(":")
    hour = int(hour_text)
    minute = int(minute_text)
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("推送时间必须是 00:00 到 23:59")
    return hour, minute


def parse_reminder_times(value: str | list[str]) -> list[str]:
    """解析一个或多个 HH:MM 推送时间，去重并排序。"""
    if isinstance(value, str):
        raw_values = re.split(r"[,，\n\s]+", value)
    else:
        raw_values = value

    times = []
    seen = set()
    for item in raw_values:
        clean = str(item).strip()
        if not clean:
            continue
        hour, minute = parse_reminder_time(clean)
        normalized = f"{hour:02d}:{minute:02d}"
        if normalized not in seen:
            seen.add(normalized)
            times.append(normalized)

    if not times:
        raise ValueError("至少需要一个推送时间。")
    return sorted(times)


def get_reminder_times(default: str = "09:57,16:00") -> list[str]:
    """读取本地多个推送时间，兼容旧的单时间配置。"""
    value = get_setting("reminder_times", "")
    if not value:
        value = get_setting("reminder_time", default)
    return parse_reminder_times(value)


def update_reminder_times(values: str | list[str]):
    """保存多个推送时间，并刷新当前进程里的定时任务。"""
    times = parse_reminder_times(values)
    set_setting("reminder_times", ",".join(times))
    set_setting("reminder_time", times[0])
    if _scheduler:
        schedule_daily_jobs(_scheduler, times)
    return times


def update_reminder_time(value: str):
    """兼容旧调用：保存单个推送时间。"""
    return update_reminder_times([value])


def schedule_daily_jobs(scheduler: BackgroundScheduler, reminder_times: list[str]):
    """注册或替换多个每日提醒任务。"""
    for job in list(scheduler.get_jobs()):
        if job.id.startswith("daily_feishu_reminder"):
            scheduler.remove_job(job.id)

    for index, reminder_time in enumerate(reminder_times):
        hour, minute = parse_reminder_time(reminder_time)
        scheduler.add_job(
            send_daily_reminder,
            CronTrigger(hour=hour, minute=minute),
            id=f"daily_feishu_reminder_{index}",
            replace_existing=True,
            max_instances=1,
        )


def send_missed_today_reminder_if_needed(reminder_times: list[str]):
    """如果今天已过推送时间但没有自动成功记录，启动时补发一次。"""
    now = now_beijing()
    if has_successful_auto_log(now.strftime("%Y-%m-%d")):
        return
    for reminder_time in reminder_times:
        hour, minute = parse_reminder_time(reminder_time)
        scheduled_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now > scheduled_at:
            send_daily_reminder(send_type="catchup")
            return


def start_scheduler():
    """启动后台定时器；Streamlit 重跑时不会重复启动。"""
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    reminder_times = get_reminder_times(os.getenv("REMINDER_TIMES", os.getenv("REMINDER_TIME", "09:57,16:00")))
    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    schedule_daily_jobs(_scheduler, reminder_times)
    _scheduler.start()
    try:
        send_missed_today_reminder_if_needed(reminder_times)
    except Exception:
        # 补发失败已经写入日志，不能阻断后台启动。
        pass
    return _scheduler
