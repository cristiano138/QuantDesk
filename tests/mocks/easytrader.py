"""easytrader 假模块（tests/mocks）——离线复现同花顺「独立下单程序」的接口形态。

真实 easytrader 靠 pywinauto 点同花顺 xiadan.exe 的控件，CI/本机都没有客户端，
但适配器要验的是我们自己的代码：中文键归一、状态映射、串行执行、空委托号、
控件异常截图。这份假模块把 SDK 的**返回形态**（中文键 dict、字符串委托号、
控件类异常）照抄出来，并记录每次调用发生在哪个线程 id（串行执行器的断言依据）。

脚本化入口全在模块级 `state` 上：

    import easytrader as fake
    fake.state.balance = {"可用资金": 1000.0, ...}
    fake.state.errors["today_entrusts"] = fake.ElementNotFoundError("控件找不到")
    fake.state.order_buy_result = ""          # 未受理
"""

from __future__ import annotations

import base64
import threading
from pathlib import Path

__all__ = ["use", "state", "reset", "ElementNotFoundError", "UserError"]

# 1x1 透明 PNG：截图取证用例只验「文件真的落盘了」
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class ElementNotFoundError(Exception):
    """模拟 pywinauto 找不到控件（同花顺版本升级后的典型故障）。"""


class UserError(RuntimeError):
    """模拟非控件类故障（会话失效、资金不足被客户端拒绝等）。"""


class _State:
    """可脚本化的假客户端数据 + 调用记录。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        # —— 数据（中文键，与真实客户端一致）——
        self.balance: dict = {"可用资金": 100000.0, "参考市值": 50000.0, "股票市值": 50000.0,
                             "冻结市值": 0.0, "总资产": 150000.0}
        self.positions: list[dict] = []
        self.entrusts: list[dict] = []
        self.trades: list[dict] = []
        # —— 故障注入 ——
        self.connect_error: Exception | None = None
        self.errors: dict[str, Exception] = {}       # 方法名 → 抛该异常（一次性设置即可）
        self.window_capture_error: Exception | None = None
        # —— 下单返回值（真实 SDK 返回字符串委托号，空串表示未受理）——
        self.order_buy_result = "1"
        self.order_sell_result = "2"
        self.cancel_result = None
        # —— 调用记录 ——
        self.calls: list[tuple[str, int]] = []       # (方法名, 调用所在线程 id)
        self.kwargs: dict[str, dict] = {}            # 方法名 → 最近一次调用的入参
        self.client: str = ""
        self.connect_arg = ""


state = _State()


def _record(name: str, **kwargs) -> None:
    state.calls.append((name, threading.get_ident()))
    if kwargs:
        state.kwargs[name] = kwargs
    err = state.errors.get(name)
    if err is not None:
        raise err


class FakeImage:
    """pywinauto `capture_as_image()` 返回物的替身（有 save(path)）。"""

    def save(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_PNG_1PX)


class FakeWindow:
    """下单窗口替身。"""

    def capture_as_image(self):
        if state.window_capture_error is not None:
            raise state.window_capture_error
        return FakeImage()


class FakeUser:
    """easytrader.use("ths") 的返回值替身。"""

    def __init__(self) -> None:
        self.client = FakeWindow()      # 真实 SDK 里是 pywinauto 窗口对象

    # ---------- 生命周期 ----------
    def connect(self, exe_path: str = "", **kwargs) -> None:
        _record("connect", exe_path=exe_path, kwargs=kwargs)
        state.connect_arg = str(exe_path)
        if state.connect_error is not None:
            raise state.connect_error

    def exit(self) -> None:
        _record("exit")

    # ---------- 查询 ----------
    def stock_balance(self) -> dict:
        _record("stock_balance")
        return dict(state.balance)

    def stock_positions(self) -> list[dict]:
        _record("stock_positions")
        return [dict(r) for r in state.positions]

    def today_entrusts(self) -> list[dict]:
        _record("today_entrusts")
        return [dict(r) for r in state.entrusts]

    def today_trades(self) -> list[dict]:
        _record("today_trades")
        return [dict(r) for r in state.trades]

    # ---------- 交易 ----------
    def order_buy(self, symbol: str = "", price: float = 0.0, amount: int = 0,
                  **kwargs) -> str:
        _record("order_buy", symbol=symbol, price=price, amount=amount)
        return state.order_buy_result

    def order_sell(self, symbol: str = "", price: float = 0.0, amount: int = 0,
                   **kwargs) -> str:
        _record("order_sell", symbol=symbol, price=price, amount=amount)
        return state.order_sell_result

    def cancel_order(self, order=None, **kwargs):
        _record("cancel_order", order=order, kwargs=kwargs)
        return state.cancel_result

    def cancel_order_all(self):
        _record("cancel_order_all")
        return True


USER = FakeUser()


def use(client: str = "ths", **kwargs) -> FakeUser:
    """easytrader.use("ths") —— 返回同一个可脚本化的 user 对象。"""
    _record("use", client=client)
    state.client = client
    return USER


def reset() -> None:
    """用例内重置状态（`state` 是单例，适配器已持有的 user 对象会跟着读到新状态；
    模块本身在 fake_sdk 退出时会从 sys.modules 清掉，不泄漏给其他用例）。"""
    state.reset()
