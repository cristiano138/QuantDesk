"""D06：撮合引擎测试（DoD：≥15 用例；含印花税卖出手工核算；T+1 当日买不可卖）。"""

from datetime import datetime

from quantdesk.core.enums import OrderType, Side
from quantdesk.core.models import Bar, Order, Tick, Trade
from quantdesk.engine.matching import (
    FeeCalculator,
    MatchConfig,
    PositionBook,
    apply_fill,
    match_on_bar,
    match_on_tick,
)


def mk_bar(o, h, l, c, vol=10000, dt=datetime(2024, 6, 3, 15)) -> Bar:
    return Bar(symbol="600000.SH", freq="1d", dt=dt, open=o, high=h, low=l,
               close=c, volume=vol, amount=o * vol)


def mk_tick(last, bids=None, asks=None, dt=datetime(2024, 6, 3, 9, 31)) -> Tick:
    return Tick(symbol="600000.SH", dt=dt, last=last, open=last, high=last,
                low=last, volume=1000, amount=last * 1000,
                bids=bids or [], asks=asks or [])


def mk_order(side=Side.BUY, price=10.0, volume=100, otype=OrderType.LIMIT,
             filled=0) -> Order:
    return Order(order_id="O1", symbol="600000.SH", side=side, price=price,
                 volume=volume, order_type=otype, filled_volume=filled,
                 status="submitted")


# ---------- bar 撞价 ----------

def test_bar_limit_buy_fills_when_low_at_or_below_price():
    r = match_on_bar(mk_order(price=10.0), mk_bar(10.2, 10.4, 9.9, 10.1))
    assert r.filled and r.filled_volume == 100
    assert r.fill_price == 10.0


def test_bar_limit_buy_no_fill_when_low_above_price():
    r = match_on_bar(mk_order(price=10.0), mk_bar(10.2, 10.5, 10.1, 10.3))
    assert not r.filled


def test_bar_limit_buy_gap_down_fills_at_open():
    # 开盘价优先：open 9.5 < 委托价 10.0 → 按 open 成交
    r = match_on_bar(mk_order(price=10.0), mk_bar(9.5, 9.8, 9.4, 9.6))
    assert r.filled and r.fill_price == 9.5


def test_bar_limit_sell_fills_when_high_at_or_above_price():
    r = match_on_bar(mk_order(Side.SELL, price=10.0), mk_bar(9.8, 10.1, 9.7, 10.0))
    assert r.filled and r.fill_price == 10.0


def test_bar_limit_sell_gap_up_fills_at_open():
    r = match_on_bar(mk_order(Side.SELL, price=10.0), mk_bar(10.5, 10.8, 10.3, 10.6))
    assert r.filled and r.fill_price == 10.5


def test_bar_limit_sell_no_fill_when_high_below_price():
    r = match_on_bar(mk_order(Side.SELL, price=10.5), mk_bar(9.8, 10.1, 9.7, 10.0))
    assert not r.filled


def test_bar_limit_buy_at_limit_up_boundary():
    # 昨收 9.08 → 主板涨停 9.99；bar low == 涨停价 → 边界成交（纯价格规则，见模块 docstring）
    lu = 9.99
    r = match_on_bar(mk_order(price=lu), mk_bar(lu, lu, lu, lu, vol=5000))
    assert r.filled and r.fill_price == lu
    # bar low 高于委托价 → 不成交
    r2 = match_on_bar(mk_order(price=lu), mk_bar(10.05, 10.1, 10.0, 10.05, vol=5000))
    assert not r2.filled


def test_bar_partial_fill_capped_by_bar_volume():
    # 委托 10000 股，bar 量 3000 → 部分成交 3000
    r = match_on_bar(mk_order(volume=10000), mk_bar(10.0, 10.2, 9.9, 10.1, vol=3000))
    assert r.filled and r.filled_volume == 3000


def test_bar_volume_limit_ratio():
    cfg = MatchConfig(volume_limit_ratio=0.5)
    r = match_on_bar(mk_order(volume=10000), mk_bar(10.0, 10.2, 9.9, 10.1, vol=3000), cfg)
    assert r.filled_volume == 1500


def test_bar_suspended_no_fill():
    # 停牌（volume=0）→ 不成交（含市价单）
    r = match_on_bar(mk_order(), mk_bar(10.0, 10.0, 10.0, 10.0, vol=0))
    assert not r.filled
    r2 = match_on_bar(mk_order(otype=OrderType.MARKET), mk_bar(10.0, 10.0, 10.0, 10.0, vol=0))
    assert not r2.filled


def test_bar_market_buy_open_plus_slippage():
    cfg = MatchConfig(slippage=0.001)
    r = match_on_bar(mk_order(otype=OrderType.MARKET, price=0), mk_bar(10.0, 10.2, 9.9, 10.1), cfg)
    assert r.filled and r.fill_price == round(10.0 * 1.001, 2)


def test_bar_market_sell_open_minus_slippage():
    cfg = MatchConfig(slippage=0.001)
    r = match_on_bar(mk_order(Side.SELL, otype=OrderType.MARKET), mk_bar(10.0, 10.2, 9.9, 10.1), cfg)
    assert r.filled and r.fill_price == round(10.0 * 0.999, 2)


def test_bar_no_refill_beyond_order_volume():
    o = mk_order(volume=100, filled=100)
    r = match_on_bar(o, mk_bar(10.0, 10.2, 9.9, 10.1))
    assert not r.filled  # 已满量，不再成交


# ---------- tick 撞价 ----------

def test_tick_limit_buy_fills_when_last_at_or_below_price():
    r = match_on_tick(mk_order(price=10.0), mk_tick(9.95))
    assert r.filled and r.fill_price == 9.95  # 取对委托更优者


def test_tick_limit_buy_fill_at_price_when_last_equal():
    r = match_on_tick(mk_order(price=10.0), mk_tick(10.0))
    assert r.filled and r.fill_price == 10.0


def test_tick_limit_buy_no_fill_when_last_above_price():
    r = match_on_tick(mk_order(price=10.0), mk_tick(10.01))
    assert not r.filled


def test_tick_limit_sell_fills_when_last_at_or_above_price():
    r = match_on_tick(mk_order(Side.SELL, price=10.0), mk_tick(10.05))
    assert r.filled and r.fill_price == 10.05


def test_tick_limit_sell_no_fill_when_last_below_price():
    r = match_on_tick(mk_order(Side.SELL, price=10.0), mk_tick(9.99))
    assert not r.filled


def test_tick_market_buy_opponent_price_plus_slippage():
    # 市价买按对手价 ask[0] + 滑点
    cfg = MatchConfig(slippage=0.001)
    t = mk_tick(10.0, bids=[(9.98, 500)], asks=[(10.02, 800)])
    r = match_on_tick(mk_order(otype=OrderType.MARKET), t, cfg)
    assert r.filled and r.fill_price == round(10.02 * 1.001, 2)


def test_tick_market_sell_opponent_price_minus_slippage():
    cfg = MatchConfig(slippage=0.001)
    t = mk_tick(10.0, bids=[(9.98, 500)], asks=[(10.02, 800)])
    r = match_on_tick(mk_order(Side.SELL, otype=OrderType.MARKET), t, cfg)
    assert r.filled and r.fill_price == round(9.98 * 0.999, 2)


def test_tick_market_no_book_falls_back_to_last():
    cfg = MatchConfig(slippage=0.0)
    r = match_on_tick(mk_order(otype=OrderType.MARKET), mk_tick(10.0), cfg)
    assert r.filled and r.fill_price == 10.0


# ---------- 费用 ----------

def test_fee_buy_min_commission_boundary():
    # 100 股 × 5.00 = 500 元：佣金 max(0.125, 5) = 5.00（min5 边界）+ 过户费 0.05
    fee = FeeCalculator()
    amt = 500.0
    assert fee.commission(amt) == 5.0
    assert fee.buy_fee(amt) == 5.05


def test_fee_stamp_tax_buy_side_zero():
    fee = FeeCalculator()
    amt = 10000.0
    # 买入只有佣金 + 过户费：max(2.5,5)=5 + 1.0 = 6.00，无印花税
    assert fee.buy_fee(amt) == 6.0


def test_fee_sell_manual_calculation():
    """一笔含印花税的卖出手工核算（DoD 要求核算过程写入本注释）：

    卖出 1000 股 @ 10.00 元：
      成交金额   = 1000 × 10.00 = 10000.00 元
      佣金       = max(10000 × 0.00025, 5) = max(2.50, 5.00) = 5.00 元
      印花税     = 10000 × 0.0005 = 5.00 元（卖出单边）
      过户费     = 10000 × 0.0001 = 1.00 元（双边）
      费用合计   = 5.00 + 5.00 + 1.00 = 11.00 元
      净入金     = 10000.00 - 11.00 = 9989.00 元
    引擎输出必须与上式完全一致。
    """
    fee = FeeCalculator()
    assert fee.sell_fee(10000.0) == 11.0

    # 全链路：bar 撞价 → 成交 → 费用与净入金一致
    o = mk_order(Side.SELL, price=10.0, volume=1000)
    cfg = MatchConfig(fee=fee)
    r = match_on_bar(o, mk_bar(10.0, 10.0, 10.0, 10.0, vol=2000), cfg)
    assert r.filled_volume == 1000 and r.fill_price == 10.0
    assert r.trades[0].amount == 10000.0
    assert r.fee == 11.0
    book = PositionBook()
    cash_delta = book.apply_trade(r.trades[0], fee=r.fee)
    assert cash_delta == 9989.0


def test_fee_zero_amount_buy_still_min_commission():
    # 理论不会发生（量上限 0 不成交），但费率函数独立成立
    fee = FeeCalculator()
    assert fee.buy_fee(0.0) == 5.0


# ---------- 状态推进与 T+1 ----------

def test_apply_fill_status_transitions():
    o = mk_order(volume=100)
    r = match_on_bar(o, mk_bar(10.0, 10.2, 9.9, 10.1, vol=40))
    o2 = apply_fill(o, r)
    assert o2.filled_volume == 40 and o2.status == "part_filled"
    assert o2.avg_filled_price == 10.0
    # 补齐剩余 → FILLED；均价按两次成交加权
    o.filled_volume, o.status = 40, "part_filled"
    r2 = match_on_bar(o, mk_bar(10.0, 10.2, 9.9, 10.1, vol=60),
                      MatchConfig(volume_limit_ratio=1.0))
    o3 = apply_fill(o, r2)
    assert o3.status == "filled" and o3.filled_volume == 100


def test_apply_fill_does_not_mutate_input():
    o = mk_order(volume=100)
    r = match_on_bar(o, mk_bar(10.0, 10.2, 9.9, 10.1))
    apply_fill(o, r)
    assert o.filled_volume == 0 and o.status == "submitted"  # 纯函数：入参不变


def test_t1_buy_today_not_available():
    """T+1：当日买入 available=0（DoD：当日买不可卖）。"""
    book = PositionBook()
    t = Trade(trade_id="T1", order_id="O1", symbol="600000.SH", side=Side.BUY,
              price=10.0, volume=500, amount=5000.0, traded_at=0)
    book.apply_trade(t, fee=6.0)
    p = book.position("600000.SH")
    assert p.volume == 500 and p.available == 0        # 当日不可卖
    assert book.available("600000.SH") == 0
    # 均价含费用：(5000 + 6) / 500 = 10.012
    assert p.avg_cost == 10.012


def test_t1_release_next_day():
    book = PositionBook()
    t = Trade(trade_id="T1", order_id="O1", symbol="600000.SH", side=Side.BUY,
              price=10.0, volume=500, amount=5000.0, traded_at=0)
    book.apply_trade(t, fee=6.0)
    book.on_new_trading_day()   # 交易日切换（由 calendar 驱动，D09）
    assert book.available("600000.SH") == 500


def test_t1_sell_reduces_volume_and_available():
    book = PositionBook()
    buy = Trade(trade_id="T1", order_id="O1", symbol="600000.SH", side=Side.BUY,
                price=10.0, volume=500, amount=5000.0, traded_at=0)
    book.apply_trade(buy)
    book.on_new_trading_day()
    sell = Trade(trade_id="T2", order_id="O2", symbol="600000.SH", side=Side.SELL,
                 price=10.5, volume=200, amount=2100.0, traded_at=1)
    delta = book.apply_trade(sell, fee=6.03)
    p = book.position("600000.SH")
    assert p.volume == 300 and p.available == 300     # 卖出同时扣减两者
    assert delta == round(2100.0 - 6.03, 2)           # 净入金
    assert p.avg_cost == 10.0                          # 卖出不改均价


def test_t1_sell_cannot_exceed_available():
    """卖出使 available 击穿 0 的防御：买入当日 available=0，卖出后仍不得为正。"""
    book = PositionBook()
    buy = Trade(trade_id="T1", order_id="O1", symbol="600000.SH", side=Side.BUY,
                price=10.0, volume=100, amount=1000.0, traded_at=0)
    book.apply_trade(buy)
    sell = Trade(trade_id="T2", order_id="O2", symbol="600000.SH", side=Side.SELL,
                 price=10.0, volume=100, amount=1000.0, traded_at=1)
    book.apply_trade(sell)
    assert book.position("600000.SH") is None   # 清仓后无持仓记录
    assert book.available("600000.SH") == 0
