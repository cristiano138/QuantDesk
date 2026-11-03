"""核心数据模型（dataclass，宪法核心契约：字段不得删减）。

注意：实盘下单价格永远是原始市场价（不复权）；Position.available 表达 T+1 语义
（当日买入为 0，次日由交易日历驱动释放）。
"""

from dataclasses import dataclass, field
from datetime import datetime

from quantdesk.core.enums import Frequency, OrderStatus, OrderType, Side
from quantdesk.core.timeutil import now_ms


@dataclass
class Bar:
    """统一 K 线（存储列: dt/open/high/low/close/volume/amount，dt 升序）。"""

    symbol: str
    freq: Frequency | str
    dt: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float


@dataclass
class Tick:
    """实时快照，含五档盘口。"""

    symbol: str
    dt: datetime
    last: float
    open: float
    high: float
    low: float
    volume: float
    amount: float
    bids: list[tuple[float, float]] = field(default_factory=list)  # (price, vol) × 5
    asks: list[tuple[float, float]] = field(default_factory=list)  # (price, vol) × 5


@dataclass
class Order:
    """订单。order_id 由引擎（RiskGate）预生成，适配器只补 broker_order_id。"""

    order_id: str
    symbol: str
    side: Side | str
    price: float
    volume: int
    order_type: OrderType | str = OrderType.LIMIT
    strategy_id: str = "manual"
    status: OrderStatus | str = OrderStatus.PENDING_CONFIRM
    filled_volume: int = 0
    avg_filled_price: float = 0.0
    message: str = ""
    broker_order_id: str | None = None
    created_at: int = field(default_factory=now_ms)
    updated_at: int = field(default_factory=now_ms)


@dataclass
class Trade:
    """成交回报。"""

    trade_id: str
    order_id: str
    symbol: str
    side: Side | str
    price: float
    volume: int
    amount: float
    traded_at: int
    broker_trade_id: str | None = None


@dataclass
class Position:
    """持仓。available 遵守 T+1（当日买入为 0）。"""

    symbol: str
    volume: int
    available: int
    avg_cost: float
    market_value: float
    strategy_id: str = "manual"


@dataclass
class Account:
    """资金账户。"""

    total_asset: float
    cash: float
    market_value: float
    frozen: float = 0.0
    updated_at: int = field(default_factory=now_ms)
