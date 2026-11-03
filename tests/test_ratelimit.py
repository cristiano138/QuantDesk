"""D04：限速器测试（最小间隔 + 0 间隔直通）。"""

import time

import pytest

from quantdesk.core.ratelimit import RateLimiter


def test_rate_limit_spacing():
    rl = RateLimiter(min_interval=0.03, jitter=0.0)
    t0 = time.monotonic()
    for _ in range(4):
        rl.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.09  # 3 个完整间隔


def test_zero_interval_no_sleep():
    rl = RateLimiter(min_interval=0.0)
    t0 = time.monotonic()
    for _ in range(100):
        rl.acquire()
    assert time.monotonic() - t0 < 0.5


def test_negative_interval_rejected():
    with pytest.raises(ValueError):
        RateLimiter(min_interval=-1)


def test_thread_safety():
    rl = RateLimiter(min_interval=0.01, jitter=0.0)
    import threading

    times: list[float] = []

    def worker():
        for _ in range(3):
            rl.acquire()
            times.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(times) == 9
