"""xtquant（miniQMT/QMT）假 SDK —— 离线单测专用，行为可脚本化 + 全量调用记录。

真实 xtquant 由券商 QMT 客户端分发，且必须有登录中的客户端才拿得到数据与交易会话，
CI 与本机离线环境都装不出来。但适配器要验的是**我们自己的代码**（列名归一化、参数映射、
状态机、回调只入队、断线 fail-closed、重连重放），所以本包按真实接口面复刻一层壳：

- `DATA`     : 各接口的返回值（历史 K 线、full tick、板块列表、资金/持仓/委托/成交）
- `SCRIPT`   : 故障与行为注入（连接返回码、下单返回 -1、抛异常、拒绝某复权口径…）
- `CALLS`    : 调用流水 `{"method", "thread", "args", "kwargs"}` —— thread 是**调用线程名**，
               用于断言「SDK 回调线程只入队、归一化在工作线程」
- `SUBSCRIPTIONS` / `TRADERS` : 订阅句柄表与 trader 实例表，供测试反向触发回调

用法（配 tests/mocks.fake_sdk）：

    from tests.mocks import fake_sdk

    with fake_sdk("xtquant"):
        from xtquant import xtdata, xttrader
        import xtquant
        xtquant.SCRIPT["connect_ret"] = -1
        xtquant.DATA["history"]["600000.SH"] = df
        xtdata.fire_quote("600000.SH", "tick", payload)
        xttrader.fire("on_disconnected")

接口面与字段名都取「我们代码实际读到的那些」，真实取值仍需在线核对
（见 docs/integration/qmt.md 的在线核对清单）。
"""

from __future__ import annotations

import threading

__all__ = ["SCRIPT", "CALLS", "DATA", "SUBSCRIPTIONS", "TRADERS", "reset"]

# ---------- 测试脚本（故障/返回值开关） ----------
SCRIPT: dict = {}
# ---------- 调用流水 ----------
CALLS: list[dict] = []
# ---------- 接口返回数据 ----------
DATA: dict = {}
# ---------- 运行时对象表（订阅句柄、trader 实例） ----------
SUBSCRIPTIONS: dict = {}
TRADERS: list = []

_SEQ = [1000]

_DEFAULTS: dict = {
    "sector_list": ["600000.SH", "000001.SZ"],
    "history": {},          # code → DataFrame（index 为 'YYYYMMDD'/'YYYYMMDDHHMMSS' 字符串）
    "full_tick": {},        # code → get_full_tick 口径的单票快照 dict
    "downloaded": [],       # download_history_data 调用记录
    "asset": None,          # XtAsset
    "positions": [],        # [XtPosition]
    "orders": [],           # [XtOrder]
    "trades": [],           # [XtTrade]
    "connected": False,     # trader.connect() 最近一次是否成功
}


def _fresh_defaults() -> dict:
    out = {}
    for k, v in _DEFAULTS.items():
        if isinstance(v, dict):
            out[k] = dict(v)
        elif isinstance(v, list):
            out[k] = list(v)
        else:
            out[k] = v                      # None / bool 等标量直接取
    return out


def reset() -> None:
    """回到干净状态（fake_sdk 每次进入都会重装模块，本函数供用例内二次清理）。"""
    SCRIPT.clear()
    CALLS.clear()
    SUBSCRIPTIONS.clear()
    TRADERS.clear()
    _SEQ[0] = 1000
    DATA.clear()
    DATA.update(_fresh_defaults())


def next_seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def record(method: str, args: tuple = (), kwargs: dict | None = None) -> None:
    """记一笔调用（含调用线程名：断言线程纪律的关键证据）。"""
    CALLS.append({"method": method, "thread": threading.current_thread().name,
                  "args": tuple(args), "kwargs": dict(kwargs or {})})


def find(method: str) -> dict | None:
    for c in CALLS:
        if c["method"] == method:
            return c
    return None


def calls_of(method: str) -> list[dict]:
    return [c for c in CALLS if c["method"] == method]


def fail(method: str) -> None:
    """按 SCRIPT["fail"][method] 的剩余次数抛异常（用于「最多重试 1 次」的用例）。"""
    table = SCRIPT.get("fail") or {}
    n = int(table.get(method, 0))
    if n > 0:
        table[method] = n - 1
        SCRIPT["fail"] = table
        raise RuntimeError(f"模拟 SDK 故障：{method}")


# 导入即干净状态：假 SDK 不存在「残留上一用例数据」的可能
reset()
