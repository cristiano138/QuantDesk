"""ChannelHub —— 把「主流股票软件」接进运行时的装配中枢。

此前 services 层直接 new 具体适配器（scheduler 里写死 `[AkshareFeed(), TushareFeed()]`，
trading 里实盘通道永远是 None），后果是：装了 QMT/通达信也用不上，改优先级要改代码。
本模块把装配收口到一处，按设置动态解析，核心引擎与各服务不再知道任何具体通道名。

三条纪律：
1. **fail-closed**：通道解析失败（未安装 / 未启用 / 凭据缺失 / 连不上）一律返回 None +
   原因，由 RiskGate 拒单，绝不「先用模拟撮合顶一下」——实盘路径上静默降级比停机严重得多；
2. **实时不换源**（ADR-5 宁停勿错）：实时只认主源，主源断了就广播断线，不悄悄切到
   第二源继续驱动策略（两个源的 tick 口径不同，混着喂策略会产生错误信号）；
   历史/财务是查询型，可以按优先级降级；
3. **凭据只从保险库来**：账号/token/资金路径经 `core/secrets.resolve()` 注入，
   不进日志（脱敏）、不进 SQLite、不进 git。

线程安全：装配过程加锁；适配器实例被多方共享，其内部线程安全由各自实现负责。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from quantdesk.adapters import describe_adapters, get_adapter_class
from quantdesk.adapters.base.broker import BrokerAdapter
from quantdesk.adapters.base.datafeed import DataFeedAdapter
from quantdesk.core.enums import FeedCapability
from quantdesk.core.errors import AdapterError, ConfigError, ConnectionLostError
from quantdesk.core.events import Event, EventType, EventBus, SYSTEM_BUS
from quantdesk.core.secrets import get_secret, mask

log = logging.getLogger(__name__)

__all__ = ["ChannelHub", "get_hub"]

# 通道凭据：(保险库键, 传给 connect 的参数名, 是否必填)
_CREDENTIALS: dict[str, list[tuple[str, str, bool]]] = {
    "qmt": [("qmt.userdata_path", "userdata_path", True),
            ("qmt.account_id", "account_id", True)],
    "gm": [("gm.token", "token", True), ("gm.account_id", "account_id", True),
           ("gm.endpoint", "endpoint", False)],
    "easytrader": [("easytrader.ths_path", "ths_path", True)],
}

_PAPER_CHANNEL = "paper"
_RETRY_COOLDOWN_S = 15.0          # 装配失败后的重试冷却（避免每单都去撞一次连接）
_FEED_RETRY_COOLDOWN_S = 60.0     # 行情通道同上：冷却期内不外呼、不刷日志

# 行情通道的凭据（交易通道见 _CREDENTIALS）：feed 的 connect() 无参，凭据由适配器
# 自己经 core.secrets.resolve() 取，这里只登记「UI 该问用户要什么」。
_FEED_CREDENTIALS: dict[str, list[tuple[str, str, bool, str]]] = {
    "tushare": [("tushare.token", "token", True, "Tushare Pro 注册后获得的接口 token")],
    "ths_ifind": [("ifind.username", "username", False, "iFinD 账号（试用/正式）"),
                  ("ifind.password", "password", False, "iFinD 密码"),
                  ("ifind.refresh_token", "refresh_token", False,
                   "iFinD 刷新令牌（推荐，免密登录）")],
}


def credential_spec() -> list[dict]:
    """凭据清单（供设置页渲染表单 + 校验键名）。

    只描述键，不含任何已存值；已存与否由 describe() 的掩码结果回答。
    """
    out: list[dict] = []
    for channel, items in _CREDENTIALS.items():
        for key, arg, required in items:
            out.append({"channel": channel, "key": key, "arg": arg,
                        "required": required, "label": _CRED_LABELS.get(key, arg)})
    for channel, items in _FEED_CREDENTIALS.items():
        for key, arg, required, label in items:
            out.append({"channel": channel, "key": key, "arg": arg,
                        "required": required, "label": label})
    return out


_CRED_LABELS = {
    "qmt.userdata_path": "miniQMT userdata 目录（客户端安装目录下的 userdata）",
    "qmt.account_id": "资金账号",
    "gm.token": "掘金 Token",
    "gm.account_id": "掘金账户 ID（仿真/实盘）",
    "gm.endpoint": "掘金服务地址（留空默认，仿真与实盘可不同）",
    "easytrader.ths_path": "同花顺独立下单程序 xiadan.exe 路径",
}


def _instantiate(cls: type, **want: Any) -> Any:
    """按构造签名过滤 kwargs：各适配器构造参数不同（event_bus/db_path/限速…），
    中枢只传它接收的那部分，未知参数不硬塞（否则新增一个构造参数就要改这里）。"""
    import inspect

    params = inspect.signature(cls).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return cls(**want)
    allowed = {k: v for k, v in want.items() if k in params}
    return cls(**allowed)


class ChannelHub:
    def __init__(self, settings=None, bus: EventBus | None = None,
                 store=None, db_path=None) -> None:
        self.settings = settings              # None → 每次读最新配置（支持热切通道）
        self.bus = bus or SYSTEM_BUS
        self.store = store
        self.db_path = db_path
        self._feeds: dict[str, DataFeedAdapter] = {}
        self._feed_errors: dict[str, str] = {}
        self._feed_retry_at: dict[str, float] = {}   # 装配失败后的重试冷却（monotonic 截止点）
        self._fed = None                          # FederatedFeed 单例（持有订阅引用计数）
        self._broker: BrokerAdapter | None = None
        self._broker_channel: str = ""
        self._broker_error: str = ""
        self._next_retry_at = 0.0
        self._lock = threading.RLock()

    # ---------- 配置 ----------
    def _cfg(self):
        if self.settings is not None:
            return self.settings
        from quantdesk.config import get_settings

        return get_settings()

    @staticmethod
    def _split(csv: str) -> list[str]:
        return [x.strip() for x in (csv or "").split(",") if x.strip()]

    # ---------- 行情通道 ----------
    def feed(self, name: str, *, force: bool = False) -> DataFeedAdapter | None:
        """取（并按需连接）一个行情通道；不可用返回 None，原因记在 feed_errors()。

        装配失败后进入冷却（force=True 可跳过，供面板「测试连接」用）：否则每次外呼都重新
        实例化 + connect 一遍失败的 SDK，既刷满日志，也可能撞上服务端限流。
        """
        with self._lock:
            if name in self._feeds:
                return self._feeds[name]
            if not force and time.monotonic() < self._feed_retry_at.get(name, 0.0):
                return None
            try:
                cls = get_adapter_class(name)
            except KeyError:
                self._feed_errors[name] = "未注册或被禁用（未安装 / 默认禁用通道需先在设置页启用）"
                return None
            except Exception as e:  # noqa: BLE001
                self._feed_errors[name] = str(e)
                return None
            try:
                inst = _instantiate(cls, event_bus=self.bus, store=self.store)
                inst.connect()
            except (AdapterError, ConfigError, TypeError) as e:
                self._feed_errors[name] = f"{e.__class__.__name__}: {e}"
                self._feed_retry_at[name] = time.monotonic() + _FEED_RETRY_COOLDOWN_S
                log.warning("行情通道装配失败 %s: %s（%.0fs 内不再重试，显式探活可绕过）",
                            name, e, _FEED_RETRY_COOLDOWN_S)
                return None
            self._feeds[name] = inst
            self._feed_errors.pop(name, None)
            self._feed_retry_at.pop(name, None)
            log.info("行情通道已接入: %s", name)
            return inst

    def _chain(self, csv: str, require: set[FeedCapability] | None = None) -> list[str]:
        """优先级串 → 实际可用的通道名列表（跳过未就绪者，原因留痕）。"""
        out = []
        for name in self._split(csv):
            if require:
                meta = self._meta(name)
                caps = set(meta.get("capabilities") or ())
                if caps and "dynamic" not in caps:
                    if not require <= caps:
                        self._feed_errors[name] = f"能力不匹配（需 {sorted(r.value for r in require)}）"
                        continue
            if self.feed(name) is not None:
                out.append(name)
        return out

    def history_feeds(self) -> list[DataFeedAdapter]:
        return [f for f in (self.feed(n) for n in self.history_chain()) if f is not None]

    def financial_feeds(self) -> list[DataFeedAdapter]:
        names = self._split(self._cfg().feed_priority_financial)
        return [f for f in (self.feed(n) for n in names) if f is not None]

    def history_chain(self) -> list[str]:
        return self._chain(self._cfg().feed_priority_history,
                           {FeedCapability.HISTORY_BAR})

    def financial_chain(self) -> list[str]:
        return self._chain(self._cfg().feed_priority_financial,
                           {FeedCapability.FINANCIAL})

    def realtime_feed(self) -> DataFeedAdapter | None:
        """主实时源（第一个就绪且声明实时能力者）。断线不换源——见模块 docstring 第 2 条。"""
        for name in self._split(self._cfg().feed_priority_realtime):
            inst = self.feed(name)
            if inst is None:
                continue
            caps = set(getattr(inst, "capabilities", ()) or ())
            if FeedCapability.REALTIME_TICK in caps or FeedCapability.REALTIME_BAR in caps:
                return inst
        return None

    def realtime_channel_name(self) -> str:
        for name in self._split(self._cfg().feed_priority_realtime):
            if name in self._feeds:
                caps = set(getattr(self._feeds[name], "capabilities", ()) or ())
                if caps & {FeedCapability.REALTIME_TICK, FeedCapability.REALTIME_BAR}:
                    return name
        return ""

    def federated(self) -> "object":
        """组装好的组合行情源（services 层唯一直接使用的行情入口，ADR-4）。

        必须缓存成单例：FederatedFeed 持有实时订阅的引用计数表，每次新建实例
        计数就丢——订阅会在下一次装配时被当成全新标的重复向主源挂一遍。
        """
        from quantdesk.adapters.data.federated import FederatedFeed

        with self._lock:
            if self._fed is not None:
                return self._fed
            hist = self.history_feeds()
            fin = self.financial_feeds()
            self._fed = FederatedFeed(history_feeds=hist, financial_feeds=fin or hist,
                                      store=self.store, realtime_feed=self.realtime_feed())
            return self._fed

    # ---------- 交易通道 ----------
    def live_broker(self, *, force: bool = False) -> BrokerAdapter | None:
        """当前实盘通道（模拟盘开关 ON 时不会被调用；解析失败返回 None → RiskGate 拒单）。

        force=True 只在面板「测试连接」用——跳过冷却做一次真实尝试；下单路径必须留冷却。
        """
        channel = str(getattr(self._cfg(), "broker_channel", _PAPER_CHANNEL)) or _PAPER_CHANNEL
        with self._lock:
            if channel == _PAPER_CHANNEL:
                self._close_broker_locked("通道切回模拟盘")
                return None
            if self._broker is not None and self._broker_channel == channel:
                return self._broker
            if not force and time.monotonic() < self._next_retry_at:
                return None                      # 冷却期内不反复撞连接
            try:
                cls = get_adapter_class(channel)
            except KeyError:
                self._fail_broker_locked(
                    f"实盘通道 {channel!r} 不可用：未注册/未安装/默认禁用未启用")
                return None
            creds, missing = self._credentials_for(channel)
            if missing:
                self._fail_broker_locked(
                    f"实盘通道 {channel!r} 缺凭据：{'、'.join(missing)}"
                    "（请到设置页「凭据保险库」录入，加密存储不回显）")
                return None
            try:
                inst = _instantiate(cls, event_bus=self.bus, db_path=self.db_path)
                inst.connect(**creds)
            except (AdapterError, ConfigError, TypeError) as e:
                self._fail_broker_locked(f"实盘通道 {channel!r} 连接失败: {e}")
                return None
            self._close_broker_locked("切换到新通道")
            self._broker, self._broker_channel = inst, channel
            self._broker_error = ""
            log.info("实盘通道已接入: %s（%s）", channel,
                     ", ".join(f"{k}={mask(str(v))}" for k, v in creds.items()))
            return inst

    @staticmethod
    def _credentials_for(channel: str) -> tuple[dict, list[str]]:
        creds: dict[str, str] = {}
        missing: list[str] = []
        for key, arg, required in _CREDENTIALS.get(channel, []):
            val = get_secret(key)
            if val:
                creds[arg] = val
            elif required:
                missing.append(key)
        return creds, missing

    def _fail_broker_locked(self, reason: str) -> None:
        self._broker_error = reason
        self._next_retry_at = time.monotonic() + _RETRY_COOLDOWN_S
        log.warning("%s", reason)

    def _close_broker_locked(self, why: str) -> None:
        if self._broker is None:
            return
        try:
            self._broker.disconnect()
        except Exception as e:  # noqa: BLE001 清理失败不阻塞切换
            log.warning("通道 %s 断开异常（%s）: %s", self._broker_channel, why, e)
        self._broker, self._broker_channel = None, ""

    # ---------- 观测 ----------
    def _meta(self, name: str) -> dict:
        for row in describe_adapters():
            if row["name"] == name:
                return row
        return {}

    def feed_errors(self) -> dict[str, str]:
        with self._lock:
            return dict(self._feed_errors)

    def status(self) -> dict:
        """面板数据：路由现状 + 每通道状态（未连接/已连接/失败原因）。"""
        cfg = self._cfg()
        rows = describe_adapters()
        for row in rows:
            name = row["name"]
            if row["status"] != "ready":
                row["connected"] = False
            else:
                row["connected"] = (name in self._feeds) or (self._broker is not None
                                                             and name == self._broker_channel)
            row["reason"] = self._feed_errors.get(name, "") or ""
            wd = getattr(self._feeds.get(name), "watchdog", None)
            if wd is not None and hasattr(wd, "health"):
                row["channel_health"] = wd.health.to_dict()
        return {
            "simulation_only": bool(cfg.simulation_only),
            "broker_channel": cfg.broker_channel,
            "live_connected": self._broker is not None,
            "live_error": self._broker_error,
            "history_chain": self.history_chain(),
            "financial_chain": self.financial_chain(),
            "realtime_chain": self._split(cfg.feed_priority_realtime),
            "realtime_active": self.realtime_channel_name(),
            "channels": rows,
        }

    def start(self) -> None:
        """启动时预热历史链（失败只记录，不阻断服务起来）。"""
        for name in self.history_chain():
            self.feed(name)

    def stop(self) -> None:
        with self._lock:
            if self._fed is not None:
                try:
                    self._fed.disconnect()
                except Exception as e:  # noqa: BLE001 退出清理尽力而为
                    log.debug("组合行情源断开异常: %s", e)
                self._fed = None
            for name, inst in list(self._feeds.items()):
                try:
                    inst.disconnect()
                except Exception as e:  # noqa: BLE001 退出清理尽力而为
                    log.debug("行情通道断开异常 %s: %s", name, e)
            self._feeds.clear()
            self._close_broker_locked("服务停止")

    # ---------- 事件 ----------
    def announce(self, action: str, **payload: Any) -> None:
        self.bus.publish(Event(EventType.RISK, data={"action": action, **payload},
                               source="channels"))


_hub: ChannelHub | None = None
_hub_lock = threading.Lock()


def get_hub() -> ChannelHub:
    """进程级单例（API/服务共用同一批连接——QMT 这类通道全局只能有一份会话）。"""
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = ChannelHub()
        return _hub


def reset_hub_for_test() -> None:
    global _hub
    with _hub_lock:
        if _hub is not None:
            _hub.stop()
        _hub = None
