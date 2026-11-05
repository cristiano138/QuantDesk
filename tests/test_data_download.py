"""数据下载任务接口：提交即返回、进度可轮、失败原因按标的落位。

这里测的是"界面点下载之后到底发生了什么"——数据管理页那个下载按钮曾经没有后端接口，
是真交付缺陷。用例把行情源与入库都换成替身，只验编排：参数校验、逐标的进度、
fill/overwrite 走的是不同写入语义、源全不可用时的如实报错（不假装在下载）。
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from quantdesk.api import routes_data
from quantdesk.config import get_settings
from quantdesk.services.data_store import DownloadReport


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """跑测试不许碰真实 data 目录（宪法级纪律：本项目为此付过一次代价）。"""
    monkeypatch.setenv("QUANTDESK_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    routes_data._TASKS.clear()
    yield
    routes_data._TASKS.clear()
    get_settings.cache_clear()


@pytest.fixture()
def client():
    from quantdesk.app import app

    return TestClient(app)


class _Call:
    """DataStoreService 的替身：记下每次调用的写入语义与参数，返回可控报表。"""

    def __init__(self) -> None:
        self.records: list[tuple] = []

    def _rep(self, symbols):
        rep = DownloadReport(started_at=0)
        for s in symbols:
            if s.endswith(".SZ"):
                rep.failed[s] = "AdapterError: 源侧限速中"
            else:
                rep.ok[s] = 120
        rep.finished_at = int(time.time() * 1000)
        return rep

    def backfill(self, symbols, freq, start, end, adjust=None, **kw):
        self.records.append(("fill", list(symbols), freq.value, adjust.value, start, end))
        return self._rep(symbols)

    def batch_download(self, symbols, freq, start, end, adjust=None, **kw):
        self.records.append(("overwrite", list(symbols), freq.value, adjust.value, start, end))
        return self._rep(symbols)


@pytest.fixture
def calls(monkeypatch):
    rec = _Call()
    monkeypatch.setattr("quantdesk.services.data_store.DataStoreService.backfill", rec.backfill)
    monkeypatch.setattr("quantdesk.services.data_store.DataStoreService.batch_download",
                        rec.batch_download)
    return rec


@pytest.fixture
def hub(monkeypatch):
    class _Feed:
        name = "akshare"

    class _Hub:
        ready = True

        def history_chain(self):
            return ["qmt_xtdata", "tushare", "akshare"]

        def history_feeds(self):
            return [_Feed()] if self.ready else []

        def feed_errors(self):
            return {} if self.ready else {"akshare": "限速冷却中"}

        def federated(self):
            return object()

    h = _Hub()
    monkeypatch.setattr("quantdesk.services.channels.get_hub", lambda: h)
    return h


def _wait(task_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = routes_data._TASKS[task_id].to_dict()
        if state["state"] in ("done", "error"):
            return state
        time.sleep(0.05)
    raise AssertionError("任务没进终态")


def test_download_returns_task_id_and_progresses(client, calls, hub):
    r = client.post("/api/data/download", json={
        "symbols": ["600000.SH", "000001.SZ"], "freq": "1d",
        "start": "2024-01-01", "end": "2024-03-01", "adjust": "qfq"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"].startswith("DL-") and body["total"] == 2

    final = _wait(body["task_id"])
    assert final["state"] == "done"
    assert final["done"] == 2 and final["current"] == ""
    assert final["ok"]["600000.SH"] == 120
    # 单标的失败不吞：另一只照常成功，失败这只带后端原文
    assert "000001.SZ" in final["failed"] and "限速" in final["failed"]["000001.SZ"]
    # 失败的那只不许同时出现在 ok 里（0 行 ≠ 成功，浏览器实测过这个双列表缺陷）
    assert "000001.SZ" not in final["ok"]


def test_default_mode_is_fill_not_overwrite(client, calls, hub):
    """默认必须只补缺不覆盖：公开源字段不齐，整段重写等于把已有成交额打空。"""
    tid = client.post("/api/data/download", json={"symbols": ["600000.SH"]}).json()["task_id"]
    _wait(tid)
    assert calls.records[-1][0] == "fill"


def test_overwrite_mode_is_explicit(client, calls, hub):
    tid = client.post("/api/data/download", json={
        "symbols": ["600519.SH"], "mode": "overwrite"}).json()["task_id"]
    _wait(tid)
    assert calls.records[-1][0] == "overwrite"


def test_adjust_aliases_map_to_enum_values(client, calls, hub):
    for sent, want in (("qfq", "forward"), ("hfq", "backward"), ("none", "none")):
        tid = client.post("/api/data/download", json={
            "symbols": ["600000.SH"], "adjust": sent}).json()["task_id"]
        _wait(tid)
        assert calls.records[-1][3] == want, sent


def test_symbols_are_normalized_and_deduped_before_download(client, calls, hub):
    """界面允许粘 sh600000 / 600000 这类写法，入库必须用统一代码。"""
    tid = client.post("/api/data/download", json={
        "symbols": ["sh600000", "600000.SH", "00700.HK"]}).json()["task_id"]
    final = _wait(tid)
    # 逐标的各调一次（进度与失败都要落到具体那一只），所以看调用轨迹的集合
    assert {r[1][0] for r in calls.records} == {"600000.SH", "00700.HK"}
    assert final["total"] == 2 and set(final["ok"]) | set(final["failed"]) == {"600000.SH", "00700.HK"}


def test_bad_params_are_400_before_any_thread(client, calls, hub):
    r = client.post("/api/data/download", json={"symbols": []})
    assert r.status_code == 400 and "symbols" in r.json()["detail"]

    r = client.post("/api/data/download", json={"symbols": ["600000.SH"], "freq": "13m"})
    assert r.status_code == 400 and "未知周期" in r.json()["detail"]

    r = client.post("/api/data/download", json={"symbols": ["600000.SH"], "mode": "yolo"})
    assert r.status_code == 400

    r = client.post("/api/data/download", json={"symbols": ["不是代码"]})
    assert r.status_code == 400 and "代码无法识别" in r.json()["detail"]

    r = client.post("/api/data/download", json={
        "symbols": ["600000.SH"], "start": "2025-06-01", "end": "2024-01-01"})
    assert r.status_code == 400 and "开始日期晚于结束日期" in r.json()["detail"]

    r = client.post("/api/data/download", json={"symbols": ["600000.SH"], "start": "6月1号"})
    assert r.status_code == 400 and "YYYY-MM-DD" in r.json()["detail"]
    assert not routes_data._TASKS            # 校验失败不留半截任务


def test_all_sources_unready_reports_honest_reason_without_downloading(client, calls, hub):
    hub.ready = False
    tid = client.post("/api/data/download", json={"symbols": ["600000.SH"]}).json()["task_id"]
    final = _wait(tid)
    assert final["state"] == "error"
    assert "未就绪" in final["error"] and "feed_priority_history" in final["error"]
    assert calls.records == []               # 一条都没尝试拉——错误要说清原因，不是慢慢失败


def test_task_404_detail_says_why(client, calls, hub):
    r = client.get("/api/data/task/DL-NOTEXIST")
    assert r.status_code == 404
    assert "不存在" in r.json()["detail"] and "重启" in r.json()["detail"]


def test_chains_endpoint_lists_ready_and_unready(client, hub):
    body = client.get("/api/data/chains").json()
    assert body["history_chain"] == ["qmt_xtdata", "tushare", "akshare"]
    assert body["ready"] == ["akshare"]
    # 没被尝试装配的通道不能算就绪——按"没报错"反推会把 qmt/tushare 显示成能用
    assert {u["name"] for u in body["unready"]} == {"qmt_xtdata", "tushare"}
    hub.ready = False
    body = client.get("/api/data/chains").json()
    assert body["ready"] == []
    assert {"name": "akshare", "reason": "限速冷却中"} in body["unready"]


def test_default_range_is_last_year(client, calls, hub):
    tid = client.post("/api/data/download", json={"symbols": ["600000.SH"]}).json()["task_id"]
    final = _wait(tid)
    start, end = final["start"], final["end"]
    assert start < end
    assert (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days == 365


# ---------------- 本地库清单 ----------------

def test_inventory_lists_symbols_freqs_and_ranges(tmp_path, monkeypatch):
    """清单是"回测为什么报本地无数据"的唯一自查入口，也是降级数据的可见化手段。"""
    import pandas as pd

    from quantdesk.core.enums import AdjustType, Frequency
    from quantdesk.store.parquet_store import ParquetStore

    monkeypatch.setenv("QUANTDESK_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    store = ParquetStore(root=tmp_path / "parquet")
    df = pd.DataFrame({
        "dt": pd.to_datetime(["2024-01-02", "2024-01-03"]),
        "open": [10.0, 10.1], "high": [10.2, 10.3], "low": [9.9, 10.0],
        "close": [10.05, 10.2], "volume": [100, 200], "amount": [0.0, 0.0],
    })
    store.write("600000.SH", Frequency.D1, df, AdjustType.FORWARD)
    store.write("00700.HK", Frequency.D1, df.assign(amount=[1e6, 2e6]), AdjustType.NONE)

    inv = store.inventory()
    assert len(inv) == 2
    hk = next(i for i in inv if i["symbol"] == "00700.HK")
    assert hk["adjust"] == "none" and hk["rows"] == 2 and hk["amount_zero_pct"] == 0.0
    tx = next(i for i in inv if i["symbol"] == "600000.SH")
    assert tx["amount_zero_pct"] == 100.0          # 腾讯日线那种"有行无额"必须被看见
    assert tx["start"] == "2024-01-02" and tx["end"] == "2024-01-03"


def test_inventory_endpoint_totals(client):
    r = client.get("/api/data/inventory")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"root", "items", "symbols", "entries", "rows", "bad_files", "rows_by_freq"}
    assert body["entries"] == len(body["items"])
