"""SQLite 表模型（宪法存储层）。

七张业务表：strategies / orders / trades / positions_snapshot / watchlist /
compliance / backtest_runs；另含 schema_version 与 trading_days（D02 日历共用）。
时间戳统一 INTEGER 毫秒（UTC）；orders.status 直接存 OrderStatus 枚举值
（含 pending_confirm / expired_confirm，为 D17 二次确认铺路）。
"""

from sqlmodel import Field, SQLModel

from quantdesk.core.timeutil import now_ms


class SchemaVersion(SQLModel, table=True):
    __tablename__ = "schema_version"

    id: int | None = Field(default=None, primary_key=True)
    version: int = 0
    applied_at: int = Field(default_factory=now_ms)


class StrategyRow(SQLModel, table=True):
    __tablename__ = "strategies"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    path: str
    params_json: str = "{}"
    enabled: bool = True
    created_at: int = Field(default_factory=now_ms)
    updated_at: int = Field(default_factory=now_ms)


class OrderRow(SQLModel, table=True):
    __tablename__ = "orders"

    order_id: str = Field(primary_key=True)
    broker_order_id: str | None = Field(default=None, index=True)
    strategy_id: str = Field(default="manual", index=True)
    symbol: str = Field(index=True)
    side: str
    price: float
    volume: int
    order_type: str
    status: str = Field(default="pending_confirm", index=True)
    filled_volume: int = 0
    avg_filled_price: float = 0.0
    message: str = ""
    created_at: int = 0
    updated_at: int = 0


class TradeRow(SQLModel, table=True):
    __tablename__ = "trades"

    trade_id: str = Field(primary_key=True)
    order_id: str = Field(index=True)
    broker_trade_id: str | None = Field(default=None, index=True)
    symbol: str = Field(index=True)
    side: str
    price: float
    volume: int
    amount: float
    traded_at: int = Field(index=True)


class PositionSnapshotRow(SQLModel, table=True):
    __tablename__ = "positions_snapshot"

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(index=True)
    volume: int
    available: int  # T+1：当日买入为 0
    avg_cost: float
    market_value: float
    strategy_id: str = Field(default="manual", index=True)
    snapshot_at: int = Field(index=True)


class WatchlistRow(SQLModel, table=True):
    __tablename__ = "watchlist"

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(unique=True)
    name: str = ""
    note: str = ""
    added_at: int = Field(default_factory=now_ms)


class ComplianceRow(SQLModel, table=True):
    __tablename__ = "compliance"

    id: int | None = Field(default=None, primary_key=True)
    item: str = Field(unique=True)
    category: str = "general"
    checked: bool = False
    note: str = ""
    updated_at: int = Field(default_factory=now_ms)


class BacktestRunRow(SQLModel, table=True):
    __tablename__ = "backtest_runs"

    id: int | None = Field(default=None, primary_key=True)
    strategy_name: str = Field(index=True)
    symbol: str
    freq: str
    start: str
    end: str
    params_json: str = "{}"
    status: str = Field(default="running", index=True)
    metrics_json: str = "{}"
    equity_json: str = ""
    remark: str = ""
    created_at: int = Field(default_factory=now_ms)
    finished_at: int | None = None


class TradingDayRow(SQLModel, table=True):
    __tablename__ = "trading_days"

    date: str = Field(primary_key=True)  # YYYYMMDD
    is_open: int
