from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


try:
    BEIJING_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    BEIJING_TZ = timezone(timedelta(hours=8))


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def today_beijing():
    return now_beijing().date()
