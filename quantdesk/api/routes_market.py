"""行情 API（D18）：历史 K 线（Parquet 直读）/ 实时快照 / 自选股。"""

from fastapi import APIRouter, HTTPException

from quantdesk.core.enums import Frequency
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
def freqs(symbol: str | None = None) -> list[str]:
    """本地可用的周期（前端周期切换按钮据此启停）。

    没选标的时返回内核支持的全部周期——这个接口是"有哪些周期可选"，
    不是"这只票有哪些"；要求 symbol 会让还没选标的的首屏拿不到枚举。
    """
    if not symbol:
        return [f.value for f in Frequency if f is not Frequency.TICK]
    return get_market_service().freqs_available(symbol)


@router.get("/snapshot")
def snapshot(symbol: str | None = None) -> dict | list | None:
    """最新 tick 快照（五档盘口数据源）。

    不带 symbol = 全量列表（泵里有多少回多少）；带 symbol 时泵里没有就落到只读行情源，
    仍然拿不到就回带 reason 的空壳——界面据此说清"为什么没有"，而不是永远一句"等待接入"。
    """
    if symbol is None:
        return get_market_service().snapshot(None)
    return get_market_service().snapshot_or_readonly(symbol)


@router.get("/financial")
def financial(symbol: str, report_type: str = "indicator") -> dict:
    """财务数据：摘要（东财）+ 利润表/资产负债表/现金流量表（新浪），AKShare 免账号就能取。

    界面以前没有入口，等于把已经接好的能力关在库里。首次是真实外呼（3~10 秒，前端有加载态），
    同标的同表种本机缓存 5 分钟；源不可用时如实 503 并给原文原因，不返回空表装作"没数据"。
    """
    svc = get_market_service()
    try:
        return svc.financial(symbol, report_type=report_type)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001 — 外部源的失败形态很多，统一换成可读原文
        raise HTTPException(status_code=503, detail=f"财务数据取不到：{e}") from e


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
