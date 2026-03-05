from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

def kst_today_ymd() -> str:
    return datetime.now(tz=KST).strftime("%Y-%m-%d")

def xp_to_level(xp: int) -> int:
    return max(0, int(xp)) // 100
