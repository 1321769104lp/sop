import re
from datetime import date, timedelta


PROJECT_LINE_PATTERN = re.compile(r"(?:项目|片名|剧名)[:：]\s*(.+)")
MILESTONE_PATTERN = re.compile(r"(.+?)[（(]\s*(\d+)\s*天\s*[）)]\s*(\d{1,2})月(\d{1,2})日")
LEVEL_PATTERN = re.compile(r"([SAB])\s*级", re.IGNORECASE)
DELIVERY_PATTERN = re.compile(r"(?:交付|交付时间|交付日期|终审与交付).*?(\d{1,2})月(\d{1,2})日")


# 按用户提供的 SOP 图片整理。若只给项目名、等级、交付时间，就用这里倒推全部节点。
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


def parse_month_day(month: int, day: int, current_year: int, previous_date=None) -> tuple[date, int]:
    """解析月日，并在跨年时自动顺延年份。"""
    parsed = date(current_year, month, day)
    if previous_date and parsed < previous_date:
        current_year += 1
        parsed = date(current_year, month, day)
    return parsed, current_year


def extract_project_name(text: str, lines: list[str]) -> str:
    """识别项目名；优先读取“项目：xxx”，否则取第一行中较像项目名的内容。"""
    for line in lines:
        match = PROJECT_LINE_PATTERN.search(line)
        if match:
            return match.group(1).strip()

    for line in lines:
        clean = re.sub(r"制作时间[截节]点[:：]?", "", line).strip()
        clean = re.sub(r"[SAB]\s*级", "", clean, flags=re.IGNORECASE).strip(" ：:")
        if clean and not DELIVERY_PATTERN.search(clean):
            return clean
    return ""


def extract_level(text: str) -> str:
    """识别 S/A/B 等级。"""
    match = LEVEL_PATTERN.search(text)
    if not match:
        return ""
    return f"{match.group(1).upper()}级"


def extract_delivery_date(text: str, year: int) -> date | None:
    """识别交付日期。"""
    match = DELIVERY_PATTERN.search(text)
    if not match:
        return None
    return date(year, int(match.group(1)), int(match.group(2)))


def build_milestones_from_template(level: str, delivery_date: date) -> tuple[str, list[dict]]:
    """根据等级 SOP 和交付日期倒推出全部节点。"""
    template = SOP_TEMPLATES[level]
    total_days = sum(item["duration"] for item in template)
    start_date = delivery_date - timedelta(days=total_days - 1)

    milestones = []
    cursor = start_date
    for item in template:
        due_date = cursor + timedelta(days=item["duration"] - 1)
        milestones.append(
            {
                "name": item["name"],
                "duration": item["duration"],
                "due_date": due_date.strftime("%Y-%m-%d"),
                "raw": f"{item['name']}（{item['duration']}天）{due_date.strftime('%m月%d日')}",
            }
        )
        cursor = due_date + timedelta(days=1)

    return start_date.strftime("%Y-%m-%d"), milestones


def parse_explicit_milestones(lines: list[str], year: int) -> list[dict]:
    """解析用户直接给出的完整节点。"""
    current_year = year
    previous_date = None
    milestones = []

    for line in lines:
        match = MILESTONE_PATTERN.search(line)
        if not match:
            continue

        name = match.group(1).strip()
        duration = int(match.group(2))
        month = int(match.group(3))
        day = int(match.group(4))
        due_date, current_year = parse_month_day(month, day, current_year, previous_date)
        previous_date = due_date
        milestones.append(
            {
                "name": name,
                "duration": duration,
                "due_date": due_date.strftime("%Y-%m-%d"),
                "raw": line,
            }
        )

    return milestones


def parse_chinese_schedule_text(text: str, year: int | None = None) -> dict:
    """解析制作排期文本，支持完整节点，也支持按等级和交付时间自动倒推。"""
    base_year = year or date.today().year
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    project_name = extract_project_name(text, lines)
    project_level = extract_level(text)
    milestones = parse_explicit_milestones(lines, base_year)
    inferred_by_template = False

    if milestones:
        first_due_date = date.fromisoformat(milestones[0]["due_date"])
        start_date = first_due_date - timedelta(days=milestones[0]["duration"] - 1)
    else:
        delivery_date = extract_delivery_date(text, base_year)
        if not project_level:
            raise ValueError("没有识别到项目等级。请写明 S级、A级 或 B级。")
        if project_level not in SOP_TEMPLATES:
            raise ValueError("当前只支持 S级、A级、B级 自动倒推。")
        if not delivery_date:
            raise ValueError("没有识别到交付时间。请写明类似“交付时间：7月14日”。")
        start_text, milestones = build_milestones_from_template(project_level, delivery_date)
        start_date = date.fromisoformat(start_text)
        inferred_by_template = True

    if not project_name:
        raise ValueError("没有识别到项目名，请写明“项目：项目名”。")

    if not project_level:
        project_level = "自定义"

    source_text = "按等级和交付时间自动倒推：" if inferred_by_template else "自动识别自制作时间节点："
    remark_lines = [
        source_text,
        *[
            f"{item['name']}（{item['duration']}天）：{item['due_date']}"
            for item in milestones
        ],
    ]

    return {
        "project_name": project_name,
        "start_date": start_date.strftime("%Y-%m-%d"),
        "episodes": 30,
        "project_level": project_level,
        "owner": "",
        "status": "进行中",
        "remark": "\n".join(remark_lines),
        "milestones": milestones,
    }
