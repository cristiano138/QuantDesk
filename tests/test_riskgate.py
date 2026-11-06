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
        assert any("±5%" in x and "ST" in x for x in v), v
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


# ---------- 涨跌停口径：交易所给的数优先于自己推算 ----------

def mk_quote_gate(tmp_path, rows, sim=True):
    """带行情快照提供器的闸：rows = {symbol: 快照 dict}。"""
    init_db(tmp_path / "q.db")
    broker = PaperBroker(initial_cash=1_000_000.0, db_path=tmp_path / "q.db")
    broker.connect()
    gate = RiskGate(paper_broker=broker, settings=StubSettings(sim),
                    db_path=tmp_path / "q.db", store=mk_store(tmp_path),
                    max_position_value=1e9, max_daily_orders=10_000,
                    quote_provider=lambda s: rows.get(s, {}))
    return gate, broker


def test_official_limit_price_wins_over_computed_band(tmp_path):
    """行情给了官方涨跌停（ST/新股/退市整理这些幅度不是 ±10% 的日子），就按官方的判。"""
    rows = {SYM: {"prev_close": 10.0, "limit_up": 12.00, "limit_down": 8.00, "name": "浦发银行",
                  "last": 11.5}}
    gate, _ = mk_quote_gate(tmp_path, rows)
    assert gate.validate(mk_order(price=11.5)) == []          # 自己算 ±10% 会误拒
    v = gate.validate(mk_order(price=12.01))
    assert any("交易所官方涨跌停价" in x for x in v), v


def test_new_listing_day_is_not_blocked(tmp_path):
    """实测缺陷：N森峰(920085) 上市首日现价 44.88、昨收 17.79，被自家 ±30% 区间拒掉。

    交易所当天不设涨跌幅——用户看到的就是「这只买不了」。
    """
    rows = {"920085.BJ": {"prev_close": 17.79, "last": 44.88, "high": 48.00, "low": 30.10,
                          "name": "N森峰", "limit_up": None, "limit_down": None}}
    gate, _ = mk_quote_gate(tmp_path, rows)
    assert gate.validate(mk_order(symbol="920085.BJ", price=44.88, volume=100)) == []


def test_new_listing_flag_in_short_name(tmp_path):
    """简称带 N/C 的新上市标记：即使当日还没成交过，也不按板块幅度拦。"""
    rows = {"301999.SZ": {"prev_close": 20.0, "last": 41.0, "high": 0, "low": 0,
                          "name": "C某某", "limit_up": None, "limit_down": None}}
    gate, _ = mk_quote_gate(tmp_path, rows)
    assert gate.validate(mk_order(symbol="301999.SZ", price=41.0, volume=100)) == []




def test_st_from_short_name_when_registry_empty(tmp_path):
    """ST 名单没同步时，用行情简称兜 ±5%——否则 ST 股按 ±10% 放行是判得过松。"""
    rows = {"000999.SZ": {"prev_close": 10.0, "last": 10.6, "high": 10.6, "low": 10.0,
                          "name": "*ST某某", "limit_up": None, "limit_down": None}}
    gate, _ = mk_quote_gate(tmp_path, rows)
    v = gate.validate(mk_order(symbol="000999.SZ", price=10.6, volume=100))
    assert any("±5%" in x for x in v), v


def test_live_quote_prev_close_used_not_stale_daily(tmp_path):
    """昨收在调用点取实时快照：本地日线是 2026-11-03 的 10.00，实时给 20.00 → 按 20 算区间。"""
    rows = {SYM: {"prev_close": 20.0, "last": 21.0, "high": 21.2, "low": 19.8,
                  "name": "浦发银行", "limit_up": None, "limit_down": None}}
    gate, _ = mk_quote_gate(tmp_path, rows)
    assert gate.validate(mk_order(price=21.0)) == []          # 按过期日线会拦成越界
    assert gate.validate(mk_order(price=11.0)) != []                      # 反向：低于新跌停照样拦


def test_b_share_price_rule_follows_main_board(tmp_path):
    """B 股与主板同规则（±10%、100 股整手），差别只在报价币种——币种由 currency_of 给。"""
    rows = {"900921.SH": {"prev_close": 0.31, "last": 0.32, "high": 0.32, "low": 0.30,
                          "name": "金煤B股", "limit_up": 0.34, "limit_down": 0.28}}
    gate, _ = mk_quote_gate(tmp_path, rows)
    assert gate.validate(mk_order(symbol="900921.SH", price=0.32, volume=100)) == []
    v = gate.validate(mk_order(symbol="900921.SH", price=0.32, volume=150))
    assert any("100 股整数倍" in x for x in v), v


def test_quote_provider_absent_falls_back_to_daily(tmp_path):
    """没接行情提供器（回测/单测裸闸）时，涨跌停仍按本地日线口径照拦不误。"""
    gate, _ = mk_gate(tmp_path)
    assert any("涨跌停区间" in x for x in gate.validate(mk_order(price=11.01)))


def test_quote_failure_is_not_cached_for_the_day(tmp_path):
    """取行情失败不能当成「今天这只没有官方区间」：下一次调用还要再问一次。"""
    calls = []

    def flaky(symbol):
        calls.append(symbol)
        if len(calls) == 1:
            raise RuntimeError("接口断了")
        return {"prev_close": 10.0, "limit_up": 12.0, "limit_down": 8.0, "name": "浦发银行"}

    init_db(tmp_path / "f.db")
    broker = PaperBroker(initial_cash=1_000_000.0, db_path=tmp_path / "f.db")
    broker.connect()
    gate = RiskGate(paper_broker=broker, settings=StubSettings(True), db_path=tmp_path / "f.db",
                    store=None, quote_provider=flaky, max_position_value=1e9)
    assert gate.validate(mk_order(price=11.5)) == []      # 第一次拿不到 → 模拟盘放行并告警
    assert gate.validate(mk_order(price=11.5)) == []      # 第二次问到了官方区间
    assert len(calls) >= 2


# ---------- 规则卡（界面与执行同源） ----------

def test_rule_card_per_board(tmp_path):
    rows = {
        "600000.SH": {"prev_close": 10.0, "last": 10.0, "high": 10.2, "low": 9.8,
                      "limit_up": 11.0, "limit_down": 9.0, "name": "浦发银行"},
        "688836.SH": {"prev_close": 652.1, "last": 660.0, "high": 670.0, "low": 640.0,
                      "limit_up": 782.5, "limit_down": 521.7, "name": "宇树科技-W"},
        "920770.BJ": {"prev_close": 14.0, "last": 14.4, "high": 14.6, "low": 13.9,
                      "limit_up": 18.2, "limit_down": 9.8, "name": "艾能聚"},
        "900921.SH": {"prev_close": 0.31, "last": 0.32, "high": 0.33, "low": 0.30,
                      "limit_up": 0.34, "limit_down": 0.28, "name": "金煤B股"},
        "00700.HK": {"prev_close": 401.6, "last": 430.4, "high": 431.2, "low": 410.2,
                     "limit_up": None, "limit_down": None, "name": "腾讯控股"},
    }
    gate, _ = mk_quote_gate(tmp_path, rows)
    got = {s: gate.rule_card(s) for s in rows}
    assert got["600000.SH"]["min_volume"] == 100 and got["600000.SH"]["t_plus_1"] is True
    assert got["600000.SH"]["limit_up"] == 11.0
    assert got["688836.SH"]["min_volume"] == 200          # 科创板起步 200 股
    assert got["920770.BJ"]["min_volume"] == 100
    assert got["900921.SH"]["currency"] == "USD"          # 沪B 是美元报价
    assert got["00700.HK"]["currency"] == "HKD"
    assert got["00700.HK"]["limit_up"] is None            # 港股没有涨跌停
    assert got["00700.HK"]["t_plus_1"] is False           # 港股 T+0
    assert got["00700.HK"]["ok"] is True                  # 模拟盘里港股可买
    assert got["00700.HK"]["min_volume"] == 1             # 港股每手股数快照不给 → 按 1 股递增
    assert got["00700.HK"]["need_cash"] > 430.4 * 1       # 含港股费用，不是只算货款
    assert "每手股数" in got["00700.HK"]["lot_rule"]


def test_rule_card_live_mode_marks_hk_untradable(tmp_path):
    rows = {"00700.HK": {"prev_close": 401.6, "last": 430.4, "high": 431.2, "low": 410.2,
                         "limit_up": None, "limit_down": None, "name": "腾讯控股"}}
    gate, _ = mk_quote_gate(tmp_path, rows, sim=False)
    card = gate.rule_card("00700.HK")
    assert card["ok"] is False and "模拟盘" in card["reason"]


def test_rule_card_unknown_code_is_a_message_not_a_crash(tmp_path):
    gate, _ = mk_quote_gate(tmp_path, {})
    card = gate.rule_card("不存在的代码")
    assert card["ok"] is False and "代码认不出来" in card["error"]
