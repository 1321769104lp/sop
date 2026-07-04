from datetime import date, datetime, timedelta


SOP_TEMPLATES = {
    "S级": [
        {"name": "资产确定", "duration": 3},
        {"name": "首集制作与修改", "duration": 2},
        {"name": "一卡前制作", "duration": 5},
        {"name": "全集制作", "duration": 16},
        {"name": "终审与交付", "duration": 2},
    ],
    "A级": [
        {"name": "资产确定", "duration": 3},
        {"name": "首集制作与修改", "duration": 2},
        {"name": "一卡前制作", "duration": 3},
        {"name": "全集制作", "duration": 10},
        {"name": "终审与交付", "duration": 2},
    ],
    "B级": [
        {"name": "资产确定", "duration": 3},
        {"name": "首集制作与修改", "duration": 1},
        {"name": "2-10集制作", "duration": 2},
        {"name": "全集制作", "duration": 2},
        {"name": "终审与交付", "duration": 2},
    ],
}


def parse_date_value(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def production_day(start_date, today: date) -> int:
    return (today - parse_date_value(start_date)).days + 1


def cycle_days_for_level(project_level: str, default_cycle_days: int = 10) -> int:
    template = SOP_TEMPLATES.get(project_level)
    if not template:
        return default_cycle_days
    return sum(max(1, int(item["duration"])) for item in template)


def delivery_date_from_cycle(start_date, cycle_days: int) -> date:
    return parse_date_value(start_date) + timedelta(days=max(1, int(cycle_days)) - 1)


def cycle_days_from_milestones(milestones: list[dict], default_cycle_days: int = 10) -> int:
    if not milestones:
        return default_cycle_days
    return sum(max(1, int(item.get("duration") or 1)) for item in milestones)


def resolve_delivery_date(project: dict, cycle_days: int | None = None) -> dict:
    raw_delivery = (project.get("delivery_date") or "").strip()
    if raw_delivery:
        return {
            "delivery_date": parse_date_value(raw_delivery),
            "is_estimated_delivery": False,
        }
    fallback_cycle_days = cycle_days or cycle_days_for_level(project.get("project_level") or "自定义")
    return {
        "delivery_date": delivery_date_from_cycle(project["start_date"], fallback_cycle_days),
        "is_estimated_delivery": True,
    }


def delivery_state(project: dict, today: date, cycle_days: int | None = None) -> dict:
    resolved = resolve_delivery_date(project, cycle_days=cycle_days)
    delivery_date = resolved["delivery_date"]
    remaining_days = (delivery_date - today).days
    is_overdue = (not resolved["is_estimated_delivery"]) and remaining_days < 0
    is_near_delivery = (not resolved["is_estimated_delivery"]) and 0 <= remaining_days <= 2
    return {
        "delivery_date": delivery_date,
        "delivery_remaining_days": remaining_days,
        "is_overdue": is_overdue,
        "is_near_delivery": is_near_delivery,
        "is_delivery_node": is_overdue or is_near_delivery,
        "is_estimated_delivery": resolved["is_estimated_delivery"],
    }


def apply_delivery_state(info: dict, project: dict, today: date, cycle_days: int | None = None) -> dict:
    state = delivery_state(project, today=today, cycle_days=cycle_days)
    info.update(
        {
            "delivery_date": state["delivery_date"].strftime("%Y-%m-%d"),
            "delivery_remaining_days": state["delivery_remaining_days"],
            "is_overdue": state["is_overdue"],
            "is_near_delivery": state["is_near_delivery"],
            "is_delivery_node": state["is_delivery_node"],
            "is_delivery": state["is_delivery_node"],
            "is_estimated_delivery": state["is_estimated_delivery"],
        }
    )
    return info


def format_delivery_countdown(remaining_days: int | None) -> str:
    if remaining_days is None:
        return "未配置"
    if remaining_days < 0:
        return f"已超{abs(remaining_days)}天"
    if remaining_days == 0:
        return "今天交付"
    return f"剩{remaining_days}天"


def delivery_label(row: dict) -> str:
    return "预估交付" if row.get("is_estimated_delivery") else "交付"


def summarize_rows(rows: list[dict]) -> dict:
    return {
        "total": len(rows),
        "delivery_risk": sum(1 for row in rows if row.get("is_delivery_node") or row.get("is_delivery")),
        "overdue": sum(1 for row in rows if row.get("is_overdue") or row.get("stage") == "超期"),
        "first_episode": sum(1 for row in rows if row.get("is_first_episode")),
        "today_nodes": sum(1 for row in rows if row.get("is_due_today")),
    }


def format_summary_line(summary: dict) -> str:
    return (
        f"总览：{summary['total']}个｜"
        f"交付风险{summary['delivery_risk']}（超期{summary['overdue']}）｜"
        f"首集{summary['first_episode']}｜"
        f"今日节点{summary['today_nodes']}"
    )
