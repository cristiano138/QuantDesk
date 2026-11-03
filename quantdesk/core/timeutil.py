"""时间工具。

宪法工作流约定：时间戳统一存 INTEGER 毫秒（UTC），展示层再本地化。
"""

import time
from datetime import datetime, timezone


def now_ms() -> int:
    """当前 UTC 毫秒时间戳。"""
    return int(time.time() * 1000)


def ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def datetime_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)
