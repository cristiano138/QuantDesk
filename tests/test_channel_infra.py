"""接入层基础设施单测：可选依赖治理 + 注册表启停 + 断线自愈守护。"""

from __future__ import annotations

import types

import pytest

from quantdesk.adapters import describe_adapters, get_adapter_class, list_adapters
from quantdesk.adapters.base.optional import (
    AdapterMeta,
    dependency_available,
    import_optional,
    meta_of,
)
from quantdesk.core.errors import AdapterError
from quantdesk.core.events import Event, EventBus
from quantdesk.core.resilience import Backoff, ReconnectWatchdog


# ---------- 可选依赖 ----------
def test_dependency_available_false_for_missing_module():
    assert dependency_available("no_such_sdk_module_xyz") is False


def test_dependency_available_true_for_installed_and_imported():
    assert dependency_available("pandas") is True
    import pandas  # noqa: F401
    assert dependency_available("pandas") is True


def test_import_optional_raises_structured_error_with_guidance():
    with pytest.raises(AdapterError) as ei:
        import_optional("no_such_sdk_module_xyz", channel="QMT",
                        install_hint="请安装券商提供的 xtquant whl")
    msg = str(ei.value)
    assert "QMT" in msg and "whl" in msg          # 必须给「怎么办」，不只是堆栈
    assert "no_such_sdk_module_xyz" in msg


def test_import_optional_returns_module_when_present():
    fake = types.ModuleType("fake_optional_sdk")
    fake.mark = "ok"
    import sys

    sys.modules["fake_optional_sdk"] = fake
    try:
        assert import_optional("fake_optional_sdk", channel="测试",
                               install_hint="-").mark == "ok"
    finally:
        sys.modules.pop("fake_optional_sdk", None)


def test_meta_of_reads_class_declared_metadata_with_defaults():
    class Feed:
        name = "demo"
        label = "演示通道"
        requires = ("pandas", "missing_sdk_abc")
        official = False
        default_enabled = False
        enable_flag = "enable_demo"

    m = meta_of(Feed, kind="data")
    assert (m.name, m.kind, m.label) == ("demo", "data", "演示通道")
    assert m.official is False and m.default_enabled is False
    assert m.enable_flag == "enable_demo"
    assert m.deps_ready() is False
    assert m.missing_deps() == ["missing_sdk_abc"]
    assert m.capabilities == ()                    # 未声明能力 → 空


def test_requires_any_semantics():
    """通达信这类「双传输层择一」通道：装了一条路就不能报 unready。"""
    class OneWay:
        name = "tdx_aidata"
        requires = ("TdxAiData", "pytdx")
        requires_any = True

    class AllWay:
        name = "strict"
        requires = ("pandas", "no_such_module_xyz")

    any_meta = meta_of(OneWay, kind="data")
    all_meta = meta_of(AllWay, kind="data")
    assert any_meta.deps_ready() is False                  # 两个都没装
    assert any_meta.missing_deps() == ["TdxAiData", "pytdx"]
    assert all_meta.deps_ready() is False
    assert all_meta.missing_deps() == ["no_such_module_xyz"]

    import sys
    import types

    sys.modules["pytdx"] = types.ModuleType("pytdx")
    try:
        assert any_meta.deps_ready() is True               # 任一条路通即就绪
        assert any_meta.missing_deps() == []               # 不再列另一条路
    finally:
        sys.modules.pop("pytdx", None)


def test_builtin_adapters_describe_with_capabilities():
    rows = {r["name"]: r for r in describe_adapters()}
    assert "akshare" in rows and "paper" in rows
    assert rows["akshare"]["kind"] == "data"
    assert rows["paper"]["kind"] == "broker" and rows["paper"]["is_paper"] is True
    assert "history_bar" in rows["akshare"]["capabilities"]


# ---------- 默认禁用通道（宪法第 9 条：注册表源头不可见） ----------
def _install_fake_disabled(monkeypatch, name: str = "demo_gui", flag: str = "enable_easytrader"):
    import quantdesk.adapters as reg

    reg._scan()
    cls = type("Demo", (), {"name": name})
    meta = AdapterMeta(name=name, kind="broker", label="演示非官方通道", prereq="",
                       official=False, requires=(), default_enabled=False,
                       enable_flag=flag, is_paper=False, capabilities=())
    monkeypatch.setitem(reg._ALL, name, cls)      # monkeypatch 用例结束自动还原
    monkeypatch.setitem(reg._META, name, meta)
    return name


def test_disabled_adapter_invisible_to_trading_api(monkeypatch):
    from quantdesk import config as cfg

    name = _install_fake_disabled(monkeypatch)
    s = cfg.Settings(simulation_only=True, enable_easytrader=False)
    monkeypatch.setattr(cfg, "get_settings", lambda: s)
    assert name not in list_adapters()
    with pytest.raises(KeyError):
        get_adapter_class(name)
    row = next(r for r in describe_adapters() if r["name"] == name)
    assert row["status"] == "disabled"            # 面板可见（可引导启用），API 不可见
    assert name not in [r["name"] for r in describe_adapters(include_disabled=False)]


def test_enabling_flag_makes_adapter_visible(monkeypatch):
    from quantdesk import config as cfg

    name = _install_fake_disabled(monkeypatch)
    s = cfg.Settings(simulation_only=True, enable_easytrader=True)
    monkeypatch.setattr(cfg, "get_settings", lambda: s)
    assert name in list_adapters()
    assert get_adapter_class(name) is not None
    row = next(r for r in describe_adapters() if r["name"] == name)
    assert row["status"] in ("ready", "unready")     # 取决于本机 SDK，两者都合法
    assert name in [r["name"] for r in describe_adapters(include_disabled=False)]


# ---------- 退避 ----------
def test_backoff_sequence_grows_and_caps():
    b = Backoff(base=1.0, cap=8.0, factor=2.0, jitter=0.0)
    assert [b.next_delay() for _ in range(5)] == [1.0, 2.0, 4.0, 8.0, 8.0]
    b.reset()
    assert b.next_delay() == 1.0


def test_backoff_jitter_stays_within_bounds():
    b = Backoff(base=4.0, cap=60.0, factor=2.0, jitter=0.1)
    for _ in range(20):
        d = b.next_delay()
        assert 0.0 <= d <= 60.0 * 1.11


# ---------- 守护：判死 → 退避重连 → 重连后重放 ----------
class Collector:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(self, event: Event) -> None:
        self.events.append(event)

    def actions(self) -> list[str]:
        return [e.data["action"] for e in self.events if isinstance(e.data, dict)]


def test_watchdog_probe_failure_marks_lost_and_broadcasts_once():
    bus = EventBus()
    c = Collector()
    bus.subscribe(None, c.publish)
    wd = ReconnectWatchdog(name="qmt", probe=lambda: False, reconnect=lambda: None,
                           bus=bus, interval=0.01, backoff=Backoff(base=0, cap=0, jitter=0))
    assert wd.check_once() is False
    assert wd.check_once() is False
    assert wd.health.state == "lost"
    assert c.actions() == ["channel_down"]         # 状态未再变化就不重复刷屏
    wd.stop()


def test_watchdog_reconnect_runs_resubscribe_hook_and_broadcasts_up():
    bus = EventBus()
    c = Collector()
    bus.subscribe(None, c.publish)
    replayed: list[str] = []
    ok = {"v": False}

    def reconnect():
        ok["v"] = True

    wd = ReconnectWatchdog(name="qmt", probe=lambda: ok["v"], reconnect=reconnect,
                           on_reconnected=lambda: replayed.append("subs"),
                           bus=bus, interval=0.01,
                           backoff=Backoff(base=0, cap=0, jitter=0))
    wd.check_once()
    assert wd.health.state == "lost"
    assert wd.recover_once() is True
    assert replayed == ["subs"]                     # 宪法义务：重连成功后重放订阅
    assert wd.health.state == "ok"
    assert "channel_up" in c.actions()
    assert wd.health.reconnects == 1
    wd.stop()


def test_watchdog_reconnect_failure_keeps_retrying_and_counts_attempts():
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("终端未登录")

    wd = ReconnectWatchdog(name="tdx", probe=lambda: False, reconnect=boom,
                           backoff=Backoff(base=0, cap=0, jitter=0))
    assert wd.recover_once() is False
    assert wd.recover_once() is False
    assert calls["n"] == 2
    assert wd.health.reconnect_attempts == 2
    assert "终端未登录" in wd.health.last_error
    wd.stop()


def test_watchdog_mark_lost_wakes_and_recovered_state_flips_back():
    ok = {"v": True}
    wd = ReconnectWatchdog(name="gm", probe=lambda: ok["v"], reconnect=lambda: None,
                           backoff=Backoff(base=0, cap=0, jitter=0))
    wd.start()
    wd.check_once()
    assert wd.health.state == "ok"
    ok["v"] = False
    wd.mark_lost("SDK disconnected 回调")
    wd.check_once()
    assert wd.health.state == "lost"
    ok["v"] = True
    assert wd.recover_once() is True
    assert wd.health.state == "ok"
    wd.stop()
