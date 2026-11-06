"""交易服务（D09）：下单/撤单/查询 API 的服务端编排。

- 一切下单意图经 engine/riskgate.submit()（订单唯一咽喉）；
- PaperBroker 持久化与未来实盘同表（orders/trades/positions_snapshot/account_state）；
- 模块级单例 get_trading_service()（API 层使用；测试可自建实例注入临时库）。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from quantdesk.adapters.brokers.paper_broker import PaperBroker
from quantdesk.core.enums import OrderStatus, OrderType, Side
from quantdesk.core.events import SYSTEM_BUS, EventBus
from quantdesk.core.models import Account, Order, Position, Tick, Trade
from quantdesk.core.symbols import normalize
from quantdesk.core.timeutil import now_ms
from quantdesk.engine.riskgate import RiskGate
from quantdesk.store import repo

log = logging.getLogger(__name__)

__all__ = ["TradingService", "get_trading_service"]

_QUOTE_FRESH_S = 60.0    # 手上这份行情多久之内还够用（超过就当没有，当场补一次）


def _age_seconds(dt: datetime) -> float:
    """快照距今多少秒。测试里的 tick 常是 naive 时间，按本机时区补齐再比。"""
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return max((datetime.now().astimezone() - dt).total_seconds(), 0.0)
    except Exception:                         # noqa: BLE001 判不出年龄就当还新，不为难用户多打一次接口
        return 0.0


class TradingService:
    """下单编排 + 查询编排。

    通道装配交给 ChannelHub：模拟盘固定走 paper，实盘按 settings.broker_channel
    动态解析（未安装/未启用/缺凭据/连不上 → live_broker() 返回 None → RiskGate
    fail-closed 拒单）。查询侧同理：实盘时优先问券商，问不到再回落本地库。
    """

    def __init__(self, initial_cash: float = 1_000_000.0, db_path=None,
                 settings=None, bus: EventBus | None = None,
                 hub=None) -> None:
        self.bus = bus or SYSTEM_BUS          # 默认接进程级枢纽 → /ws/events 下行
        self.hub = hub if hub is not None else _make_hub(settings, self.bus, db_path)
        self.paper = PaperBroker(initial_cash=initial_cash, db_path=db_path,
                                 event_bus=self.bus)
        self.gate = RiskGate(paper_broker=self.paper, live_broker=None,
                             live_provider=self.hub.live_broker if self.hub else None,
                             settings=settings, db_path=db_path, event_bus=self.bus,
                             quote_provider=self._quote_row)
        self._started = False
        self._lock = threading.RLock()

    # ---------- 通道 ----------
    def _live(self):
        return self.hub.live_broker() if self.hub is not None else None

    def _active_broker(self):
        """当前该用哪个通道做查询：模拟盘 paper，实盘优先券商、券商不可用回落 paper。"""
        if self.gate._sim_only():
            return self.paper
        return self._live() or self.paper

    # ---------- 生命周期 ----------
    def start(self) -> None:
        with self._lock:
            if not self._started:
                self.paper.connect()
                self.gate.restore_pending()       # D17：重启后待确认单重载
                self._started = True

    def stop(self) -> None:
        with self._lock:
            if self._started:
                self.paper.disconnect()
                if self.hub is not None:
                    self.hub.stop()
                self._started = False

    # ---------- 二次确认（D17，实盘路径） ----------
    def confirm_order(self, order_id: str) -> Order:
        self.start()
        return self.gate.confirm(order_id)

    def reject_order(self, order_id: str) -> Order:
        self.start()
        return self.gate.reject(order_id)

    # ---------- 下单/撤单 ----------
    def _quote_row(self, symbol: str) -> dict:
        """下单/校验要用的行情快照（昨收、交易所涨跌停价、现价、简称）。

        走只读盯盘服务：它自带 TTL 缓存与逐标的取数，不重复打公开接口。
        取不到就回空 dict，RiskGate 自己按「无从判定」处理（模拟盘放行 + LOG，实盘 fail-closed）。
        """
        from quantdesk.services.watch import get_watch_service

        try:
            return get_watch_service().live_row(symbol) or {}
        except Exception as e:                         # noqa: BLE001 行情不可用不应让下单崩掉
            log.info("取行情快照失败 %s: %s", symbol, e)
            return {}

    def _ensure_quote(self, symbol: str) -> None:
        """保证模拟盘手上有这只标的的可用行情，然后委托才可能当场撮合。

        实时泵只推它订阅过的标的，休市时更不推。过去这只票不在订阅里，限价单就会
        一直挂在簿上不动——用户体感是「这只买不了」，其实是没人喂行情。手动下单是
        用户主动动作（宪法第 11 条③里允许外呼的那一类），当场取一次快照喂进去。
        """
        tick = self.paper.latest_tick(symbol)
        if tick is not None and _age_seconds(tick.dt) < _QUOTE_FRESH_S:
            return
        row = self._quote_row(symbol)
        last = float(row.get("last") or 0)
        if last <= 0:
            return
        ts = int(row.get("ts_ms") or now_ms())
        self.paper.on_tick(Tick(
            symbol=symbol, dt=datetime.fromtimestamp(ts / 1000).astimezone(),
            last=last, open=float(row.get("open") or 0), high=float(row.get("high") or 0),
            low=float(row.get("low") or 0), volume=float(row.get("volume") or 0),
            amount=float(row.get("amount") or 0),
            bids=[tuple(x) for x in (row.get("bids") or [])],
            asks=[tuple(x) for x in (row.get("asks") or [])]))

    def place_order(self, symbol: str, side: str, volume: int,
                    price: float | None = None, order_type: str = "limit",
                    strategy_id: str = "manual") -> Order:
        self.start()
        sym = normalize(symbol)
        self._ensure_quote(sym)
        intent = Order(
            order_id=self.gate.new_order_id(),
            symbol=sym,
            side=Side(side),
            price=float(price) if price is not None else 0.0,
            volume=int(volume),
            order_type=OrderType(order_type),
            strategy_id=strategy_id,
            status=OrderStatus.PENDING_CONFIRM,   # 进入闸门前的初始态
        )
        return self.gate.submit(intent)

    def cancel_order(self, order_id: str) -> bool:
        self.start()
        broker = self._active_broker()
        return bool(broker.cancel_order(order_id))

    def order_rules(self, symbol: str) -> dict:
        """下单框要显示的那张「规则卡」：板块/币种/最小委托量/价格区间/能不能买。

        判据一份都不写在这里——全部由 RiskGate 出，与真正执行的那套同源。
        """
        self.start()
        return self.gate.rule_card(symbol)

    # ---------- 查询 ----------
    def get_order(self, order_id: str) -> Order | None:
        self.start()
        # 按主键直查（get_orders 有 1000 条截断，列表查找会漏旧单）
        return repo.get_order(order_id, db_path=self.paper.db_path)

    def list_orders(self, status: str | None = None) -> list[Order]:
        self.start()
        st = OrderStatus(status) if status else None
        return self._active_broker().get_orders(status=st)

    def positions(self) -> list[Position]:
        self.start()
        return self._active_broker().get_position()

    def account(self) -> Account:
        self.start()
        return self._active_broker().get_account()

    def trades(self, limit: int = 500) -> list[Trade]:
        self.start()
        ts = self._active_broker().get_trades()
        return ts[-limit:]



_service: TradingService | None = None
_service_lock = threading.Lock()


def _make_hub(settings, bus, db_path):
    """独立实例注入用（测试传 settings 对象）；失败不阻断服务构造。"""
    from quantdesk.services.channels import ChannelHub

    return ChannelHub(settings=settings, bus=bus, db_path=db_path)


def get_trading_service() -> TradingService:
    """模块级单例（真实库 + 全局通道中枢，与 /api/channels 共用同一批连接）。"""
    global _service
    with _service_lock:
        if _service is None:
            from quantdesk.services.channels import get_hub

            hub = get_hub()
            _service = TradingService(hub=hub)
            _service.start()
        return _service
