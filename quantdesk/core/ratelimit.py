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
        self._last_t: float | None = None      # 上一次"真正放行"的时刻

    def acquire(self) -> float:
        """阻塞直到放行，返回排队等待秒数（不含抖动）。

        整段持锁睡到点，并把间隔基准记成实际放行时刻。早先的写法在锁外睡觉、
        并提前把下一班排到 `_next_t`，于是睡眠迟到（Windows 粒度约 15.6ms）那段
        会被当成"已经等过了"：实测 min_interval=0.03 时相邻两次放行只隔 0.015s，
        限速形同虚设——东财/腾讯这类公开接口会把本机直接打进冷却。
        """
        with self._lock:
            now = time.monotonic()
            wait = 0.0 if self._last_t is None else \
                max(0.0, self._last_t + self.min_interval - now)
            if wait > 0:
                deadline = now + wait
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(remaining)
            if self.min_interval > 0 and self.jitter > 0:
                time.sleep(self.min_interval * self.jitter * random.random())
            self._last_t = time.monotonic()
            return wait
