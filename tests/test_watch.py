"""只读盯盘（A股/港股）离线单测：假通道喂数据，不碰外网。

覆盖 DoD 的八类点里与盯盘相关的部分：
口径归一（单位/内外盘）、方向码校准、买卖量分层、收盘定格落库与回读、
通道缺失/断线时的如实降级、API 层只读语义（无任何下单入口）。
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantdesk.core.errors import ConnectionLostError
from quantdesk.core.events import EventBus
from quantdesk.core.symbols import SymbolError, is_hk, normalize
from quantdesk.services import watch as watch_mod
from quantdesk.services.watch import (
    WatchService,
    aggregate_flow,
    calibrate_side_map,
    minute_series,
    session_of,
)
from quantdesk.store import repo
from quantdesk.store.db import init_db, reset_engine


# ---------------- 假通道 ----------------

class FakeTx:
    """腾讯批量快照的替身：只返回构造好的行，记录被调了几次。"""

    name = "tx_quote"

    def __init__(self, rows=None, fail=False):
        self.rows = rows or {}
        self.fail = fail
        self.calls = 0
        self._connected = True

    def get_snapshot(self, symbols):
        self.calls += 1
        if self.fail:
            raise ConnectionLostError("腾讯快照断线")
        return {s: dict(self.rows[s]) for s in symbols if s in self.rows}


class FakeEm:
    """东财逐笔/个股字段替身；df 为逐笔表，quote 为个股补充字段。"""

    name = "em_quote"

    def __init__(self, df=None, quote=None, fail=False):
        self.df = df if df is not None else pd.DataFrame()
        self.quote = quote or {}
        self.fail = fail
        self._connected = True
        self.tick_calls = 0

    def get_transactions(self, symbol, count=5000):
        self.tick_calls += 1
        if self.fail:
            raise ConnectionLostError("东财逐笔限流")
        return self.df.copy()

    def get_quote(self, symbol):
        if self.fail:
            raise ConnectionLostError("东财快照限流")
        return dict(self.quote)

    def get_intraday(self, symbol):
        if self.fail:
            raise ConnectionLostError("东财分时限流")
        return pd.DataFrame({"dt": pd.to_datetime(["2026-11-04 09:31", "2026-11-04 09:32"]),
                             "price": [10.0, 10.1], "avg_price": [10.0, 10.05],
                             "volume": [100.0, 200.0]})


def _ticks(rows):
    """按盯盘口径造逐笔表：dt/time/price/volume/deals/side_code/amount。"""
    df = pd.DataFrame(rows, columns=["dt", "time", "price", "volume", "deals",
                                     "side_code", "amount"])
    df["dt"] = pd.to_datetime(df["dt"])
    df["side_code"] = df["side_code"].astype(str)
    return df


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTDESK_DATA_DIR", str(tmp_path))
    from quantdesk.config import get_settings

    get_settings.cache_clear()
    reset_engine()
    path = init_db(tmp_path / "watch.db")
    yield path
    reset_engine()
    get_settings.cache_clear()


# 真实时段函数要在任何补丁之前抓下来：两个钉时钟的 helper 都要包原始实现，
# 否则后打的补丁会包到前一个补丁上，钉住的时钟永远是第一个时刻
_REAL_SESSION_OF = watch_mod.session_of


@pytest.fixture(autouse=True)
def _intraday_clock(monkeypatch):
    """把时段钉成"盘中"：这些用例测的是取数与口径，不该取决于 pytest 几点跑。

    钉成收盘后，服务端的休市缓存闸会让第二次 flow 直接复用缓存——用例就会看着像挂了。
    """
    monkeypatch.setattr(watch_mod, "session_of",
                        lambda market, now=None: _REAL_SESSION_OF(market, datetime(2026, 11, 4, 10, 30)))
    yield


def _closed_clock(monkeypatch):
    """切成收盘后：任何自动路径都不该再外呼。"""
    monkeypatch.setattr(watch_mod, "session_of",
                        lambda market, now=None: _REAL_SESSION_OF(market, datetime(2026, 11, 4, 21, 0)))


# ---------------- 代码归一与港股判定 ----------------

@pytest.mark.parametrize("raw,internal", [
    ("600000", "600000.SH"),
    ("600000.SH", "600000.SH"),
    ("sh600000", "600000.SH"),
    ("000001.SZ", "000001.SZ"),
    ("sz000001", "000001.SZ"),
    ("00700.HK", "00700.HK"),
    ("hk00700", "00700.HK"),
    ("700.HK", "00700.HK"),
    ("0700", "00700.HK"),
    ("116.00700", "00700.HK"),
    ("688981", "688981.SH"),
    ("00005", "00005.HK"),
])
def test_normalize_accepts_common_shapes(raw, internal):
    assert normalize(raw) == internal


def test_normalize_rejects_junk():
    for bad in ("", "   ", "ABC", "600000.XX", "SH.600000", "1234567", "600000SH"):
        with pytest.raises((SymbolError, ValueError)):
            normalize(bad)


def test_is_hk_only_for_hk_suffix():
    assert is_hk("00700.HK") and is_hk("hk00700")
    assert not is_hk("600000.SH") and not is_hk("000001.SZ")


# ---------------- 买卖量核心算法 ----------------

def test_calibrate_side_map_uses_official_outer_inner():
    # 码 2 合计 600 与外盘一致、码 1 合计 400 与内盘一致 → 2=buy
    df = _ticks([
        ("2026-11-04 09:31:00", "09:31:00", 10.0, 300.0, 3, "2", 3000.0),
        ("2026-11-04 09:32:00", "09:32:00", 10.0, 300.0, 3, "2", 3000.0),
        ("2026-11-04 09:33:00", "09:33:00", 10.0, 200.0, 2, "1", 2000.0),
        ("2026-11-04 09:34:00", "09:34:00", 10.0, 200.0, 2, "1", 2000.0),
        ("2026-11-04 09:25:00", "09:25:00", 10.0, 50.0, 1, "4", 500.0),
    ])
    mapping, confident = calibrate_side_map(df, outer=600.0, inner=400.0, total=1050.0)
    assert mapping["2"] == "buy" and mapping["1"] == "sell" and mapping["4"] == "neutral"
    assert confident is True


def test_calibrate_side_map_detects_swapped_codes():
    # 官方外盘 300 只等于码 1 的量 → 此源里 1 才是买（方向码会随接口版本翻转，不能硬编码）
    df = _ticks([
        ("2026-11-04 09:31:00", "09:31:00", 10.0, 300.0, 3, "1", 3000.0),
        ("2026-11-04 09:33:00", "09:33:00", 10.0, 600.0, 6, "2", 6000.0),
    ])
    mapping, _ = calibrate_side_map(df, outer=300.0, inner=600.0, total=900.0)
    assert mapping["1"] == "buy" and mapping["2"] == "sell"


def test_calibrate_side_map_falls_back_to_default_when_no_official():
    """没有官方内外盘可对时：给默认约定的分层，但必须标注未校准（不假装对齐过）。"""
    df = _ticks([("2026-11-04 09:31:00", "09:31:00", 10.0, 100.0, 1, "2", 1000.0)])
    mapping, confident = calibrate_side_map(df, outer=None, inner=None, total=100.0)
    assert mapping == {"1": "sell", "2": "buy", "4": "neutral"}
    assert confident is False


def test_aggregate_flow_buckets_by_amount():
    df = _ticks([
        ("2026-11-04 09:31:00", "09:31:00", 10.0, 100.0, 1, "2", 1_000.0),      # small buy
        ("2026-11-04 09:32:00", "09:32:00", 10.0, 100.0, 1, "1", 50_000.0),     # medium sell
        ("2026-11-04 09:33:00", "09:33:00", 10.0, 100.0, 1, "2", 300_000.0),    # big buy
        ("2026-11-04 09:34:00", "09:34:00", 10.0, 100.0, 1, "2", 5_000_000.0),  # huge buy
        ("2026-11-04 09:25:00", "09:25:00", 10.0, 100.0, 1, "4", 10_000.0),     # neutral
    ])
    agg = aggregate_flow(df, {"2": "buy", "1": "sell", "4": "neutral"})
    assert agg["tick_buy_volume"] == 300.0 and agg["tick_sell_volume"] == 100.0
    assert agg["tick_neutral_volume"] == 100.0
    assert agg["buy_amount"] == 5_301_000.0 and agg["sell_amount"] == 50_000.0
    b = agg["buckets"]
    assert b["small"]["buy_volume"] == 100.0 and b["small"]["ticks"] == 2   # 含一笔中性
    assert b["medium"]["sell_volume"] == 100.0 and b["medium"]["buy_volume"] == 0.0
    assert b["big"]["buy_volume"] == 100.0 and b["huge"]["buy_volume"] == 100.0


def test_minute_series_groups_by_minute():
    df = _ticks([
        ("2026-11-04 09:31:10", "09:31:10", 10.0, 10.0, 1, "2", 100.0),
        ("2026-11-04 09:31:40", "09:31:40", 10.0, 20.0, 1, "1", 200.0),
        ("2026-11-04 09:32:10", "09:32:10", 10.0, 5.0, 1, "2", 50.0),
    ])
    rows = minute_series(df, {"2": "buy", "1": "sell"})
    assert [r["dt"] for r in rows] == ["09:31", "09:32"]
    assert rows[0]["buy"] == 10.0 and rows[0]["sell"] == 20.0


@pytest.mark.parametrize("moment,market,expected", [
    (datetime(2026, 11, 7, 10, 30), "CN", "weekend"),
    (datetime(2026, 11, 4, 10, 0), "CN", "open"),
    (datetime(2026, 11, 4, 12, 0), "CN", "lunch"),
    (datetime(2026, 11, 4, 15, 20), "CN", "post_close"),
    (datetime(2026, 11, 4, 21, 0), "CN", "closed"),
    (datetime(2026, 11, 4, 9, 15), "CN", "pre_open"),
    (datetime(2026, 11, 4, 16, 20), "HK", "post_close"),
])
def test_session_of_labels_the_trading_window(moment, market, expected):
    assert session_of(market, moment)["state"] == expected


# ---------------- 服务装配（假通道） ----------------

def _row(sym, name="测试股", outer=600.0, inner=400.0, vol=1000.0):
    return {"symbol": sym, "name": name, "last": 10.0, "prev_close": 9.9, "pct_chg": 1.01,
            "open": 9.95, "high": 10.2, "low": 9.9, "volume": vol, "amount": vol * 10.0,
            "volume_unit": "手" if not sym.endswith(".HK") else "股",
            "outer_volume": outer, "inner_volume": inner,
            "bids": [(9.99, 100), (9.98, 200)], "asks": [(10.01, 300)]}


def test_refresh_fills_live_and_list_rows(db, monkeypatch):
    monkeypatch.setattr(watch_mod, "_service", None)
    tx = FakeTx({"600000.SH": _row("600000.SH"), "00700.HK": _row("00700.HK", outer=None,
                                                                   inner=None, vol=10000.0)})
    em = FakeEm(quote={"outer_volume": 5000.0, "inner_volume": 5000.0, "turnover_rate": 0.8,
                       "avg_price": 10.0})
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=tx, em_feed=em)
    rows = svc.refresh(["600000.SH", "00700.HK"])
    assert len(rows) == 2
    hk = next(r for r in rows if r["symbol"] == "00700.HK")
    assert hk["outer_volume"] == 5000.0              # 腾讯不给内外盘 → 东财补齐（官方口径优先）
    assert hk["flow_extra_source"] == "em_quote"
    listing = svc.list_rows()
    assert listing[0]["symbol"] == "00700.HK"        # 按成交额降序
    assert listing[1]["outer_pct"] == 60.0


def test_refresh_keeps_stale_rows_when_feed_down(db, monkeypatch):
    monkeypatch.setattr(watch_mod, "_service", None)
    good = FakeTx({"600000.SH": _row("600000.SH")})
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=good, em_feed=None)
    svc.refresh(["600000.SH"])
    svc._tx = FakeTx(fail=True)                     # 同池再刷，模拟断线
    out = svc.refresh(["600000.SH"])
    assert len(out) == 1 and out[0]["stale"] is True
    assert "断线" in svc.last_error
    assert svc.stats["errors"] == 1


def test_flow_prefers_official_and_reports_tick_gap(db, monkeypatch):
    monkeypatch.setattr(watch_mod, "_service", None)
    # 逐笔：主动买 620 + 主动卖 380 + 集合竞价 100；官方口径只有 1000（不含盘后/竞价差额）
    df = _ticks([
        ("2026-11-04 09:31:00", "09:31:00", 10.0, 620.0, 6, "2", 6200.0),
        ("2026-11-04 09:33:00", "09:33:00", 10.0, 380.0, 4, "1", 3800.0),
        ("2026-11-04 09:25:00", "09:25:00", 9.95, 100.0, 1, "4", 995.0),
    ])
    tx = FakeTx({"600000.SH": _row("600000.SH", outer=600.0, inner=400.0, vol=1000.0)})
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=tx, em_feed=FakeEm(df=df))
    svc.refresh(["600000.SH"])
    res = svc.flow("600000.SH", force=True)
    assert res.flow_source == "official_outer_inner"
    assert res.outer_pct == pytest.approx(60.0, abs=0.01)     # 占比按官方 600/1000，不按逐笔
    assert res.calibrated is True and res.side_map["2"] == "buy"
    assert res.tick_buy_volume == 620.0 and res.tick_sell_volume == 380.0
    assert res.tick_neutral_volume == 100.0
    # 逐笔合计 1100 > 官方 1000，差异必须如实上报而不是抹平
    assert res.tick_vs_official_pct == pytest.approx(10.0, abs=0.01)
    assert "官方外盘" in res.caveat
    assert res.unit == "手" and res.currency == "CNY"


def test_flow_fetches_snapshot_when_cache_is_cold(db, monkeypatch):
    """买卖量的官方口径必须在调用点取到。

    冷启动的新进程（或标的还不在池里、或详情先于列表加载）时，live 缓存是空的：
    过去 `_compute_flow` 直接读空 dict → 主口径静默退化成逐笔聚合，
    而界面仍写着"主口径=官方内外盘"，方向码也因为没有官方值可对齐而整天判不准。
    收盘后打开详情正是这条路径（自动刷新不跑），所以必须是取数而不是降级。
    """
    monkeypatch.setattr(watch_mod, "_service", None)
    df = _ticks([
        ("2026-11-04 09:31:00", "09:31:00", 10.0, 620.0, 6, "2", 6200.0),
        ("2026-11-04 09:33:00", "09:33:00", 10.0, 380.0, 4, "1", 3800.0),
    ])
    tx = FakeTx({"600000.SH": _row("600000.SH", outer=600.0, inner=400.0, vol=1000.0)})
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=tx, em_feed=FakeEm(df=df))
    assert svc._live == {}                       # 没有任何一次 refresh()
    res = svc.flow("600000.SH", force=True)
    assert tx.calls >= 1, "缓存为空时必须当场取快照，而不是拿空值降级"
    assert res.flow_source == "official_outer_inner"
    assert res.outer_volume == 600.0 and res.outer_pct == pytest.approx(60.0, abs=0.01)
    assert res.calibrated is True                # 有官方值可对齐，方向码才谈得上校准


def test_flow_hk_snapshot_probe_is_not_repeated(db, monkeypatch):
    """腾讯与东财都不给某只的内外盘时，探过一次就记 `outer_probed`，不再每次调用都重复补。"""
    monkeypatch.setattr(watch_mod, "_service", None)
    tx = FakeTx({"00700.HK": _row("00700.HK", outer=None, inner=None, vol=10000.0)})
    em = FakeEm(df=pd.DataFrame(), quote={})     # 东财也补不出来
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=tx, em_feed=em)
    svc.flow("00700.HK", force=True)
    assert svc._live["00700.HK"]["outer_probed"] is True
    calls_after_first = tx.calls
    res = svc.flow("00700.HK", force=True)
    assert tx.calls == calls_after_first, "已探过内外盘的标的，不该为补字段再取一次快照"
    assert res.flow_source != "official_outer_inner"    # 补不出来就如实降级


def test_flow_without_official_falls_back_to_default_codes(db, monkeypatch):
    monkeypatch.setattr(watch_mod, "_service", None)
    df = _ticks([
        ("2026-11-04 09:31:00", "09:31:00", 10.0, 700.0, 7, "2", 7000.0),
        ("2026-11-04 09:32:00", "09:32:00", 10.0, 300.0, 3, "1", 3000.0),
    ])
    tx = FakeTx({"600000.SH": _row("600000.SH", outer=None, inner=None, vol=1000.0)})
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=tx, em_feed=FakeEm(df=df))
    svc.refresh(["600000.SH"])
    res = svc.flow("600000.SH", force=True)
    assert res.flow_source == "em_tick"
    assert res.outer_pct == pytest.approx(70.0)
    assert res.calibrated is False                    # 没有官方口径就不声称校准
    assert res.side_map["2"] == "buy"                 # 但按默认约定给出可用分层
    assert "仅由逐笔聚合" in res.caveat and "默认约定" in res.caveat


def test_unconfident_calibration_is_not_cached_for_the_day(db, monkeypatch):
    """盘前第一笔数据残缺导致校准失败时，不能把失败结论钉一整天。"""
    monkeypatch.setattr(watch_mod, "_service", None)
    tx = FakeTx({"600000.SH": _row("600000.SH", outer=None, inner=None)})
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=tx, em_feed=FakeEm(
        df=_ticks([("2026-11-04 09:25:00", "09:25:00", 10.0, 100.0, 1, "4", 1000.0)])))
    svc.refresh(["600000.SH"])
    svc.flow("600000.SH", force=True)
    assert svc._calibration == {}                     # 未校准 → 不写缓存
    # 换成有官方内外盘的快照后应能立刻校准
    svc._tx = FakeTx({"600000.SH": _row("600000.SH", outer=600.0, inner=400.0, vol=1000.0)})
    svc._em = FakeEm(df=_ticks([
        ("2026-11-04 09:31:00", "09:31:00", 10.0, 600.0, 6, "2", 6000.0),
        ("2026-11-04 09:32:00", "09:32:00", 10.0, 400.0, 4, "1", 4000.0)]))
    svc.refresh(["600000.SH"])
    res = svc.flow("600000.SH", force=True)
    assert res.calibrated is True and "global" in svc._calibration


def test_flow_survives_tick_failure_and_still_reports_official(db, monkeypatch):
    monkeypatch.setattr(watch_mod, "_service", None)
    tx = FakeTx({"600000.SH": _row("600000.SH")})
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=tx, em_feed=FakeEm(fail=True))
    svc.refresh(["600000.SH"])
    res = svc.flow("600000.SH", force=True)
    assert res.outer_volume == 600.0                  # 官方量照常给出
    assert res.tick_buy_volume == 0.0
    assert "逐笔获取失败" in res.caveat
    assert svc.stats["errors"] == 1


def test_freeze_writes_and_reads_back(db, monkeypatch):
    monkeypatch.setattr(watch_mod, "_service", None)
    df = _ticks([
        ("2026-11-04 09:31:00", "09:31:00", 10.0, 600.0, 6, "2", 6000.0),
        ("2026-11-04 09:32:00", "09:32:00", 10.0, 400.0, 4, "1", 4000.0),
    ])
    tx = FakeTx({"600000.SH": _row("600000.SH"), "00700.HK": _row("00700.HK", outer=None,
                                                                  inner=None)})
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=tx, em_feed=FakeEm(df=df))
    svc.set_pool(["600000.SH", "00700.HK"])
    out = svc.freeze()
    assert out["saved"] == 2 and out["failed"] == []
    latest = repo.flow_latest(out["trade_date"], db_path=db)
    assert {r["symbol"] for r in latest} == {"600000.SH", "00700.HK"}
    a = next(r for r in latest if r["symbol"] == "600000.SH")
    assert a["outer_volume"] == 600.0 and a["tick_buy_volume"] == 600.0
    assert a["flow_source"] == "official_outer_inner"
    import json
    assert json.loads(a["side_map"])["2"] == "buy"
    # 幂等：同一天再定格不产生重复行
    svc.freeze()
    assert len(repo.flow_latest(out["trade_date"], db_path=db)) == 2
    hist = repo.flow_history("600000.SH", db_path=db)
    assert hist[0]["trade_date"] == out["trade_date"]
    assert out["trade_date"] in repo.flow_dates(db_path=db)


def test_freeze_skips_empty_days(db, monkeypatch):
    """停牌/无成交日不落空行——否则历史表里全是 0，复盘看不出区别。"""
    monkeypatch.setattr(watch_mod, "_service", None)
    tx = FakeTx({"600000.SH": _row("600000.SH", outer=None, inner=None, vol=0.0)})
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=tx, em_feed=FakeEm(df=pd.DataFrame()))
    svc.set_pool(["600000.SH"])
    assert svc.freeze()["saved"] == 0
    assert repo.flow_dates(db_path=db) == []


def test_detail_shape_and_ticks_pagination(db, monkeypatch):
    monkeypatch.setattr(watch_mod, "_service", None)
    df = _ticks([
        ("2026-11-04 09:31:00", "09:31:00", 10.0, 100.0, 1, "2", 1000.0),
        ("2026-11-04 09:32:00", "09:32:00", 10.1, 200.0, 2, "1", 2020.0),
        ("2026-11-04 09:33:00", "09:33:00", 10.2, 300.0, 3, "2", 3060.0),
    ])
    tx = FakeTx({"600000.SH": _row("600000.SH", outer=400.0, inner=200.0, vol=600.0)})
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=tx, em_feed=FakeEm(df=df,
                              quote={"pe_ttm": 5.0}))
    svc.set_pool(["600000.SH"])
    d = svc.detail("600000.SH")
    assert d["board"] == "main" and d["session"]["market"] == "CN"
    assert "bids" not in d["snapshot"] and len(d["book"]["bids"]) == 2
    assert d["flow"]["symbol"] == "600000.SH"
    assert d["quote_extra"]["pe_ttm"] == 5.0
    assert d["intraday"][0]["dt"] == "09:31"
    page = svc.ticks("600000.SH", limit=2)
    assert page["total"] == 3 and page["calibrated"] is True
    assert page["side_map"]["2"] == "buy"
    assert page["rows"][0]["time"] == "09:33:00"               # 最新在前
    assert {r["direction"] for r in page["rows"]} <= {"buy", "sell", "neutral", "unknown"}
    sells = svc.ticks("600000.SH", side="sell")
    assert all(r["direction"] == "sell" for r in sells["rows"])
    assert sells["total"] == 1                                   # 只有一笔主动卖


def test_empty_pool_falls_back_to_default_watchlist(db, monkeypatch):
    monkeypatch.setattr(watch_mod, "_service", None)
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=FakeTx(), em_feed=None)
    pool = svc.pool()
    assert pool and all(s.endswith((".SH", ".SZ", ".HK")) for s in pool)
    assert any(s.endswith(".HK") for s in pool)         # 港股必须在默认池里
    assert len(pool) == len(set(pool))


def test_add_and_remove_normalizes_symbols(db, monkeypatch):
    monkeypatch.setattr(watch_mod, "_service", None)
    tx = FakeTx({"00700.HK": _row("00700.HK", name="腾讯控股")})
    # 两个通道都注桩：留 None 会让服务去装配真实 ChannelHub（进而真连数据源），单测就该关在本地
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=tx, em_feed=FakeEm())
    added = svc.add("hk00700")
    assert added["symbol"] == "00700.HK" and added["board"] == "hk"
    assert added["name"] == "腾讯控股"
    svc.add("600000")
    assert set(svc.pool()) >= {"00700.HK", "600000.SH"}
    assert svc.remove("00700") is True
    assert "00700.HK" not in svc.pool()
    assert {r.symbol for r in repo.watchlist_list(db_path=db)} == {"600000.SH"}
    with pytest.raises((SymbolError, ValueError)):
        svc.add("不是代码")


def test_status_is_honest_when_feeds_missing(db, monkeypatch):
    """通道装配中枢也没货时：状态里必须写清"未就绪"，刷新返回空并留错误原因，不编数字。"""
    monkeypatch.setattr(watch_mod, "_service", None)

    class EmptyHub:
        def feed(self, name):
            return None

    import quantdesk.services.channels as channels_mod

    monkeypatch.setattr(channels_mod, "get_hub", lambda: EmptyHub())
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=None, em_feed=None)
    st = svc.status()
    assert st["feeds"]["tx_quote"]["ready"] is False
    assert st["flow_ready"] is False
    assert "只读" in st["note"]
    assert svc.refresh(["600000.SH"]) == [] and svc.last_error != ""
    assert svc.stats["errors"] == 1


# ---------------- API 层（只读语义） ----------------

def _svc_with_data(db, monkeypatch):
    monkeypatch.setattr(watch_mod, "_service", None)
    df = _ticks([
        ("2026-11-04 09:31:00", "09:31:00", 10.0, 400.0, 4, "2", 4000.0),
        ("2026-11-04 09:32:00", "09:32:00", 10.0, 200.0, 2, "1", 2000.0),
        ("2026-11-04 09:33:00", "09:33:00", 10.0, 100.0, 1, "2", 1000.0),
    ])
    tx = FakeTx({"600000.SH": _row("600000.SH", outer=500.0, inner=200.0, vol=700.0)})
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=tx, em_feed=FakeEm(df=df))
    svc.set_pool(["600000.SH"])
    watch_mod._service = svc
    return svc


def test_watch_api_endpoints(db, monkeypatch):
    from quantdesk.app import app

    _svc_with_data(db, monkeypatch)
    with TestClient(app) as client:
        assert client.get("/api/watch/status").json()["pool"] == ["600000.SH"]
        ref = client.post("/api/watch/refresh").json()
        assert ref["count"] == 1 and ref["list"][0]["symbol"] == "600000.SH"
        listing = client.get("/api/watch/list").json()["rows"]
        assert listing[0]["outer_pct"] == pytest.approx(500.0 / 700.0 * 100, abs=0.02)
        d = client.get("/api/watch/600000.SH").json()
        assert d["flow"]["outer_volume"] == 500.0 and d["book"]["asks"]
        t = client.get("/api/watch/600000.SH/ticks", params={"limit": 2}).json()
        assert t["total"] == 3 and len(t["rows"]) == 2
        assert client.get("/api/watch/600000.SH/ticks", params={"side": "nope"}).status_code == 400
        assert client.get("/api/watch/不是代码").status_code == 400
        h = client.get("/api/watch/history").json()["rows"]
        assert isinstance(h, list)
        assert client.get("/api/watch/pool").json()["pool"] == ["600000.SH"]
        body = client.post("/api/watch/pool", json={"symbols": ["hk00700", "不是代码"]}).json()
        assert [a["symbol"] for a in body["added"]] == ["00700.HK"]
        assert body["added"][0]["board"] == "hk"
        assert body["errors"][0]["symbol"] == "不是代码"
        assert "00700.HK" in body["pool"]
        assert client.request("DELETE", "/api/watch/pool", json={"symbol": "00700"}).json()["removed"]


def test_watch_api_has_no_order_endpoints(db, monkeypatch):
    """只读产品不能在盯盘路由上留下任何写入口——用 openapi 路径清单兜住这条红线。"""
    from quantdesk.app import app

    paths = {p for p in app.openapi()["paths"]}
    watch_paths = [p for p in paths if p.startswith("/api/watch")]
    assert watch_paths, "盯盘路由没注册上"
    for p in watch_paths:
        assert not any(k in p.lower() for k in ("order", "buy", "sell", "trade", "cancel"))


def test_watch_kline_uses_history_feed_when_local_empty(db, monkeypatch):
    from quantdesk.app import app

    svc = _svc_with_data(db, monkeypatch)

    class HistoryTx(FakeTx):
        def get_history(self, symbol, freq, start, end, adjust=None):
            return pd.DataFrame({
                "dt": pd.to_datetime(["2026-11-03", "2026-11-04"]),
                "open": [9.8, 9.9], "high": [10.0, 10.2], "low": [9.7, 9.9],
                "close": [9.9, 10.1], "volume": [1000.0, 1200.0], "amount": [0.0, 0.0]})

    svc._tx = HistoryTx({"600000.SH": _row("600000.SH")})
    with TestClient(app) as client:
        r = client.get("/api/watch/600000.SH/kline", params={"freq": "1d", "days": 5})
        assert r.status_code == 200
        body = r.json()
        assert body["rows"][-1]["close"] == 10.1
        assert "tx_quote" in body["source"]
        assert body["unit"] == "手"
        assert client.get("/api/watch/600000.SH/kline", params={"freq": "60"}).status_code == 400


# ---------------- 休市闸（自动路径不外呼） ----------------

def test_closed_session_does_not_call_out(db, monkeypatch):
    """盯盘页是常开页面：休市时任何自动路径都不许再打公开接口（否则本机 IP 被冷却）。"""
    _closed_clock(monkeypatch)
    df = _ticks([("2026-11-04 09:31:00", "09:31:00", 10.0, 600.0, 6, "2", 6000.0),
                 ("2026-11-04 09:32:00", "09:32:00", 10.0, 400.0, 4, "1", 4000.0)])
    tx = FakeTx({"600000.SH": _row("600000.SH")})
    em = FakeEm(df=df)
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=tx, em_feed=em)
    svc.set_pool(["600000.SH"])
    svc.refresh(["600000.SH"])              # 显式刷新：允许外呼
    svc.flow("600000.SH")                   # 把当日数据取回来（第一次当然会打接口）
    svc.detail("600000.SH")
    n_snap, n_tick = tx.calls, em.tick_calls
    out = svc.refresh_auto()                # 自动刷新（调度任务/页面轮询走这条）
    assert out["polled"] is False and tx.calls == n_snap
    assert "休市" in out["reason"] and len(out["rows"]) == 1
    assert svc.flow("600000.SH").outer_volume == 600.0      # 复用缓存
    assert em.tick_calls == n_tick
    svc.ticks("600000.SH")                                  # 翻页复用同一份取数
    svc.detail("600000.SH")
    assert tx.calls == n_snap and em.tick_calls == n_tick


def test_force_bypasses_the_closed_session_guard(db, monkeypatch):
    """用户按「立即刷新」时必须拿到真答案：force 绕过时段闸与缓存。"""
    _closed_clock(monkeypatch)
    df = _ticks([("2026-11-04 09:31:00", "09:31:00", 10.0, 600.0, 6, "2", 6000.0),
                 ("2026-11-04 09:32:00", "09:32:00", 10.0, 400.0, 4, "1", 4000.0)])
    tx = FakeTx({"600000.SH": _row("600000.SH")})
    em = FakeEm(df=df)
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=tx, em_feed=em)
    svc.set_pool(["600000.SH"])
    svc.detail("600000.SH")
    n_snap, n_tick = tx.calls, em.tick_calls
    out = svc.refresh_auto(force=True)
    assert out["polled"] is True and tx.calls == n_snap + 1
    d = svc.detail("600000.SH", force=True)
    assert em.tick_calls > n_tick                           # force 一路传到取数层
    assert d["flow"]["outer_volume"] == 600.0


def test_refresh_auto_polls_while_market_open(db, monkeypatch):
    """盘中共识：open/pre_open/post_close 都要真的去打（post_close 还有盘后成交）。"""
    monkeypatch.setattr(watch_mod, "session_of",
                        lambda market, now=None: _REAL_SESSION_OF(market, datetime(2026, 11, 4, 15, 20)))
    tx = FakeTx({"600000.SH": _row("600000.SH")})
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=tx, em_feed=FakeEm())
    svc.set_pool(["600000.SH"])
    out = svc.refresh_auto()
    assert out["polled"] is True and tx.calls == 1


def test_refresh_route_reports_skipped_and_force(db, monkeypatch):
    from quantdesk.app import app

    _closed_clock(monkeypatch)
    svc = _svc_with_data(db, monkeypatch)
    svc.refresh(["600000.SH"])
    calls = svc._tx.calls
    with TestClient(app) as client:
        r = client.post("/api/watch/refresh").json()
        assert r["polled"] is False and r["skipped"] is True and r["count"] == 1
        assert svc._tx.calls == calls                 # 自动刷新一次都没外呼
        f = client.post("/api/watch/refresh", params={"force": True}).json()
        assert f["polled"] is True and f["skipped"] is False
        assert svc._tx.calls == calls + 1


def test_unit_labels_are_chinese_from_the_service(db, monkeypatch):
    """两个源把量单位写成 lot/share，服务层统一成中文并保留原码——单位差一次就是 100 倍。"""
    tx = FakeTx({"600000.SH": {**_row("600000.SH"), "volume_unit": "lot"},
                 "00700.HK": {**_row("00700.HK"), "volume_unit": "share"}})
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=tx, em_feed=FakeEm())
    rows = {r["symbol"]: r for r in svc.refresh(["600000.SH", "00700.HK"])}
    assert rows["600000.SH"]["volume_unit"] == "手"
    assert rows["00700.HK"]["volume_unit"] == "股"
    assert rows["600000.SH"]["volume_unit_code"] == "lot"
    assert rows["00700.HK"]["volume_unit_code"] == "share"


def test_first_load_seeds_even_when_closed(db, monkeypatch):
    """收盘后打开页面也必须看到当日真实数字：缓存里缺谁就补谁，之后才闭嘴。"""
    _closed_clock(monkeypatch)
    tx = FakeTx({"600000.SH": _row("600000.SH")})
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=tx, em_feed=FakeEm())
    svc.set_pool(["600000.SH"])
    first = svc.refresh_auto()
    assert first["polled"] is True and len(first["rows"]) == 1
    assert "快照" in first["reason"]
    second = svc.refresh_auto()
    assert second["polled"] is False and tx.calls == 1      # 只打了一次
    assert len(second["rows"]) == 1                          # 但列表照样有数


def test_partial_cache_still_seeds_the_rest_of_the_pool(db, monkeypatch):
    """休市时的"要不要补取"必须按标的逐个判，不能用"缓存空不空"。

    真实现场：重启后端 → 先用 curl 打开一只的详情（详情会补取该只快照）→ 再开盯盘页。
    此时 `bool(self._live)` 为真，旧逻辑认为"已经种过了"，其余 7 只永远不会被取，
    列表于是显示一排 `last=0.0` 的陈旧行——而"价格是 0"看起来比"没数据"更像结论。
    """
    _closed_clock(monkeypatch)
    rows = {s: _row(s) for s in ("600000.SH", "000001.SZ", "00700.HK")}
    tx = FakeTx(rows)
    svc = WatchService(bus=EventBus(), db_path=db, tx_feed=tx, em_feed=FakeEm())
    svc.set_pool(["600000.SH", "000001.SZ", "00700.HK"])
    svc.flow("00700.HK", force=True)                  # 详情只把 00700 放进缓存
    assert set(svc._live) == {"00700.HK"}
    out = svc.refresh_auto()
    assert out["polled"] is True, "池内还有两只没快照，休市也必须补一次"
    assert len(out["rows"]) == 3
    listing = svc.list_rows()
    assert all(r["last"] > 0 for r in listing), "补齐后不该再有 0 价行"
    assert not any(r["stale"] for r in listing)
    calls = tx.calls
    again = svc.refresh_auto()                        # 全都齐了 → 闭嘴
    assert again["polled"] is False and tx.calls == calls
    assert "未外呼" in again["reason"]

    # 补不上来的标的（代码错/停牌无数据）不能变成"每次进页面都撞一次接口"：
    # 冷却是按服务算的，一次成功种过之后，短期内新增缺失标的只报原因不再外呼
    svc.set_pool(["600000.SH", "000001.SZ", "00700.HK", "600519.SH"])
    svc._seed_attempt_at = 0.0                         # 当作冷却已过期
    miss = svc.refresh_auto()
    assert miss["polled"] is True                      # 缺 600519，试一次
    tried = tx.calls
    hold = svc.refresh_auto()
    assert hold["polled"] is False and tx.calls == tried, "冷却期内不该再撞"
    assert "不重复补取" in hold["reason"]
