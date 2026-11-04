"""交易服务（D09）：下单/撤单/查询 API 的服务端编排。

- 一切下单意图经 engine/riskgate.submit()（订单唯一咽喉）；
- PaperBroker 持久化与未来实盘同表（orders/trades/positions_snapshot/account_state）；
- 模块级单例 get_trading_service()（API 层使用；测试可自建实例注入临时库）。
"""

from __future__ import annotations

import threading

from quantdesk.adapters.brokers.paper_broker import PaperBroker
from quantdesk.core.enums import OrderStatus, OrderType, Side
from quantdesk.core.events import SYSTEM_BUS, EventBus
from quantdesk.core.models import Account, Order, Position, Trade
from quantdesk.engine.riskgate import RiskGate
from quantdesk.store import repo

__all__ = ["TradingService", "get_trading_service"]


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
                             settings=settings, db_path=db_path, event_bus=self.bus)
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
    def place_order(self, symbol: str, side: str, volume: int,
                    price: float | None = None, order_type: str = "limit",
                    strategy_id: str = "manual") -> Order:
        self.start()
        intent = Order(
            order_id=self.gate.new_order_id(),
            symbol=symbol,
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
