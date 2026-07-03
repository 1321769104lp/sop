import os
from datetime import date, datetime, timedelta

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


_scheduler = None


def calculate_importance(info: dict) -> int:
    """数字越小越重要，飞书和首页都按这个排序。"""
    if info.get("stage") == "超期":
        return 1
    if info.get("is_due_today"):
        return 2
    if info.get("is_delivery"):
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
    if info.get("stage") == "超期":
        return "【超期】"
    if info.get("is_due_today") and info.get("is_delivery"):
        return "【今日交付】"
    if info.get("is_due_today"):
        return "【今日节点】"
    if info.get("is_delivery"):
        return "【交付】"
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
    today = today or date.today()
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
    }
    info["importance_rank"] = calculate_importance(info)
    return info


def build_today_rows(today=None) -> list[dict]:
    """生成今日推进表数据，已交付项目不展示。"""
    rows = []
    for project in list_projects(include_delivered=False):
        milestones = get_project_milestones(project["id"])
        if milestones:
            info = build_custom_schedule_info(project, milestones, today=today)
        else:
            # 没有结构化节点的旧项目，继续使用原来的 10 天 SOP 作为兜底。
            info = get_sop_info(project["start_date"], today=today)
            info["is_due_today"] = info["day"] in (4, 6, 8, 9, 10)
            info["is_delivery"] = info["day"] in (9, 10) or info["stage"] == "超期"
            info["is_first_episode"] = info["day"] == 4
            info["is_batch"] = 5 <= info["day"] <= 8
            info["is_asset"] = 1 <= info["day"] <= 3
            info["days_to_due"] = max(0, 10 - info["day"]) if info["day"] > 0 else 999
            info["importance_rank"] = calculate_importance(info)

        rows.append(
            {
                "priority_label": build_priority_label(info),
                "project_name": project["project_name"],
                "start_date": project["start_date"],
                "day": info["day"],
                "stage": info["stage"],
                "task": info["task"],
                "urge": project.get("owner") or info["urge"],
                "risk": info["risk"],
                "status": project["status"],
                "remark": project.get("remark", ""),
                "importance_rank": info.get("importance_rank", 99),
                "days_to_due": info.get("days_to_due"),
                "is_delivery": info.get("is_delivery", False),
                "is_first_episode": info.get("is_first_episode", False),
                "is_due_today": info.get("is_due_today", False),
            }
        )
    return sorted(rows, key=reminder_sort_key)


def build_feishu_message(today=None) -> str:
    """构造飞书每日提醒文本。"""
    today = today or date.today()
    rows = build_today_rows(today=today)
    overdue_count = sum(1 for row in rows if row["stage"] == "超期")
    delivery_count = sum(1 for row in rows if row.get("is_delivery"))
    first_episode_count = sum(1 for row in rows if row.get("is_first_episode"))

    lines = [
        "【今日短剧SOP推进提醒】",
        "",
        f"日期：{today.strftime('%Y-%m-%d')}",
        f"进行中项目：{len(rows)} 个",
        f"超期项目：{overdue_count} 个",
        f"交付节点：{delivery_count} 个",
        f"首集关键节点：{first_episode_count} 个",
        "",
        "━━━━━━━━━━━━━━",
    ]

    if not rows:
        lines.append("今日暂无需要提醒的项目。")
        return "\n".join(lines)

    for row in rows:
        lines.extend(
            [
                "",
                f"{row['priority_label']}《{row['project_name']}》",
                f"开始日期：{row['start_date']}",
                f"今天是第{row['day']}天",
                f"当前阶段：{row['stage']}",
                f"今日任务：{row['task']}",
                f"你要催：{row['urge']}",
                f"风险提醒：{row['risk']}",
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


def update_reminder_time(value: str):
    """保存推送时间，并刷新当前进程里的定时任务。"""
    parse_reminder_time(value)
    set_setting("reminder_time", value)
    if _scheduler:
        schedule_daily_job(_scheduler, value)


def schedule_daily_job(scheduler: BackgroundScheduler, reminder_time: str):
    """注册或替换每日提醒任务。"""
    hour, minute = parse_reminder_time(reminder_time)
    scheduler.add_job(
        send_daily_reminder,
        CronTrigger(hour=hour, minute=minute),
        id="daily_feishu_reminder",
        replace_existing=True,
        max_instances=1,
    )


def send_missed_today_reminder_if_needed(reminder_time: str):
    """如果今天已过推送时间但没有自动成功记录，启动时补发一次。"""
    hour, minute = parse_reminder_time(reminder_time)
    now = datetime.now()
    scheduled_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now <= scheduled_at:
        return
    if has_successful_auto_log(now.strftime("%Y-%m-%d")):
        return
    send_daily_reminder(send_type="catchup")


def start_scheduler():
    """启动后台定时器；Streamlit 重跑时不会重复启动。"""
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    reminder_time = get_setting("reminder_time", os.getenv("REMINDER_TIME", "10:00"))
    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    schedule_daily_job(_scheduler, reminder_time)
    _scheduler.start()
    try:
        send_missed_today_reminder_if_needed(reminder_time)
    except Exception:
        # 补发失败已经写入日志，不能阻断后台启动。
        pass
    return _scheduler
