"""回测明细落库 + 回测取消（本轮补上的两个真实空缺）。

一、明细落库：此前 backtest_runs 只存指标/净值/状态，成交/委托/日志只活在服务进程
   内存里（保留最近 50 份）——后端一重启，「回测报告」页点历史运行就只有指标没有明细。
   现在明细按块写 backtest_detail 表。本文件的关键用例一律用**新实例读同一个库**
   来验证「重启」，而不是在同一进程里换个对象——同一进程的内存对象测不出重启。

二、取消：此前异步队列里排队/运行中的回测没有取消入口。现在
   - 排队中 → 摘出队列直接 cancelled（根本不执行）；
   - 运行中 → 置标志位，引擎在当前 bar 边界停下，状态 cancelled、指标留空
     （半截成交不许冒充 completed）；
   - 服务重启后库里悬挂的 queued|running → 如实标 interrupted，不永远挂着「运行中」。

状态取值语义钉死：queued / running / completed / failed（这四个含义不变）
             + cancelled / interrupted（新增）。progress 另有仅进度面的 cancelling。

隔离纪律：本文件全部用例跑在 tmp_path 上（QUANTDESK_DATA_DIR + 清 settings 缓存 +
reset_engine + 默认库建表），不碰真实 data/。
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from quantdesk.config import get_settings
from quantdesk.core.enums import AdjustType, Frequency
from quantdesk.engine.strategy_base import BaseStrategy
from quantdesk.services import backtest_svc as svc_mod
from quantdesk.services.backtest_svc import BacktestService
from quantdesk.store import repo
from quantdesk.store.db import init_db, reset_engine
from quantdesk.store.parquet_store import ParquetStore
from tests.test_backtest import synth_df, write_store, SYMBOL

TERMINAL_STATUSES = ("completed", "failed", "cancelled", "interrupted")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """把 data_dir 钉到临时目录并建好默认库（测试绝不污染真实 data/）。"""
    monkeypatch.setenv("QUANTDESK_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    reset_engine()
    init_db()
    yield tmp_path
    reset_engine()
    get_settings.cache_clear()


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "bt.db"
    init_db(p)
    return p


class _SlowStrategy(BaseStrategy):
    """每根 bar 睡 1ms 的假策略：让「运行中取消」在测试里是确定的而不是碰运气。

    只在取消用例里通过 monkeypatch load_strategy 注入——真实的 bar 边界停止
    语义与真策略无关，引擎看的是 should_stop()，所以这里要的是可控耗时，
    不是真实信号。落库/回看/状态语义的用例一律用真 strategies/examples.dual_ma。
    """

    name = "slow_fake"
    params: dict = {"volume": 800, "sleep_ms": 1.0}

    def __init__(self, params=None):
        super().__init__(params)
        self.n = 0

    def on_start(self):
        self.ctx.subscribe([SYMBOL])

    def on_bar(self, bar):
        self.n += 1
        if self.n % 5 == 0:
            self.ctx.buy(SYMBOL, int(self.params["volume"]))
        elif self.n % 5 == 2:
            pos = self.ctx.get_position(SYMBOL)
            if pos and pos.available:
                self.ctx.sell(SYMBOL, int(pos.available))
        time.sleep(float(self.params["sleep_ms"]) / 1000.0)

    def on_stop(self):
        pass


def _mk_store(tmp_path, n=400):
    return write_store(tmp_path, synth_df(n))


def _run_kwargs() -> dict:
    return dict(strategy="dual_ma", symbols=[SYMBOL], freq="1d",
                start="2020-01-01", params={"fast": 3, "slow": 8, "volume": 800})


# ============================================================ 一、明细落库

def test_run_persists_details_and_new_service_reads_them(tmp_path, db):
    """跑完回测 → 明细落库；换**全新实例**（等价服务重启）读同一个库，结构一致。"""
    store = _mk_store(tmp_path)
    svc = BacktestService(store=store, db_path=db)
    out = svc.run(**_run_kwargs())
    run_id = out["run_id"]
    assert out["trades"], "本用例要有成交才有意义"
    assert out["detail_source"] == "db"

    # 库里确实有块（不是只存在于返回值里）
    stats = repo.backtest_detail_stats(run_id, db_path=db)
    assert stats["trades"]["rows"] == len(out["trades"])
    assert stats["meta"]["chunks"] == 1

    # 模拟重启：新实例 + 新 ParquetStore，内存缓存必然是空的（detail_source 会是 db）
    restarted = BacktestService(store=ParquetStore(root=tmp_path / "parquet"), db_path=db)
    assert restarted._results == {}          # 前置检查：新实例确实没有进程内缓存
    got = restarted.get(run_id)
    assert got is not None and got["status"] == "completed"
    for key in ("trades", "orders", "logs", "bars", "config", "metrics", "equity"):
        assert key in got, f"重启后响应缺键 {key}（前端会因契约漂移白屏）"
    assert got["detail_source"] == "db"
    assert got["trades"] == out["trades"], "重启后成交明细必须与重启前逐字段相同"
    assert got["orders"] == out["orders"]
    assert got["logs"] == out["logs"]
    assert got["bars"] == out["bars"] and got["config"] == out["config"]
    assert got["metrics"] == out["metrics"]
    assert got["equity"] == out["equity"]
    assert got["progress"]["status"] == "completed"   # 进度回落库里真值，不再是 unknown


def test_detail_survives_process_restart_at_http_level(tmp_path, db, monkeypatch):
    """HTTP 层重启验证：POST 跑完 → 换成新服务实例（同库）→ GET 仍返回成交明细。"""
    from quantdesk.app import app

    store = _mk_store(tmp_path)
    monkeypatch.setattr(svc_mod, "_service", BacktestService(store=store, db_path=db))
    with TestClient(app) as client:
        r = client.post("/api/backtest", json=dict(strategy="dual_ma", symbols=[SYMBOL],
                                                   freq="1d", start="2020-01-01",
                                                   params={"fast": 3, "slow": 8,
                                                           "volume": 800},
                                                   async_run=False))
        assert r.status_code == 200, r.text
        posted = r.json()
        run_id = posted["run_id"]

    # 新"进程"：另起一个服务实例，只共享同一个库文件
    svc2 = BacktestService(store=ParquetStore(root=tmp_path / "parquet"), db_path=db)
    monkeypatch.setattr(svc_mod, "_service", svc2)
    with TestClient(app) as client:
        got = client.get(f"/api/backtest/{run_id}").json()
    assert got["trades"] == posted["trades"]
    assert got["detail_source"] == "db"
    assert client.get(f"/api/backtest/{run_id}/progress").json()["status"] == "completed"


def test_details_are_chunked_and_reordered_by_seq(tmp_path, db, monkeypatch):
    """上千行明细分块写入后按 seq 拼回原序（分块不许打乱成交顺序）。"""
    init_db(db)
    run_id = repo.backtest_create("dual_ma", SYMBOL, "1d", "", "", db_path=db)
    trades = [{"trade_id": f"T{i:05d}", "seq": i} for i in range(1234)]
    written = repo.backtest_detail_write(run_id, {"trades": trades}, db_path=db)
    assert written["trades"] == {"rows": 1234, "chunks": 7}     # 200/块
    back = repo.backtest_detail_read(run_id, db_path=db)["trades"]
    assert back == trades                                       # 逐条同序同值
    # 单块损坏不拖垮整份明细（宁可少几条也不要用假数据凑完整）
    from sqlmodel import select

    with repo.get_session(db) as s:
        bad = s.exec(select(repo.BacktestDetailRow).where(
            repo.BacktestDetailRow.run_id == run_id,
            repo.BacktestDetailRow.seq == 3)).first()
        bad.payload_json = "{坏数据"
        s.commit()
    partial = repo.backtest_detail_read(run_id, db_path=db)["trades"]
    assert len(partial) == 1234 - 200 and partial[0] == trades[0]


def test_detail_write_is_idempotent_per_kind(tmp_path, db):
    """同一 run 重写明细只覆盖传进来的 kind，其它 kind 不受牵连。"""
    init_db(db)
    run_id = repo.backtest_create("dual_ma", SYMBOL, "1d", "", "", db_path=db)
    repo.backtest_detail_write(run_id, {"trades": [{"a": 1}], "logs": ["L1"],
                                        "meta": {"bars": 3}}, db_path=db)
    repo.backtest_detail_write(run_id, {"trades": [{"a": 2}, {"a": 3}]}, db_path=db)
    back = repo.backtest_detail_read(run_id, db_path=db)
    assert back["trades"] == [{"a": 2}, {"a": 3}]
    assert back["logs"] == ["L1"] and back["meta"] == {"bars": 3}


def test_legacy_run_without_detail_reads_empty_not_missing(tmp_path, db):
    """改造前完成的历史运行（库里没有明细块）：返回空集合 + detail_source=none，不炸。"""
    init_db(db)
    old_id = repo.backtest_create("dual_ma", SYMBOL, "1d", "", "", db_path=db)
    repo.backtest_finish(old_id, metrics_json='{"total_return":0.02}',
                         equity_json="[]", db_path=db)
    svc = BacktestService(store=ParquetStore(root=tmp_path / "p"), db_path=db)
    got = svc.get(old_id)
    assert got["status"] == "completed"
    assert got["trades"] == [] and got["orders"] == [] and got["logs"] == []
    assert got["bars"] == 0 and got["config"] == {}
    assert got["detail_source"] == "none"
    assert got["progress"]["status"] == "completed"


def test_details_written_before_status_and_broadcast(tmp_path, db, monkeypatch):
    """宪法第 4 条「先落库后广播」：写终态与发 WS 事件时，明细必须已在库里。"""
    store = _mk_store(tmp_path)
    svc = BacktestService(store=store, db_path=db)
    seen: dict[str, dict] = {}

    orig_finish = repo.backtest_finish

    def spy_finish(run_id, **kw):
        seen["at_finish"] = repo.backtest_detail_stats(run_id, db_path=db)
        return orig_finish(run_id, **kw)

    def spy_publish(payload):
        rid = (payload or {}).get("run_id")
        if (payload or {}).get("action") == "backtest_done" and rid is not None:
            seen["at_broadcast"] = repo.backtest_detail_stats(rid, db_path=db)

    monkeypatch.setattr(repo, "backtest_finish", spy_finish)
    monkeypatch.setattr(svc_mod, "_publish_log", spy_publish)
    out = svc.run(**_run_kwargs())
    assert seen["at_finish"]["trades"]["rows"] == len(out["trades"])
    assert seen["at_broadcast"]["trades"]["rows"] == len(out["trades"])


# ============================================================ 二、取消

def _wait_running(svc: BacktestService, run_id: int, timeout: float = 20.0) -> bool:
    """等这条运行真的进入 running（worker 已取走并写了状态）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = repo.backtest_get(run_id, db_path=svc.db_path)
        if row is not None and row.status == "running":
            return True
        time.sleep(0.02)
    return False


def test_cancel_queued_run_never_executes(tmp_path, db, monkeypatch):
    """排队中取消：摘出队列、状态 cancelled、不执行（没有明细、没有指标）。"""
    store = _mk_store(tmp_path)
    svc = BacktestService(store=store, db_path=db)
    # 不起 worker：submit 的载荷就确定地停在队列里（排队分支不靠抢时序）
    monkeypatch.setattr(svc, "_ensure_worker", lambda: None)
    run_id = svc.submit(**_run_kwargs())
    assert repo.backtest_get(run_id, db_path=db).status == "queued"

    res = svc.request_cancel(run_id)
    assert res["ok"] is True and res["changed"] is True
    assert res["status"] == "cancelled"
    row = repo.backtest_get(run_id, db_path=db)
    assert row.status == "cancelled" and row.finished_at is not None
    assert row.metrics_json in ("{}", "")            # 没跑过就没有指标
    assert "排队" in row.remark or "取消" in row.remark
    assert repo.backtest_detail_stats(run_id, db_path=db) == {}   # 未执行 → 无明细
    assert svc.progress(run_id)["status"] == "cancelled"
    # 队列里那条 id 后续被 worker 拿到也不会执行（载荷已摘除）
    svc.stop_worker()


def _wait_progress(svc: BacktestService, run_id: int, at_least: int,
                   timeout: float = 30.0) -> int:
    """等进度真的推进到至少 N 根 bar（取消要发生在「已经跑出一部分」之后才有意义）。"""
    deadline = time.time() + timeout
    done = 0
    while time.time() < deadline:
        done = int(svc.progress(run_id).get("done") or 0)
        if done >= at_least:
            return done
        time.sleep(0.02)
    return done


def test_cancel_running_run_stops_at_bar_boundary(tmp_path, db, monkeypatch):
    """运行中取消：引擎在 bar 边界停下 → 状态 cancelled、指标空、明细是部分结果。"""
    store = _mk_store(tmp_path, n=4000)
    svc = BacktestService(store=store, db_path=db)
    monkeypatch.setattr(svc_mod, "load_strategy", lambda name: _SlowStrategy)

    done = threading.Event()
    box: dict[str, object] = {}

    def worker():
        try:
            box["out"] = svc.run(strategy="slow_fake", symbols=[SYMBOL], freq="1d",
                                 start="2020-01-01", params={"volume": 800})
        except Exception as e:                       # noqa: BLE001 交给断言报出
            box["err"] = e
        finally:
            done.set()

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    # run() 里才建 run_id：等库里出现第一条运行
    deadline = time.time() + 20
    run_id = None
    while time.time() < deadline and run_id is None:
        rows = repo.backtest_list(limit=5, db_path=db)
        run_id = rows[0]["run_id"] if rows else None
        if run_id is None:
            time.sleep(0.02)
    assert run_id is not None and _wait_running(svc, run_id)
    assert _wait_progress(svc, run_id, 25) >= 25, "引擎没推进，取消用例失去意义"

    mid = svc.request_cancel(run_id)
    assert mid["ok"] is True and mid.get("cancelling") is True
    assert mid["status"] == "running", "运行中的取消不改状态，等引擎自己落到 cancelled"

    assert done.wait(60), "回测线程未在时限内停下"
    th.join(timeout=5)
    assert "err" not in box, box.get("err")

    row = repo.backtest_get(run_id, db_path=db)
    out = box["out"]
    assert row.status == "cancelled"
    assert row.finished_at is not None
    assert json_empty(row.metrics_json), "取消的运行不许留指标（半截结果不能冒充 completed）"
    assert "取消" in row.remark and "部分结果" in row.remark
    assert out["status"] == "cancelled" and out["partial"] is True
    assert out["metrics"] == {}
    assert out["bars"] > 0 and out["trades"], "取消前应已有部分成交可回看"
    # 部分明细如实落库、可回看
    stats = repo.backtest_detail_stats(run_id, db_path=db)
    assert stats["meta"]["chunks"] == 1
    assert stats.get("trades", {}).get("rows", 0) == len(out["trades"])
    restarted = BacktestService(store=ParquetStore(root=tmp_path / "parquet"), db_path=db)
    got = restarted.get(run_id)
    assert got["status"] == "cancelled" and got["partial"] is True
    assert got["metrics"] == {} and got["trades"] == out["trades"]
    assert got["detail_source"] == "db"
    assert svc.progress(run_id)["status"] == "cancelled"


def json_empty(v: str) -> bool:
    return (v or "{}").strip() in ("{}", "")


def test_cancel_stops_soon_not_at_the_end(tmp_path, db, monkeypatch):
    """取消要真的提前停下（而不是把整轮回测跑完才标 cancelled）——否则取消是假的。"""
    store = _mk_store(tmp_path, n=4000)
    svc = BacktestService(store=store, db_path=db)
    monkeypatch.setattr(svc_mod, "load_strategy", lambda name: _SlowStrategy)
    box: dict[str, object] = {}
    done = threading.Event()

    # 全跑完至少要 4000 根 × 1ms ≈ 4s；这里 0.6s 就取消，实际处理 bar 数必须远小于总数
    def worker():
        try:
            box["out"] = svc.run(strategy="slow_fake", symbols=[SYMBOL], freq="1d",
                                 start="2020-01-01", params={"volume": 800})
        finally:
            done.set()

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    deadline = time.time() + 20
    run_id = None
    while time.time() < deadline and run_id is None:
        rows = repo.backtest_list(limit=5, db_path=db)
        run_id = rows[0]["run_id"] if rows else None
        time.sleep(0.02)
    assert run_id and _wait_running(svc, run_id)
    time.sleep(0.3)
    svc.request_cancel(run_id)
    assert done.wait(30)

    out = box["out"]
    assert out["bars"] < 2000, f"取消后仍处理了 {out['bars']} 根 bar，取消没起作用"
    assert out["bars"] > 0


def test_cancel_completed_run_is_idempotent_and_never_rewrites(tmp_path, db):
    """终态不许被取消改写：completed 仍是 completed，ok=False、changed=False。"""
    store = _mk_store(tmp_path)
    svc = BacktestService(store=store, db_path=db)
    out = svc.run(**_run_kwargs())
    res = svc.request_cancel(out["run_id"])
    assert res["ok"] is False and res["changed"] is False
    assert res["status"] == "completed"
    assert repo.backtest_get(out["run_id"], db_path=db).status == "completed"
    # 再点一次仍然幂等（前端重复点击不会把状态打回）
    assert svc.request_cancel(out["run_id"])["status"] == "completed"
    # 已取消的运行重复取消同样幂等
    failed_id = repo.backtest_create("dual_ma", SYMBOL, "1d", "", "", db_path=db)
    repo.backtest_set_status(failed_id, "failed", remark="故意造一条", db_path=db)
    assert svc.request_cancel(failed_id)["status"] == "failed"
    assert repo.backtest_get(failed_id, db_path=db).status == "failed"
    assert svc.request_cancel(999999) is None


def test_restart_marks_dangling_run_interrupted(tmp_path, db):
    """重启后库里悬挂的 queued|running → interrupted（带 finished_at 与原因），不永远挂着。"""
    init_db(db)
    hanging = repo.backtest_create("dual_ma", SYMBOL, "1d", "", "", db_path=db)
    repo.backtest_set_status(hanging, "running", db_path=db)
    queued = repo.backtest_create("dual_ma", SYMBOL, "1d", "", "", db_path=db)
    repo.backtest_set_status(queued, "queued", db_path=db)
    done = repo.backtest_create("dual_ma", SYMBOL, "1d", "", "", db_path=db)
    repo.backtest_finish(done, metrics_json='{"total_return":0.1}', db_path=db)

    BacktestService(store=ParquetStore(root=tmp_path / "p"), db_path=db)   # 构造即清理

    assert repo.backtest_get(hanging, db_path=db).status == "interrupted"
    assert repo.backtest_get(hanging, db_path=db).finished_at is not None
    assert "重启" in repo.backtest_get(hanging, db_path=db).remark
    assert repo.backtest_get(queued, db_path=db).status == "interrupted"
    assert repo.backtest_get(done, db_path=db).status == "completed"       # 终态不动


def test_cancel_dangling_run_after_restart_reports_interrupted(tmp_path, db):
    """取消一条「库里在跑、本进程没有」的运行 → 如实 interrupted，不假装已取消。"""
    init_db(db)
    rid = repo.backtest_create("dual_ma", SYMBOL, "1d", "", "", db_path=db)
    repo.backtest_set_status(rid, "running", db_path=db)
    svc = BacktestService(store=ParquetStore(root=tmp_path / "p"), db_path=db,
                          sweep_orphans=False)          # 先不清理，单独验取消分支
    res = svc.request_cancel(rid)
    assert res["status"] == "interrupted" and res["changed"] is True
    assert "重启" in res["message"]
    assert repo.backtest_get(rid, db_path=db).status == "interrupted"


# ============================================================ 三、状态取值语义不漂移

def test_status_vocabulary_and_async_lifecycle(tmp_path, db, monkeypatch):
    """异步队列走完整生命周期，状态字样只允许六个取值；旧四个含义不变。"""
    store = _mk_store(tmp_path)
    svc = BacktestService(store=store, db_path=db)
    rid = svc.submit(**_run_kwargs())
    observed = {repo.backtest_get(rid, db_path=db).status}
    for _ in range(600):
        observed.add(repo.backtest_get(rid, db_path=db).status)
        if observed & set(TERMINAL_STATUSES):
            break
        time.sleep(0.02)
    assert observed <= {"queued", "running", "completed", "failed", "cancelled",
                        "interrupted", "cancelling"}
    assert observed & {"queued", "running"}, observed
    final = repo.backtest_get(rid, db_path=db).status
    assert final == "completed", f"未取消的运行不该变终态：{final}"
    row = repo.backtest_get(rid, db_path=db)
    assert row.metrics_json and row.metrics_json != "{}"     # completed 必带指标
    got = svc.get(rid)
    assert got["partial"] is False and got["metrics"]["trade_count"] == len(got["trades"])
    listed = repo.backtest_list(limit=10, db_path=db)
    assert {r["status"] for r in listed} <= {"queued", "running", "completed", "failed",
                                             "cancelled", "interrupted"}


def test_api_cancel_and_detail_contract(tmp_path, db, monkeypatch):
    """API 契约：cancel 端点的响应键与 404、GET 明细在换实例后仍同构。"""
    from quantdesk.app import app

    store = _mk_store(tmp_path)
    svc = BacktestService(store=store, db_path=db)
    monkeypatch.setattr(svc_mod, "_service", svc)
    with TestClient(app) as client:
        out = client.post("/api/backtest", json=dict(strategy="dual_ma", symbols=[SYMBOL],
                                                     freq="1d", start="2020-01-01",
                                                     params={"fast": 3, "slow": 8,
                                                             "volume": 800},
                                                     async_run=False)).json()
        rid = out["run_id"]
        detail = client.get(f"/api/backtest/{rid}").json()
        assert set(detail) >= {"run_id", "strategy", "symbol", "freq", "start", "end",
                               "params", "status", "metrics", "equity", "remark",
                               "created_at", "finished_at", "progress", "trades",
                               "orders", "logs", "bars", "config"}
        assert detail["trades"] == out["trades"]
        # 新增键（只能新增，不改旧语义）
        assert detail["detail_source"] in ("memory", "db")
        assert detail["partial"] is False
        assert isinstance(detail["logs_total"], int)

        res = client.post(f"/api/backtest/{rid}/cancel").json()
        assert res["ok"] is False and res["status"] == "completed"
        assert client.get(f"/api/backtest/{rid}").json()["status"] == "completed"
        assert client.post("/api/backtest/424242/cancel").status_code == 404

        ack = client.post("/api/backtest", json=dict(strategy="dual_ma", symbols=[SYMBOL],
                                                     params={"fast": 3, "slow": 8,
                                                             "volume": 800},
                                                     start="2020-01-01")).json()
        assert ack["async"] is True and ack["status"] == "queued"
        assert ack["detail_source"] == "none"

    # 新实例读同一个库：明细仍返回，且与跑完当时一致
    svc2 = BacktestService(store=ParquetStore(root=tmp_path / "parquet"), db_path=db)
    monkeypatch.setattr(svc_mod, "_service", svc2)
    with TestClient(app) as client:
        again = client.get(f"/api/backtest/{rid}").json()
    assert again["trades"] == out["trades"] and again["detail_source"] == "db"
    assert again["metrics"] == out["metrics"]


def test_cancel_via_api_while_running(tmp_path, db, monkeypatch):
    """端到端：API 入队一个大回测 → POST cancel → 库里终态 cancelled、GET 能回看部分明细。"""
    from quantdesk.app import app

    store = _mk_store(tmp_path, n=4000)
    svc = BacktestService(store=store, db_path=db)
    monkeypatch.setattr(svc_mod, "_service", svc)
    monkeypatch.setattr(svc_mod, "load_strategy", lambda name: _SlowStrategy)
    with TestClient(app) as client:
        rid = client.post("/api/backtest/async", json=dict(strategy="slow_fake",
                                                            symbols=[SYMBOL], freq="1d",
                                                            start="2020-01-01",
                                                            params={"volume": 800})
                          ).json()["run_id"]
        assert _wait_running(svc, rid)
        res = client.post(f"/api/backtest/{rid}/cancel").json()
        assert res["ok"] is True and res.get("cancelling") is True
        status = None
        for _ in range(900):
            status = client.get(f"/api/backtest/{rid}").json()["status"]
            if status in TERMINAL_STATUSES:
                break
            time.sleep(0.02)
        assert status == "cancelled", status
        body = client.get(f"/api/backtest/{rid}").json()
        assert body["metrics"] == {} and body["partial"] is True
        assert body["detail_source"] == "memory"      # 本进程缓存命中（结构与库一致）
        assert client.get(f"/api/backtest/{rid}/progress").json()["status"] == "cancelled"
    # 重启后同样成立
    svc2 = BacktestService(store=ParquetStore(root=tmp_path / "parquet"), db_path=db)
    got = svc2.get(rid)
    assert got["status"] == "cancelled" and got["detail_source"] == "db"
    assert got["metrics"] == {} and got["partial"] is True


# ============================================================ 三、空跑如实说明
#
# 起因是真实回显：从盯盘池带入 2 只代码、策略参数 symbol 还是默认的 600000.SH，
# 结果 status=completed、bars=484、trades=0、remark=''、logs=[] —— 界面上一条平直
# 净值曲线 + 「成交明细 0 笔」，用户只能理解为「策略没信号」。配置不一致被系统
# 静默吞掉，这属于「图上看起来一切正常」那一类缺陷，所以钉死在这里。

OTHER = "000001.SZ"


def _two_symbol_store(tmp_path):
    store = _mk_store(tmp_path)
    store.write(OTHER, Frequency.D1, synth_df(400, seed=11), AdjustType.FORWARD)
    return store


def test_idle_run_when_strategy_target_absent_from_symbols_is_rejected_loudly(tmp_path, db):
    """策略交易的那只不在标的列表里 → 0 成交必须写成「配置不一致」，不许留空备注。"""
    store = _two_symbol_store(tmp_path)
    svc = BacktestService(store=store, db_path=db)
    out = svc.run(strategy="dual_ma", symbols=[SYMBOL, OTHER], freq="1d", start="2020-01-01",
                  params={"symbol": "601318.SH", "fast": 3, "slow": 8, "volume": 800})

    assert out["status"] == "completed", "这不是错误，不能改判 failed——配置不一致是用户的配置"
    assert out["trades"] == [] and out["bars"] > 0, "前置：确实是跑了 bar 却一根成交都没有"
    assert out["logs"][0].startswith("空跑提示"), out["logs"][:2]
    assert "601318.SH" in out["logs"][0] and SYMBOL in out["logs"][0]

    row = repo.backtest_get(out["run_id"], db_path=db)
    assert "空跑" in row.remark and "601318.SH" in row.remark, "备注是前端唯一的事实源，必须落库"
    assert out["logs_total"] == len(out["logs"]), "塞进日志的这条也要算进总数，口径不许分叉"

    # 重启后仍在（备注与日志都进了库，不是只活在本进程返回值里）
    got = BacktestService(store=ParquetStore(root=tmp_path / "other_parquet"),
                          db_path=db).get(out["run_id"])
    assert got["remark"] == row.remark and got["logs"] == out["logs"]


def test_idle_run_note_absent_when_target_in_symbols_or_strategy_trades_anyway(
    tmp_path, db, monkeypatch
):
    """两个不许误报的分支：目标就在列表里；目标不在列表但策略照样成交。"""
    store = _two_symbol_store(tmp_path)
    svc = BacktestService(store=store, db_path=db)

    ok = svc.run(strategy="dual_ma", symbols=[SYMBOL, OTHER], freq="1d", start="2020-01-01",
                 params={"symbol": SYMBOL, "fast": 3, "slow": 8, "volume": 800})
    assert ok["trades"], "本分支要有成交才有对照意义"
    assert "空跑" not in repo.backtest_get(ok["run_id"], db_path=db).remark

    class _AnySymbolBuyer(BaseStrategy):
        """参数里写着 symbol，但实际交易别的代码（多标的策略的常见写法）。"""
        name = "any_symbol"
        params = {"symbol": "601318.SH", "volume": 800}

        def on_start(self):
            self.ctx.subscribe([SYMBOL])

        def on_bar(self, bar):
            if bar.symbol == SYMBOL and self.ctx.get_position(SYMBOL) is None:
                self.ctx.buy(SYMBOL, int(self.params["volume"]))

    monkeypatch.setattr(svc_mod, "load_strategy", lambda name: _AnySymbolBuyer)
    ran = svc.run(strategy="any_symbol", symbols=[SYMBOL, OTHER], freq="1d",
                  start="2020-01-01", params={"volume": 800})
    assert ran["trades"], "本分支要有成交才有对照意义"
    assert "空跑" not in repo.backtest_get(ran["run_id"], db_path=db).remark, \
        "有成交说明策略不只认这个参数，标成空跑就是误报"
