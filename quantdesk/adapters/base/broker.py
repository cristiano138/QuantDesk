"""交易通道插件基类（宪法核心契约：签名一个不改）。

铁律（宪法适配器合同，所有实现必须遵守）：
1. 适配器只做"忠实执行 + 如实回报"，不做风控、不做确认——
   全部校验与二次确认在 engine/riskgate.py 的订单咽喉完成，
   适配器收到的 Order 一定是已确认、已校验的（order_id 由引擎预生成）。
2. 提交失败必须抛 BrokerError，禁止静默失败；
   断线必须抛 ConnectionLostError 且可被 health_check 探测；
   重连成功后必须自行重放订阅 / 刷新对账。
3. 下单价格必须是真实市场价（AdjustType.NONE 口径），禁止复权价。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from quantdesk.core.enums import OrderStatus
from quantdesk.core.models import Account, Order, Position, Trade


class BrokerAdapter(ABC):
    """交易通道插件基类。"""

    name: str = "base"
    is_paper: bool = False   # 模拟通道标记（paper_broker 为 True）

    # ---------- 生命周期 ----------
    @abstractmethod
    def connect(self, **credentials: str) -> None:
        """登录 / 建连。credentials 来自 config（账号、miniQMT 路径、token 等）。"""

    @abstractmethod
    def disconnect(self) -> None: ...

    # ---------- 交易 ----------
    @abstractmethod
    def place_order(self, order: Order) -> Order:
        """提交委托，同步返回补齐 broker_order_id 的最新快照。
        失败必须抛 BrokerError，不允许静默失败。"""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """发起撤单。返回是否受理；最终状态以订单回报为准。"""

    # ---------- 查询 ----------
    @abstractmethod
    def get_position(self, symbol: str | None = None) -> list[Position]:
        """当前持仓。available 必须遵守 T+1 语义（当日买入为 0）。"""

    @abstractmethod
    def get_account(self) -> Account: ...

    @abstractmethod
    def get_orders(self, status: OrderStatus | None = None) -> list[Order]:
        """当日委托列表。"""

    @abstractmethod
    def get_trades(self, start: datetime | None = None) -> list[Trade]:
        """当日成交（模拟通道从 SQLite 重放全部）。"""

    # ---------- 可选 ----------
    def poll(self) -> None:
        """无回调能力（如 easytrader）的通道实现轮询刷新；回调型通道留空。"""
