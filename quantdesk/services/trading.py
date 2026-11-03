"""交易服务（D09）：下单/撤单/查询 API 的服务端编排。

- 一切下单意图经 engine/riskgate.submit()（订单唯一咽喉）；
- PaperBroker 持久化与未来实盘同表（orders/trades/positions_snapshot/account_state）；
- 模块级单例 get_trading_service()（API 层使用；测试可自建实例注入临时库）。
"""

from __future__ import annotations

import threading

from quantdesk.adapters.brokers.paper_broker import PaperBroker
from quantdesk.core.enums import OrderStatus, OrderType, Side
from quantdesk.core.events import EventBus
from quantdesk.core.models import Account, Order, Position, Trade
from quantdesk.engine.riskgate import RiskGate

__all__ = ["TradingService", "get_trading_service"]


class TradingService:
    def __init__(self, initial_cash: float = 1_000_000.0, db_path=None,
                 settings=None) -> None:
        self.bus = EventBus()
        self.paper = PaperBroker(initial_cash=initial_cash, db_path=db_path,
                                 event_bus=self.bus)
        self.gate = RiskGate(paper_broker=self.paper, live_broker=None,
                             settings=settings, db_path=db_path, event_bus=self.bus)
        self._started = False
        self._lock = threading.RLock()

    # ---------- 生命周期 ----------
    def start(self) -> None:
        with self._lock:
            if not self._started:
                self.paper.connect()
                self._started = True

    def stop(self) -> None:
        with self._lock:
            if self._started:
                self.paper.disconnect()
                self._started = False

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
        return self.paper.cancel_order(order_id)

    # ---------- 查询 ----------
    def get_order(self, order_id: str) -> Order | None:
        self.start()
        return _find(self.paper.get_orders(), order_id)

    def list_orders(self, status: str | None = None) -> list[Order]:
        self.start()
        st = OrderStatus(status) if status else None
        return self.paper.get_orders(status=st)

    def positions(self) -> list[Position]:
        self.start()
        return self.paper.get_position()

    def account(self) -> Account:
        self.start()
        return self.paper.get_account()

    def trades(self, limit: int = 500) -> list[Trade]:
        self.start()
        ts = self.paper.get_trades()
        return ts[-limit:]


def _find(orders: list[Order], order_id: str) -> Order | None:
    for o in orders:
        if o.order_id == order_id:
            return o
    return None


_service: TradingService | None = None
_service_lock = threading.Lock()


def get_trading_service() -> TradingService:
    """模块级单例（真实库）。"""
    global _service
    with _service_lock:
        if _service is None:
            _service = TradingService()
            _service.start()
        return _service
