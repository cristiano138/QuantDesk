"""xttrader 假实现（交易侧接口面 + 回报对象）。

覆盖 QmtBroker 用到的接口面：XtQuantTrader（start/connect/subscribe/order_stock/
cancel_order_stock/query_stock_*）与 XtQuantTraderCallback 的五个回报钩子。

故障注入（xtquant.SCRIPT）：
    connect_ret = -1              # trader.connect() 返回非 0 → 适配器应抛 ConnectionLostError
    subscribe_ret = -1            # 账户订阅失败
    order_stock_ret = -1          # 下单被拒 → 适配器应抛 BrokerError（禁止静默）
    order_stock_raises = True     # SDK 直接抛异常
    cancel_ret = False            # 撤单不受理
    fail = {"query_stock_asset": 1}   # 指定次数抛异常（探活/重试语义）

反向触发回报（模拟券商推送线程）：
    xttrader.fire("on_stock_order", XtOrder(...))
    xttrader.fire("on_disconnected")
"""

from __future__ import annotations

from xtquant import DATA, SCRIPT, TRADERS, fail, record

__all__ = ["XtQuantTrader", "XtQuantTraderCallback", "XtAsset", "XtPosition",
           "XtOrder", "XtTrade", "XtOrderError", "XtCancelError", "fire"]


class _Bag:
    """回报对象的公共底座：kwargs 直落属性（真实 SDK 也是纯数据类）。"""

    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)

    def __repr__(self) -> str:
        keys = ",".join(sorted(self.__dict__))
        return f"{self.__class__.__name__}<{keys}>"


class XtAsset(_Bag):
    """资金：total_asset / cash / market_value / frozen_cash。"""


class XtPosition(_Bag):
    """持仓：volume 是总量、can_use_volume 是可卖量（T+1 落点）。"""


class XtOrder(_Bag):
    """委托回报：order_id 为 QMT 内部整型编号，order_sysid 为柜台合同编号。"""


class XtTrade(_Bag):
    """成交回报。"""


class XtOrderError(_Bag):
    """委托错误回报（只有 order_id + 错误码/错误信息）。"""


class XtCancelError(_Bag):
    """撤单错误回报。"""


class XtQuantTraderCallback:
    """回调基类：真实 SDK 同名基类的方法集合（子类按需覆盖）。"""

    def on_disconnected(self):  # noqa: D102  基类留空
        pass

    def on_stock_order(self, order):
        pass

    def on_stock_trade(self, trade):
        pass

    def on_order_error(self, order_error):
        pass

    def on_cancel_error(self, cancel_error):
        pass

    def on_order_activity(self, order_activity):
        pass

    def on_account_status(self, status):
        pass


class XtQuantTrader:
    """交易会话对象。每次 connect 都应换一个新的 session_id（复用会报「已存在会话」）。"""

    def __init__(self, path, session, *args, **kwargs) -> None:
        self.path = path
        self.session = session
        self.callback: XtQuantTraderCallback | None = None
        self.subscribed: list[str] = []
        record("XtQuantTrader", (path, session), kwargs)
        TRADERS.append(self)

    # ---------- 生命周期 ----------
    def register_callback(self, callback) -> None:
        record("register_callback")
        self.callback = callback

    def start(self) -> None:
        record("start")
        if SCRIPT.get("start_raises"):
            raise RuntimeError("模拟故障：QMT 交易线程启动失败")

    def connect(self) -> int:
        record("connect")
        fail("connect")
        ret = int(SCRIPT.get("connect_ret", 0))
        DATA["connected"] = ret == 0
        return ret

    def stop(self) -> None:
        record("stop")
        DATA["connected"] = False

    # ---------- 账户 ----------
    def subscribe(self, account) -> int:
        record("subscribe", (getattr(account, "account_id", ""),))
        fail("subscribe")
        if int(SCRIPT.get("subscribe_ret", 0)) != 0:
            return int(SCRIPT["subscribe_ret"])
        self.subscribed.append(getattr(account, "account_id", ""))
        return 0

    def unsubscribe(self, account) -> int:
        record("unsubscribe", (getattr(account, "account_id", ""),))
        return 0

    # ---------- 交易 ----------
    def order_stock(self, account, stock_code, order_type, order_volume, price_type=0,
                    price=0.0, strategy_name="", order_remark="") -> int:
        record("order_stock", (getattr(account, "account_id", ""), stock_code, order_type,
                               order_volume, price_type, price, strategy_name, order_remark))
        fail("order_stock")
        if SCRIPT.get("order_stock_raises"):
            raise RuntimeError("模拟故障：xttrader 提交异常")
        return int(SCRIPT.get("order_stock_ret", 88001))

    def cancel_order_stock(self, account, order_id) -> bool:
        record("cancel_order_stock", (getattr(account, "account_id", ""), order_id))
        fail("cancel_order_stock")
        return bool(SCRIPT.get("cancel_ret", True))

    # ---------- 查询 ----------
    def query_stock_asset(self, account):
        record("query_stock_asset", (getattr(account, "account_id", ""),))
        fail("query_stock_asset")
        return DATA.get("asset")

    def query_stock_positions(self, account):
        record("query_stock_positions", (getattr(account, "account_id", ""),))
        fail("query_stock_positions")
        return list(DATA.get("positions") or [])

    def query_stock_orders(self, account, cancel_only=False):
        record("query_stock_orders", (getattr(account, "account_id", ""),),
               {"cancel_only": cancel_only})
        fail("query_stock_orders")
        return list(DATA.get("orders") or [])

    def query_stock_trades(self, account):
        record("query_stock_trades", (getattr(account, "account_id", ""),))
        fail("query_stock_trades")
        return list(DATA.get("trades") or [])


def fire(method: str, *args) -> int:
    """在「SDK 线程」上触发所有已注册回调的 method；返回触发数（0=没挂上回调）。"""
    fired = 0
    for trader in TRADERS:
        cb = trader.callback
        if cb is not None and hasattr(cb, method):
            getattr(cb, method)(*args)
            fired += 1
    return fired
