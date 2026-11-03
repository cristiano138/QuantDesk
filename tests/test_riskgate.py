"""D16：RiskGate 闸② A 股规则校验测试（≥12 边界用例，含复权价兜底拦截）。"""

from datetime import datetime

import pandas as pd
import pytest

from quantdesk.adapters.brokers.paper_broker import PaperBroker
from quantdesk.core.enums import AdjustType, Frequency, OrderStatus, Side
from quantdesk.core.models import Order, Tick
from quantdesk.core.symbols import set_st_names
from quantdesk.engine.riskgate import RiskGate
from quantdesk.store.db import init_db
from quantdesk.store.parquet_store import HISTORY_COLUMNS, ParquetStore

SYM = "600000.SH"
PREV_CLOSE = 10.00          # 昨收原始价 → 主板涨跌停 [9.00, 11.00]


class StubSettings:
    def __init__(self, simulation_only=True):
        self.simulation_only = simulation_only


def mk_store(tmp_path) -> ParquetStore:
    store = ParquetStore(root=tmp_path / "parquet")
    dt = pd.Timestamp("2026-11-03")            # 昨日日线
    row = [{"dt": dt, "open": PREV_CLOSE, "high": PREV_CLOSE, "low": PREV_CLOSE,
            "close": PREV_CLOSE, "volume": 10000.0, "amount": PREV_CLOSE * 10000}]
    store.write(SYM, Frequency.D1, pd.DataFrame(row)[HISTORY_COLUMNS], AdjustType.NONE)
    store.write("300001.SZ", Frequency.D1, pd.DataFrame(row)[HISTORY_COLUMNS],
                AdjustType.NONE)
    store.write("688001.SH", Frequency.D1, pd.DataFrame(row)[HISTORY_COLUMNS],
                AdjustType.NONE)
    return store


def mk_gate(tmp_path, sim=True, max_position_value=1_000_000.0, max_daily_orders=200,
            initial_cash=100_000.0):
    init_db(tmp_path / "g.db")
    broker = PaperBroker(initial_cash=initial_cash, db_path=tmp_path / "g.db")
    broker.connect()
    broker.on_tick(Tick(symbol=SYM, dt=datetime(2026, 11, 4, 9, 31), last=10.0,
                        open=10.0, high=10.0, low=10.0, volume=10000, amount=100000,
                        bids=[(9.98, 500)], asks=[(10.02, 500)]))
    gate = RiskGate(paper_broker=broker, live_broker=None,
                    settings=StubSettings(sim), db_path=tmp_path / "g.db",
                    store=mk_store(tmp_path), max_position_value=max_position_value,
                    max_daily_orders=max_daily_orders)
    return gate, broker


def mk_order(side=Side.BUY, price=10.0, volume=500, symbol=SYM,
             otype="limit", oid="T1"):
    return Order(order_id=oid, symbol=symbol, side=side, price=price,
                 volume=volume, order_type=otype, status="pending_confirm")


# ---------- 整手 ----------

def test_buy_must_be_lot_multiple(tmp_path):
    gate, _ = mk_gate(tmp_path)
    v = gate.validate(mk_order(volume=150))
    assert any("100 股整数倍" in x for x in v)


def test_buy_lot_ok(tmp_path):
    gate, _ = mk_gate(tmp_path)
    assert gate.validate(mk_order(volume=200)) == []


def test_star_board_min_200(tmp_path):
    gate, _ = mk_gate(tmp_path)
    v = gate.validate(mk_order(symbol="688001.SH", volume=100))
    assert any("科创板" in x for x in v)
    # ≥200 后 1 股递增：201 合法
    assert gate.validate(mk_order(symbol="688001.SH", volume=201)) == []


def test_sell_odd_lot_allowed(tmp_path):
    """卖出允许零股（A 股卖出可非整手，如送股/配股产生）。"""
    gate, broker = mk_gate(tmp_path)
    broker.on_new_trading_day()
    broker.book._entry(SYM).update({"volume": 550, "available": 550})
    assert gate.validate(mk_order(Side.SELL, price=10.0, volume=30)) == []


# ---------- T+1 ----------

def test_sell_beyond_available_rejected(tmp_path):
    gate, _ = mk_gate(tmp_path)                    # 无持仓
    v = gate.validate(mk_order(Side.SELL, price=10.0, volume=100))
    assert any("T+1" in x for x in v)


def test_sell_within_available_ok(tmp_path):
    gate, broker = mk_gate(tmp_path)
    broker.on_new_trading_day()
    broker.book._entry(SYM).update({"volume": 500, "available": 500})
    assert gate.validate(mk_order(Side.SELL, price=10.0, volume=500)) == []


# ---------- 涨跌停价格 ----------

def test_price_above_limit_up_rejected(tmp_path):
    gate, _ = mk_gate(tmp_path)
    v = gate.validate(mk_order(price=11.01))       # 昨收 10 → 涨停 11.00
    assert any("涨跌停区间" in x for x in v)


def test_price_below_limit_down_rejected(tmp_path):
    gate, _ = mk_gate(tmp_path)
    v = gate.validate(mk_order(price=8.99))        # 跌停 9.00
    assert any("涨跌停区间" in x for x in v)


def test_price_at_limit_boundary_ok(tmp_path):
    gate, _ = mk_gate(tmp_path)
    assert gate.validate(mk_order(price=11.00)) == []
    assert gate.validate(mk_order(price=9.00)) == []


def test_adjusted_price_caught_by_band(tmp_path):
    """复权价误用兜底：前/后复权价显著偏离原始价区间 → 被涨跌停闸拦截。"""
    gate, _ = mk_gate(tmp_path)
    # hfq 价（远高于原始价区间）
    v = gate.validate(mk_order(price=15.60))
    assert any("疑似复权价误用" in x for x in v)
    # qfq 除权后价（远低于原始价区间）
    v2 = gate.validate(mk_order(price=6.20))
    assert any("疑似复权价误用" in x for x in v2)


def test_st_band_5pct(tmp_path):
    """ST ±5%：昨收 10 → [9.50, 10.50]。"""
    set_st_names(["600000.SH"])
    try:
        gate, _ = mk_gate(tmp_path)
        assert gate.validate(mk_order(price=10.50)) == []
        v = gate.validate(mk_order(price=10.51))
        assert any("（ST）" in x for x in v)
    finally:
        # 清理 ST 注册（全局态，防影响其他用例）
        import quantdesk.core.symbols as sym_mod

        sym_mod._ST_SYMBOLS.discard("600000.SH")


def test_gem_board_20pct_band(tmp_path):
    gate, _ = mk_gate(tmp_path)
    v = gate.validate(mk_order(symbol="300001.SZ", price=12.5))   # ±20% → 12.00 上限
    assert any("涨跌停区间" in x for x in v)
    assert gate.validate(mk_order(symbol="300001.SZ", price=12.0)) == []


# ---------- 资金 / 仓位 / 日委托数 ----------

def test_insufficient_cash(tmp_path):
    gate, _ = mk_gate(tmp_path, initial_cash=1_000.0)
    v = gate.validate(mk_order(price=10.0, volume=500))   # 5000+费 > 1000
    assert any("资金不足" in x for x in v)


def test_position_value_cap(tmp_path):
    gate, _ = mk_gate(tmp_path, max_position_value=4_000.0)
    v = gate.validate(mk_order(price=10.0, volume=500))   # 5000 > 4000
    assert any("单票市值超限" in x for x in v)


def test_daily_order_count_cap(tmp_path):
    gate, _ = mk_gate(tmp_path, max_daily_orders=3)
    for i in range(3):
        gate.submit(mk_order(price=10.5, volume=100, oid=f"N{i}"))
    v = gate.validate(mk_order(price=10.5, volume=100, oid="N9"))
    assert any("日委托数超限" in x for x in v)


# ---------- 提交链路集成 ----------

def test_submit_rejected_with_message_and_persisted(tmp_path):
    """拦截 → REJECTED + message 写明原因 + 落库可查 + RISK 事件。"""
    init_db(tmp_path / "e.db")
    broker = PaperBroker(initial_cash=100_000.0, db_path=tmp_path / "e.db")
    broker.connect()
    events = []
    from quantdesk.core.events import EventBus

    bus = EventBus()
    bus.subscribe("risk", lambda e: events.append(e))
    gate = RiskGate(paper_broker=broker, db_path=tmp_path / "e.db", event_bus=bus,
                    settings=StubSettings(True), store=mk_store(tmp_path))
    out = gate.submit(mk_order(price=15.60, volume=500))   # 复权价
    assert out.status == OrderStatus.REJECTED
    assert "涨跌停区间" in out.message
    from quantdesk.store.repo import get_order

    assert get_order(out.order_id, db_path=tmp_path / "e.db").message == out.message
    assert len(events) == 1


def test_live_mode_rejects_without_prev_close(tmp_path):
    """实盘路径缺昨收 → fail-closed 拒单（模拟盘放行并告警）。"""
    init_db(tmp_path / "lv.db")
    gate = RiskGate(paper_broker=None, live_broker=None, settings=StubSettings(False),
                    db_path=tmp_path / "lv.db", store=None)
    out = gate.submit(mk_order(price=10.0))
    assert out.status == OrderStatus.REJECTED       # live 无通道（fail-closed 优先）
    # 模拟盘无昨收 → 跳过价格校验放行（其余闸照跑）
    gate2, _ = mk_gate(tmp_path, sim=True)
    gate2.store = None
    gate2._prev_close_cache.clear()
    assert gate2.validate(mk_order(price=10.0)) == []
