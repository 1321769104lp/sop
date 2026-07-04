from datetime import date, datetime


from delivery_rules import SOP_TEMPLATES
from utils_time import today_beijing


# SOP 规则集中放在这里，后续要改每天的任务，只需要改这个字典。
SOP_RULES = {
    1: {
        "stage": "资产启动",
        "task": "建立角色、服装、场景、道具清单。",
        "urge": "资产负责人 / 美术 / 制片",
        "risk": "资产清单不完整会影响后续制作。",
        "risk_level": 5,
    },
    2: {
        "stage": "资产制作",
        "task": "继续资产制作，确认资产缺口。",
        "urge": "资产负责人 / 美术",
        "risk": "及时补齐缺口，避免第4天首集卡住。",
        "risk_level": 5,
    },
    3: {
        "stage": "资产最终确认",
        "task": "资产最终确认，准备进入首集制作。",
        "urge": "资产负责人 / 导演 / 制片",
        "risk": "今天必须锁定核心资产。",
        "risk_level": 5,
    },
    4: {
        "stage": "首集关键节点",
        "task": "第一集制作、修改、确认首集成片。",
        "urge": "导演 / 剪辑 / 制片 / 甲方确认人",
        "risk": "首集风格决定后续批量制作方向，属于关键节点。",
        "risk_level": 3,
    },
    5: {
        "stage": "第2-10集批量制作",
        "task": "推进第2-10集制作，目标约4.5集/天。",
        "urge": "导演 / 剪辑 / 后期",
        "risk": "产能不足会影响第6天完成第2-10集。",
        "risk_level": 4,
    },
    6: {
        "stage": "第2-10集成片",
        "task": "完成第2-10集成片。",
        "urge": "导演 / 剪辑 / 制片",
        "risk": "第2-10集今天应形成可验收成片。",
        "risk_level": 4,
    },
    7: {
        "stage": "第11-30集批量制作",
        "task": "推进第11-30集制作，目标约10集/天。",
        "urge": "导演 / 剪辑 / 后期",
        "risk": "批量制作压力高，注意每日产出。",
        "risk_level": 4,
    },
    8: {
        "stage": "第11-30集成片",
        "task": "完成第11-30集成片。",
        "urge": "导演 / 剪辑 / 制片",
        "risk": "第11-30集今天应完成成片。",
        "risk_level": 4,
    },
    9: {
        "stage": "全片终审",
        "task": "全片终审、修改、素材整理。",
        "urge": "制片 / 导演 / 甲方确认人",
        "risk": "交付前最后修改日，必须收口问题。",
        "risk_level": 2,
    },
    10: {
        "stage": "项目交付",
        "task": "百度网盘上传、验收确认单、项目交付。",
        "urge": "制片 / 交付负责人 / 甲方确认人",
        "risk": "今天是默认交付日，请确认验收和交付材料。",
        "risk_level": 2,
    },
}


def parse_date(value) -> date:
    """把字符串、datetime、date 统一转成 date。"""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def production_day(start_date, today=None) -> int:
    """开始制作日期当天算第 1 天。"""
    today = today or today_beijing()
    start = parse_date(start_date)
    return (today - start).days + 1


def get_sop_info(start_date, today=None, project_level: str = "B级") -> dict:
    """根据开始日期和 S/A/B 等级计算今日 SOP 信息。"""
    today = today or today_beijing()
    day = production_day(start_date, today)
    template = SOP_TEMPLATES.get(project_level) or SOP_TEMPLATES["B级"]

    if day <= 0:
        return {
            "day": day,
            "stage": "未开始",
            "task": "项目尚未到开始制作日期。",
            "urge": "负责人",
            "risk": "请确认项目是否提前启动。",
            "risk_level": 6,
            "is_due_today": False,
            "is_first_episode": False,
            "is_batch": False,
            "is_asset": False,
        }

    cursor = 1
    for item in template:
        duration = int(item["duration"])
        end_day = cursor + duration - 1
        if cursor <= day <= end_day:
            stage = item["name"]
            is_due_today = day == end_day
            if is_due_today:
                task = f"今日应完成“{stage}”。"
                risk = f"今天是“{stage}”节点日，请确认产出和反馈。"
                risk_level = 2
            else:
                task = f"推进“{stage}”，本阶段计划 {duration} 天。"
                risk = f"距离“{stage}”节点日还有 {end_day - day} 天。"
                risk_level = 3 if "首集" in stage else 5
            return {
                "day": day,
                "stage": stage,
                "task": task,
                "urge": "承制方 / 制片",
                "risk": risk,
                "risk_level": risk_level,
                "is_due_today": is_due_today,
                "is_first_episode": "首集" in stage,
                "is_batch": "全集" in stage or "一卡" in stage or "2-10" in stage or "制作" in stage,
                "is_asset": "资产" in stage,
            }
        cursor = end_day + 1

    return {
        "day": day,
        "stage": "超出SOP周期",
        "task": "已超过当前等级的 SOP 制作周期，请确认实际制作进度。",
        "urge": "制片 / 项目负责人 / 承制方",
        "risk": "制作周期已走完，交付风险仍以正式交付日期为准。",
        "risk_level": 3,
        "is_due_today": False,
        "is_first_episode": False,
        "is_batch": False,
        "is_asset": False,
    }


def risk_sort_key(row: dict) -> tuple:
    """今日提醒排序：数字越小越靠前。"""
    return (row.get("risk_level", 99), row.get("start_date", ""), row.get("project_name", ""))
