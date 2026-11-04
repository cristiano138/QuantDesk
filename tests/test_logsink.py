"""运行日志落库与检索（LogSink）：LOG/RISK 事件 → 批量入库 → 条件分页查询 → 来源枚举 → 保留期清理。

前端「策略页 · 历史日志」面板的事实源就是这几个查询，所以过滤维度逐个锁住，
避免「只有一个关键字能用」这种只能演示不能排障的实现。
"""

from __future__ import annotations

import pytest

from quantdesk.core.events import Event, EventType, EventBus
from quantdesk.store.db import init_db, reset_engine
from quantdesk.services.logsink import LogSink


@pytest.fixture
def sink(tmp_path, monkeypatch):
    from quantdesk import config as cfg

    monkeypatch.setattr(cfg, "data_dir", lambda: tmp_path)
    reset_engine()
    init_db(tmp_path / "logs.db")
    bus = EventBus()
    s = LogSink(bus=bus, db_path=tmp_path / "logs.db")
    s.start()
    yield s, bus
    s.stop()
    reset_engine()


def _emit(bus: EventBus, message: str, *, source: str, level: str = "INFO",
          strategy_id: str = "", risk: bool = False) -> None:
    etype = EventType.RISK if risk else EventType.LOG
    bus.publish(Event(etype, data={"message": message, "level": level,
                                   "strategy_id": strategy_id}, source=source))


def test_log_events_are_written_and_queryable(sink):
    s, bus = sink
    _emit(bus, "策略启动", source="dual_ma", strategy_id="dual_ma")
    _emit(bus, "限速冷却中", source="akshare")
    assert s.flush_now() >= 0                       # 立即落库（不等批量周期）
    out = s.query(limit=50)
    assert out["total"] == 2
    assert {i["message"] for i in out["items"]} == {"策略启动", "限速冷却中"}


def test_query_filters_cover_every_panel_control(sink):
    s, bus = sink
    _emit(bus, "金叉触发", source="dual_ma", strategy_id="dual_ma")
    _emit(bus, "下单被拒：资金不足", source="riskgate", level="WARN", risk=True)
    _emit(bus, "行情断线", source="akshare")
    s.flush_now()

    assert s.query(source="akshare")["total"] == 1
    assert s.query(strategy_id="dual_ma")["total"] == 1
    assert s.query(level="WARN")["total"] == 1
    assert s.query(contains="资金不足")["total"] == 1
    assert s.query(contains="不存在的关键字")["total"] == 0

    page = s.query(limit=1, offset=1)
    assert page["total"] == 3 and len(page["items"]) == 1          # 分页不吞总数

    future = s.query(since_ms=int(_now_ms() + 60_000))
    assert future["total"] == 0                                   # 时间窗生效
    assert s.query(until_ms=int(_now_ms() + 60_000))["total"] == 3


def test_sources_endpoint_feeds_the_source_filter(sink):
    """历史日志页的「来源」下拉不能让用户手打字符串——由已入库的来源枚举驱动。"""
    s, bus = sink
    for _ in range(3):
        _emit(bus, "心跳超时", source="supervisor")
    _emit(bus, "订阅重放完成", source="qmt")
    s.flush_now()

    rows = {(r["source"], r["count"]) for r in s.sources()}
    assert ("supervisor", 3) in rows and ("qmt", 1) in rows
    counts = [r["count"] for r in s.sources()]
    assert counts == sorted(counts, reverse=True)                 # 按条数降序，噪声源排前面


def test_purge_before_removes_only_older_rows(sink):
    s, bus = sink
    _emit(bus, "旧日志", source="akshare")
    s.flush_now()
    cutoff = int(_now_ms() + 1000)
    assert s.purge_before(cutoff) == 1
    assert s.query()["total"] == 0


def _now_ms() -> float:
    from quantdesk.core.timeutil import now_ms

    return now_ms()
