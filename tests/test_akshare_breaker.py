"""D04 补充：AKShare 限流熔断测试（封禁期内不外呼，防重试风暴延长封禁）。"""

import pytest

from quantdesk.adapters.data.akshare_feed import AkshareFeed
from quantdesk.core.enums import AdjustType, Frequency
from quantdesk.core.errors import ConnectionLostError


class Push2RateLimitError(Exception):
    """模拟 akshare 的限流异常（类名含 RateLimit 即被识别）。"""


def test_rate_limit_trips_breaker_without_retry():
    feed = AkshareFeed(min_interval=0.0)
    calls = {"n": 0}

    def banned():
        calls["n"] += 1
        raise Push2RateLimitError("IP 被临时限流")

    with pytest.raises(ConnectionLostError):
        feed._call(banned)
    assert calls["n"] == 1  # 限流类异常不重试（重试会延长封禁）


def test_breaker_blocks_subsequent_calls():
    feed = AkshareFeed(min_interval=0.0)
    calls = {"n": 0}

    def banned():
        calls["n"] += 1
        raise Push2RateLimitError("限流")

    with pytest.raises(ConnectionLostError):
        feed._call(banned)
    with pytest.raises(ConnectionLostError) as exc:
        feed._call(lambda: "never")
    assert calls["n"] == 1          # 冷却期内不再触网
    assert "冷却" in str(exc.value)


def test_non_rate_limit_error_still_retries_once():
    feed = AkshareFeed(min_interval=0.0)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("瞬时抖动")
        return "ok"

    assert feed._call(flaky) == "ok"
    assert calls["n"] == 2


def test_cooldown_grows_on_repeated_trips():
    feed = AkshareFeed(min_interval=0.0)
    assert feed._cooldown == 300.0
    feed._trip()
    assert feed._cooldown == 900.0
    feed._trip()
    assert feed._cooldown == 1800.0
    feed._trip()
    assert feed._cooldown == 1800.0  # 封顶


def test_success_resets_cooldown():
    feed = AkshareFeed(min_interval=0.0)
    feed._trip()
    feed._reset_cooldown()
    assert feed._cooldown == 300.0
    assert feed._blocked_until == 0.0


def test_real_get_history_during_cooldown(tmp_path, monkeypatch):
    """get_history 在冷却期内直接抛 ConnectionLostError（不触网）。"""
    feed = AkshareFeed(min_interval=0.0)
    feed._trip()

    def boom(*_a, **_k):  # pragma: no cover 不应触网
        raise AssertionError("冷却期内不应触网")

    monkeypatch.setattr(AkshareFeed, "_ak", staticmethod(boom))
    with pytest.raises(ConnectionLostError):
        feed.get_history("000001.SZ", Frequency.D1, "2024-01-01", "2024-01-31",
                         AdjustType.NONE)
