"""盯盘 API（只读看盘）：实时列表、单标的全量详情、逐笔分页、K线、历史定格。

宪法约束照旧：API 层只依赖 services，不碰适配器与存储细节。
本模块**没有任何下单入口**——只读产品就不该留写的口子。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from quantdesk.core.errors import QuantDeskError
from quantdesk.core.symbols import normalize
from quantdesk.services.watch import get_watch_service, session_of

router = APIRouter(prefix="/api/watch", tags=["watch"])


class PoolRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list, description='如 ["600000.SH","00700.HK"]')


class PoolRemoveRequest(BaseModel):
    symbol: str


def _svc():
    return get_watch_service()


def _status_payload() -> dict:
    """状态 + 服务器时间 + 两市场时段——三个入口都得带齐，否则前端换个接口就显示「—」。"""
    st = _svc().status()
    st["server_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st["trading_day"] = {m: session_of(m) for m in ("CN", "HK")}
    return st


@router.get("/status")
def status() -> dict:
    """盯盘运行态：池、通道、当前时段、方向码校准结果、失败原因。"""
    return _status_payload()


@router.get("/pool")
def pool() -> dict:
    return {"pool": _svc().pool()}


@router.post("/pool")
def add_pool(req: PoolRequest) -> dict:
    added, errors = [], []
    for s in req.symbols:
        try:
            added.append(_svc().add(s))
        except QuantDeskError as e:
            errors.append({"symbol": s, "error": str(e)})
    if not added and errors:
        raise HTTPException(status_code=400, detail=errors)
    return {"added": added, "errors": errors, "pool": _svc().pool()}


@router.delete("/pool")
def remove_pool(req: PoolRemoveRequest) -> dict:
    try:
        ok = _svc().remove(req.symbol)
    except QuantDeskError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"removed": ok, "symbol": req.symbol, "pool": _svc().pool()}


@router.post("/refresh")
def refresh(force: bool = False) -> dict:
    """刷一整池（前端「刷新」按钮；进页面也会调一次）。

    默认带时段闸：两个市场都休市时不外呼，直接回最近一次快照（`polled=false`）。
    盯盘页是常开页面，没有这道闸就是"通宵每 5 秒打一次公开接口"。
    手动点「立即刷新」传 force=true 绕过，保证用户想看最新值时一定拿得到真答案。
    """
    out = _svc().refresh_auto(force=force)
    return {"count": len(out["rows"]), "polled": out["polled"],
            "skipped": not out["polled"], "reason": out.get("reason", ""),
            "list": _svc().list_rows(), "status": _status_payload()}


@router.get("/list")
def watch_list() -> dict:
    """盯盘列表（含买卖量摘要）。不主动外呼，读最近一次快照。"""
    return {"rows": _svc().list_rows(), "status": _status_payload()}


@router.get("/history")
def history(symbol: str | None = None, trade_date: int | None = None, limit: int = 60) -> dict:
    """收盘定格的历史（某天各标的的主动买卖结构；无 symbol 时按日查全池）。"""
    return {"rows": _svc().history(symbol=symbol, trade_date=trade_date, limit=limit)}


@router.get("/{symbol}")
def detail(symbol: str, force: bool = False) -> dict:
    """单标的全量详情：快照 + 五档 + 当日买卖量 + 分时 + 个股字段 + 当前时段。"""
    try:
        normalize(symbol)
    except QuantDeskError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        return _svc().detail(symbol, force=force)
    except QuantDeskError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/{symbol}/ticks")
def ticks(symbol: str, limit: int = 500, offset: int = 0,
          side: str | None = None) -> dict:
    """逐笔成交分页（direction 用当日校准后的方向码；side 可筛 buy/sell/neutral）。"""
    if side not in (None, "buy", "sell", "neutral"):
        raise HTTPException(status_code=400,
                            detail="side 只支持 buy / sell / neutral 或留空")
    try:
        return _svc().ticks(symbol, limit=limit, offset=offset, side=side)
    except QuantDeskError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/{symbol}/kline")
def kline(symbol: str, freq: str = "1d", days: int = 250,
          adjust: str = "forward") -> dict:
    """日/周/月 K 线（港股历史用腾讯源，A股优先本地 Parquet，缺数据回落腾讯）。"""
    from quantdesk.core.enums import AdjustType, Frequency

    try:
        f = Frequency(freq)
        a = AdjustType(adjust)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if f not in (Frequency.D1, Frequency.W1, Frequency.MO1):
        raise HTTPException(status_code=400,
                            detail="盯盘 K 线只支持 1d/1w/1M；分钟级看分时接口")
    sym = normalize(symbol)
    end = datetime.now()
    start = end - timedelta(days=int(days) * 2 + 30)
    rows: list[dict] = []
    source = ""
    try:
        from quantdesk.store.parquet_store import ParquetStore

        local = ParquetStore().read(sym, f, start, end, a)
        if not local.empty:
            rows = [{"dt": f"{t:%Y-%m-%d}", "open": o, "high": h, "low": l, "close": c,
                     "volume": v, "amount": amt}
                    for t, o, h, l, c, v, amt in zip(local["dt"], local["open"], local["high"],
                                                     local["low"], local["close"], local["volume"],
                                                     local["amount"])]
            source = "local_parquet"
    except QuantDeskError as e:
        rows = []
        source = f"local_error:{e}"
    if not rows:
        tx = _svc().history_feed()
        if tx is None:
            raise HTTPException(status_code=503, detail="K 线通道未就绪（腾讯行情不可用）")
        try:
            df = tx.get_history(sym, f, start, end, a)
        except QuantDeskError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        rows = [{"dt": f"{t:%Y-%m-%d}", "open": o, "high": h, "low": l, "close": c,
                 "volume": v, "amount": amt}
                for t, o, h, l, c, v, amt in zip(df["dt"], df["open"], df["high"], df["low"],
                                                 df["close"], df["volume"], df["amount"])]
        source = (source + "+" if source else "") + "tx_quote"
    return {"symbol": sym, "freq": f.value, "adjust": a.value, "source": source,
            "unit": "股" if sym.endswith(".HK") else "手",
            "note": "腾讯源不返回成交额（amount=0）；A股优先本地 Parquet" if "tx_quote" in source
                    else "",
            "rows": rows[-int(days):]}
