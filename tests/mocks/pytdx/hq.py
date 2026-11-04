"""假 pytdx.hq：``TdxHq_API`` 的可脚本化替身（离线单测唯一依赖）。

为什么需要它：pytdx 连的是通达信公开行情服务器（真机可连、CI 不可靠），而适配器要测的是
我们这层的逻辑——分页偏移、列名归一化、请求队列串行、快照合成 Tick、断线归一与重连重放。

本 mock 刻意扮演**非线程安全**的 SDK：同一实例被并发进入时在 ``SCRIPT.overlaps`` 计数，
用来证明适配器的「单工作线程串行消费请求队列」纪律真的成立（不是嘴上说说）。

用法（配合 ``tests.mocks.fake_sdk("pytdx")``）：

    import pytdx.hq as hq
    hq.SCRIPT.reset()
    hq.SCRIPT.bars = hq.make_bars(periods=10)
    hq.SCRIPT.fail_after = 2          # 第 2 次数据调用之后一直抛（模拟中途掉线）
"""

from __future__ import annotations

import threading
import time

import pandas as pd

DEFAULT_MARKET = 0
DEFAULT_CODE = "000001"


class _Script:
    """mock 的剧本与调用流水（模块级单例；fake_sdk 退出时随模块一起丢弃）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False
        self.reset()

    def reset(self) -> None:
        # —— 数据 ——
        self.bars: list[dict] = []
        self.bars_by_key: dict[tuple[int, int, str], list[dict]] = {}
        self.quotes: list[dict] | None = []
        self.transactions: list[dict] = []
        self.security_count = 1000
        # —— 连接行为 ——
        self.connect_error: Exception | None = None   # 非 None → connect() 直接抛
        self.connect_ok = True                        # False → connect() 返回假值
        self.connect_fails = 0                        # 前 N 次 connect 返回假值（试连换机用）
        self.connect_attempts: list[tuple[str, int]] = []
        self.instantiated = 0
        self.init_kwargs: dict = {}
        self.disconnects = 0
        # —— 调用流水与并发检测 ——
        self.calls: list[tuple[int, str, tuple]] = []  # (线程 id, 方法名, 实参)
        self.overlaps = 0                              # 并发进入同一实例的次数（应当恒为 0）
        self.slow = 0.005                              # 每次数据调用的停留时长（放大竞态窗口）
        # —— 故障注入 ——
        self.fail_methods: set[str] = set()            # 指定方法总是抛（如 {"get_security_count"}）
        self.fail_after = 0                            # >0：从第 N+1 次数据调用起持续抛
        self.data_calls = 0

    # ---------- 流水 ----------
    def record(self, method: str, args: tuple) -> int:
        tid = threading.get_ident()
        with self._lock:
            self.calls.append((tid, method, args))
        return tid

    def methods(self, method: str | None = None) -> list[str]:
        return [m for _, m, _ in self.calls if method is None or m == method]

    def thread_ids(self, method: str | None = None) -> set[int]:
        return {tid for tid, m, _ in self.calls if method is None or m == method}

    def args_of(self, method: str) -> list[tuple]:
        return [a for _, m, a in self.calls if m == method]

    def reset_calls(self) -> None:
        with self._lock:
            self.calls.clear()
            self.data_calls = 0
            self.overlaps = 0

    # ---------- 并发检测（模拟非线程安全 SDK）----------
    def enter(self) -> None:
        with self._lock:
            if self._active:
                self.overlaps += 1
            self._active = True

    def exit(self) -> None:
        with self._lock:
            self._active = False

    # ---------- 剧本查询 ----------
    def bars_for(self, category: int, market: int, code: str) -> list[dict]:
        return self.bars_by_key.get((category, market, code), self.bars)


SCRIPT = _Script()


def make_bars(*, category: int = 4, market: int = DEFAULT_MARKET, code: str = DEFAULT_CODE,
              periods: int = 20, start: str = "2024-01-02", base_price: float = 10.0,
              intraday: bool = False, segmented: bool = False,
              freq: str = "D") -> list[dict]:
    """生成 pytdx 风格的 bar 记录（**时间升序**，与真返回一致）。

    - ``intraday=False``：日线，``datetime`` 形如 "2024-01-02 15:00"（真 pytdx 日线带收盘时刻）；
    - ``intraday=True``：分钟线，``datetime`` 形如 "2024-01-02 09:35"；
    - ``segmented=True``：只给 year/month/day/hour/minute 字段（旧版返回形态，验证解析分支）。
    """
    out: list[dict] = []
    step = pd.Timedelta(minutes=5) if intraday else pd.Timedelta(days=1)
    if freq == "B":
        step = pd.tseries.offsets.BDay(1)
    t = pd.Timestamp(start)
    for i in range(periods):
        ts = t + i * step
        price = round(base_price + i * 0.1, 3)
        rec = {"open": price, "high": round(price + 0.2, 3), "low": round(price - 0.1, 3),
               "close": round(price + 0.05, 3),
               "vol": 12345.0 + i,                      # 单位：手（真实口径，不换算）
               "amount": round((12345.0 + i) * price * 100, 2),
               "year": int(ts.year), "month": int(ts.month), "day": int(ts.day),
               "hour": int(ts.hour), "minute": int(ts.minute)}
        if segmented:
            rec["datetime"] = "" if intraday else None  # 旧版没有 datetime 字段
        else:
            rec["datetime"] = f"{ts:%Y-%m-%d %H:%M}" if intraday else f"{ts:%Y-%m-%d} 15:00"
        out.append(rec)
    return out


def make_quotes(*, market: int = DEFAULT_MARKET, code: str = DEFAULT_CODE,
                price: float = 11.0, with_levels: bool = True) -> dict:
    """生成 pytdx get_security_quotes 的一条快照记录（字段名以真返回为准）。"""
    rec: dict = {"market": market, "code": code, "price": price,
                 "last_close": round(price - 0.1, 3), "open": round(price - 0.05, 3),
                 "high": round(price + 0.3, 3), "low": round(price - 0.4, 3),
                 "vol": 20000.0,                        # 单位：手
                 "amount": 220000.0, "servertime": "15:00:00.000"}
    if with_levels:
        for i in range(1, 6):
            rec[f"bid{i}"] = round(price - 0.01 * i, 3)
            rec[f"bid_vol{i}"] = 100.0 * i
            rec[f"ask{i}"] = round(price + 0.01 * i, 3)
            rec[f"ask_vol{i}"] = 200.0 * i
    return rec


def make_transactions(*, market: int = DEFAULT_MARKET, code: str = DEFAULT_CODE,
                      count: int = 5, start_time: str = "09:30:00",
                      date: str = "2024-01-02", with_datetime: bool = True) -> list[dict]:
    """生成 pytdx get_transaction_data 的分笔记录（真返回是**时间倒序**，这里照抄）。"""
    out: list[dict] = []
    base = pd.Timestamp(f"{date} {start_time}")
    for i in range(count):
        ts = base + pd.Timedelta(seconds=3 * i)
        rec = {"price": round(10.0 + i * 0.01, 3), "vol": 10.0 + i,     # 单位：手
               "num": 1 + i, "direction": "BUY" if i % 2 else "SELL"}
        if with_datetime:
            rec["datetime"] = f"{ts:%Y-%m-%d %H:%M:%S}"
        else:
            rec["time"] = f"{ts:%H:%M:%S}"
            rec["year"], rec["month"], rec["day"] = ts.year, ts.month, ts.day
        out.append(rec)
    return list(reversed(out))


class TdxHq_API:
    """假行情 API：按 ``SCRIPT`` 剧本返回数据、记录调用、注入故障。"""

    def __init__(self, heartbeat: bool = False, **kwargs) -> None:
        SCRIPT.instantiated += 1
        SCRIPT.init_kwargs = {"heartbeat": heartbeat, **kwargs}
        self._connected = False

    # ---------- 连接 ----------
    def connect(self, ip: str = "", port: int = 0, timeout=None) -> bool:
        SCRIPT.record("connect", (ip, port, timeout))
        SCRIPT.connect_attempts.append((ip, port))
        if SCRIPT.connect_error is not None:
            raise SCRIPT.connect_error
        if SCRIPT.connect_fails > 0:
            SCRIPT.connect_fails -= 1
            return False
        if not SCRIPT.connect_ok:
            return False
        self._connected = True
        return True

    def disconnect(self) -> None:
        SCRIPT.record("disconnect", ())
        SCRIPT.disconnects += 1
        self._connected = False

    # ---------- 探活 ----------
    def get_security_count(self, market: int = 0) -> int:
        SCRIPT.record("get_security_count", (market,))
        SCRIPT.enter()
        try:
            if "get_security_count" in SCRIPT.fail_methods:
                raise ConnectionError("mock pytdx: 探活失败")
            return int(SCRIPT.security_count)
        finally:
            SCRIPT.exit()

    # ---------- K 线 ----------
    def get_security_bars(self, category: int, market: int, code: str,
                          start: int, count: int) -> list[dict]:
        self._begin("get_security_bars", (category, market, code, start, count))
        try:
            all_bars = SCRIPT.bars_for(int(category), int(market), str(code))
            # 真 pytdx 语义：start 是「从最新往回数」的偏移，页内按时间升序返回
            right = len(all_bars) - int(start)
            left = max(0, right - int(count))
            return list(all_bars[left:max(0, right)])
        finally:
            SCRIPT.exit()

    # ---------- 快照 ----------
    def get_security_quotes(self, stock_list: list[tuple[int, str]]) -> list[dict] | None:
        self._begin("get_security_quotes", tuple(stock_list))
        try:
            if SCRIPT.quotes is None:
                return None                     # 真返回在无数据时给空/None
            wanted = {(int(m), str(c)) for m, c in stock_list}
            return [dict(q) for q in SCRIPT.quotes
                    if (int(q.get("market", 0)), str(q.get("code", ""))) in wanted]
        finally:
            SCRIPT.exit()

    # ---------- 分笔 ----------
    def get_transaction_data(self, market: int, code: str, start: int,
                            count: int) -> list[dict]:
        self._begin("get_transaction_data", (market, code, start, count))
        try:
            return list(SCRIPT.transactions)
        finally:
            SCRIPT.exit()

    # ---------- 内部 ----------
    def _begin(self, method: str, args: tuple) -> None:
        SCRIPT.record(method, args)
        SCRIPT.enter()
        SCRIPT.data_calls += 1
        if method in SCRIPT.fail_methods:
            SCRIPT.exit()
            raise ConnectionError(f"mock pytdx {method}: 注入的固定失败")
        if SCRIPT.fail_after and SCRIPT.data_calls > SCRIPT.fail_after:
            SCRIPT.exit()
            raise ConnectionError(f"mock pytdx {method}: 连接被远端关闭（fail_after 注入）")
        if SCRIPT.slow:
            time.sleep(SCRIPT.slow)
