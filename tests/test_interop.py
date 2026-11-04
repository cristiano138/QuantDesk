"""主流股票软件互操作单测：通达信板块格式、公式方言、规则求值同源、路径安全。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quantdesk.services import interop
from quantdesk.services.interop import InteropError, SignalRule


def _bars(closes: list[float], start="2026-01-05") -> pd.DataFrame:
    dt = pd.date_range(start, periods=len(closes), freq="D")
    arr = np.asarray(closes, dtype=float)
    return pd.DataFrame({"dt": dt, "open": arr, "high": arr * 1.01,
                         "low": arr * 0.99, "close": arr,
                         "volume": np.linspace(1000, 5000, len(closes)),
                         "amount": arr * 2000})


# ---------- 通达信板块文件 ----------
def test_tdx_line_format_market_prefix_and_roundtrip():
    assert interop.to_tdx_line("600000.SH") == "1600000"
    assert interop.to_tdx_line("000001.SZ") == "0000001"
    assert interop.to_tdx_line("300750.SZ") == "0300750"
    for sym in ("600000.SH", "000001.SZ", "688981.SH"):
        assert interop.from_tdx_line(interop.to_tdx_line(sym)) == sym


def test_bse_requires_explicit_prefix_and_is_skipped_not_guessed():
    with pytest.raises(InteropError):
        interop.to_tdx_line("830001.BJ")
    assert interop.to_tdx_line("830001.BJ", bse_prefix="2") == "2830001"
    text = interop.export_tdx_block(["600000.SH", "830001.BJ"])
    assert "1600000" in text and "830001" not in text.split("{")[0]
    assert "北交所" in text                              # 跳过必须留痕告知，不能静默


def test_export_uses_crlf_and_parses_back():
    text = interop.export_tdx_block(["600000.SH", "000001.SZ", "600519.SH"])
    assert "\r\n" in text
    assert interop.parse_tdx_block(text) == ["600000.SH", "000001.SZ", "600519.SH"]


def test_parse_tdx_ignores_comments_and_bad_lines():
    text = "{QuantDesk 导出}\r\n1600000\r\n乱码行\r\n0000001\r\n"
    assert interop.parse_tdx_block(text) == ["600000.SH", "000001.SZ"]


def test_parse_bare_block_handles_names_and_junk():
    text = "600000,浦发银行\r\n000001\r\nSZ600519\r\n601318.SH\r\n"
    assert interop.parse_bare_block(text) == ["600000.SH", "000001.SZ"]


# ---------- 落盘与路径安全 ----------
def test_write_block_names_are_validated(tmp_path, monkeypatch):
    from quantdesk import config as cfg

    monkeypatch.setattr(cfg, "data_dir", lambda: tmp_path)
    with pytest.raises(InteropError):
        interop.write_block("../../etc/passwd", "1600000", "tdx")
    with pytest.raises(InteropError):
        interop.write_block("板块|管道", "1600000", "tdx")
    res = interop.write_block("量化信号", "1600000\r\n0000001", "tdx")
    out = tmp_path / "exports" / "量化信号.blk"
    assert res["path"] == str(out) and out.exists()
    assert out.read_bytes().decode("gbk").startswith("1600000")   # 真的 GBK 编码


def test_write_to_client_only_when_install_dir_exists(tmp_path, monkeypatch):
    from quantdesk import config as cfg

    monkeypatch.setattr(cfg, "data_dir", lambda: tmp_path)

    class S:
        tdx_install_dir = ""

    monkeypatch.setattr(cfg, "get_settings", lambda: S())
    res = interop.write_block("板块A", "1600000", "tdx", write_to_client=True)
    assert res["client_path"] == ""                      # 目录不存在 → 不假装写成功

    base = tmp_path / "tdx"
    base.mkdir()
    S.tdx_install_dir = str(base)
    res = interop.write_block("板块B", "1600000", "tdx", write_to_client=True)
    assert res["client_path"].endswith("板块B.blk")
    assert (base / "T0002" / "blocknew" / "板块B.blk").exists()


# ---------- 公式方言 ----------
def test_formula_for_both_dialects_differs_in_assignment():
    rule = SignalRule(kind="ma_cross", params={"fast": 5, "slow": 20})
    tdx = interop.compile_formula(rule, "tdx")
    ths = interop.compile_formula(rule, "ths")
    assert "F5:MA(CLOSE,5);" in tdx
    assert "F5:=MA(CLOSE,5);" in ths
    assert "CROSS(F5,S20)" in tdx and "XG" in tdx
    with pytest.raises(InteropError):
        interop.compile_formula(rule, "unknown_dialect")


def test_formula_for_each_rule_kind():
    for kind, params in (("ma_cross", {"fast": 10, "slow": 30, "direction": "death"}),
                         ("macd_golden", {}), ("rsi_threshold", {"period": 14, "op": "above",
                                                                 "value": 70}),
                         ("volume_spike", {"ma_days": 5, "multiple": 3})):
        text = interop.compile_formula(SignalRule(kind=kind, params=params), "tdx")
        assert text.endswith("XG\r\n") and kind          # 条件写在最后一条语句
        assert "{" in text and "}" in text               # 注释块（公式管理器惯例）


def test_rule_param_validation_is_strict():
    with pytest.raises(InteropError):
        SignalRule(kind="no_such_kind")
    with pytest.raises(InteropError):
        SignalRule(kind="ma_cross", params={"fast": 20, "slow": 5})       # fast 必须 < slow
    with pytest.raises(InteropError):
        SignalRule(kind="rsi_threshold", params={"op": "boom"})
    with pytest.raises(InteropError):
        SignalRule(kind="volume_spike", params={"multiple": 0.5})


# ---------- 求值与公式同源 ----------
def test_ma_cross_golden_fires_on_exactly_the_cross_bar():
    closes = [10.0] * 10 + [11.0]           # 末根才让 MA5 抬到 MA10 之上
    rule = SignalRule(kind="ma_cross", params={"fast": 5, "slow": 10})
    assert rule.evaluate(_bars(closes))["hit"] is True
    assert rule.evaluate(_bars(closes[:-1]))["hit"] is False   # 全平：没穿越


def test_ma_cross_death_and_insufficient_data():
    closes = [20.0] * 10 + [18.0]
    rule = SignalRule(kind="ma_cross", params={"fast": 5, "slow": 10,
                                               "direction": "death"})
    assert rule.evaluate(_bars(closes))["hit"] is True
    assert rule.evaluate(_bars(closes[:-1]))["hit"] is False
    short = rule.evaluate(_bars([10, 10.5]))
    assert short["hit"] is False and "reason" in short["context"]


def test_macd_golden_and_volume_spike_evaluate():
    rule = SignalRule(kind="macd_golden")
    # 先跌后升：整段前缀里至少出现一次 DIF 上穿 DEA，才算这条规则真的在工作
    closes = list(np.linspace(20, 10, 40)) + list(np.linspace(10.0, 13.5, 15))
    hits = [rule.evaluate(_bars(closes[:i]))["hit"] for i in range(36, len(closes) + 1)]
    assert any(hits)
    ctx = rule.evaluate(_bars(closes))["context"]
    assert {"dif", "dea"} <= set(ctx)
    df = _bars(list(np.linspace(10, 11, 10)))
    df["volume"] = [1000.0] * 9 + [5000.0]
    assert SignalRule(kind="volume_spike",
                      params={"ma_days": 5, "multiple": 2}).evaluate(df)["hit"] is True


def test_rsi_above_and_cross_above_differ():
    closes = list(np.linspace(10, 15, 40))
    rule = SignalRule(kind="rsi_threshold", params={"period": 6, "op": "above", "value": 50})
    res = rule.evaluate(_bars(closes))
    assert res["hit"] is True and res["context"]["rsi"] > 50
    rule2 = SignalRule(kind="rsi_threshold", params={"period": 6, "op": "cross_above",
                                                     "value": 90})
    assert rule2.evaluate(_bars(closes))["hit"] is False


def test_evaluate_without_local_data_raises_readable_error():
    with pytest.raises(InteropError):
        SignalRule(kind="ma_cross").evaluate(pd.DataFrame())


# ---------- API 冒烟 ----------
@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from quantdesk import config as cfg
    from quantdesk.store.db import init_db, reset_engine

    monkeypatch.setattr(cfg, "data_dir", lambda: tmp_path)
    reset_engine()
    init_db(tmp_path / "t.db")
    # 进程级单例必须复位：它们持有上一个用例的库路径与连接
    for mod, attr in (("market_data", "_service"), ("trading", "_service"),
                      ("reconcile", "_service"), ("channels", "_hub")):
        import importlib

        m = importlib.import_module(f"quantdesk.services.{mod}")
        target = getattr(m, attr)
        if attr == "_hub" and target is not None:
            target.stop()
        monkeypatch.setattr(m, attr, None)
    from quantdesk.app import app

    with TestClient(app) as cl:
        yield cl
    reset_engine()


def test_api_targets_and_formula_roundtrip(client):
    body = client.get("/api/interop/targets").json()
    assert {b["id"] for b in body["blocks"]} == {"tdx", "ths", "em"}
    kinds = {r["kind"] for r in body["rules"]}
    assert {"ma_cross", "macd_golden", "rsi_threshold", "volume_spike"} <= kinds
    r = client.post("/api/interop/formula",
                    json={"kind": "ma_cross", "params": {"fast": 5, "slow": 20},
                          "dialect": "tdx"})
    assert r.status_code == 200 and "CROSS(F5,S20)" in r.json()["formula"]


def test_api_export_and_import_roundtrip(client, monkeypatch):
    r = client.post("/api/interop/export/block",
                    json={"name": "测试板块", "symbols": ["600000.SH", "000001.SZ"],
                          "targets": ["tdx", "ths"]})
    assert r.status_code == 200
    files = r.json()["files"]
    assert len(files) == 2 and all(f["bytes"] > 0 for f in files)

    d = client.get("/api/interop/download", params={"name": "测试板块.blk"})
    assert d.status_code == 200 and "1600000" in d.json()["content"]

    imp = client.post("/api/interop/import/block",
                      json={"target": "tdx", "content": d.json()["content"]})
    assert imp.json()["parsed"] == 2 and len(imp.json()["added"]) == 2


def test_api_rejects_bad_paths_and_kinds(client):
    assert client.get("/api/interop/download", params={"name": "../quantdesk.db"}
                      ).status_code == 404
    assert client.post("/api/interop/formula",
                       json={"kind": "nope", "params": {}, "dialect": "tdx"}
                       ).status_code == 400
    assert client.post("/api/interop/export/block",
                       json={"name": "空板块", "symbols": [], "targets": ["tdx"]}
                       ).status_code == 400


def test_api_signals_scans_local_bars_and_separates_no_data(client):
    """回归：/signals 曾漏传 freq 给 ParquetStore.read → 恒 500（信号选股页整块功能不可用）。

    同时锁住两条口径：指标读前复权（宪法第 5 条），本地无数据单列 no_local_data
    ——不能把「没下载」当成「没信号」。
    """
    from quantdesk.core.enums import AdjustType, Frequency
    from quantdesk.store.parquet_store import ParquetStore
    from tests.test_backtest import synth_df

    ParquetStore().write("600000.SH", Frequency.D1, synth_df(120), adjust=AdjustType.FORWARD)
    r = client.post("/api/interop/signals",
                    json={"kind": "ma_cross", "params": {"fast": 5, "slow": 20},
                          "symbols": ["600000.SH", "000001.SZ"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["scanned"] == 1 and out["no_local_data"] == ["000001.SZ"]
    assert out["freq"] == "1d" and out["adjust"] == "forward"
    assert "600000.SH" in out["details"] and "hit" in out["details"]["600000.SH"]
    assert set(out["hits"]) <= {"600000.SH"}

    empty = client.post("/api/interop/signals",
                        json={"kind": "ma_cross", "params": {}, "symbols": []})
    assert empty.status_code == 400 and "自选股" in empty.json()["detail"]
