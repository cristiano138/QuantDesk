"""只读红线：港股（.HK）只能看不能买。

用户明确要求「只用来看 A 股和 H 股，不要接入买卖功能」。行情侧已经能取到港股实时数据，
所以这条红线必须在服务端咽喉处兜住：RiskGate 之前没有任何一处认识 .HK。
这里同时锁住三件事——规范代码、非规范写法、绕过 submit 直连 validate 的路径都拒得掉，
并且拒单是结构化 REJECTED（不是异常、不是 500），A 股自身的路径不受影响。
"""

from __future__ import annotations

import pytest

from quantdesk.core.enums import OrderStatus, Side
from quantdesk.core.models import Order
from quantdesk.core.symbols import is_hk, normalize
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


def test_validate_rejects_hk_on_both_sides(tmp_path):
    gate, _ = mk_gate(tmp_path)
    for side in (Side.BUY, Side.SELL):
        v = gate.validate(mk_order(side, price=300.0, volume=100, symbol=HK))
        assert any("港股" in x for x in v), v


def test_submit_rejects_hk_and_never_fills(tmp_path):
    gate, broker = mk_gate(tmp_path)
    cash_before = broker.get_account().cash
    o = gate.submit(mk_order(Side.BUY, price=300.0, volume=100, symbol=HK, oid="HK1"))
    assert o.status == OrderStatus.REJECTED
    assert "港股" in o.message
    # 拒单会先落库留痕（宪法第 4 条），但通道侧必须什么都没发生：无成交、无持仓、资金不动
    assert broker.get_trades() == []
    assert broker.get_position(HK) == []
    assert broker.get_account().cash == cash_before
    stored = repo.get_order("HK1", db_path=gate.db_path)
    assert stored is not None and stored.status == OrderStatus.REJECTED


def test_submit_with_loose_hk_code_is_rejected_not_crash(tmp_path):
    """手输 hk00700 / 0700 这类写法：结果是拒单，而不是把异常抛到 API 层。"""
    gate, _ = mk_gate(tmp_path)
    for raw in ("hk00700", "0700", "5"):
        o = gate.submit(mk_order(Side.BUY, price=10.0, volume=100, symbol=raw, oid=f"L-{raw}"))
        assert o.status == OrderStatus.REJECTED
        assert "港股" in o.message


def test_bypassing_submit_still_guarded(tmp_path):
    """策略进程/手工调用绕过 submit 直接 validate 也要拦住（闸②独立成立）。"""
    gate, _ = mk_gate(tmp_path)
    assert any("港股" in x for x in gate.validate(mk_order(symbol=HK)))


def test_illegal_symbol_still_reported_as_illegal(tmp_path):
    """加了港股红线不能把非法代码的处理顺带改坏。"""
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
