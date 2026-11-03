"""D07：回测引擎测试（DoD：1000 bar 无事件丢失；bar 时间严格单调；fake 策略买卖在 equity curve 体现）。"""

import numpy as np
import pandas as pd
import pytest

from quantdesk.core.enums import AdjustType, Frequency, OrderStatus, Side
from quantdesk.engine.backtest import BacktestConfig, BacktestEngine, HistoricalDataFeed
from quantdesk.engine.strategy_base import BaseStrategy
from quantdesk.store.parquet_store import HISTORY_COLUMNS, ParquetStore

SYMBOL = "600000.SH"


def synth_df(n=1000, start="2020-01-01", seed=7) -> pd.DataFrame:
    """合成日线：缓慢上行 + 正弦波动，保证有金叉/死叉可触发买卖。"""
    rng = np.random.default_rng(seed)
    dt = pd.bdate_range(start, periods=n)
    base = 10 + np.arange(n) * 0.01 + 2 * np.sin(np.arange(n) / 20)
    close = np.round(base + rng.normal(0, 0.05, n), 2)
    open_ = np.round(np.roll(close, 1) + rng.normal(0, 0.02, n), 2)
    open_[0] = 10.0
    high = np.round(np.maximum(open_, close) + 0.05, 2)
    low = np.round(np.minimum(open_, close) - 0.05, 2)
    vol = rng.integers(50_000, 200_000, n).astype(float)
    return pd.DataFrame({
        "dt": dt, "open": open_, "high": high, "low": low, "close": close,
        "volume": vol, "amount": np.round(close * vol, 2),
    })[HISTORY_COLUMNS]


def write_store(tmp_path, df, symbol=SYMBOL):
    store = ParquetStore(root=tmp_path / "parquet")
    store.write(symbol, Frequency.D1, df, AdjustType.FORWARD)
    return store


class FakeStrategy(BaseStrategy):
    """fake 策略：第 10 根 bar 买入 500 股（市价），第 30 根 bar 全部卖出（市价）。"""

    name = "fake"
    params = {"buy_at": 10, "sell_at": 30, "volume": 500}

    def __init__(self, params=None):
        super().__init__(params)
        self.seen_bars = []
        self.logs = []

    def on_start(self):
        self.ctx.subscribe([SYMBOL])

    def on_bar(self, bar):
        self.seen_bars.append(bar)
        i = len(self.seen_bars) - 1
        if i == self.params["buy_at"]:
            self.ctx.buy(SYMBOL, self.params["volume"])           # 市价
        if i == self.params["sell_at"]:
            pos = self.ctx.get_position(SYMBOL)                   # T+1 感知
            if pos and pos.available >= self.params["volume"]:
                self.ctx.sell(SYMBOL, self.params["volume"])

    def on_stop(self):
        pass


def test_1000_bars_no_loss_and_strict_monotonic(tmp_path):
    df = synth_df(1000)
    store = write_store(tmp_path, df)
    strat = FakeStrategy()
    eng = BacktestEngine(strat, BacktestConfig(symbols=[SYMBOL], start="2020-01-01"),
                         store=store)
    result = eng.run()
    assert result.bars == 1000
    assert len(strat.seen_bars) == 1000                            # 无事件丢失
    dts = [b.dt for b in strat.seen_bars]
    assert all(b > a for a, b in zip(dts, dts[1:]))                # bar 时间严格单调
    assert len(result.equity) == 1000                              # 逐 bar 权益曲线


def test_fake_strategy_trades_reflected_in_equity(tmp_path):
    df = synth_df(200)
    store = write_store(tmp_path, df)
    strat = FakeStrategy()
    eng = BacktestEngine(strat, BacktestConfig(symbols=[SYMBOL]), store=store)
    result = eng.run()

    # 买入成交：次日 bar 开盘价成交（无未来函数）
    buys = [o for o in result.orders if o.side == Side.BUY
            and o.status in (OrderStatus.FILLED, OrderStatus.PART_FILLED)]
    sells = [o for o in result.orders if o.side == Side.SELL
             and o.status == OrderStatus.FILLED]
    assert len(buys) == 1 and buys[0].filled_volume == 500
    assert len(sells) == 1 and sells[0].filled_volume == 500
    assert len(result.trades) == 2

    # equity curve 体现：买入后现金下降、市值上升；清仓后市值归零
    eq = result.equity.set_index("dt")
    buy_dt = pd.Timestamp(buys[0].updated_at, unit="ms").normalize()
    after_buy = eq[eq.index > buy_dt].iloc[0]
    assert after_buy["cash"] < 1_000_000.0
    assert after_buy["market_value"] > 0
    after_sell = eq.iloc[-1]
    assert after_sell["market_value"] == 0.0
    assert after_sell["cash"] == pytest.approx(after_sell["equity"])
    # 净值变化 = 价格变动 - 费用（数量级健康检查： equity 仍为正且与初始同数量级）
    assert 0.5e6 < after_sell["equity"] < 2e6


def test_no_lookahead_order_filled_at_next_bar_open(tmp_path):
    """委托在提交 bar 的下一根成交，且开盘价优先——无未来函数。"""
    df = synth_df(200)
    store = write_store(tmp_path, df)
    seen = []

    class MarkStrategy(BaseStrategy):
        name = "mark"

        def on_bar(self, bar):
            seen.append(bar)
            if len(seen) == 5:
                self.ctx.buy(SYMBOL, 100, price=bar.close)   # 限价=第5根收盘

    eng = BacktestEngine(MarkStrategy(), BacktestConfig(symbols=[SYMBOL]), store=store)
    result = eng.run()
    filled = [t for t in result.trades]
    assert len(filled) == 1
    bar5, bar6 = seen[4], seen[5]      # 第5根提交（index4），第6根成交（index5）
    assert filled[0].traded_at == int(bar6.dt.timestamp() * 1000)
    # 成交价 = min(限价, 次根开盘价)（开盘更优取开盘，否则按限价——不劣于委托）
    assert filled[0].price == min(bar5.close, bar6.open)


def test_t1_sell_blocked_same_day(tmp_path):
    """当日买入当日卖 → 引擎层拒单（T+1 可用不足）。"""
    df = synth_df(50)
    store = write_store(tmp_path, df)
    seen = []

    class DayTrade(BaseStrategy):
        name = "daytrade"

        def on_bar(self, bar):
            seen.append(bar)
            i = len(seen) - 1
            if i == 5:
                self.ctx.buy(SYMBOL, 500)
            if i == 6:
                self.ctx.sell(SYMBOL, 500)      # 次日——但撮合发生在第7根，卖出提交时仍不可用

    eng = BacktestEngine(DayTrade(), BacktestConfig(symbols=[SYMBOL]), store=store)
    result = eng.run()
    # 第 6 根买入 → 第 7 根撮合成交（此时新交易日已释放？买入发生在第7根，
    # 第7根内提交的卖出在第8根撮合——available 仍为 0 → 被拒）
    sell_orders = [o for o in result.orders if o.side == Side.SELL]
    assert sell_orders and sell_orders[0].status == OrderStatus.REJECTED
    assert "T+1" in sell_orders[0].message


def test_t1_sell_allowed_next_next_day(tmp_path):
    """T 日买入（T+1 bar 成交），T+2 bar 提交卖出 → 成功。"""
    df = synth_df(50)
    store = write_store(tmp_path, df)
    seen = []

    class PatientStrategy(BaseStrategy):
        name = "patient"

        def on_bar(self, bar):
            seen.append(bar)
            i = len(seen) - 1
            if i == 5:
                self.ctx.buy(SYMBOL, 500)        # 第6根提交，第7根成交
            if i == 8:
                pos = self.ctx.get_position(SYMBOL)
                assert pos is not None and pos.available == 500
                self.ctx.sell(SYMBOL, 500)

    eng = BacktestEngine(PatientStrategy(), BacktestConfig(symbols=[SYMBOL]), store=store)
    result = eng.run()
    sells = [o for o in result.orders if o.side == Side.SELL]
    assert len(sells) == 1 and sells[0].status == OrderStatus.FILLED


def test_limit_order_ttl_cancel(tmp_path):
    """限价单次根 bar 未成交 → TTL(=1) 到期撤单。"""
    df = synth_df(50)
    store = write_store(tmp_path, df)
    seen = []

    class FarLimit(BaseStrategy):
        name = "farlimit"

        def on_bar(self, bar):
            seen.append(bar)
            if len(seen) == 5:
                self.ctx.buy(SYMBOL, 100, price=1.0)   # 远低于市价，永不成交

    eng = BacktestEngine(FarLimit(), BacktestConfig(symbols=[SYMBOL]), store=store)
    result = eng.run()
    o = result.orders[0]
    assert o.status == OrderStatus.CANCELLED
    assert "未成交撤单" in o.message
    assert all(t.order_id != o.order_id for t in result.trades)


def test_insufficient_cash_reduced_to_affordable(tmp_path):
    """现金不足 → 缩量到可负担整手（而非整单拒绝/负现金）。"""
    df = synth_df(50)
    store = write_store(tmp_path, df)
    seen = []

    class BigBuy(BaseStrategy):
        name = "bigbuy"

        def on_bar(self, bar):
            seen.append(bar)
            if len(seen) == 5:
                self.ctx.buy(SYMBOL, 10_000_000)       # 远超 100 万本金

    eng = BacktestEngine(BigBuy(), BacktestConfig(symbols=[SYMBOL], initial_cash=100_000),
                         store=store)
    result = eng.run()
    fills = [t for t in result.trades]
    assert len(fills) == 1
    assert fills[0].volume <= 9_900                    # 整手缩量
    assert eng.cash >= 0                               # 现金不为负
    o = result.orders[0]
    # 资金不足 → 订单有效量对齐到可负担量并 FILLED（消息留痕缩量，不再产生悬挂余量）
    assert o.status == OrderStatus.FILLED
    assert o.filled_volume == fills[0].volume
    assert "资金不足缩量" in o.message


def test_multi_symbol_merge_and_clock(tmp_path):
    """双标的合并回放：两标的 bar 数不丢、时钟不回退。"""
    df1 = synth_df(100, seed=1)
    df2 = synth_df(100, start="2020-01-02", seed=2)
    store = ParquetStore(root=tmp_path / "parquet")
    store.write(SYMBOL, Frequency.D1, df1, AdjustType.FORWARD)
    store.write("000001.SZ", Frequency.D1, df2, AdjustType.FORWARD)
    seen = []

    class Counter(BaseStrategy):
        name = "counter"

        def on_bar(self, bar):
            seen.append(bar)

    eng = BacktestEngine(Counter(),
                         BacktestConfig(symbols=[SYMBOL, "000001.SZ"]), store=store)
    result = eng.run()
    assert result.bars == len(df1) + len(df2)
    dts = pd.DatetimeIndex([b.dt for b in seen])
    assert dts.is_monotonic_increasing
    assert set(b.symbol for b in seen) == {SYMBOL, "000001.SZ"}


def test_duplicate_dt_rejected(tmp_path):
    """同标的重复 dt → 拒绝回放（数据损坏防线；存储层已去重，此处从读取侧注入）。"""
    df = synth_df(30)
    store = write_store(tmp_path, df)

    class DupStore(ParquetStore):
        def read(self, symbol, freq, start=None, end=None, adjust=AdjustType.NONE):
            out = super().read(symbol, freq, start=start, end=end, adjust=adjust)
            if symbol == SYMBOL and not out.empty:
                return pd.concat([out, out.iloc[[-1]]], ignore_index=True)
            return out

    eng = BacktestEngine(FakeStrategy(), BacktestConfig(symbols=[SYMBOL]),
                         store=DupStore(root=tmp_path / "parquet"))
    with pytest.raises(ValueError, match="重复/乱序"):
        eng.run()


def test_history_includes_current_bar(tmp_path):
    """ctx.history 含当前 bar（MA(T) 用收盘价 T 计算）。"""
    df = synth_df(30)
    store = write_store(tmp_path, df)
    seen = []

    class HistStrategy(BaseStrategy):
        name = "hist"

        def on_bar(self, bar):
            seen.append(bar)
            h = self.ctx.history(SYMBOL, bars=5)
            assert len(h) == min(len(seen), 5)
            assert h.iloc[-1]["dt"] == bar.dt                 # 含当前 bar
            assert list(h.columns) == HISTORY_COLUMNS

    eng = BacktestEngine(HistStrategy(), BacktestConfig(symbols=[SYMBOL]), store=store)
    eng.run()


def test_history_other_freq_empty_without_data(tmp_path):
    df = synth_df(30)
    store = write_store(tmp_path, df)

    class FreqProbe(BaseStrategy):
        name = "freqprobe"

        def __init__(self):
            super().__init__()
            self.result = None

        def on_start(self):
            self.result = self.ctx.history(SYMBOL, freq="1w", bars=10)

    strat = FreqProbe()
    eng = BacktestEngine(strat, BacktestConfig(symbols=[SYMBOL]), store=store)
    eng.run()
    assert strat.result is not None and strat.result.empty   # 本地无周线 → 空表


def test_historical_data_feed_range_filter(tmp_path):
    df = synth_df(100)
    store = write_store(tmp_path, df)
    feed = HistoricalDataFeed(store)
    out = feed.load([SYMBOL], "1d", start="2020-02-01", end="2020-03-01")
    assert not out[SYMBOL].empty
    assert out[SYMBOL]["dt"].min() >= pd.Timestamp("2020-02-01")
    assert out[SYMBOL]["dt"].max() <= pd.Timestamp("2020-03-01")
    # 未入库标的 → 空表不抛
    empty = feed.load(["000000.SH"], "1d")
    assert empty["000000.SH"].empty
