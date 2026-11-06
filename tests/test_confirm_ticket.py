"""K01 剩余项：待确认单的上下文与截止时间必须持久化，判定不能只靠内存字典和清理线程。

复现的问题是这一类：武装确认时只把「模式/账户/通道/规则版本/截止时间」记在进程内存里，
于是重启后既核不出身份（老实用户的待确认单永远确认不了），也判不了过期
（清理线程没跑到 = 没判过），并发确认只靠一把进程内锁挡着。
宪法第 4 条要求「先落库后广播、重启可对账、待确认单重启不丢」——不丢不等于能用，
这里要的是重启后仍能按同一份事实做判定，且不重新授予已花掉的确认窗口。
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from quantdesk.core.enums import OrderStatus, Side
from quantdesk.core.errors import ConnectionLostError
from quantdesk.core.models import Order
from quantdesk.store import repo
from quantdesk.store.db import init_db

from tests.test_confirm_flow import MockLiveBroker, StubSettings, _mk_prev_close_store, mk_order

DB = "confirm.db"
TIMEOUT_S = 30.0


def make_gate(tmp_path, live=None, settings=None, timeout_s=TIMEOUT_S, account=""):
    """按生产口径装配：确认窗口是真的截止时间，不是靠测试配置关掉校验。"""
    init_db(tmp_path / DB)
    broker = live or MockLiveBroker()
    broker.connect()
    if account:
        broker.account_identity = lambda: account          # 实例属性覆盖，测试身份指纹用
    from quantdesk.engine.riskgate import RiskGate

    gate = RiskGate(live_broker=broker, settings=settings or StubSettings(False),
                    db_path=tmp_path / DB, store=_mk_prev_close_store(tmp_path),
                    confirm_timeout_s=timeout_s)
    return gate, broker


def test_submit_persists_confirm_context(tmp_path):
    """武装确认时就要把判据落库：模式、通道、账户指纹、规则版本、UTC 毫秒截止时间。"""
    gate, _ = make_gate(tmp_path)
    out = gate.submit(mk_order("T1"))
    assert out.status == OrderStatus.PENDING_CONFIRM

    ctx = repo.confirm_context("T1", db_path=tmp_path / DB)
    assert ctx is not None, "待确认上下文没落库，重启后就无从核对"
    assert ctx["mode"] == "live"
    assert ctx["channel"] == "mock_live"
    assert isinstance(ctx["deadline"], int) and ctx["deadline"] > 0
    assert ctx["rules"], "规则版本必须落库：规则换了版，旧确认不能照用"
    span = ctx["deadline"] - out.created_at
    assert TIMEOUT_S * 1000 * 0.6 <= span <= TIMEOUT_S * 1000 * 1.6, f"截止时间不像窗口内该到的值：{span}"


def test_confirm_after_restart_with_same_identity_still_works(tmp_path):
    """重启不丢也不该等于永远确认不了：同一通道同一账户时，按落库事实放行。"""
    gate, live = make_gate(tmp_path)
    gate.submit(mk_order("T2"))
    gate.stop()                                   # 模拟进程结束（内存全清）

    live2 = MockLiveBroker()
    gate2, _ = make_gate(tmp_path, live=live2)     # 新进程：新实例、同一通道名
    n = gate2.restore_pending()
    assert n >= 1, "restore_pending 应把待确认单读回来"
    out = gate2.confirm("T2")
    assert out.status == OrderStatus.FILLED, f"同身份重启后应能确认：{out.message}"
    assert len(live2.placed) == 1


def test_confirm_after_restart_with_other_channel_stays_closed(tmp_path):
    """通道从 A 换成 B：原确认不得投到新通道，也不得悄悄变成模拟成交。"""
    gate, _ = make_gate(tmp_path)
    gate.submit(mk_order("T3"))
    gate.stop()

    live2 = MockLiveBroker()
    live2.name = "qmt"                             # 装机通道换了
    gate2, _ = make_gate(tmp_path, live=live2)
    gate2.restore_pending()
    out = gate2.confirm("T3")
    assert out.status not in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
    assert "通道" in out.message and live2.placed == []


def test_account_changed_invalidates_old_confirmation(tmp_path):
    """换资金账号 = 换主体，旧确认作废；落库的账户指纹就是用来发现这件事的。"""
    gate, _ = make_gate(tmp_path, account="INTERNAL-ACCT-A")
    gate.submit(mk_order("T4"))
    gate.stop()

    live2 = MockLiveBroker()
    gate2, _ = make_gate(tmp_path, live=live2, account="INTERNAL-ACCT-B")   # 同通道名、不同账户
    gate2.restore_pending()
    out = gate2.confirm("T4")
    assert out.status not in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
    assert "账户" in out.message and live2.placed == []


def test_deadline_is_checked_at_confirm_even_with_sweeper_stopped(tmp_path):
    """清理线程停不停都要当场判过期：判据是落库的绝对截止时间。"""
    gate, live = make_gate(tmp_path, timeout_s=0.4)
    gate.submit(mk_order("T5"))
    gate.stop()                                    # 线程停了，没有任何后台回收
    time.sleep(0.5)
    out = gate.confirm("T5")
    assert out.status == OrderStatus.EXPIRED_CONFIRM, f"过期单必须当场作废：{out.status}"
    assert live.placed == []
    assert repo.get_order("T5", db_path=tmp_path / DB).status == "expired_confirm"


def test_expired_order_is_not_re_granted_a_new_window(tmp_path):
    """过期作废后重启：不能因为「重启后重新计时」再给用户一次确认机会。"""
    gate, live = make_gate(tmp_path, timeout_s=0.4)
    gate.submit(mk_order("T6"))
    time.sleep(0.5)
    gate._expire_overdue()
    gate.stop()

    live2 = MockLiveBroker()
    gate2, _ = make_gate(tmp_path, live=live2, timeout_s=TIMEOUT_S)
    gate2.restore_pending()
    assert repo.get_order("T6", db_path=tmp_path / DB).status == "expired_confirm"
    with pytest.raises(ValueError):
        gate2.confirm("T6")                        # 已作废，不在待确认队列
    assert live2.placed == []


def test_legacy_pending_row_without_context_fails_closed(tmp_path):
    """老库里没有上下文的待确认单：明确失效并要求重新提交，不猜身份、不新建窗口。"""
    init_db(tmp_path / DB)
    o = replace(mk_order("T7"), status=OrderStatus.PENDING_CONFIRM)
    repo.upsert_order(o, db_path=tmp_path / DB)    # 直接落库，绕开 arm_confirm

    live = MockLiveBroker()
    gate, _ = make_gate(tmp_path, live=live)       # make_gate 又建了一次库，幂等
    gate.restore_pending()
    out = gate.confirm("T7")
    assert out.status == OrderStatus.EXPIRED_CONFIRM
    assert "重新提交" in out.message and live.placed == []


def test_double_confirm_only_submits_once(tmp_path):
    """重复确认：第二次不能再投一笔真钱委托（防重复提交要落在持久化状态转换上）。"""
    gate, live = make_gate(tmp_path)
    gate.submit(mk_order("T8"))
    first = gate.confirm("T8")
    assert first.status == OrderStatus.FILLED
    assert len(live.placed) == 1

    with pytest.raises(ValueError):
        gate.confirm("T8")                         # 已不在待确认状态
    assert len(live.placed) == 1, "重复确认又投了一笔"


def test_concurrent_confirm_submits_once(tmp_path):
    """两个线程同时点确认：只允许一次投递——这是状态转换的原子性，不是运气。"""
    gate, live = make_gate(tmp_path)
    gate.submit(mk_order("T9"))
    start = threading.Barrier(4)

    def hit(_):
        start.wait()
        try:
            return gate.confirm("T9")
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        outs = list(pool.map(hit, range(4)))
    assert len(live.placed) == 1, f"并发确认投了 {len(live.placed)} 笔"
    assert [o for o in outs if o is not None][0].status == OrderStatus.FILLED


def test_intent_is_persisted_before_broker_call(tmp_path):
    """先记录意图再调券商：调用进行中数据库里已经是 submitted，崩了也不会被当成待确认重发。"""
    seen = {}

    class SpyBroker(MockLiveBroker):
        def place_order(self, order):
            seen["status"] = repo.get_order("TA", db_path=tmp_path / DB).status
            return super().place_order(order)

    live = SpyBroker()
    gate, _ = make_gate(tmp_path, live=live)
    gate.submit(mk_order("TA"))
    gate.confirm("TA")
    assert seen.get("status") == "submitted", f"调券商前没落意图：{seen}"


def test_uncertain_result_is_persisted_and_never_resent(tmp_path):
    """通道断线导致结果不明：持久化成可查询的「不确定」，重启后先核对券商，不盲目重发。"""
    class FlakyBroker(MockLiveBroker):
        def place_order(self, order):
            self.placed.append(order)
            raise ConnectionLostError("通道在投递中途断开")

    live = FlakyBroker()
    gate, _ = make_gate(tmp_path, live=live)
    gate.submit(mk_order("TB"))
    out = gate.confirm("TB")
    assert out.status == OrderStatus.FAILED
    row = repo.get_order("TB", db_path=tmp_path / DB)
    assert "不确定" in row.message or "断" in row.message
    assert repo.list_uncertain(db_path=tmp_path / DB), "结果不明的委托必须查得到"

    live2 = MockLiveBroker()
    gate2, _ = make_gate(tmp_path, live=live2)
    gate2.restore_pending()
    assert live2.placed == [], "重启后自动重发了结果不明的委托"


def test_old_database_upgrades_and_legacy_pending_fails_closed(tmp_path):
    """升级路径实测：老库（没有确认判据这几列）直接启动要能补列，旧待确认单不能投。

    create_all 只建表不加列——缺列时新代码第一次 SELECT 就报 no such column，
    软件直接打不开；补出来的默认值又必须让 `confirm()` 认得出「这条没有判据」。
    """
    import sqlite3

    from quantdesk.engine.riskgate import RiskGate
    from quantdesk.store.db import ensure_columns, get_engine

    path = tmp_path / DB
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE orders (order_id TEXT PRIMARY KEY, broker_order_id TEXT,"
        " strategy_id TEXT, symbol TEXT, side TEXT, price FLOAT, volume INTEGER,"
        " order_type TEXT, status TEXT, filled_volume INTEGER, avg_filled_price FLOAT,"
        " message TEXT, created_at INTEGER, updated_at INTEGER);"
        "INSERT INTO orders (order_id, strategy_id, symbol, side, price, volume,"
        " order_type, status, message, created_at, updated_at) VALUES"
        " ('OLD1','manual','600000.SH','buy',10.0,100,'limit','pending_confirm','',"
        " 1730000000000, 1730000000000);"
    )
    con.commit()
    con.close()

    added = ensure_columns(get_engine(path))
    assert {"orders.confirm_deadline", "orders.confirm_mode", "orders.confirm_channel",
            "orders.confirm_account", "orders.confirm_rules",
            "orders.result_uncertain"} <= set(added), added
    assert ensure_columns(get_engine(path)) == []          # 幂等：第二次什么都不做

    live = MockLiveBroker()
    live.connect()
    gate = RiskGate(live_broker=live, settings=StubSettings(False), db_path=path,
                    store=_mk_prev_close_store(tmp_path), confirm_timeout_s=TIMEOUT_S)
    assert gate.restore_pending() == 1
    out = gate.confirm("OLD1")
    assert out.status == OrderStatus.EXPIRED_CONFIRM
    assert "重新提交" in out.message and live.placed == []
    assert repo.get_order("OLD1", db_path=path).message == out.message


def test_broker_refusal_is_failed_but_not_uncertain(tmp_path):
    """券商明确拒了（结构化 BrokerError）：结果确定，不该混进「待核对」队列。"""
    from quantdesk.core.errors import BrokerError

    class RefuseBroker(MockLiveBroker):
        def place_order(self, order):
            raise BrokerError("券商拒绝：可平数量不足")

    live = RefuseBroker()
    gate, _ = make_gate(tmp_path, live=live)
    gate.submit(mk_order("TC"))
    out = gate.confirm("TC")
    assert out.status == OrderStatus.FAILED and "券商拒绝" in out.message
    assert repo.list_uncertain(db_path=tmp_path / DB) == []


def test_ruleset_change_invalidates_old_confirmation(tmp_path, monkeypatch):
    """确认窗口里规则换了版：旧结论不沿用，作废重下（判据落库才查得出来）。"""
    from quantdesk.core import constants

    gate, live = make_gate(tmp_path)
    gate.submit(mk_order("TD"))
    monkeypatch.setattr(constants, "ruleset_version", lambda: "upgrade-changed-lot-rules")
    out = gate.confirm("TD")
    assert out.status not in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
    assert "规则" in out.message and live.placed == []


def test_confirm_and_reject_race_has_one_outcome(tmp_path):
    """确认与拒绝同时到达：只可能有一个生效，且不会既投真钱又记成用户拒绝。"""
    gate, live = make_gate(tmp_path)
    gate.submit(mk_order("TE"))
    start = threading.Barrier(2)

    def run(fn):
        start.wait()
        try:
            return fn("TE")
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(run, gate.confirm)
        b = pool.submit(run, gate.reject)
        outs = [a.result(), b.result()]

    assert len(live.placed) <= 1, "确认/拒绝竞态投了不止一笔"
    final = repo.get_order("TE", db_path=tmp_path / DB).status
    assert final in ("filled", "rejected"), f"竞态后状态不可解释：{final}"
    assert [o for o in outs if o is not None], "两条路都没给出结果"
    assert (len(live.placed) == 1) == (final == "filled"), "投递与终态对不上"
