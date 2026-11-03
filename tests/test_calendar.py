"""D02：交易日历测试（缓存/查询/回退/token 缺失结构化报错）。"""

import pandas as pd
import pytest

from quantdesk.core.calendar import TUSHARE_GUIDE, TradingCalendar, fetch_trade_cal
from quantdesk.core.errors import ConfigError

ROWS = [
    ("20260101", 0),  # 周四 元旦休市
    ("20260102", 1),  # 周五
    ("20260103", 0),  # 周六
    ("20260104", 0),  # 周日
    ("20260105", 1),  # 周一
    ("20260106", 1),  # 周二
]


@pytest.fixture()
def cal(tmp_path):
    c = TradingCalendar(tmp_path / "cal.db")
    c.upsert(ROWS)
    yield c
    c.close()


def test_is_open(cal):
    assert cal.is_open("20260102") is True
    assert cal.is_open("20260103") is False
    assert cal.is_open("20260201") is None  # 未缓存 → 未知


def test_prev_next_day(cal):
    assert cal.prev_day("20260105") == "20260102"
    assert cal.next_day("20260102") == "20260105"
    assert cal.prev_day("20260101") is None


def test_open_days_range(cal):
    assert cal.open_days("20260101", "20260106") == ["20260102", "20260105", "20260106"]


def test_upsert_idempotent(cal):
    cal.upsert([("20260102", 1)])  # 重复写不报错
    assert cal.is_open("20260102") is True


def test_ensure_range_covered_no_network(cal, monkeypatch):
    def boom(*_a, **_k):  # pragma: no cover  覆盖完整时不应触发网络
        raise AssertionError("不应外呼")

    monkeypatch.setattr("quantdesk.core.calendar.fetch_trade_cal", boom)
    assert cal.ensure_range("20260101", "20260106", token="x") is False


def test_ensure_range_fetches_when_missing(cal, monkeypatch):
    calls = []

    def fake_fetch(start, end, token):
        calls.append((start, end, token))
        return [("20260107", 1)]

    monkeypatch.setattr("quantdesk.core.calendar.fetch_trade_cal", fake_fetch)
    assert cal.ensure_range("20260101", "20260107", token="tok") is True
    assert calls and calls[0][2] == "tok"
    assert cal.is_open("20260107") is True


def test_ensure_range_no_token_raises_guidance(cal):
    with pytest.raises(ConfigError) as exc:
        cal.ensure_range("20260201", "20260228", token="")
    assert TUSHARE_GUIDE in str(exc.value)


def test_fallback_weekdays(cal):
    # 2026-02-07(周六)~2026-02-10(周二)：未缓存区间 → 工作日启发式
    days, fb = cal.open_days_with_fallback("2026-02-07", "2026-02-10")
    assert fb is True
    assert days == ["20260209", "20260210"]


def test_fetch_trade_cal_signature(monkeypatch):
    """fetch_trade_cal 走 tushare pro_api（此处仅验证参数与行转换）。"""

    class FakePro:
        def trade_cal(self, exchange, start_date, end_date):
            assert exchange == "SSE"
            assert start_date == "20240101" and end_date == "20240105"
            return pd.DataFrame({"cal_date": ["20240101", "20240102"], "is_open": [1, 0]})

    import tushare as ts

    monkeypatch.setattr(ts, "pro_api", lambda _t: FakePro())
    rows = fetch_trade_cal("2024-01-01", "2024-01-05", token="fake")
    assert rows == [("20240101", 1), ("20240102", 0)]
