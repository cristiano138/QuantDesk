"""限速工具：最小间隔 + 随机抖动（防重试风暴/反爬封禁，宪法 C6/D04）。"""

import random
import threading
import time


class RateLimiter:
    """线程安全的最小间隔限速器。

    acquire() 阻塞到下一次放行；两次放行间隔 >= min_interval，
    并附加 [0, jitter×min_interval) 的均匀随机抖动。
    min_interval=0 时不限速（单测/离线使用）。
    """

    def __init__(self, min_interval: float, jitter: float = 0.2):
        if min_interval < 0:
            raise ValueError("min_interval 必须 >= 0")
        self.min_interval = float(min_interval)
        self.jitter = float(jitter)
        self._lock = threading.Lock()
        self._next_t = 0.0

    def acquire(self) -> float:
        """阻塞直到放行，返回排队等待秒数（不含抖动）。"""
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_t - now)
            self._next_t = max(now, self._next_t) + self.min_interval
        if wait > 0:
            # 睡眠可能被 OS 调度提前唤醒/延迟进入：循环补齐到目标时刻，
            # 保证实际放行间隔不小于 min_interval（防限速器失效触发反爬）
            target = time.monotonic() + wait
            while True:
                remaining = target - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(remaining)
        if self.min_interval > 0 and self.jitter > 0:
            time.sleep(self.min_interval * self.jitter * random.random())
        return wait
