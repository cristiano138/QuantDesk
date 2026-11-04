"""D04：限速器测试（最小间隔 + 0 间隔直通）。"""

import threading
import time

import pytest

from quantdesk.core.ratelimit import RateLimiter


def test_rate_limit_spacing():
    rl = RateLimiter(min_interval=0.03, jitter=0.0)
    times: list[float] = []
    for _ in range(4):
        rl.acquire()
        times.append(time.monotonic())
    # 间隔契约：相邻放行间隔 >= min_interval（留 5ms 计时/调度容差，Windows 睡眠粒度）
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(g >= 0.025 for g in gaps), gaps
    assert times[-1] - times[0] >= 0.085  # 3 个完整间隔（含容差）


def test_spacing_is_not_eaten_by_late_wakeups():
    """回归锁：睡眠迟到时，欠下的时间要往后顺延，不能被当成"已经等过了"。

    旧实现在锁外睡觉、并提前排好下一班，Windows 15.6ms 粒度一迟到就会出现
    min_interval=0.03 却只隔 0.015s 的放行——限速漏了，公开接口会把本机打进冷却。
    """
    n, step = 12, 0.02
    rl = RateLimiter(min_interval=step, jitter=0.0)
    times = []
    for _ in range(n):
        rl.acquire()
        times.append(time.monotonic())
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert min(gaps) >= step - 0.003, [round(g, 4) for g in gaps]
    assert times[-1] - times[0] >= (n - 1) * step - 0.005


def test_concurrent_callers_stay_serialised():
    """多线程一起抢：任意两次放行的间隔都不能小于 min_interval 的一半（并发测量有噪声，
    这里要抓住的是"塌方式并发"——旧实现迟到唤醒时会一次放行好几个）。"""
    step = 0.02
    rl = RateLimiter(min_interval=step, jitter=0.0)
    times: list[float] = []
    lock = threading.Lock()

    def worker():
        for _ in range(4):
            rl.acquire()
            with lock:
                times.append(time.monotonic())

    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    ordered = sorted(times)
    assert len(ordered) == 16
    gaps = [b - a for a, b in zip(ordered, ordered[1:])]
    assert min(gaps) >= step * 0.5, [round(g, 4) for g in gaps]


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
