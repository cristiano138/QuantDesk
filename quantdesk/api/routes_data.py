"""数据管理 API：历史数据下载任务（提交即返回 task_id，前端轮询进度）。

为什么要有这一层：下载是「几十只标的 × 几次外呼 × 秒级到分钟级」的活，同步接口会
让请求挂在那里超时，用户看到的是浏览器转圈而不是进度。这里把编排放进后台线程，
逐标的回显写入行数与失败原因——单标的失败不拖垮整批，但一定要看得见。

写入口径（宪法第 11 条同源）：默认 fill=只补缺不覆盖。公开免费源字段不齐（腾讯日线没有
成交额），整段重写等于把已有行的成交额打空——那是数据资产上的静默劣化，所以覆盖需要显式
mode=overwrite。
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from quantdesk.core.enums import AdjustType, Frequency
from quantdesk.core.errors import QuantDeskError
from quantdesk.core.symbols import normalize

router = APIRouter(prefix="/api/data", tags=["data"])

# 前端下拉用的是市场惯用缩写（qfq/hfq），内核枚举是 forward/backward——两个都收，
# 但只在这一处映射；服务层与存储层一律用枚举值。
_FREQ_ALIASES = {f.value: f for f in Frequency if f is not Frequency.TICK}
_ADJUST_ALIASES = {
    "none": AdjustType.NONE, "forward": AdjustType.FORWARD, "backward": AdjustType.BACKWARD,
    "qfq": AdjustType.FORWARD, "hfq": AdjustType.BACKWARD,
}
_MODES = ("fill", "overwrite")

_KEEP_TASKS = 50          # 只留最近 50 个任务（进程内；重启即清，与回测明细同一口径）


class DownloadRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list,
                               description='统一代码列表，如 ["600000.SH","00700.HK"]')
    freq: str = Field(default="1d", description="1d / 1w / 1M / 1m / 5m / 15m / 30m / 60m")
    start: str | None = Field(default=None, description="YYYY-MM-DD；留空取最近 365 天")
    end: str | None = Field(default=None, description="YYYY-MM-DD；留空取今天")
    adjust: str = Field(default="qfq", description="none / qfq(forward) / hfq(backward)")
    mode: str = Field(default="fill", description="fill=只补缺不覆盖（默认）· overwrite=整段重写")


@dataclass
class DownloadTask:
    task_id: str
    total: int
    symbols: list[str]
    freq: str
    adjust: str
    mode: str
    start: str
    end: str
    state: str = "queued"        # queued / running / done / error
    done: int = 0
    current: str = ""
    ok: dict[str, int] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    error: str = ""
    source: str = ""
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    finished_at: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("symbols", None)          # 列表可能很长，进度接口不回它
        return d


_LOCK = threading.RLock()
_TASKS: dict[str, DownloadTask] = {}


def _tasks() -> dict[str, DownloadTask]:
    return _TASKS


@router.get("/chains")
def chains() -> dict:
    """当前历史降级链：谁在主、谁未就绪、未就绪缺什么——下载失败先看这里。

    ready 直接取中枢实际装配出来的那批（feed 实例的 name），而不是"没报错就算就绪"：
    没被尝试过的通道不会出现在错误表里，按错误表反推会把 qmt/tushare 显示成就绪。
    """
    from quantdesk.services.channels import get_hub

    hub = get_hub()
    chain = hub.history_chain()
    ready = [getattr(f, "name", "") for f in hub.history_feeds() if getattr(f, "name", "")]
    errors = hub.feed_errors()
    return {
        "history_chain": chain,
        "ready": [n for n in chain if n in ready],
        "unready": [{"name": n, "reason": errors.get(n, "未装配（未尝试或依赖缺失）")}
                    for n in chain if n not in ready],
    }


@router.post("/download")
def download(req: DownloadRequest) -> dict:
    """提交下载任务，立刻返回 task_id（进度走 GET /api/data/task/{task_id}）。"""
    if not req.symbols:
        raise HTTPException(status_code=400, detail="symbols 为空：请至少填写一个标的代码")
    freq = _FREQ_ALIASES.get((req.freq or "").strip().lower())
    if freq is None:
        raise HTTPException(status_code=400,
                            detail=f"未知周期: {req.freq}（可选: {sorted(_FREQ_ALIASES)}）")
    adjust = _ADJUST_ALIASES.get((req.adjust or "qfq").strip().lower())
    if adjust is None:
        raise HTTPException(status_code=400,
                            detail=f"未知复权口径: {req.adjust}（可选: {sorted(_ADJUST_ALIASES)}）")
    if req.mode not in _MODES:
        raise HTTPException(status_code=400, detail=f"mode 只能是 {'/'.join(_MODES)}")

    syms, bad = [], []
    for s in req.symbols:
        try:
            code = normalize(str(s))
        except QuantDeskError as e:
            bad.append(f"{s!r}: {e}")
            continue
        if code not in syms:
            syms.append(code)
    if bad:
        raise HTTPException(status_code=400, detail="代码无法识别 —— " + "；".join(bad))

    end = _parse_day(req.end) or datetime.now()
    start = _parse_day(req.start) or (end - timedelta(days=365))
    if start > end:
        raise HTTPException(status_code=400,
                            detail=f"开始日期晚于结束日期: {start:%Y-%m-%d} > {end:%Y-%m-%d}")

    task = DownloadTask(task_id=f"DL-{uuid4().hex[:12].upper()}", total=len(syms), symbols=syms,
                        freq=freq.value, adjust=adjust.value, mode=req.mode,
                        start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
    with _LOCK:
        _TASKS[task.task_id] = task
        overflow = len(_TASKS) - _KEEP_TASKS
        for old in sorted(_TASKS.values(), key=lambda t: t.created_at)[: max(0, overflow)]:
            if old.state in ("done", "error"):
                _TASKS.pop(old.task_id, None)

    t = threading.Thread(target=_run, args=(task.task_id,), daemon=True,
                         name=f"download-{task.task_id}")
    t.start()
    return {"task_id": task.task_id, "total": task.total, "state": task.state}


@router.get("/task/{task_id}")
def task_state(task_id: str) -> dict:
    with _LOCK:
        task = _TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404,
                            detail=f"下载任务 {task_id} 不存在（服务重启后内存任务会清空）")
    return task.to_dict()


@router.get("/inventory")
def inventory() -> dict:
    """本地库清单：这台机器上到底攒了哪些标的/周期/区间。

    以前数据管理页只有"往库里写"的入口，没有"看库里有什么"的入口——
    回测报「本地无数据」时用户无法自查缺哪段，只能反复下载。
    """
    from quantdesk.store.parquet_store import ParquetStore

    store = ParquetStore()
    items = store.inventory()
    freqs: dict[str, int] = {}
    for it in items:
        freqs[it["freq"]] = freqs.get(it["freq"], 0) + it["rows"]
    return {
        "root": str(store.root),
        "items": items,
        "symbols": len({it["symbol"] for it in items}),
        "entries": len(items),
        "rows": sum(it["rows"] for it in items),
        "bad_files": sum(it["bad_files"] for it in items),
        "rows_by_freq": freqs,
    }


def _parse_day(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise HTTPException(status_code=400, detail=f"日期格式应为 YYYY-MM-DD，收到: {value}")


def _run(task_id: str) -> None:
    """后台线程：逐标的取源并入库，每一步都写回任务对象（前端 1s 轮一次）。"""
    with _LOCK:
        task = _TASKS.get(task_id)
    if task is None:
        return
    task.state = "running"
    try:
        from quantdesk.services.channels import get_hub
        from quantdesk.services.data_store import DataStoreService
        from quantdesk.store.parquet_store import ParquetStore

        hub = get_hub()
        feed = hub.federated()
        if not hub.history_feeds():
            task.state = "error"
            task.error = ("历史行情源全部未就绪：降级链 "
                          f"{hub.history_chain()} 里没有可用通道。"
                          "请在通道页查看缺失依赖，或在设置里改 feed_priority_history")
            task.finished_at = int(time.time() * 1000)
            return

        store = ParquetStore()
        svc = DataStoreService(feed=feed, store=store)
        freq = Frequency(task.freq)
        adjust = AdjustType(task.adjust)
        start = datetime.strptime(task.start, "%Y-%m-%d")
        end = datetime.strptime(task.end, "%Y-%m-%d")

        for i, sym in enumerate(task.symbols, 1):
            task.current = sym
            try:
                # 一只一次调用：进度与失败原因都能落到具体标的（整批调用只有一张总报表）
                rep = (svc.backfill([sym], freq, start, end, adjust) if task.mode == "fill"
                       else svc.batch_download([sym], freq, start, end, adjust))
                if sym in rep.failed:
                    # 失败的标的绝不再进 ok：写 0 行会让界面同时列出「成功 1 只（0 行）」和
                    # 「失败 1 只」——同一个标的两个结论，比只报一个错更难查
                    task.failed[sym] = str(rep.failed[sym])
                else:
                    task.ok[sym] = int(rep.ok.get(sym, 0))
            except Exception as e:  # noqa: BLE001 — 单标的异常只记在这一行上
                task.failed[sym] = repr(e)
            task.done = i
        task.state = "done"
    except Exception as e:  # noqa: BLE001 — 任务级异常如实上报，不留 running 假死
        task.state = "error"
        task.error = repr(e)
    finally:
        task.current = ""
        task.finished_at = int(time.time() * 1000)
