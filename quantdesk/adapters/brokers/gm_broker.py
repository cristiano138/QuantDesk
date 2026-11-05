"""掘金量化（gm）交易通道适配器——宪法定位的「备选实盘通道」。

线程模型（关键，掘金 Python 交易接口必须在其终端线程内调用，跨线程直接调会崩）：
- connect() 起一个**专用终端线程**：该线程内 set_token → 构造 Terminal/Context →
  订阅低活跃标的维持事件循环 → 保持会话；
- 所有下单/撤单/查询请求经 queue.Queue 投递进终端线程执行，调用方带超时等待结果
  （终端线程卡住 → 超时抛 ConnectionLostError，绝不永久阻塞）；
- SDK 回报回调（掘金推送线程）只把原始回报入队，**另一个工作线程**归一化后发布
  EventBus——回调线程永不阻塞、永不直接广播（宪法线程纪律）。

版本差异处理（掘金新旧接口不同）：
- 旧：create_context() + context.rest.api.order_*/get_*；
- 新：Terminal + terminal.order_*/get_*；
- 本适配器用 _api_surface() 一处探测（getattr 判断对象能力），把差异封在适配层，
  探测失败抛结构化 AdapterError 带「请按接入卡核对接口名」的引导，绝不静默用错接口。
  具体 SDK 方法名/参数顺序/回报字段需在线核对，见 docs/integration/gm.md。

适配器合同（宪法，全程遵守）：只执行不风控（收到已确认 Order）；提交失败抛 BrokerError
禁止静默；断线抛 ConnectionLostError 且可被 health_check 探测；重连成功刷新对账；
下单价永远是未复权原始市场价；token 只进本地加密配置、日志脱敏前 4 位 + ***。
适配器不落库（落库在 services 层），只做归一化与回报广播。
"""

from __future__ import annotations

import dataclasses
import logging
import queue
import threading
import uuid
from collections.abc import Callable
from datetime import datetime

from quantdesk.adapters.base.broker import BrokerAdapter
from quantdesk.adapters.base.optional import import_optional
from quantdesk.core.enums import OrderStatus, OrderType, Side
from quantdesk.core.errors import AdapterError, BrokerError, ConnectionLostError
from quantdesk.core.events import Event, EventType, EventBus
from quantdesk.core.models import Account, Order, Position, Trade
from quantdesk.core.symbols import from_internal, to_internal
from quantdesk.core.timeutil import now_ms

log = logging.getLogger(__name__)

__all__ = ["GmBroker"]

_INSTALL_HINT = "需要掘金行情与交易组件（随掘金终端提供）；请先安装并登录掘金终端"

# ---------- 参数映射表（置顶；真实取值/参数顺序需在线核对，集中一处便于修改） ----------
# 委托方向 / 持仓方向 / 委托类型（掘金常量，getattr 优先取 SDK，缺失回退下列默认）
_CONST_DEFAULTS = {
    "ORDER_BUY": 2, "ORDER_SELL": 4,
    "POSITION_LONG": 1, "POSITION_SHORT": 2,
    "ORDER_TYPE_LIMIT": "limit", "ORDER_TYPE_MARKET": "market",
    "PRICE_TYPE_LIMIT": "limit", "PRICE_TYPE_LATEST": "latest",
    "ORDER_DURATION_DAY": "day",
}
_SIDE_CONST = {"buy": "ORDER_BUY", "sell": "ORDER_SELL"}
_OTYPE_CONST = {"limit": "ORDER_TYPE_LIMIT", "market": "ORDER_TYPE_MARKET"}
_PTYPE_CONST = {"limit": "PRICE_TYPE_LIMIT", "market": "PRICE_TYPE_LATEST"}

# ---------- 订单状态映射（置顶；未识别值保守处理 + log.warning） ----------
# 掘金 order_status 常见取值（数字与字符串都覆盖，真实枚举名需在线核对）
_ORDER_STATUS_MAP = {
    1: OrderStatus.SUBMITTED, 2: OrderStatus.SUBMITTED, 8: OrderStatus.SUBMITTED,
    12: OrderStatus.SUBMITTED, 9: OrderStatus.SUBMITTED,
    6: OrderStatus.PART_FILLED,
    7: OrderStatus.FILLED,
    3: OrderStatus.CANCELLED, 4: OrderStatus.CANCELLED, 10: OrderStatus.CANCELLED,
    13: OrderStatus.CANCELLED,
    5: OrderStatus.REJECTED, 11: OrderStatus.FAILED,
    "new": OrderStatus.SUBMITTED, "accepted": OrderStatus.SUBMITTED,
    "live": OrderStatus.SUBMITTED, "pending_new": OrderStatus.SUBMITTED,
    "pending": OrderStatus.SUBMITTED, "accepted_by_counter": OrderStatus.SUBMITTED,
    "replaced": OrderStatus.SUBMITTED,
    "part_filled": OrderStatus.PART_FILLED, "partially_filled": OrderStatus.PART_FILLED,
    "filled": OrderStatus.FILLED, "suspended": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED, "cancelled": OrderStatus.CANCELLED,
    "expired": OrderStatus.CANCELLED, "canceled_pending": OrderStatus.CANCELLED,
    "stopped": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED, "unknown": OrderStatus.FAILED,
}


def _s(v) -> str:
    return v.value if isinstance(v, (Side, OrderType)) else str(v)


def _mask(secret: str | None) -> str:
    """脱敏：仅前 4 位 + ***（宪法日志脱敏）。"""
    if not secret:
        return "<empty>"
    return secret[:4] + "***" if len(secret) > 4 else "****"


class _Task:
    """投递到终端线程执行的一次调用（结果/异常经 event 回传给调用方）。"""

    __slots__ = ("fn", "event", "result", "exc")

    def __init__(self, fn: Callable[[], object]) -> None:
        self.fn = fn
        self.event = threading.Event()
        self.result = None
        self.exc: BaseException | None = None


class GmBroker(BrokerAdapter):
    """掘金终端通道：终端线程串行执行 SDK 调用，回报经工作线程归一化广播。"""

    name = "gm"
    label = "掘金量化终端"
    prereq = "安装并登录掘金终端 + 绑定券商（或仿真账户）"
    official = True
    is_paper = False
    requires = ("gm",)

    def __init__(self, event_bus: EventBus | None = None,
                 request_timeout: float = 5.0, health_timeout: float = 2.0,
                 heartbeat_symbol: str = "SHSE.600000") -> None:
        self.bus = event_bus
        self.request_timeout = float(request_timeout)
        self.health_timeout = float(health_timeout)
        self.heartbeat_symbol = heartbeat_symbol

        # 凭据（connect 时填充；token 绝不进日志）
        self.token: str = ""
        self.account_id: str = ""
        self.endpoint: str = ""     # 仿真/实盘切换参数，绝不写死
        self.mode: str = ""

        self._sdk = None            # gm.api 模块（惰性导入）
        self._session = None        # Terminal 或 Context（回调挂载对象）
        self._ns = None             # 实际持有 order_*/get_* 的命名空间（新=session/旧=session.rest.api）
        self._surface = ""          # "terminal" | "context"

        self._req_q: queue.Queue[_Task | None] = queue.Queue()
        self._report_q: queue.Queue[tuple | None] = queue.Queue()
        self._term_thread: threading.Thread | None = None
        self._report_thread: threading.Thread | None = None
        self._connected = False
        self._lost = False
        self._closing = False

        # 关联锚映射：掘金 order_id ↔ 本地 order_id（回报/撤单据此关联，非锚点，见文档）
        self._anchor: dict[str, str] = {}
        self._orders: dict[str, Order] = {}       # 本地 order_id → 最新快照
        self._execs: list[Trade] = []             # 成交回报缓存（get_trades 用）
        self._positions: list[Position] = []
        self._account: Account | None = None
        self._lock = threading.RLock()

    # ================= 生命周期 =================
    def connect(self, **credentials: str) -> None:
        if self._connected:
            return
        token = str(credentials.get("token", "")).strip()
        if not token:
            raise BrokerError("gm connect 需要 token（掘金终端令牌）")
        self.token = token
        self.account_id = str(credentials.get("account_id", "")).strip()
        self.endpoint = str(credentials.get("endpoint", "")).strip()   # 不写死：由参数决定仿真/实盘
        self.mode = str(credentials.get("mode", "")).strip()
        if not self.endpoint:
            raise BrokerError("gm connect 需要 endpoint（仿真/实盘终端地址，由配置注入不写死）")

        self._sdk = import_optional("gm.api", channel="掘金", install_hint=_INSTALL_HINT)
        self._closing = False
        self._start_threads()
        try:
            self._submit(self._init_session, self.request_timeout)      # 终端线程内建会话
        except ConnectionLostError as e:
            self._stop_threads()
            raise BrokerError(f"掘金终端会话建立失败: {e}") from e
        self._connected = True
        self._lost = False
        # token 脱敏，绝不打全文（宪法第 7 条）
        log.info("gm 通道已连接: account=%s endpoint=%s token=%s",
                 self.account_id or "<none>", self.endpoint, _mask(self.token))

    def disconnect(self) -> None:
        self._connected = False
        self._closing = True
        if self._session is not None and self._term_thread is not None:
            try:
                self._submit(self._teardown_session, self.request_timeout)
            except Exception:  # noqa: BLE001 主动断开会话失败不阻塞关闭
                log.warning("gm 会话关闭调用失败（忽略，继续停线程）", exc_info=True)
        self._stop_threads()
        self._session = None
        self._ns = None

    def health_check(self) -> bool:
        """终端线程存活 + 一次轻量查询；断线可被探测（返回 False，不抛）。"""
        if not self._connected or self._lost:
            return False
        t = self._term_thread
        if t is None or not t.is_alive():
            self._lost = True
            return False
        try:
            self._submit(lambda: self._ns.get_account_snapshot(account_id=self.account_id),
                         self.health_timeout)
            return True
        except ConnectionLostError:
            self._lost = True
            return False
        except Exception as e:  # noqa: BLE001 探活吞异常返回 False（可探测语义）
            log.warning("gm health_check 查询失败: %s", e)
            return False

    def reconnect(self) -> None:
        """重建终端会话 + 履行宪法义务：重连成功后刷新对账快照（重放订阅在 _init_session 内）。"""
        if not self._connected:
            raise ConnectionLostError("gm 未 connect，无法 reconnect")
        self._lost = False
        self._submit(self._init_session, self.request_timeout)
        # 刷新对账：全量拉取资金/持仓/委托（差异比对归 services，此处只提供最新快照）
        self.refresh_reconcile()
        log.info("gm 通道重连成功，已刷新对账快照: account=%s", _mask(self.token))

    def refresh_reconcile(self) -> None:
        """主动拉取一遍账户/持仓/委托，刷新适配器缓存（重连义务的可测入口）。"""
        self.get_account()
        self.get_position()
        self.get_orders()

    def snapshot_for_reconcile(self) -> tuple[list[Order], list[Trade],
                                             list[Position], Account | None]:
        """供对账服务比对：(orders, trades, positions, account)。对账本身不归适配器。"""
        with self._lock:
            orders = [dataclasses.replace(o) for o in self._orders.values()]
            trades = list(self._execs)
            positions = list(self._positions)
            account = dataclasses.replace(self._account) if self._account else None
        return orders, trades, positions, account

    # ================= 交易 =================
    def place_order(self, order: Order) -> Order:
        self._require_live()
        gm_symbol = from_internal(order.symbol, "gm")
        side_c = self._c(_SIDE_CONST[_s(order.side)])
        pos_c = self._c("POSITION_LONG")
        otype_c = self._c(_OTYPE_CONST[_s(order.order_type)])
        ptype_c = self._c(_PTYPE_CONST[_s(order.order_type)])
        price = float(order.price) if _s(order.order_type) == "limit" else 0.0
        # 关联锚：SDK 支持 order_remark 时带本地 order_id；无锚则靠 _anchor 映射（见文档）
        anchor = uuid.uuid4().hex[:16]

        def _do():
            return self._ns.order_stock(
                account_id=self.account_id, symbol=gm_symbol,
                side=side_c, position=pos_c, order_type=otype_c,
                quantity=int(order.volume), price_type=ptype_c, price=price,
                order_duration=self._c("ORDER_DURATION_DAY"), order_remark=anchor)

        resp = self._submit(_do, self.request_timeout)
        if not isinstance(resp, dict):
            raise BrokerError(f"gm 下单返回非预期结构: {resp!r}")
        code = resp.get("status_code")
        msg = resp.get("status_msg") or resp.get("errmsg") or ""
        if code not in (0, "0", None):
            # 非 0 返回码 = 提交失败，禁止静默，抛 BrokerError 带码与 errmsg
            raise BrokerError(f"gm 下单失败 code={code} msg={msg}")
        data = resp.get("data") or {}
        broker_id = str(data.get("order_id") or data.get("client_order_id") or anchor)
        placed = dataclasses.replace(
            order, broker_order_id=broker_id, status=OrderStatus.SUBMITTED,
            message=msg or "已提交掘金", updated_at=now_ms())
        with self._lock:
            self._anchor[broker_id] = placed.order_id
            self._anchor[anchor] = placed.order_id
            self._orders[placed.order_id] = placed
        self._publish(EventType.ORDER, placed)
        return placed

    def cancel_order(self, order_id: str) -> bool:
        """发起撤单：返回是否受理；最终状态以回报为准（掘金回报会转 CANCELLED）。"""
        self._require_live()
        with self._lock:
            o = self._orders.get(order_id)
        broker_id = (o.broker_order_id if o and o.broker_order_id else order_id)

        def _do():
            return self._ns.cancel_order(order_id=broker_id, account_id=self.account_id)

        try:
            resp = self._submit(_do, self.request_timeout)
        except ConnectionLostError:
            raise
        except BrokerError as e:
            # 撤单被业务拒绝（如订单已成交）——如实报告「未受理」，非断线不上抛
            log.warning("gm 撤单未受理 order_id=%s: %s", order_id, e)
            return False
        code = resp.get("status_code") if isinstance(resp, dict) else None
        accepted = code in (0, "0", None)
        if not accepted:
            log.warning("gm 撤单未受理 order_id=%s code=%s msg=%s",
                        order_id, code, (resp or {}).get("status_msg", ""))
        return bool(accepted)

    def poll(self) -> None:
        """回调型通道：掘金有推送能力，无需轮询（宪法注明空实现）。"""

    # ================= 查询（终端线程取原始数据，调用方线程归一化） =================
    def get_account(self) -> Account:
        self._require_live()
        raw = self._submit(lambda: self._ns.get_account_snapshot(account_id=self.account_id),
                           self.request_timeout)
        row = _first_dict(raw)
        acc = Account(
            total_asset=float(row.get("balance", row.get("total_asset", 0.0))),
            cash=float(row.get("available", row.get("cash", 0.0))),
            market_value=float(row.get("market_value", 0.0)),
            frozen=float(row.get("frozen", 0.0)),
            updated_at=now_ms())
        with self._lock:
            self._account = acc
        return acc

    def get_position(self, symbol: str | None = None) -> list[Position]:
        self._require_live()
        raw = self._submit(lambda: self._ns.get_position(account_id=self.account_id),
                           self.request_timeout)
        ps = [self._to_position(r) for r in _iter_dicts(raw)]
        if symbol is not None:
            ps = [p for p in ps if p.symbol == symbol]
        with self._lock:
            self._positions = ps
        return ps

    def get_orders(self, status: OrderStatus | None = None) -> list[Order]:
        self._require_live()
        raw = self._submit(lambda: self._ns.get_orders(account_id=self.account_id),
                           self.request_timeout)
        out = [self._to_order(r) for r in _iter_dicts(raw)]
        with self._lock:
            for o in out:
                self._anchor.setdefault(str(o.broker_order_id or o.order_id), o.order_id)
                self._orders[o.order_id] = o
        if status is None:
            return out
        return [o for o in out if _so(o.status) == _so(status)]

    def get_trades(self, start: datetime | None = None) -> list[Trade]:
        self._require_live()
        raw = self._submit(lambda: self._ns.get_execution_reports(account_id=self.account_id),
                           self.request_timeout)
        out = [self._to_trade(r) for r in _iter_dicts(raw)]
        start_ms = int(start.timestamp() * 1000) if start else None
        with self._lock:
            for t in out:
                if not any(e.trade_id == t.trade_id for e in self._execs):
                    self._execs.append(t)
        if start_ms is None:
            return out
        return [t for t in out if t.traded_at >= start_ms]

    # ================= SDK 能力探测（版本差异封在此处） =================
    def _api_surface(self) -> str:
        """探测掘金 SDK 接口面：新=Terminal / 旧=create_context。不受支持抛结构化错误。"""
        sdk = self._sdk
        if hasattr(sdk, "Terminal"):
            return "terminal"
        if hasattr(sdk, "create_context"):
            return "context"
        raise AdapterError(
            "掘金 SDK 版本不受支持：既未提供 Terminal（新接口）也未提供 create_context（旧接口）。"
            "请按接入卡 docs/integration/gm.md 核对接口名并升级/回退 SDK 版本。")

    # ================= 终端线程内部（以下函数只在终端线程执行） =================
    def _init_session(self) -> None:
        sdk = self._sdk
        self._sdk.set_token(self.token)
        surface = self._api_surface()
        self._surface = surface
        if surface == "terminal":
            session = sdk.Terminal(token=self.token, account_id=self.account_id,
                                   endpoint=self.endpoint, mode=self.mode)
            self._session = session
            self._ns = session
        else:  # context（旧接口：context.rest.api.order_*/get_*）
            ctx = sdk.create_context()
            self._session = ctx
            rest = getattr(ctx, "rest", None)
            api = getattr(rest, "api", None) if rest is not None else None
            if api is None:
                raise AdapterError(
                    "掘金旧接口 create_context() 返回对象缺少 rest.api 命名空间，"
                    "接口名与本项目探测策略不符，请按 docs/integration/gm.md 核对。")
            self._ns = api
        # 挂载回报回调（掘金推送线程调用；回调只入队，见下方 _on_* 方法）
        self._session.on_execution_report = self._on_execution_report
        self._session.on_order_status = self._on_order_status
        self._session.on_disconnected = self._on_disconnected
        # 订阅低活跃标的维持事件循环/心跳（订阅失败按接口差异容错，见文档）
        sub = getattr(self._session, "subscribe_quote", None)
        if callable(sub):
            try:
                sub([self.heartbeat_symbol])
            except Exception as e:  # noqa: BLE001 心跳订阅失败不致命，但要留痕
                log.warning("gm 心跳订阅失败（不影响下单，事件循环可能不活跃）: %s", e)

    def _teardown_session(self) -> None:
        for name in ("on_execution_report", "on_order_status", "on_disconnected"):
            try:
                setattr(self._session, name, None)
            except Exception:  # noqa: BLE001
                pass
        unsub = getattr(self._session, "unsubscribe_quote", None)
        if callable(unsub):
            try:
                unsub([self.heartbeat_symbol])
            except Exception:  # noqa: BLE001
                pass

    def _c(self, const_name: str):
        """取掘金常量：SDK 优先，缺失回退适配器默认（真实取值需在线核对）。"""
        v = getattr(self._sdk, const_name, None) if self._sdk is not None else None
        return v if v is not None else _CONST_DEFAULTS[const_name]

    # ================= 回报回调（掘金 SDK 线程：只入队，绝不广播/阻塞） =================
    def _on_execution_report(self, report) -> None:
        self._report_q.put(("exec", report))

    def _on_order_status(self, status) -> None:
        self._report_q.put(("order", status))

    def _on_disconnected(self, reason=None) -> None:
        # 断线信号只标记 + 入队；真正的重连/对账由 services/watchdog 驱动
        self._lost = True
        self._report_q.put(("disc", reason))

    # ================= 工作线程：归一化 + 广播 =================
    def _report_loop(self) -> None:
        while True:
            try:
                item = self._report_q.get(timeout=0.2)
            except queue.Empty:
                if self._closing:
                    return
                continue
            if item is None:
                return
            kind, payload = item
            try:
                self._handle_report(kind, payload)
            except Exception as e:  # noqa: BLE001 归一化失败留痕，不拖垮工作线程
                log.exception("gm 回报归一化失败 kind=%s: %s", kind, e)

    def _handle_report(self, kind: str, payload) -> None:
        if kind == "exec":
            trade = self._to_trade(payload if isinstance(payload, dict) else _asdict(payload))
            with self._lock:
                self._execs.append(trade)
            self._publish(EventType.TRADE, trade)
        elif kind == "order":
            order = self._to_order(payload if isinstance(payload, dict) else _asdict(payload))
            with self._lock:
                self._orders[order.order_id] = order
            self._publish(EventType.ORDER, order)
        elif kind == "disc":
            self._publish(EventType.RISK, {"action": "channel_down", "channel": self.name,
                                           "detail": str(payload or "掘金终端连接断开")})

    # ================= 归一化（SDK dict/对象 → 核心模型） =================
    def _to_order(self, row: dict) -> Order:
        broker_id = str(row.get("order_id") or row.get("cl_order_id") or "")
        with self._lock:
            local_id = self._anchor.get(broker_id) or self._anchor.get(
                str(row.get("order_remark") or "")) or broker_id
        raw_status = row.get("status", row.get("order_status"))
        status = self._map_status(raw_status)
        return Order(
            order_id=local_id, symbol=to_internal(str(row.get("symbol", "")), "gm"),
            side=Side.SELL if _s_num(row.get("side")) == 4 else Side.BUY,
            price=float(row.get("price", 0.0) or 0.0),
            volume=int(row.get("volume", row.get("quantity", 0)) or 0),
            order_type=OrderType.MARKET if str(row.get("order_type")) in ("market", "3", "5") else OrderType.LIMIT,
            strategy_id=str(row.get("strategy_id", "manual")),
            status=status,
            filled_volume=int(row.get("filled_volume", row.get("traded_volume", 0)) or 0),
            avg_filled_price=float(row.get("avg_px", row.get("vwap", 0.0)) or 0.0),
            message=str(row.get("status_msg", row.get("msg", "")) or ""),
            broker_order_id=broker_id or None,
            created_at=_ms(row.get("created_at", row.get("timestamp"))),
            updated_at=now_ms())

    def _to_trade(self, row: dict) -> Trade:
        broker_id = str(row.get("order_id") or "")
        with self._lock:
            local_id = self._anchor.get(broker_id) or broker_id
            exec_id = str(row.get("exec_id") or row.get("trade_id") or uuid.uuid4().hex)
            dup = any(e.trade_id == exec_id for e in self._execs)
        return Trade(
            trade_id=exec_id, order_id=local_id,
            symbol=to_internal(str(row.get("symbol", "")), "gm"),
            side=Side.SELL if _s_num(row.get("side")) == 4 else Side.BUY,
            price=float(row.get("price", 0.0) or 0.0),
            volume=int(row.get("volume", row.get("filled_volume", 0)) or 0),
            amount=float(row.get("amount", 0.0) or 0.0),
            traded_at=_ms(row.get("timestamp", row.get("transact_time"))),
            broker_trade_id=exec_id if not dup else exec_id)

    def _to_position(self, row: dict) -> Position:
        # T+1：available 取掘金可用字段 can_use_volume（当日买入不可用）
        volume = int(row.get("volume", row.get("quantity", 0)) or 0)
        available = int(row.get("can_use_volume", row.get("available", volume)) or 0)
        avg_cost = float(row.get("vwap", row.get("open_price", row.get("avg_cost", 0.0))) or 0.0)
        market_value = float(row.get("market_value", 0.0) or 0.0)
        return Position(
            symbol=to_internal(str(row.get("symbol", "")), "gm"),
            volume=volume, available=available, avg_cost=avg_cost,
            market_value=market_value,
            strategy_id=str(row.get("strategy_id", "manual")))

    def _map_status(self, raw) -> OrderStatus:
        if isinstance(raw, OrderStatus):
            return raw
        key: object
        if isinstance(raw, str) and raw.isdigit():
            key = int(raw)
        else:
            key = raw.lower().strip() if isinstance(raw, str) else raw
        mapped = _ORDER_STATUS_MAP.get(key)
        if mapped is None:
            log.warning("gm 未识别订单状态 %r——保守按 SUBMITTED 处理（请核对状态映射表）", raw)
            return OrderStatus.SUBMITTED
        return mapped

    # ================= 线程底座 =================
    def _start_threads(self) -> None:
        self._term_thread = threading.Thread(target=self._terminal_loop,
                                             name="gm-terminal", daemon=True)
        self._term_thread.start()
        self._report_thread = threading.Thread(target=self._report_loop,
                                               name="gm-report", daemon=True)
        self._report_thread.start()

    def _stop_threads(self) -> None:
        self._closing = True
        self._req_q.put(None)
        self._report_q.put(None)
        for t in (self._term_thread, self._report_thread):
            if t is not None and t.is_alive() and t is not threading.current_thread():
                t.join(timeout=2.0)

    def _terminal_loop(self) -> None:
        """终端线程：SDK 全部调用在此串行执行（掘金接口不能跨线程调用）。"""
        while True:
            task = self._req_q.get()
            if task is None:
                return
            try:
                task.result = task.fn()
            except BaseException as e:  # noqa: BLE001 捕获后回传调用方，线程不退出
                task.exc = e
            finally:
                task.event.set()

    def _submit(self, fn: Callable[[], object], timeout: float):
        """把调用投递到终端线程并带超时等待结果（卡住则超时抛错，绝不永久阻塞）。"""
        if self._term_thread is None or not self._term_thread.is_alive():
            self._lost = True
            raise ConnectionLostError("gm 终端线程未存活——通道不可用（fail-closed）")
        task = _Task(fn)
        self._req_q.put(task)
        if not task.event.wait(timeout):
            self._lost = True
            raise ConnectionLostError(f"gm 终端线程响应超时（{timeout}s）——按断线处理")
        if task.exc is not None:
            exc = task.exc
            if isinstance(exc, (ConnectionLostError, AdapterError)):
                raise exc
            if self._lost:
                raise ConnectionLostError(f"gm 调用失败（通道已断线）: {exc}") from exc
            raise BrokerError(f"gm 调用异常: {exc.__class__.__name__}: {exc}") from exc
        return task.result

    def _require_live(self) -> None:
        if not self._connected:
            raise ConnectionLostError("gm 通道未连接")
        if self._lost:
            raise ConnectionLostError("gm 通道断线——fail-closed（等待重连后刷新对账）")

    def _publish(self, etype: EventType, data) -> None:
        if self.bus is not None:
            self.bus.publish(Event(etype, data=data, source=self.name))


# ================= 归一化辅助（纯函数，便于单测） =================
def _so(v) -> str:
    return v.value if isinstance(v, OrderStatus) else str(v)


def _s_num(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        s = str(v).lower()
        return 4 if "sell" in s else 2


def _asdict(obj) -> dict:
    if isinstance(obj, dict):
        return obj
    d = getattr(obj, "__dict__", None)
    return dict(d) if isinstance(d, dict) else {"value": obj}


def _ms(v) -> int:
    if v is None or v == "":
        return now_ms()
    try:
        n = float(v)
    except (TypeError, ValueError):
        return now_ms()
    # 掘金常见为秒或毫秒或字符串时间——按数量级归一到毫秒
    return int(n) if n > 1e11 else int(n * 1000) if n > 1e9 else int(n * 1000)


def _first_dict(raw) -> dict:
    for r in _iter_dicts(raw):
        return r
    return {}


def _iter_dicts(raw):
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [_asdict(x) for x in raw]
    # SDK 返回对象带 list 属性（如 account 列表）
    lst = getattr(raw, "list", None)
    if isinstance(lst, (list, tuple)):
        return [_asdict(x) for x in lst]
    return [_asdict(raw)]


ADAPTER = GmBroker
