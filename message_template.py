from pathlib import Path


TEMPLATE_PATH = Path("data/feishu_message_template.txt")
DEFAULT_MESSAGE_TEMPLATE = """【今日项目提醒｜{date}】
{summary}

{separator}
{items}
"""


class SafeTemplateValues(dict):
    """未知占位符原样保留，避免用户模板写错后直接报错。"""

    def __missing__(self, key):
        return "{" + key + "}"


def load_message_template() -> str:
    if not TEMPLATE_PATH.exists():
        return DEFAULT_MESSAGE_TEMPLATE
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    return text if text.strip() else DEFAULT_MESSAGE_TEMPLATE


def save_message_template(text: str) -> None:
    TEMPLATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TEMPLATE_PATH.write_text(text.strip() + "\n", encoding="utf-8")


def reset_message_template() -> None:
    save_message_template(DEFAULT_MESSAGE_TEMPLATE)


def render_message_template(
    *,
    template: str,
    date_text: str,
    summary_text: str,
    items_text: str,
    separator: str = "━━━━━━━━━━━━━━",
) -> str:
    values = SafeTemplateValues(
        {
            "date": date_text,
            "summary": summary_text,
            "items": items_text,
            "project_items": items_text,
            "separator": separator,
            "title": f"今日项目提醒｜{date_text}",
        }
    )
    return template.format_map(values).strip()
