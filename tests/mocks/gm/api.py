"""掘金 gm.api 假实现——脚本化行为 + 调用线程记录（离线单测专用）。

用 gm.api 顶层可见符号（set_token / Terminal / create_context + 常量），
以及本模块暴露给测试的脚本与记录钩子：

    import gm.api as gm
    gm.reset()
    gm.SCRIPT["place_code"] = 1001          # 下单返回错误码 → 适配器应抛 BrokerError
    gm.SCRIPT["terminal_fail"] = "未登录"    # Terminal 构造失败
    gm.CALLS                                # [{method, thread, args, kwargs, ret}]
    t = broker._session                      # 拿 Terminal 句柄注入回报
    t.simulate_execution_report({...})
"""

from __future__ import annotations

import threading

# ---------- 常量（贴合掘金，供适配器 getattr 命中；不一致时适配器回退自身默认） ----------
ORDER_BUY = 2
ORDER_SELL = 4
POSITION_LONG = 1
POSITION_SHORT = 2
ORDER_TYPE_LIMIT = "limit"
ORDER_TYPE_MARKET = "market"
PRICE_TYPE_LIMIT = "limit"
PRICE_TYPE_LATEST = "latest"
ORDER_DURATION_DAY = "day"

# ---------- 测试脚本与记录 ----------
SCRIPT: dict = {}
CALLS: list[dict] = []
_TOKEN: list[str] = []
_BLOCK: threading.Event | None = None    # 置位后 SDK 调用挂起，模拟终端线程卡死


def reset() -> None:
    global _BLOCK
    SCRIPT.clear()
    CALLS.clear()
    _TOKEN.clear()
    _BLOCK = None


def set_token(token: str) -> None:
    _TOKEN.append(token)
    if SCRIPT.get("set_token_fail"):
        raise RuntimeError(SCRIPT["set_token_fail"])


def last_token() -> str | None:
    return _TOKEN[-1] if _TOKEN else None


def _record(method: str, args, kwargs, ret):
    CALLS.append({"method": method, "thread": threading.get_ident(),
                  "args": args, "kwargs": kwargs, "ret": ret})
    return ret


def _maybe_block():
    """下单类调用可在脚本要求时挂起（用于验证调用方按超时抛错而非永久阻塞）。"""
    ev = SCRIPT.get("block_on")
    if ev is not None and isinstance(ev, threading.Event):
        ev.wait(timeout=SCRIPT.get("block_timeout", 10.0))


class _ApiBase:
    """新旧两版共享的调用实现：记录线程 + 返回脚本数据。"""

    def __init__(self, owner: "Terminal | None" = None) -> None:
        self._owner = owner

    def order_stock(self, **kw):
        _maybe_block()
        code = SCRIPT.get("place_code", 0)
        msg = SCRIPT.get("place_msg", "" if code == 0 else "下单被拒")
        if code == 0:
            data = {"order_id": SCRIPT.get("broker_order_id", "GM-BROKER-1"),
                    "client_order_id": kw.get("order_remark", "")}
            ret = {"status_code": 0, "status_msg": "", "data": data}
        else:
            ret = {"status_code": code, "status_msg": msg, "data": None}
        return _record("order_stock", (), kw, ret)

    def cancel_order(self, **kw):
        code = SCRIPT.get("cancel_code", 0)
        ret = {"status_code": code,
               "status_msg": SCRIPT.get("cancel_msg", "" if code == 0 else "不可撤")}
        return _record("cancel_order", (), kw, ret)

    def get_account_snapshot(self, **kw):
        _maybe_block()
        if SCRIPT.get("query_fail"):
            raise ConnectionError(SCRIPT["query_fail"])
        return _record("get_account_snapshot", (), kw,
                       SCRIPT.get("account", [{"balance": 100000.0, "available": 80000.0,
                                               "market_value": 20000.0, "frozen": 0.0}]))

    def get_position(self, **kw):
        _maybe_block()
        if SCRIPT.get("query_fail"):
            raise ConnectionError(SCRIPT["query_fail"])
        return _record("get_position", (), kw, SCRIPT.get(
            "positions", [{"symbol": "SHSE.600000", "volume": 1000,
                           "can_use_volume": 600, "vwap": 10.0, "market_value": 10500.0}]))

    def get_orders(self, **kw):
        _maybe_block()
        if SCRIPT.get("query_fail"):
            raise ConnectionError(SCRIPT["query_fail"])
        return _record("get_orders", (), kw, SCRIPT.get("orders", []))

    def get_execution_reports(self, **kw):
        _maybe_block()
        if SCRIPT.get("query_fail"):
            raise ConnectionError(SCRIPT["query_fail"])
        return _record("get_execution_reports", (), kw, SCRIPT.get("executions", []))

    def subscribe_quote(self, symbols, *a, **k):
        return _record("subscribe_quote", (symbols,), k, None)

    def unsubscribe_quote(self, symbols, *a, **k):
        return _record("unsubscribe_quote", (symbols,), k, None)


class Terminal(_ApiBase):
    """掘金新版接口对象：session == 命名空间（terminal.order_*/get_*），回调挂在自身。"""

    def __init__(self, token: str = "", account_id: str = "", endpoint: str = "",
                 mode: str = "") -> None:
        super().__init__(owner=self)
        _record("__init__", (), {"token_len": len(token), "account_id": account_id,
                                 "endpoint": endpoint, "mode": mode}, None)
        if SCRIPT.get("terminal_fail"):
            raise RuntimeError(SCRIPT["terminal_fail"])
        self.account_id = account_id
        self.endpoint = endpoint
        self.mode = mode
        self.on_execution_report = None
        self.on_order_status = None
        self.on_disconnected = None
        self._subscribed: list[str] = []

    # 测试注入钩子：模拟掘金推送线程回调（适配器只应入队，不在此线程广播）
    def simulate_execution_report(self, report: dict) -> None:
        if callable(self.on_execution_report):
            self.on_execution_report(report)

    def simulate_order_status(self, status: dict) -> None:
        if callable(self.on_order_status):
            self.on_order_status(status)

    def simulate_disconnected(self, reason: str = "终端断开") -> None:
        if callable(self.on_disconnected):
            self.on_disconnected(reason)


class _Rest:
    def __init__(self, api: _ApiBase) -> None:
        self.api = api


class _Context(_ApiBase):
    """掘金旧版接口对象：命名空间在 context.rest.api，回调挂在 context 自身。"""

    def __init__(self) -> None:
        super().__init__()
        self.rest = _Rest(self)
        self.on_execution_report = None
        self.on_order_status = None
        self.on_disconnected = None

    def simulate_execution_report(self, report: dict) -> None:
        if callable(self.on_execution_report):
            self.on_execution_report(report)

    def simulate_order_status(self, status: dict) -> None:
        if callable(self.on_order_status):
            self.on_order_status(status)

    def simulate_disconnected(self, reason: str = "终端断开") -> None:
        if callable(self.on_disconnected):
            self.on_disconnected(reason)


def create_context(*a, **k) -> _Context:
    _record("create_context", a, k, None)
    if SCRIPT.get("context_fail"):
        raise RuntimeError(SCRIPT["context_fail"])
    return _Context()
