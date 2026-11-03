"""行情 API（D18）：历史 K 线（Parquet 直读）/ 实时快照 / 自选股。"""

from fastapi import APIRouter, HTTPException

from quantdesk.services.market_data import get_market_service

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/history")
def history(symbol: str, freq: str = "1d", start: str | None = None,
            end: str | None = None, adjust: str = "forward") -> list[dict]:
    """K 线历史（Parquet 本地直读——切周期不外呼、不耗数据源配额）。"""
    svc = get_market_service()
    try:
        df = svc.history(symbol, freq=freq, start=start, end=end, adjust=adjust)
    except Exception as e:                         # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e)) from e
    if df.empty:
        return []
    out = df.copy()
    out["dt"] = pd_to_iso(out["dt"])
    return out.round(4).to_dict(orient="records")


def pd_to_iso(s):
    import pandas as pd

    return pd.to_datetime(s).map(lambda t: t.isoformat())


@router.get("/freqs")
def freqs(symbol: str) -> list[str]:
    """该标的本地可用的周期（前端周期切换按钮据此启停）。"""
    return get_market_service().freqs_available(symbol)


@router.get("/snapshot")
def snapshot(symbol: str | None = None) -> dict | list:
    """最新 tick 快照（五档盘口数据源；实时源接入前为空）。"""
    return get_market_service().snapshot(symbol)


@router.get("/watchlist")
def watchlist() -> list[dict]:
    return get_market_service().watchlist()


@router.post("/watchlist")
def watchlist_add(body: dict) -> list[dict]:
    symbol = str(body.get("symbol", "")).strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol 必填")
    get_market_service().watchlist_add(symbol, name=str(body.get("name", "")),
                                       note=str(body.get("note", "")))
    return get_market_service().watchlist()


@router.delete("/watchlist/{symbol}")
def watchlist_remove(symbol: str) -> list[dict]:
    ok = get_market_service().watchlist_remove(symbol)
    if not ok:
        raise HTTPException(status_code=404, detail=f"自选股 {symbol} 不存在")
    return get_market_service().watchlist()
