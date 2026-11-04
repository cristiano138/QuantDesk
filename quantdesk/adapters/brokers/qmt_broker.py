"""QMT / miniQMT 实盘交易通道（xtquant.xttrader）—— 本项目主力实盘通道。

线程模型（与 xtdata 的行情回调是**两个不同的线程**，见 docs/integration/qmt.md 已知坑）：
- 查询与下单是同步调用，可在调用方线程直接执行（xttrader 内部有自己的会话队列）；
  唯独 **下单不重试**——重试可能造成重复委托，宁可抛错让上层决定是否重下；
- 券商回报（on_stock_order / on_stock_trade / on_order_error / on_cancel_error）跑在 SDK
  推送线程上：回调只把原始对象入队，归一化与 EventBus 广播在工作线程 `qmt-report` 完成
  （宪法线程纪律：回调内只入队禁止阻塞）；
- on_disconnected 只标记状态 + 入队，重连由 ReconnectWatchdog 退避驱动。

适配器合同（宪法，逐条落实）：
- 只执行不风控：进来的 Order 已过 RiskGate 三道闸，order_id 由引擎预生成；
- **order_remark = 本地 order_id 是委托↔回报关联的唯一锚**，任何路径都不允许省略或改写；
- 提交失败抛 BrokerError（返回 -1/0/异常都不静默）；断线抛 ConnectionLostError 且
  health_check 可探测；通道断线期间一律拒单（fail-closed，不排队不重试）；
- 重连成功后重放账户订阅并刷新资金/持仓快照；
- 价格原样透传，绝不做复权换算（宪法第 5 条）；
- 适配器不落库：落库与广播的顺序（先落库后广播）在 services 层，这里只发事件。

凭据纪律：userdata 路径与资金账号只在进程内使用，日志与异常文本一律 mask()
（前 4 位 + ***）——异常文本会被 ChannelHub 原样写进日志，所以两处都不能带明文。
"""

from __future__ import annotations

import dataclasses
import logging
import queue
import random
import threading
from collections.abc import Callable
from datetime import date, datetime

from quantdesk.adapters.base.broker import BrokerAdapter
from quantdesk.adapters.base.optional import import_optional
from quantdesk.config import get_settings
from quantdesk.core.enums import OrderStatus, OrderType, Side
from quantdesk.core.errors import (
    AdapterError,
    BrokerError,
    ConnectionLostError,
)
from quantdesk.core.events import Event, EventType, EventBus
from quantdesk.core.models import Account, Order, Position, Trade
from quantdesk.core.resilience import Backoff, ReconnectWatchdog
from quantdesk.core.symbols import from_internal, to_internal
from quantdesk.core.timeutil import now_ms

log = logging.getLogger(__name__)

__all__ = ["QmtBroker"]

_INSTALL_HINT = (
    "xtquant 需券商 QMT 客户端自带的 whl：uv pip install xtquant（或安装券商提供的 "
    "xtquant-x.x.x.whl）；本项目不改 pyproject 主依赖，详见 docs/integration/qmt.md"
)
_LOGIN_GUIDE = "请确认 miniQMT/QMT 客户端已登录且开启「极简模式（独立交易）」，userdata 路径指向客户端 userdata 目录"

# ---------- 委托方向常量（xtconstant 优先，缺失才用下列兜底值） ----------
_SIDE_CONST: dict[str, tuple[tuple[str, ...], int]] = {
    Side.BUY.value: (("STOCK_BUY", "CREDIT_BUY"), 23),
    Side.SELL.value: (("STOCK_SELL", "CREDIT_SELL"), 24),
}

# ---------- 报价类型映射（市价申报沪深规则不同，绝不共用一个常量） ----------
# 沪市：市价单只有「最优五档即时成交剩余撤销 / 剩余转限价 / 本方最优 / 对手方最优 /
#       全额成交或撤销」几档，常用 MARKET_BEST_5（最优五档即时成交剩余撤销）；
# 深市：没有与沪市等价的那一档，语义上对应「对手方最优价格申报」MARKET_PEER_PRICE_FIRST，
#       北交所暂按深市口径（未在线核对）。
# 兜底整数只是「真机缺该常量时」的占位（交易所申报模板序号风格），**不作权威值**：
# 名称与取值都必须按 docs/integration/qmt.md 在线核对清单第 2/3 条逐项打勾。
_LIMIT_PRICE_TYPE: tuple[tuple[str, ...], int] = (("FIX_PRICE",), 11)
_MARKET_PRICE_TYPE: dict[str, tuple[tuple[str, ...], int]] = {
    "SH": (("MARKET_BEST_5", "MARKET_BEST_5_SH"), 1),
    "SZ": (("MARKET_PEER_PRICE_FIRST", "MARKET_SZ_PEER_PRICE_FIRST"), 42),
    "BJ": (("MARKET_PEER_PRICE_FIRST", "MARKET_BJ_PEER_PRICE_FIRST"), 42),
}

# ---------- 委托状态映射（xtquant 48~57；同义拼写一并覆盖） ----------
_ORDER_STATUS_DEFS: tuple[tuple[tuple[str, ...], int, OrderStatus], ...] = (
    (("ORDER_UNREPORTED",), 48, OrderStatus.SUBMITTED),          # 未报
    (("ORDER_WAIT_REPORTING", "ORDER_WAIT_REPORT"), 49, OrderStatus.SUBMITTED),   # 待报
    (("ORDER_REPORTED",), 50, OrderStatus.SUBMITTED),            # 已报
    (("ORDER_REPORTED_CANCEL",), 51, OrderStatus.SUBMITTED),     # 已报待撤：撤单在途，非终态
    (("ORDER_PARTSUCC_CANCEL", "ORDER_PARTSUCC_CANCELLED"), 52, OrderStatus.PART_FILLED),  # 部成待撤
    (("ORDER_PART_CANCEL", "ORDER_PART_CANCELLED"), 53, OrderStatus.CANCELLED),   # 部撤
    (("ORDER_CANCELED", "ORDER_CANCELLED"), 54, OrderStatus.CANCELLED),           # 已撤
    (("ORDER_PART_FILLD", "ORDER_PART_FILLED"), 55, OrderStatus.PART_FILLED),     # 部成
    (("ORDER_ALL_FILLDED", "ORDER_ALL_FILLED"), 56, OrderStatus.FILLED),          # 已成
    (("ORDER_JUNK",), 57, OrderStatus.REJECTED),                                  # 废单
)
# 未识别状态码的保守取值：给非终态 SUBMITTED——既不上报「已成交/已撤」误导本地状态机，
# 也留给后续回报或对账把它收敛到真值。
_UNKNOWN_STATUS_FALLBACK = OrderStatus.SUBMITTED


def mask(value: str | None) -> str:
    """日志脱敏：前 4 位 + ***（宪法日志脱敏条款）。"""
    v = str(value or "")
    if not v:
        return "<none>"
    return v[:4] + "***" if len(v) > 4 else "****"


def _const_of(module, names: tuple[str, ...], fallback: int) -> int:
    """按候选常量名依次探测；真机缺失时回退占位值并留痕（不把猜值当权威）。"""
    for n in names:
        v = getattr(module, n, None) if module is not None else None
        if v is not None:
            return int(v)
    log.warning("xtconstant 未提供常量 %s，回退占位值 %s（请按 docs/integration/qmt.md "
                "在线核对清单修正常量名/取值）", "/".join(names), fallback)
    return int(fallback)


def build_status_map(xtconstant) -> dict[int, OrderStatus]:
    """委托状态码表：SDK 常量优先，缺失用兜底整数（同一处集中维护，便于真机校正）。"""
    m: dict[int, OrderStatus] = {}
    for names, fallback, status in _ORDER_STATUS_DEFS:
        m[_const_of(xtconstant, names, fallback)] = status
    return m


def map_order_status(raw, status_map: dict[int, OrderStatus]) -> OrderStatus:
    """状态码 → OrderStatus；未识别的码保守处理并留痕（绝不猜成终态）。"""
    if isinstance(raw, OrderStatus):
        return raw
    try:
        code = int(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("QMT 委托状态 %r 无法解析为状态码 → 保守按 %s 处理",
                    raw, _UNKNOWN_STATUS_FALLBACK.value)
        return _UNKNOWN_STATUS_FALLBACK
    mapped = status_map.get(code)
    if mapped is None:
        log.warning("QMT 未识别的委托状态码 %s → 保守按 %s 处理（待后续回报/对账收敛）",
                    code, _UNKNOWN_STATUS_FALLBACK.value)
        return _UNKNOWN_STATUS_FALLBACK
    return mapped


def _attr(obj, *names, default=None):
    """SDK 回报字段容错读取：对象/dict 都吃，候选名按顺序取第一个非空。

    为什么要容错：xtquant 各券商版本字段名有出入（如 order_status / status），
    猜死一个名字会让回报静默丢失——那比报错更危险。
    """
    for n in names:
        if isinstance(obj, dict):
            v = obj.get(n)
        else:
            v = getattr(obj, n, None)
        if v is not None and v != "":
            return v
    return default


def _qmt_time_to_ms(v, *, today: date | None = None) -> int:
    """QMT 时间字段归一化为毫秒：HHMMSS（int/str）、YYYYMMDDHHMMSS、epoch 秒/毫秒。"""
    if v is None:
        return now_ms()
    s = str(v).strip()
    if s.isdigit():
        if len(s) == 14:                              # YYYYMMDDHHMMSS（要先判：它也大于 1e11）
            try:
                return int(datetime.strptime(s, "%Y%m%d%H%M%S").timestamp() * 1000)
            except ValueError:
                return now_ms()
        n = int(s)
        if n > 10 ** 11:
            return n                                  # 已是毫秒
        if n > 10 ** 9:
            return n * 1000                           # epoch 秒
        if len(s) <= 6:                               # HHMMSS（前导零常被吃掉：93001）
            t = s.zfill(6)
            try:
                d = today or date.today()
                dt = datetime(d.year, d.month, d.day, int(t[:2]), int(t[2:4]), int(t[4:6]))
                return int(dt.timestamp() * 1000)
            except ValueError:
                return now_ms()
    try:
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except (TypeError, ValueError):
        return now_ms()


class _ReportSink:
    """SDK 回调 → 队列的唯一入口（回调线程里只做 put，绝不碰别的）。"""

    def __init__(self, q: queue.Queue, on_lost: Callable[[str], None]) -> None:
        self.q = q
        self.on_lost = on_lost

    def put(self, kind: str, payload=None) -> None:
        self.q.put((kind, payload))


def _make_callback_class(xttrader, sink: _ReportSink) -> type:
    """动态构造继承 SDK 回调基类的子类（基类只能在 connect() 里拿到，故不能在模块顶层定义）。"""
    base = getattr(xttrader, "XtQuantTraderCallback", object)

    class _QmtCallback(base):  # noqa: ANN001 — SDK 回调签名由对端决定
        def on_stock_order(self, order) -> None:
            sink.put("order", order)

        def on_stock_trade(self, trade) -> None:
            sink.put("trade", trade)

        def on_order_error(self, order_error) -> None:
            sink.put("order_error", order_error)

        def on_cancel_error(self, cancel_error) -> None:
            sink.put("cancel_error", cancel_error)

        def on_disconnected(self) -> None:
            # 断线只「标记 + 唤醒守护线程」：回调线程里绝不 sleep/重连（宪法线程纪律）
            sink.on_lost("QMT 交易会话断线（SDK on_disconnected 回报）")

    return _QmtCallback


class QmtBroker(BrokerAdapter):
    """QMT/miniQMT 实盘通道：xtquant.xttrader 同步下单 + 回报入队 + 断线自愈。"""

    name = "qmt"
    label = "QMT / miniQMT 实盘（xtquant，主力通道）"
    prereq = "券商开通 QMT 实盘权限 → 本机登录 miniQMT（极简模式）→ 设置页凭据保险库录入 " \
             "qmt.userdata_path 与 qmt.account_id → 全局模拟盘开关切 OFF（默认 ON 时不会走到这里）"
    official = True
    is_paper = False
    requires = ("xtquant",)
    default_enabled = True
    enable_flag = ""

    def __init__(self, event_bus: EventBus | None = None, *,
                 reconcile_hook: Callable[..., None] | None = None) -> None:
        self.bus = event_bus
        # 重连后的对账钩子由 services 注入（差异比对与落库都不是适配器的事）
        self.reconcile_hook = reconcile_hook

        self._trader_mod = None
        self._const = None
        self._status_map: dict[int, OrderStatus] = {}
        self._trader = None
        self._account = None
        self._session_id = 0

        self._userdata_path = ""      # 凭据：只进程内使用，日志/异常一律 mask()
        self._account_id = ""

        self._connected = False
        self._report_q: queue.Queue = queue.Queue()
        self._report_thread: threading.Thread | None = None
        self._stop_reports = threading.Event()
        self._sink: _ReportSink | None = None

        # 委托关联：broker order_id ↔ 本地 order_id（place_order 与每条回报都补齐，双向可查）
        self._local_to_broker: dict[str, str] = {}
        self._broker_to_local: dict[str, str] = {}
        self._lock = threading.RLock()

        self.watchdog: ReconnectWatchdog | None = None   # ChannelHub 状态灯读这个属性

    # ================= 生命周期 =================
    def connect(self, **credentials: str) -> None:
        """建会话：XtQuantTrader(userdata_path, session_id) → register_callback → start → connect。

        session_id 每次启动都换（日期 + 随机后缀）：复用同一 session 会与残留会话冲突，
        报「已存在相同 session」导致连不上——这是 QMT 接入的第一个坑。
        """
        userdata_path = str(credentials.get("userdata_path", "") or "").strip()
        account_id = str(credentials.get("account_id", "") or "").strip()
        if not userdata_path or not account_id:
            raise BrokerError("QMT connect 需要 userdata_path 与 account_id："
                              "请在设置页「凭据保险库」录入 qmt.userdata_path / qmt.account_id")
        if self._connected:
            return
        with self._lock:
            self._userdata_path = userdata_path
            self._account_id = account_id
            self._open_session_locked()
            self._connected = True
            self._ensure_report_thread()
            self._ensure_watchdog()
        if self.watchdog is not None:
            self.watchdog.start()
        log.info("QMT 交易通道已连接: userdata=%s account=%s session=%s",
                 mask(self._userdata_path), mask(self._account_id), self._session_id)

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            wd, self.watchdog = self.watchdog, None
            trader, account, self._trader, self._account = self._trader, self._account, None, None
        if wd is not None:
            wd.stop()
        self._stop_reports.set()
        self._report_q.put(None)
        t, self._report_thread = self._report_thread, None
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
        if trader is not None:
            try:
                if account is not None:
                    trader.unsubscribe(account)
                trader.stop()
            except Exception as e:  # noqa: BLE001 主动断开阶段的异常不阻塞清理
                log.warning("QMT 通道关闭时 unsubscribe/stop 异常（忽略）: %s", e)

    def health_check(self) -> bool:
        """连接态 + 一次轻量资金查询探测；断线可被探测（返回 False，不抛）。"""
        if not self._connected or self._trader is None:
            return False
        return self._probe()

    def _probe(self) -> bool:
        try:
            asset = self._trader.query_stock_asset(self._account)
        except Exception as e:  # noqa: BLE001
            self._mark_lost(f"QMT 探活查询失败: {e}")
            return False
        if asset is None:
            self._mark_lost("QMT 探活返回空资金快照（会话已不可用）")
            return False
        return True

    def _mark_lost(self, reason: str) -> None:
        """标记断线：本地状态立即置灰（fail-closed 拒单）+ 唤醒守护线程走退避重连。"""
        self._connected = False
        log.warning("%s —— QMT 交易通道按断线处理（fail-closed 拒单）", reason)
        if self.watchdog is not None:
            self.watchdog.mark_lost(reason)

    def snapshot_for_reconcile(self) -> tuple[list[Order], list[Trade], list[Position],
                                             Account | None]:
        """全量快照 (orders, trades, positions, account)——比对与落库在 services 层。"""
        self._require_live()
        orders = self.get_orders()
        trades = self.get_trades()
        positions = self.get_position()
        account = self.get_account()
        return orders, trades, positions, account

    # ================= 交易 =================
    def place_order(self, order: Order) -> Order:
        """提交委托：order_remark 带本地 order_id（回报关联唯一锚，绝不可省）。

        不重试：下单重试可能造成重复委托，失败就抛 BrokerError 由上层决定。
        """
        self._require_live()
        trader = self._trader
        code = from_internal(order.symbol, "qmt")
        side_names, side_fb = _SIDE_CONST[_sv(order.side)]
        qmt_side = _const_of(self._const, side_names, side_fb)
        price_type = self._price_type_of(order)
        price = float(order.price)          # 宪法第 5 条：原样透传，绝不复权换算
        try:
            ret = trader.order_stock(self._account, code, qmt_side, int(order.volume),
                                     price_type, price,
                                     strategy_name=str(order.strategy_id),
                                     order_remark=str(order.order_id))
        except Exception as e:  # noqa: BLE001 提交异常必须抛结构化错误，禁止静默
            raise BrokerError(f"QMT 下单异常（symbol={code} side={_sv(order.side)} "
                              f"volume={order.volume}）: {e.__class__.__name__}: {e}") from e
        try:
            bid = str(int(ret))
        except (TypeError, ValueError):
            raise BrokerError(f"QMT 下单返回不可解析的委托编号 {ret!r}（symbol={code}）："
                              "按提交失败处理，请核对 xtquant 版本") from None
        if int(bid) <= 0:
            raise BrokerError(
                f"QMT 下单未受理（order_stock 返回 {ret}）：请检查 miniQMT 登录状态、"
                f"资金账号与交易权限（symbol={code}）")
        with self._lock:
            self._local_to_broker[order.order_id] = bid
            self._broker_to_local[bid] = order.order_id
        placed = dataclasses.replace(order, broker_order_id=bid, status=OrderStatus.SUBMITTED,
                                     message="已提交 QMT", updated_at=now_ms())
        self._publish(EventType.ORDER, placed)
        log.info("QMT 委托已提交: local=%s broker=%s %s %s %d@%.2f type=%s",
                 order.order_id, bid, code, _sv(order.side), order.volume, price, price_type)
        return placed

    def cancel_order(self, order_id: str) -> bool:
        """发起撤单：返回是否受理，最终状态以委托回报为准。"""
        self._require_live()
        with self._lock:
            broker_id = self._local_to_broker.get(order_id)
        if not broker_id:
            # 本地没有映射 = 不是本会话下的单（重启后走查询回填，见 _learn_mapping）
            log.warning("QMT 撤单未受理：本地 order_id=%s 无 broker 映射"
                        "（重启或未过本通道下单）", order_id)
            return False
        try:
            accepted = bool(self._trader.cancel_order_stock(self._account, int(broker_id)))
        except Exception as e:  # noqa: BLE001
            if isinstance(e, ConnectionLostError):
                raise
            raise ConnectionLostError(f"QMT 撤单调用异常（通道可能已断）: {e}") from e
        if not accepted:
            log.warning("QMT 撤单未受理: local=%s broker=%s（终态以回报为准）",
                        order_id, broker_id)
        return accepted

    def poll(self) -> None:
        """回调型通道：QMT 有主动回报推送，无需轮询（宪法注明留空实现）。"""

    # ================= 查询（归一化到 core models；失败抛 BrokerError） =================
    def get_account(self) -> Account:
        self._require_live()
        raw = self._query(lambda: self._trader.query_stock_asset(self._account),
                          "query_stock_asset")
        if raw is None:
            raise BrokerError("QMT 资金查询返回空（query_stock_asset）：请确认账户已订阅成功")
        return Account(
            total_asset=float(_attr(raw, "total_asset", "equity", default=0.0) or 0.0),
            cash=float(_attr(raw, "cash", "available", default=0.0) or 0.0),
            market_value=float(_attr(raw, "market_value", "stock_value", default=0.0) or 0.0),
            frozen=float(_attr(raw, "frozen_cash", "frozen", default=0.0) or 0.0),
            updated_at=now_ms())

    def get_position(self, symbol: str | None = None) -> list[Position]:
        """持仓：available **必须取 can_use_volume**（T+1 语义的落点，宪法第 2 道闸依赖它）。"""
        self._require_live()
        raw = self._query(lambda: self._trader.query_stock_positions(self._account),
                          "query_stock_positions")
        out: list[Position] = []
        for row in (raw or []):
            code = str(_attr(row, "stock_code", "symbol", default="") or "")
            if not code:
                continue
            volume = int(_attr(row, "volume", "position_volume", default=0) or 0)
            available = int(_attr(row, "can_use_volume", "available_volume", default=0) or 0)
            position = Position(
                symbol=to_internal(code, "qmt"),
                volume=volume,
                available=available,
                avg_cost=float(_attr(row, "avg_price", "open_price", "cost_price",
                                     default=0.0) or 0.0),
                market_value=float(_attr(row, "market_value", default=0.0) or 0.0),
                strategy_id=str(_attr(row, "strategy_name", default="manual") or "manual"))
            out.append(position)
        if symbol is None:
            return out
        return [p for p in out if p.symbol == symbol]

    def get_orders(self, status: OrderStatus | None = None) -> list[Order]:
        self._require_live()
        raw = self._query(lambda: self._trader.query_stock_orders(self._account),
                          "query_stock_orders")
        out = []
        for row in (raw or []):
            o = self._to_order(row)
            self._learn_mapping(o)
            out.append(o)
        if status is None:
            return out
        return [o for o in out if _sv(o.status) == _sv(status)]

    def get_trades(self, start: datetime | None = None) -> list[Trade]:
        self._require_live()
        raw = self._query(lambda: self._trader.query_stock_trades(self._account),
                          "query_stock_trades")
        out = [self._to_trade(row) for row in (raw or [])]
        if start is None:
            return out
        start_ms = int(start.timestamp() * 1000)
        return [t for t in out if t.traded_at >= start_ms]

    # ================= 内部：会话 =================
    def _open_session_locked(self) -> None:
        """在锁内建立 trader 会话（connect 与重连共用）。失败抛 ConnectionLostError。"""
        trader_mod = import_optional("xtquant.xttrader", channel="QMT 交易",
                                     install_hint=_INSTALL_HINT)
        type_mod = import_optional("xtquant.xttype", channel="QMT 交易",
                                   install_hint=_INSTALL_HINT)
        self._const = import_optional("xtquant.xtconstant", channel="QMT 交易",
                                      install_hint=_INSTALL_HINT)
        self._trader_mod = trader_mod
        self._status_map = build_status_map(self._const)

        self._session_id = self._new_session_id()
        trader = trader_mod.XtQuantTrader(self._userdata_path, self._session_id)
        self._sink = _ReportSink(self._report_q, self._mark_lost)
        trader.register_callback(_make_callback_class(trader_mod, self._sink)())
        trader.start()
        self._trader = trader                    # 失败路径也要能被清理
        try:
            ret = trader.connect()
        except Exception as e:  # noqa: BLE001
            self._safe_stop_locked(trader)
            raise ConnectionLostError(f"QMT 交易连接异常: {e}；{_LOGIN_GUIDE}") from e
        if int(ret) != 0:
            self._safe_stop_locked(trader)
            raise ConnectionLostError(
                f"QMT 交易连接失败（trader.connect() 返回 {ret}）：{_LOGIN_GUIDE}")

        account = type_mod.StockAccount(self._account_id)
        try:
            sub_ret = trader.subscribe(account)
        except Exception as e:  # noqa: BLE001
            self._safe_stop_locked(trader)
            raise ConnectionLostError(f"QMT 账户订阅异常: {e}；{_LOGIN_GUIDE}") from e
        if int(sub_ret) != 0:
            self._safe_stop_locked(trader)
            raise ConnectionLostError(
                f"QMT 账户订阅失败（subscribe 返回 {sub_ret}）：请核对资金账号与客户端登录账号"
                f"（account={mask(self._account_id)}）")
        self._account = account

    @staticmethod
    def _new_session_id() -> int:
        """日期 + 随机后缀：同一进程内多次重连也必须各不相同（同 session 复用会连不上）。"""
        return int(f"{datetime.now():%Y%m%d}{random.randint(0, 9999):04d}")

    def _safe_stop_locked(self, trader) -> None:
        try:
            trader.stop()
        except Exception:  # noqa: BLE001 失败清理阶段的异常不再上抛
            log.debug("QMT 失败清理 trader.stop() 异常（忽略）", exc_info=True)

    def _reconnect(self) -> None:
        """守护线程的重连动作：旧会话彻底丢掉，再走一遍完整建会话流程。"""
        with self._lock:
            old, self._trader, self._account = self._trader, None, None
            self._connected = False
            if old is not None:
                try:
                    old.stop()
                except Exception:  # noqa: BLE001 旧会话已经死了，停不掉也无所谓
                    log.debug("QMT 旧会话 stop 异常（忽略）", exc_info=True)
            self._open_session_locked()
            self._connected = True

    def _after_reconnect(self) -> None:
        """宪法义务：重连成功后重放订阅（账户订阅）+ 刷新对账数据面。"""
        refreshed = ""
        try:
            account = self.get_account()
            positions = self.get_position()
            self._publish(EventType.ACCOUNT, account)
            self._publish(EventType.POSITION, positions)
            refreshed = f"（资金 {account.total_asset:.2f} / 持仓 {len(positions)} 笔）"
        except (AdapterError, ConnectionLostError) as e:
            log.warning("QMT 重连后刷新对账数据失败（回报订阅已重放，对账由 services 兜底）: %s", e)
        if self.reconcile_hook is not None:
            try:
                self.reconcile_hook(self)
            except Exception as e:  # noqa: BLE001 对账钩子异常不影响「已重连」事实
                log.warning("QMT 重连后对账钩子异常: %s", e)
        log.info("QMT 交易通道重连成功，已重放账户订阅: account=%s %s",
                 mask(self._account_id), refreshed)

    def _price_type_of(self, order: Order) -> int:
        """限价统一 FIX_PRICE；市价按**沪深差异**分支（深市没有沪市那一档等价语义）。"""
        if _sv(order.order_type) == OrderType.LIMIT.value:
            return _const_of(self._const, _LIMIT_PRICE_TYPE[0], _LIMIT_PRICE_TYPE[1])
        suffix = str(order.symbol).split(".")[-1].upper()
        names, fallback = _MARKET_PRICE_TYPE.get(suffix, _MARKET_PRICE_TYPE["SH"])
        return _const_of(self._const, names, fallback)

    # ================= 内部：归一化 =================
    def _to_order(self, row) -> Order:
        broker_id = str(_attr(row, "order_id", "order_sysid", default="") or "")
        remark = str(_attr(row, "order_remark", "remark", default="") or "")
        # 本地 order_id 的取法：① 回报里的 order_remark（我们自己写进去的锚）
        # ② 已学到的映射 ③ 兜底用 broker id（外部渠道下的单，如实标注无法关联本地）
        with self._lock:
            local_id = remark or self._broker_to_local.get(broker_id) or broker_id
            if remark and broker_id:
                self._broker_to_local.setdefault(broker_id, remark)
                self._local_to_broker.setdefault(remark, broker_id)
        raw_status = _attr(row, "order_status", "status", default=None)
        traded_volume = int(_attr(row, "traded_volume", "filled_volume", default=0) or 0)
        volume = int(_attr(row, "order_volume", "volume", default=0) or 0)
        price_type = _attr(row, "price_type", default=None)
        limit_const = _const_of(self._const, _LIMIT_PRICE_TYPE[0], _LIMIT_PRICE_TYPE[1])
        order_type = OrderType.LIMIT if (price_type is None or int(price_type) == limit_const) \
            else OrderType.MARKET
        code = str(_attr(row, "stock_code", "symbol", default="") or "")
        return Order(
            order_id=local_id,
            symbol=to_internal(code, "qmt") if code else code,
            side=Side.SELL if int(_attr(row, "order_type", "direction", default=0) or 0) == \
                _const_of(self._const, _SIDE_CONST[Side.SELL.value][0],
                          _SIDE_CONST[Side.SELL.value][1]) else Side.BUY,
            price=float(_attr(row, "price", default=0.0) or 0.0),
            volume=volume,
            order_type=order_type,
            strategy_id=str(_attr(row, "strategy_name", default="manual") or "manual"),
            status=map_order_status(raw_status, self._status_map),
            filled_volume=traded_volume,
            avg_filled_price=float(_attr(row, "traded_price", "avg_filled_price",
                                         default=0.0) or 0.0),
            message=str(_attr(row, "status_msg", "status_message", default="") or ""),
            broker_order_id=broker_id or None,
            created_at=_qmt_time_to_ms(_attr(row, "order_time", "created_at")),
            updated_at=now_ms())

    def _to_trade(self, row) -> Trade:
        broker_order_id = str(_attr(row, "order_id", default="") or "")
        remark = str(_attr(row, "order_remark", default="") or "")
        with self._lock:
            local_id = remark or self._broker_to_local.get(broker_order_id) or broker_order_id
        trade_id = str(_attr(row, "traded_id", "trade_id", default="") or "") or \
            f"qmt-{broker_order_id}-{now_ms()}"
        code = str(_attr(row, "stock_code", "symbol", default="") or "")
        price = float(_attr(row, "traded_price", "price", default=0.0) or 0.0)
        volume = int(_attr(row, "traded_volume", "volume", default=0) or 0)
        amount = float(_attr(row, "traded_amount", "amount", default=0.0) or 0.0) or \
            price * volume
        sell_const = _const_of(self._const, _SIDE_CONST[Side.SELL.value][0],
                               _SIDE_CONST[Side.SELL.value][1])
        return Trade(
            trade_id=trade_id,
            order_id=local_id,
            symbol=to_internal(code, "qmt") if code else code,
            side=Side.SELL if int(_attr(row, "order_type", "direction", default=0) or 0)
            == sell_const else Side.BUY,
            price=price, volume=volume, amount=amount,
            traded_at=_qmt_time_to_ms(_attr(row, "traded_time", "time")),
            broker_trade_id=trade_id)

    def _learn_mapping(self, order: Order) -> None:
        """把查询/回报里学到的 broker↔本地映射补齐（重启后撤单要靠它）。"""
        if not order.broker_order_id:
            return
        with self._lock:
            self._broker_to_local.setdefault(str(order.broker_order_id), order.order_id)
            if order.order_id:
                self._local_to_broker.setdefault(order.order_id, str(order.broker_order_id))

    # ================= 内部：回报工作线程 =================
    def _ensure_report_thread(self) -> None:
        if self._report_thread is not None and self._report_thread.is_alive():
            return
        self._stop_reports.clear()
        self._report_thread = threading.Thread(target=self._report_loop, name="qmt-report",
                                               daemon=True)
        self._report_thread.start()

    def _report_loop(self) -> None:
        while not self._stop_reports.is_set():
            try:
                item = self._report_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                return
            kind, payload = item
            try:
                self._handle_report(kind, payload)
            except Exception:  # noqa: BLE001 单条回报异常不拖垮工作线程
                log.exception("QMT 回报处理失败 kind=%s", kind)

    def _handle_report(self, kind: str, payload) -> None:
        if kind == "order":
            o = self._to_order(payload)
            self._publish(EventType.ORDER, o)
        elif kind == "trade":
            t = self._to_trade(payload)
            self._publish(EventType.TRADE, t)
        elif kind == "order_error":
            broker_id = str(_attr(payload, "order_id", default="") or "")
            msg = f"委托错误 {_attr(payload, 'error_id', default='')}:" \
                  f" {_attr(payload, 'error_msg', 'msg', default='')}"
            with self._lock:
                local_id = self._broker_to_local.get(broker_id) or broker_id
            log.warning("QMT 委托被拒: broker=%s local=%s %s", broker_id, local_id, msg)
            self._publish(EventType.ORDER, Order(
                order_id=local_id, symbol=str(_attr(payload, "stock_code", default="") or ""),
                side=Side.BUY, price=0.0, volume=0, status=OrderStatus.REJECTED,
                message=msg, broker_order_id=broker_id or None, updated_at=now_ms()))
        elif kind == "cancel_error":
            broker_id = str(_attr(payload, "order_id", default="") or "")
            with self._lock:
                local_id = self._broker_to_local.get(broker_id) or broker_id
            detail = f"撤单失败 {_attr(payload, 'error_id', default='')}:" \
                     f" {_attr(payload, 'error_msg', 'msg', default='')}"
            log.warning("QMT %s: broker=%s local=%s", detail, broker_id, local_id)
            self._publish(EventType.RISK, {"action": "cancel_rejected", "channel": self.name,
                                           "order_id": local_id, "detail": detail})

    # ================= 内部：基础设施 =================
    def _require_live(self) -> None:
        if not self._connected or self._trader is None:
            raise ConnectionLostError(
                f"QMT 交易通道未连接/已断线——fail-closed 拒单（不排队不重试）；{_LOGIN_GUIDE}")

    def _query(self, fn: Callable, what: str):
        """查询类调用：内部最多重试 1 次（读操作可安全重放）；最终失败抛 BrokerError。"""
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            log.warning("QMT 查询失败（%s），重试一次: %s", what, e)
            try:
                return fn()
            except Exception as e2:  # noqa: BLE001
                raise BrokerError(f"QMT 查询失败（{what}）: {e2.__class__.__name__}: {e2}；"
                                  f"{_LOGIN_GUIDE}") from e2

    def _publish(self, etype: EventType, data) -> None:
        if self.bus is not None:
            self.bus.publish(Event(etype, data=data, source=self.name))

    def _ensure_watchdog(self) -> None:
        if self.watchdog is not None:
            return
        s = get_settings()
        self.watchdog = ReconnectWatchdog(
            name=self.name, probe=self._probe, reconnect=self._reconnect,
            on_reconnected=self._after_reconnect, bus=self.bus,
            interval=s.watchdog_interval_s,
            backoff=Backoff(base=s.backoff_base_s, cap=s.backoff_cap_s),
            probe_timeout_note=f"QMT 交易通道探活失败：{_LOGIN_GUIDE}")


def _sv(v) -> str:
    """枚举/字符串统一取 value（与 paper_broker 同款小工具）。"""
    return v.value if hasattr(v, "value") else str(v)


ADAPTER = QmtBroker
