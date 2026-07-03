import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests


DATA_PATH = Path("data/projects_for_actions.json")
BEIJING_TZ = timezone(timedelta(hours=8))


def today_beijing() -> date:
    return datetime.now(BEIJING_TZ).date()


def production_day(start_date: str, today: date) -> int:
    return (today - date.fromisoformat(start_date)).days + 1


def chinese_project_name(name: str) -> str:
    clean = (name or "").strip()
    if not clean or not re.search(r"[\u4e00-\u9fff]", clean):
        return clean

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
    if not delivery_date:
        return "未配置"
    delta = (date.fromisoformat(delivery_date) - today).days
    if delta > 0:
        return f"剩{delta}天"
    if delta == 0:
        return "今天交付"
    return f"已超{abs(delta)}天"


def concise_task(row: dict) -> str:
    stage = row.get("stage", "")
    if stage == "超期":
        return "确认交付状态或延期方案"
    if row.get("is_due_today"):
        return f"确认{stage}产出"
    return f"推进{stage}"


def concise_risk(row: dict) -> str:
    if row.get("stage") == "超期":
        return "已过交付日，需立即确认"
    if row.get("is_delivery"):
        return "交付节点，注意验收和上传"
    if row.get("is_first_episode"):
        return "首集方向会影响后续批量制作"
    if row.get("is_due_today"):
        return "今天是节点日，必须收口"
    if row.get("days_to_due") is not None and row["days_to_due"] <= 2:
        return "节点临近，提前催产出"
    return "按节点推进"


def generate_sign(secret: str, timestamp: int) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def send_feishu_message(webhook_url: str, secret: str, text: str) -> dict:
    if not webhook_url:
        raise ValueError("缺少 FEISHU_WEBHOOK_URL，请在 GitHub Secrets 中配置。")

    payload = {
        "msg_type": "text",
        "content": {"text": text},
    }
    if secret:
        timestamp = int(time.time())
        payload["timestamp"] = str(timestamp)
        payload["sign"] = generate_sign(secret, timestamp)

    response = requests.post(webhook_url, json=payload, timeout=20)
    response.raise_for_status()
    return response.json()


def importance(info: dict) -> int:
    if info["stage"] == "超期":
        return 1
    if info["is_due_today"] and info["is_delivery"]:
        return 2
    if info["is_due_today"]:
        return 3
    if info["is_delivery"]:
        return 4
    if info["is_first_episode"]:
        return 5
    if info["days_to_due"] is not None and info["days_to_due"] <= 2:
        return 6
    if info["is_batch"]:
        return 7
    if info["is_asset"]:
        return 8
    return 9


def priority_label(info: dict) -> str:
    if info["stage"] == "超期":
        return "【重点｜超期】"
    if info["is_due_today"] and info["is_delivery"]:
        return "【重点｜今日交付】"
    if info["is_due_today"]:
        return "【重点｜今日节点】"
    if info["is_delivery"]:
        return "【交付】"
    if info["is_first_episode"]:
        return "【首集】"
    if info["days_to_due"] is not None and info["days_to_due"] <= 2:
        return "【临近节点】"
    if info["is_batch"]:
        return "【批量制作】"
    if info["is_asset"]:
        return "【资产】"
    return "【常规】"


def build_project_info(project: dict, today: date) -> dict:
    milestones = project.get("milestones") or []
    day = production_day(project["start_date"], today)
    owner = project.get("owner") or "项目负责人 / 制片"

    if not milestones:
        return {
            "day": day,
            "stage": "未配置节点",
            "task": "这个项目没有结构化节点，请回到本地后台重新智能识别或补充节点。",
            "urge": owner,
            "risk": "缺少节点排期，无法精确计算今日阶段。",
            "days_to_due": 999,
            "is_due_today": False,
            "is_delivery": False,
            "is_first_episode": False,
            "is_batch": False,
            "is_asset": False,
            "delivery_date": final["due_date"].strftime("%Y-%m-%d"),
        }

    normalized = []
    for item in milestones:
        due_date = date.fromisoformat(item["due_date"])
        duration = int(item.get("duration") or 1)
        normalized.append(
            {
                "name": item["name"],
                "duration": duration,
                "due_date": due_date,
                "start_date": due_date - timedelta(days=duration - 1),
            }
        )

    first = normalized[0]
    final = normalized[-1]

    if today < first["start_date"]:
        return {
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

    if today > final["due_date"]:
        return {
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
        stage = f"等待{next_item['name']}"
        return {
            "day": day,
            "stage": stage,
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

    remaining_days = (active["due_date"] - today).days
    if remaining_days == 0:
        task = f"今日应完成“{active['name']}”。"
        risk = f"今天是“{active['name']}”节点日，请确认产出和反馈。"
    else:
        task = f"推进“{active['name']}”，计划 {active['duration']} 天，节点日期 {active['due_date']}。"
        risk = f"距离“{active['name']}”节点还有 {remaining_days} 天。"

    stage = active["name"]
    return {
        "day": day,
        "stage": stage,
        "task": task,
        "urge": owner,
        "risk": risk,
        "days_to_due": remaining_days,
        "is_due_today": remaining_days == 0,
        "is_delivery": active == final or "交付" in stage,
        "is_first_episode": "首集" in stage,
        "is_batch": "全集" in stage or "一卡" in stage or "2-10" in stage,
        "is_asset": "资产" in stage,
        "delivery_date": final["due_date"].strftime("%Y-%m-%d"),
    }


def build_rows(today: date) -> list[dict]:
    payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    rows = []
    for project in payload.get("projects", []):
        if project.get("status") == "已交付":
            continue
        info = build_project_info(project, today)
        rank = importance(info)
        rows.append(
            {
                **info,
                "priority_label": priority_label(info),
                "importance_rank": rank,
                "project_name": project["project_name"],
                "display_name": chinese_project_name(project["project_name"]),
                "start_date": project["start_date"],
                "delivery_date": info.get("delivery_date"),
                "delivery_countdown": delivery_countdown(info.get("delivery_date"), today),
                "status": project.get("status", "进行中"),
            }
        )
        rows[-1]["today_focus"] = concise_task(rows[-1])
        rows[-1]["risk_brief"] = concise_risk(rows[-1])
    return sorted(
        rows,
        key=lambda row: (
            row["importance_rank"],
            row["days_to_due"] if row["days_to_due"] is not None else 999,
            row["start_date"],
            row["project_name"],
        ),
    )


def build_message(today: date) -> str:
    rows = build_rows(today)
    overdue_count = sum(1 for row in rows if row["stage"] == "超期")
    delivery_count = sum(1 for row in rows if row["is_delivery"])
    first_episode_count = sum(1 for row in rows if row["is_first_episode"])

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
                f"{row['priority_label']}《{row['display_name']}》",
                f"阶段：{row['stage']}｜交付：{row['delivery_countdown']}",
                f"今天重点：{row['today_focus']}",
                "催：承制方",
                f"风险：{row['risk_brief']}",
            ]
        )
    return "\n".join(lines)


def main():
    today = today_beijing()
    message = build_message(today)
    print(message)

    if os.getenv("DRY_RUN") == "1":
        return

    result = send_feishu_message(
        os.getenv("FEISHU_WEBHOOK_URL", ""),
        os.getenv("FEISHU_SECRET", ""),
        message,
    )
    print(result)


if __name__ == "__main__":
    main()
