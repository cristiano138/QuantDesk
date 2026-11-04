"""D27：盘后定时任务测试（调度接线 + 任务体可注入）。"""

from quantdesk.core.events import EventBus
from quantdesk.services.scheduler import PostCloseScheduler


def test_scheduler_cron_wired():
    """18:00 盘后下载 + 盯盘轮询 + A股/港股各自收盘定格与 K 线补齐；启停不抛。"""
    s = PostCloseScheduler(symbols=["600000.SH"], bus=EventBus())
    s.start()
    try:
        jobs = {j.id: j for j in s._scheduler.get_jobs()}
        assert set(jobs) == {"post_close_refresh", "watch_poll",
                             "watch_freeze_cn", "watch_freeze_hk",
                             "watch_kline_cn", "watch_kline_hk"}
        fields = {f.name: str(f) for f in jobs["post_close_refresh"].trigger.fields}
        assert fields["hour"] == "18" and fields["minute"] == "0"
        assert fields["day_of_week"] in ("mon-fri", "0-4")
        # 收盘定格按市场分开：A股 15:05、港股 16:10（港股收市竞价在 16:00-16:10）
        cn = {f.name: str(f) for f in jobs["watch_freeze_cn"].trigger.fields}
        hk = {f.name: str(f) for f in jobs["watch_freeze_hk"].trigger.fields}
        assert (cn["hour"], cn["minute"]) == ("15", "5")
        assert (hk["hour"], hk["minute"]) == ("16", "10")
        # 每日自动 K 线：定格之后再补一次本地日线（A股 15:25 / 港股 16:25）
        kcn = {f.name: str(f) for f in jobs["watch_kline_cn"].trigger.fields}
        khk = {f.name: str(f) for f in jobs["watch_kline_hk"].trigger.fields}
        assert (kcn["hour"], kcn["minute"]) == ("15", "25")
        assert (khk["hour"], khk["minute"]) == ("16", "25")
        assert jobs["watch_poll"].trigger.interval.total_seconds() >= 2.0
    finally:
        s.stop()


def test_watch_jobs_follow_the_switch(monkeypatch):
    """设置页关掉自动盯盘，调度器就不该再排那五个盯盘任务（用户有权让它不打扰接口）。"""
    import quantdesk.config as cfg

    class Off:
        watch_enabled = False
        watch_poll_interval_s = 5.0
        watch_default_symbols = ""

    monkeypatch.setattr(cfg, "get_settings", lambda: Off())
    s = PostCloseScheduler(symbols=["600000.SH"], bus=EventBus())
    s.start()
    try:
        assert {j.id for j in s._scheduler.get_jobs()} == {"post_close_refresh"}
    finally:
        s.stop()


def test_refresh_job_publishes_log():
    """任务体运行（不触网的桩路径：依赖缺失也被结构化捕获）并广播 LOG。"""
    events = []
    bus = EventBus()
    bus.subscribe("log", lambda e: events.append(e.data))
    s = PostCloseScheduler(symbols=["600000.SH"], bus=bus)
    # 不注入真实 feed：refresh_job 内部 import 真实源——CI 环境有 akshare，
    # 网络失败会落入 failed 列表（结构化），不抛异常
    out = s.refresh_job(symbols=["600000.SH"])
    assert "ran_at" in out and "ok" in out and "failed" in out
    assert any(e.get("message", "").startswith("盘后刷新完成") for e in events)


class _StubKlineFeed:
    """假 K 线通道：任务体真跑一遍，别让 NameError 这类错误藏在"只测注册"的绿线后面。"""

    name = "tx_quote"

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_history(self, symbol, freq, start, end, adjust=None):
        import pandas as pd
        self.calls.append((symbol, str(adjust)))
        return pd.DataFrame([[pd.Timestamp("2024-01-02"), 10.0, 10.5, 9.8, 10.2, 100.0, 0.0]],
                            columns=["dt", "open", "high", "low", "close", "volume", "amount"])


class _StubWatch:
    def __init__(self, pool, feed):
        self._pool = pool
        self._feed = feed

    def pool(self):
        return list(self._pool)

    def history_feed(self):
        return self._feed


def _prepare_watch(tmp_path, monkeypatch, request, pool, feed):
    monkeypatch.setenv("QUANTDESK_DATA_DIR", str(tmp_path))
    from quantdesk.config import get_settings
    get_settings.cache_clear()
    import quantdesk.services.watch as wmod
    monkeypatch.setattr(wmod, "get_watch_service", lambda: _StubWatch(pool, feed))
    # 用例结束后清掉 settings 缓存：留着 tmp 目录，后面别的用例就会读到假的数据目录
    request.addfinalizer(get_settings.cache_clear)


def test_watch_kline_job_runs_and_lands_rows(tmp_path, monkeypatch, request):
    _prepare_watch(tmp_path, monkeypatch, request, ["600000.SH", "00700.HK"], _StubKlineFeed({}))
    events = []
    bus = EventBus()
    bus.subscribe("log", lambda e: events.append(e.data.get("message", "")))
    s = PostCloseScheduler(symbols=["600000.SH"], bus=bus)

    cn = s.watch_kline_job("CN")
    hk = s.watch_kline_job("HK")
    assert cn["symbols"] == 1 and hk["symbols"] == 1
    assert cn["rows_forward"] == 1 and cn["rows_none"] == 1
    assert not cn.get("failed") and not hk.get("failed")

    from quantdesk.core.enums import AdjustType, Frequency
    from quantdesk.store.parquet_store import ParquetStore
    store = ParquetStore()
    for adj in (AdjustType.FORWARD, AdjustType.NONE):
        assert len(store.read("600000.SH", Frequency.D1, adjust=adj)) == 1
        assert len(store.read("00700.HK", Frequency.D1, adjust=adj)) == 1
    assert any("K 线补齐" in m for m in events), events


def test_watch_kline_job_reports_missing_feed(tmp_path, monkeypatch, request):
    _prepare_watch(tmp_path, monkeypatch, request, ["600000.SH"], _StubKlineFeed({}))
    import quantdesk.services.watch as wmod
    monkeypatch.setattr(wmod, "get_watch_service",
                        lambda: _StubWatch(["600000.SH"], None))
    out = PostCloseScheduler(symbols=["600000.SH"], bus=None).watch_kline_job("CN")
    assert "未就绪" in out["error"]
