from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

def kst_today_ymd() -> str:
    return datetime.now(tz=KST).strftime("%Y-%m-%d")

def xp_to_level(xp: int) -> int:
    return max(0, int(xp)) // 100


def kst_yesterday_ymd() -> str:
    from datetime import timedelta
    return (datetime.now(tz=KST) - timedelta(days=1)).strftime("%Y-%m-%d")
