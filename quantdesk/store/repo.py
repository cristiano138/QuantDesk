"""dataclass ↔ DB 互转与基础 CRUD。

- 全部写操作走模块级单写锁（WAL 单写多读，宪法第 4 条配套）；
- 订单/成交/持仓先落库后广播的「落库」半边在这里（广播在 services/engine 侧）。
"""

import json
import threading
from datetime import datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy import func
from sqlmodel import Session, select

from quantdesk.core.enums import OrderStatus, OrderType, Side
from quantdesk.core.models import Order, Position, Trade
from quantdesk.core.timeutil import now_ms
from quantdesk.store.db import get_session
from quantdesk.store.models_db import (
    BacktestRunRow,
    ComplianceRow,
    OrderRow,
    PositionSnapshotRow,
    TradeRow,
    WatchlistRow,
)

_write_lock = threading.RLock()


def _side_str(v: Side | str) -> str:
    return v.value if isinstance(v, Side) else str(v)


def _otype_str(v: OrderType | str) -> str:
    return v.value if isinstance(v, OrderType) else str(v)


def _status_str(v: OrderStatus | str) -> str:
    return v.value if isinstance(v, OrderStatus) else str(v)


def _enum_side(v: str) -> Side:
    return Side(v)


# ---------- 订单 ----------
def _order_to_row(o: Order) -> OrderRow:
    return OrderRow(
        order_id=o.order_id,
        broker_order_id=o.broker_order_id,
        strategy_id=o.strategy_id,
        symbol=o.symbol,
        side=_side_str(o.side),
        price=o.price,
        volume=o.volume,
        order_type=_otype_str(o.order_type),
        status=_status_str(o.status),
        filled_volume=o.filled_volume,
        avg_filled_price=o.avg_filled_price,
        message=o.message,
        created_at=o.created_at,
        updated_at=o.updated_at,
    )


def _row_to_order(r: OrderRow) -> Order:
    return Order(
        order_id=r.order_id,
        broker_order_id=r.broker_order_id,
        strategy_id=r.strategy_id,
        symbol=r.symbol,
        side=Side(r.side),
        price=r.price,
        volume=r.volume,
        order_type=OrderType(r.order_type),
        status=OrderStatus(r.status),
        filled_volume=r.filled_volume,
        avg_filled_price=r.avg_filled_price,
        message=r.message,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def upsert_order(order: Order, db_path=None) -> None:
    with _write_lock, get_session(db_path) as s:
        row = s.get(OrderRow, order.order_id)
        if row is None:
            s.add(_order_to_row(order))
        else:
            fresh = _order_to_row(order)
            for k in ("broker_order_id", "strategy_id", "symbol", "side", "price",
                      "volume", "order_type", "status", "filled_volume",
                      "avg_filled_price", "message", "created_at", "updated_at"):
                setattr(row, k, getattr(fresh, k))
        s.commit()


def get_order(order_id: str, db_path=None) -> Order | None:
    with get_session(db_path) as s:
        row = s.get(OrderRow, order_id)
        return _row_to_order(row) if row else None


def list_orders(status: OrderStatus | str | None = None, strategy_id: str | None = None,
                limit: int = 1000, db_path=None) -> list[Order]:
    with get_session(db_path) as s:
        stmt = select(OrderRow)
        if status is not None:
            stmt = stmt.where(OrderRow.status == _status_str(status))
        if strategy_id is not None:
            stmt = stmt.where(OrderRow.strategy_id == strategy_id)
        rows = s.exec(stmt.order_by(OrderRow.created_at.desc()).limit(limit)).all()
        return [_row_to_order(r) for r in rows]


# ---------- 成交 ----------
def _trade_to_row(t: Trade) -> TradeRow:
    return TradeRow(
        trade_id=t.trade_id,
        order_id=t.order_id,
        broker_trade_id=t.broker_trade_id,
        symbol=t.symbol,
        side=_side_str(t.side),
        price=t.price,
        volume=t.volume,
        amount=t.amount,
        traded_at=t.traded_at,
    )


def _row_to_trade(r: TradeRow) -> Trade:
    return Trade(
        trade_id=r.trade_id,
        order_id=r.order_id,
        broker_trade_id=r.broker_trade_id,
        symbol=r.symbol,
        side=Side(r.side),
        price=r.price,
        volume=r.volume,
        amount=r.amount,
        traded_at=r.traded_at,
    )


def append_trade(trade: Trade, db_path=None) -> None:
    with _write_lock, get_session(db_path) as s:
        if s.get(TradeRow, trade.trade_id) is None:
            s.add(_trade_to_row(trade))
        s.commit()


def list_trades(start_ms: int | None = None, limit: int = 1000, db_path=None) -> list[Trade]:
    with get_session(db_path) as s:
        stmt = select(TradeRow)
        if start_ms is not None:
            stmt = stmt.where(TradeRow.traded_at >= start_ms)
        rows = s.exec(stmt.order_by(TradeRow.traded_at.desc()).limit(limit)).all()
        return [_row_to_trade(r) for r in rows]


# ---------- 持仓快照 ----------
def save_positions_snapshot(positions: list[Position], snapshot_at: int | None = None,
                            db_path=None) -> int:
    """全量快照（W1 简化：整表替换，snapshot_at 统一）。"""
    ts = snapshot_at if snapshot_at is not None else now_ms()
    with _write_lock, get_session(db_path) as s:
        s.exec(sa_delete(PositionSnapshotRow))
        for p in positions:
            s.add(PositionSnapshotRow(
                symbol=p.symbol, volume=p.volume, available=p.available,
                avg_cost=p.avg_cost, market_value=p.market_value,
                strategy_id=p.strategy_id, snapshot_at=ts,
            ))
        s.commit()
        return len(positions)


def latest_positions(db_path=None) -> list[Position]:
    with get_session(db_path) as s:
        max_ts = s.exec(select(func.max(PositionSnapshotRow.snapshot_at))).one()
        if max_ts is None:
            return []
        rows = s.exec(
            select(PositionSnapshotRow).where(PositionSnapshotRow.snapshot_at == max_ts)
        ).all()
        return [Position(
            symbol=r.symbol, volume=r.volume, available=r.available,
            avg_cost=r.avg_cost, market_value=r.market_value, strategy_id=r.strategy_id,
        ) for r in rows]


# ---------- 自选股 ----------
def watchlist_add(symbol: str, name: str = "", note: str = "", db_path=None) -> None:
    with _write_lock, get_session(db_path) as s:
        row = s.exec(select(WatchlistRow).where(WatchlistRow.symbol == symbol)).first()
        if row is None:
            s.add(WatchlistRow(symbol=symbol, name=name, note=note))
        else:
            row.name, row.note = name or row.name, note or row.note
        s.commit()


def watchlist_remove(symbol: str, db_path=None) -> bool:
    with _write_lock, get_session(db_path) as s:
        row = s.exec(select(WatchlistRow).where(WatchlistRow.symbol == symbol)).first()
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def watchlist_list(db_path=None) -> list[WatchlistRow]:
    with get_session(db_path) as s:
        return list(s.exec(select(WatchlistRow).order_by(WatchlistRow.added_at)).all())


# ---------- 合规清单 ----------
def compliance_upsert(item: str, category: str = "general", checked: bool = False,
                      note: str = "", db_path=None) -> None:
    with _write_lock, get_session(db_path) as s:
        row = s.exec(select(ComplianceRow).where(ComplianceRow.item == item)).first()
        if row is None:
            s.add(ComplianceRow(item=item, category=category, checked=checked, note=note))
        else:
            row.category, row.checked = category, checked
            if note:
                row.note = note
            row.updated_at = now_ms()
        s.commit()


def compliance_list(db_path=None) -> list[ComplianceRow]:
    with get_session(db_path) as s:
        return list(s.exec(select(ComplianceRow).order_by(ComplianceRow.id)).all())


# ---------- 回测运行 ----------
def backtest_create(strategy_name: str, symbol: str, freq: str, start: str, end: str,
                    params_json: str = "{}", db_path=None) -> int:
    with _write_lock, get_session(db_path) as s:
        row = BacktestRunRow(strategy_name=strategy_name, symbol=symbol, freq=freq,
                             start=start, end=end, params_json=params_json)
        s.add(row)
        s.commit()
        s.refresh(row)
        return int(row.id)


def backtest_finish(run_id: int, metrics_json: str, equity_json: str = "",
                    remark: str = "", status: str = "completed", db_path=None) -> None:
    with _write_lock, get_session(db_path) as s:
        row = s.get(BacktestRunRow, run_id)
        if row is not None:
            row.metrics_json = metrics_json
            row.equity_json = equity_json
            row.remark = remark
            row.status = status
            row.finished_at = now_ms()
            s.commit()


def backtest_get(run_id: int, db_path=None) -> BacktestRunRow | None:
    with get_session(db_path) as s:
        return s.get(BacktestRunRow, run_id)


# ---------- 交易日（SQLModel 侧，与 core/calendar 共表） ----------
def trading_days_upsert(rows: list[tuple[str, int]], db_path=None) -> None:
    from quantdesk.store.models_db import TradingDayRow

    with _write_lock, get_session(db_path) as s:
        for d, is_open in rows:
            row = s.get(TradingDayRow, d)
            if row is None:
                s.add(TradingDayRow(date=d, is_open=is_open))
            else:
                row.is_open = is_open
        s.commit()


def trading_days_open(db_path=None) -> set[str]:
    from sqlmodel import select as _select
    from quantdesk.store.models_db import TradingDayRow

    with get_session(db_path) as s:
        rows = s.exec(_select(TradingDayRow).where(TradingDayRow.is_open == 1)).all()
        return {r.date for r in rows}


# ---------- 账户状态（D09） ----------
def account_get(db_path=None) -> "AccountStateRow | None":
    from quantdesk.store.models_db import AccountStateRow

    with get_session(db_path) as s:
        return s.get(AccountStateRow, 1)


def account_set(cash: float, initial_cash: float, db_path=None) -> None:
    from quantdesk.store.models_db import AccountStateRow

    with _write_lock, get_session(db_path) as s:
        row = s.get(AccountStateRow, 1)
        if row is None:
            row = AccountStateRow(id=1, cash=cash, initial_cash=initial_cash)
            s.add(row)
        else:
            row.cash = cash
            row.initial_cash = initial_cash
        row.updated_at = now_ms()
        s.commit()
