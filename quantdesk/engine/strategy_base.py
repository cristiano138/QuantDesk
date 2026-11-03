"""策略基类与上下文契约（D07）。

宪法同构保证的物理载体：策略文件只 import 本模块（BaseStrategy / StrategyContext）
与 core 类型，禁止 import 任何适配器。回测 / 模拟 / 实盘各有一个 StrategyContext 实现，
策略代码零修改跑通三种模式。

ctx 最小 API（T4 契约）：subscribe / history / buy / sell / get_position / log（+ now）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

import pandas as pd

from quantdesk.core.enums import Frequency
from quantdesk.core.models import Bar, Order, Position, Tick

__all__ = ["StrategyContext", "BaseStrategy"]


class StrategyContext(ABC):
    """策略唯一可见的世界。三种模式各一个实现，接口一致。"""

    @abstractmethod
    def subscribe(self, symbols: list[str]) -> None:
        """声明关注的标的（回测为记录性 no-op；实时模式驱动订阅）。"""

    @abstractmethod
    def history(self, symbol: str, freq: Frequency | str = Frequency.D1,
                bars: int = 120) -> pd.DataFrame:
        """截至当前时刻的本地历史（含当前 bar），HISTORY_COLUMNS，dt 升序。"""

    @abstractmethod
    def buy(self, symbol: str, volume: int, price: float | None = None) -> Order:
        """买入。price=None 为市价单；返回订单快照（状态以查询为准）。"""

    @abstractmethod
    def sell(self, symbol: str, volume: int, price: float | None = None) -> Order:
        """卖出。T+1 感知是策略责任：卖前应查 get_position().available。"""

    @abstractmethod
    def get_position(self, symbol: str) -> Position | None: ...

    @abstractmethod
    def log(self, message: str) -> None:
        """策略日志（进事件流/回测日志，不直接 print）。"""

    @abstractmethod
    def now(self) -> datetime:
        """当前时钟（回测=虚拟时钟；实时=真实时钟）。"""


class BaseStrategy:
    """策略基类。子类实现 on_bar / on_tick，只通过 self.ctx 与世界交互。

    params：类级默认参数表，实例化时被构造参数覆盖（同名键）。
    """

    name: str = "base"
    params: dict = {}

    def __init__(self, params: dict | None = None) -> None:
        self.params: dict = {**type(self).params, **(params or {})}
        self.ctx: StrategyContext | None = None

    # ---------- 生命周期（引擎调用） ----------
    def bind(self, ctx: StrategyContext) -> None:
        """引擎装配时注入上下文；策略代码不要自行调用。"""
        self.ctx = ctx

    def on_start(self) -> None:
        """启动钩子：在这里 subscribe / 预热。"""

    def on_bar(self, bar: Bar) -> None:
        """K 线驱动（回测与日线模拟的主路径）。"""

    def on_tick(self, tick: Tick) -> None:
        """tick 驱动（实时模拟/实盘路径）。"""

    def on_stop(self) -> None:
        """停止钩子。"""
