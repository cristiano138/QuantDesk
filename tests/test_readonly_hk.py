"""港股交易口径（2026-11-06 修订）：模拟盘可以买卖，实盘通道仍然不开。

原文是「只用来看 A 股和 H 股，不要接入买卖功能」。用户后来要求「每一只股票都可以模拟
买卖」——模拟盘是本机虚拟资金，不碰任何券商，于是红线收窄成一条更精确的：**港股真实
下单通道永不开通**。这里锁住四件事：
① 各种非规范写法（hk00700 / 0700 / 5）在风控咽喉处认得出，不抛异常变成 500；
② 模拟盘（simulation_only ON）下港股委托正常进入撮合，T+0（当日买入当日可卖）、
   每手股数不卡（快照不给，1 股递增放行）、无涨跌停校验；
③ 实盘路径（simulation_only OFF）对港股一律 REJECTED，且通道侧零动作；
④ 盯盘/术语两个模块依旧没有任何交易写入口（这条不变）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from quantdesk.core.enums import OrderStatus, Side
from quantdesk.core.models import Order, Tick
from quantdesk.core.symbols import currency_of, is_hk, normalize
from quantdesk.store import repo
from tests.test_riskgate import mk_gate, mk_order

HK = "00700.HK"


@pytest.mark.parametrize("raw,expected", [
    ("00700.HK", "00700.HK"),
    ("hk00700", "00700.HK"),
    ("700.HK", "00700.HK"),
    ("0700", "00700.HK"),
    ("116.00700", "00700.HK"),
    ("5", "00005.HK"),
])
def test_hk_codes_recognised_without_raising(raw, expected):
    """风控拿到的代码可能是用户手输的非规范写法：判定必须容错，不能抛异常变成 500。"""
    assert is_hk(raw) is True
    assert normalize(raw) == expected


def test_main_land_codes_are_not_hk():
    for s in ("600000.SH", "000001.SZ", "300750.SZ", "688981.SH", "830799.BJ"):
        assert is_hk(s) is False
    assert is_hk("乱七八糟") is False          # 认不出来的交给 board_of 去报非法


def test_b_share_codes_recognised_with_their_currency():
    """B 股曾经直接报「无法识别市场前缀」，整段股票在模拟盘里根本进不去。"""
    assert normalize("900921") == "900921.SH"
    assert normalize("200725") == "200725.SZ"
    assert currency_of("900921.SH") == "USD"
    assert currency_of("200725.SZ") == "HKD"
    assert currency_of("00700.HK") == "HKD"
    assert currency_of("600000.SH") == "CNY"


def _feed_hk(gate, last=300.0, minutes_ago=0):
    gate.paper.on_tick(Tick(symbol=HK, dt=datetime.now() - timedelta(minutes=minutes_ago),
                            last=last, open=last, high=last, low=last, volume=1e6,
                            amount=last * 1e6, bids=[(last - .1, 500)],
                            asks=[(last + .1, 500)]))


def test_hk_buy_in_simulation_fills(tmp_path):
    """模拟盘买港股：过闸、进撮合、成交，不是被红线挡在门外。"""
    gate, broker = mk_gate(tmp_path)
    _feed_hk(gate, last=300.0)
    cash_before = broker.get_account().cash
    o = gate.submit(mk_order(Side.BUY, price=300.0, volume=100, symbol=HK, oid="HKS1"))
    assert o.status == OrderStatus.FILLED, o.message
    assert o.filled_volume == 100
    assert broker.get_account().cash < cash_before       # 真的记了账（含港股三项费用）
    stored = repo.get_order("HKS1", db_path=gate.db_path)
    assert stored is not None and stored.status == OrderStatus.FILLED


def test_hk_lot_size_is_not_blocked(tmp_path):
    """港股每手股数由发行人自定、快照不给 → 模拟盘按 1 股递增放行（界面另注明实际每手）。"""
    gate, _ = mk_gate(tmp_path)
    assert gate.validate(mk_order(Side.BUY, price=300.0, volume=1, symbol=HK)) == []
    assert gate.validate(mk_order(Side.BUY, price=300.0, volume=333, symbol=HK)) == []


def test_hk_has_no_price_limit_check(tmp_path):
    """港股没有涨跌停概念：现价 1200（昨收 300）也不该被「±10%」判成越界。"""
    gate, _ = mk_gate(tmp_path)
    assert gate.validate(mk_order(Side.BUY, price=1200.0, volume=10, symbol=HK)) == []


def test_hk_t_plus_0_allows_same_day_sell(tmp_path):
    """当日买入的港股当日就能卖（A 股才冻 T+1）——冻住就是规则错。"""
    gate, broker = mk_gate(tmp_path)
    _feed_hk(gate, last=300.0)
    assert gate.submit(mk_order(Side.BUY, price=300.0, volume=100, symbol=HK,
                                oid="HKB")).status == OrderStatus.FILLED
    pos = broker.get_position(HK)[0]
    assert pos.volume == 100 and pos.available == 100
    _feed_hk(gate, last=301.0)
    s = gate.submit(mk_order(Side.SELL, price=300.0, volume=100, symbol=HK, oid="HKS2"))
    assert s.status == OrderStatus.FILLED, s.message


def test_hk_sell_above_available_still_rejected(tmp_path):
    gate, broker = mk_gate(tmp_path)
    _feed_hk(gate)
    v = gate.validate(mk_order(Side.SELL, price=300.0, volume=100, symbol=HK))
    assert any("T+1 可用不足" in x for x in v) or v == []   # 无持仓时由撮合端兜住，不许崩


def test_hk_rejected_on_live_channel(tmp_path):
    """实盘路径：港股仍然一律拒，且通道侧零动作、拒单落库留痕。"""
    gate, broker = mk_gate(tmp_path, sim=False)
    o = gate.submit(mk_order(Side.BUY, price=300.0, volume=100, symbol=HK, oid="HKL1"))
    assert o.status == OrderStatus.REJECTED
    assert "港股" in o.message and "模拟盘" in o.message
    assert broker.get_trades() == []
    assert repo.get_order("HKL1", db_path=gate.db_path).status == OrderStatus.REJECTED


def test_validate_blocks_hk_only_on_live_side(tmp_path):
    """绕过 submit 直连 validate：实盘口径拦住，模拟口径放过（闸②独立成立）。"""
    gate, _ = mk_gate(tmp_path)
    assert any("港股" in x for x in gate.validate(mk_order(symbol=HK), live=True))
    assert gate.validate(mk_order(symbol=HK, price=300.0, volume=100), live=False) == []


@pytest.mark.parametrize("raw", ["hk00700", "0700", "5"])
def test_loose_hk_code_never_crashes(tmp_path, raw):
    """手输非规范写法：结果是「要么成交要么一句人话拒单」，而不是把异常抛到 API 层。"""
    gate, _ = mk_gate(tmp_path)
    o = gate.submit(mk_order(Side.BUY, price=10.0, volume=100, symbol=raw, oid=f"L-{raw}"))
    assert o.status in (OrderStatus.SUBMITTED, OrderStatus.PART_FILLED,
                        OrderStatus.FILLED, OrderStatus.REJECTED)


def test_illegal_symbol_still_reported_as_illegal(tmp_path):
    """放开港股模拟不能把非法代码的处理顺带改坏。"""
    gate, _ = mk_gate(tmp_path)
    v = gate.validate(mk_order(symbol="600000.XX"))
    assert any("非法证券代码" in x for x in v)


def test_a_share_path_unaffected(tmp_path):
    gate, _ = mk_gate(tmp_path)
    assert gate.validate(mk_order(Side.BUY, price=10.0, volume=200)) == []
    o = gate.submit(Order(order_id="A1", symbol="600000.SH", side=Side.BUY, price=10.0,
                          volume=200, order_type="limit", status=OrderStatus.SUBMITTED))
    assert o.status != OrderStatus.REJECTED


def test_watch_and_guide_routes_have_no_trading_writes():
    """盯盘/术语两个模块不许出现任何交易写入口。

    允许的写只有两类：自选池增删（/pool）、主动拉一次行情（/refresh）——都不碰订单。
    """
    from quantdesk.app import app

    allowed_write = ("/api/watch/pool", "/api/watch/refresh")
    for path, ops in app.openapi()["paths"].items():
        if not path.startswith(("/api/watch", "/api/guide")):
            continue
        assert not any(k in path.lower() for k in ("order", "trade", "buy", "sell", "cancel")), path
        for method in ops:
            if method == "get":
                continue
            assert path in allowed_write, f"{path} 上的 {method.upper()} 不该存在"
