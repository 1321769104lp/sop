from datetime import date, datetime


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
    today = today or date.today()
    start = parse_date(start_date)
    return (today - start).days + 1


def get_sop_info(start_date, today=None) -> dict:
    """根据开始日期计算今日 SOP 信息。"""
    day = production_day(start_date, today)

    if day <= 0:
        return {
            "day": day,
            "stage": "未开始",
            "task": "项目尚未到开始制作日期。",
            "urge": "负责人",
            "risk": "请确认项目是否提前启动。",
            "risk_level": 6,
        }

    if day >= 11:
        return {
            "day": day,
            "stage": "超期",
            "task": "标记为超期，提醒确认是否交付或延期。",
            "urge": "制片 / 项目负责人 / 甲方确认人",
            "risk": "项目已超过默认 10 天周期，请确认交付、延期或暂停。",
            "risk_level": 1,
        }

    info = SOP_RULES[day].copy()
    info["day"] = day
    return info


def risk_sort_key(row: dict) -> tuple:
    """今日提醒排序：数字越小越靠前。"""
    return (row.get("risk_level", 99), row.get("start_date", ""), row.get("project_name", ""))
