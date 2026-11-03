"""交易 API（D09）：下单/撤单/订单/成交/持仓/资金。

一切下单意图经服务端 RiskGate（宪法第 2 条）——本路由只做参数编组，
不做任何风控判断（风控在 engine/riskgate.py，直连 API 也必须过闸）。
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from quantdesk.core.enums import OrderStatus
from quantdesk.services.trading import get_trading_service

router = APIRouter(prefix="/api/trading", tags=["trading"])


class PlaceOrderRequest(BaseModel):
    symbol: str = Field(description='统一代码，如 "600000.SH"')
    side: str = Field(description="buy / sell")
    volume: int = Field(gt=0, description="股数（整手）")
    price: float | None = Field(default=None, gt=0, description="限价；None=市价")
    order_type: str = Field(default="limit", description="limit / market")
    strategy_id: str = Field(default="manual")


def _order_json(o) -> dict:
    return {
        "order_id": o.order_id, "symbol": o.symbol,
        "side": o.side.value if hasattr(o.side, "value") else str(o.side),
        "price": o.price, "volume": o.volume,
        "order_type": o.order_type.value if hasattr(o.order_type, "value") else str(o.order_type),
        "status": o.status.value if hasattr(o.status, "value") else str(o.status),
        "filled_volume": o.filled_volume, "avg_filled_price": o.avg_filled_price,
        "message": o.message, "strategy_id": o.strategy_id,
        "created_at": o.created_at, "updated_at": o.updated_at,
    }


@router.post("/orders")
def place_order(req: PlaceOrderRequest) -> dict:
    """下单（经 RiskGate 三道闸；模拟盘开关 ON 时路由内置撮合）。"""
    svc = get_trading_service()
    o = svc.place_order(req.symbol, req.side, req.volume, price=req.price,
                        order_type=req.order_type, strategy_id=req.strategy_id)
    return _order_json(o)


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: str) -> dict:
    svc = get_trading_service()
    accepted = svc.cancel_order(order_id)
    if not accepted:
        raise HTTPException(status_code=409, detail=f"订单 {order_id} 不可撤（不存在或已终态）")
    return {"accepted": True, "order_id": order_id}


@router.post("/orders/{order_id}/confirm")
def confirm_order(order_id: str) -> dict:
    """实盘二次确认（闸③回调；服务端强制，前端只是表现层）。"""
    svc = get_trading_service()
    try:
        o = svc.confirm_order(order_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return _order_json(o)


@router.post("/orders/{order_id}/reject")
def reject_order(order_id: str) -> dict:
    svc = get_trading_service()
    try:
        o = svc.reject_order(order_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return _order_json(o)


@router.get("/orders")
def list_orders(status: str | None = None) -> list[dict]:
    svc = get_trading_service()
    if status is not None:
        try:
            OrderStatus(status)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"非法状态: {status}") from e
    return [_order_json(o) for o in svc.list_orders(status=status)]


@router.get("/orders/{order_id}")
def get_order(order_id: str) -> dict:
    svc = get_trading_service()
    o = svc.get_order(order_id)
    if o is None:
        raise HTTPException(status_code=404, detail=f"订单 {order_id} 不存在")
    return _order_json(o)


@router.get("/positions")
def positions() -> list[dict]:
    svc = get_trading_service()
    return [
        {
            "symbol": p.symbol, "volume": p.volume, "available": p.available,
            "avg_cost": p.avg_cost, "market_value": p.market_value,
            "strategy_id": p.strategy_id,
        }
        for p in svc.positions()
    ]


@router.get("/account")
def account() -> dict:
    svc = get_trading_service()
    a = svc.account()
    return {"total_asset": a.total_asset, "cash": a.cash, "market_value": a.market_value,
            "frozen": a.frozen, "updated_at": a.updated_at}


@router.get("/trades")
def trades(limit: int = 200) -> list[dict]:
    svc = get_trading_service()
    return [
        {
            "trade_id": t.trade_id, "order_id": t.order_id, "symbol": t.symbol,
            "side": t.side.value if hasattr(t.side, "value") else str(t.side),
            "price": t.price, "volume": t.volume, "amount": t.amount,
            "traded_at": t.traded_at,
        }
        for t in svc.trades(limit=limit)
    ]
