"""dataclass ↔ DB 互转与基础 CRUD。

- 全部写操作走模块级单写锁（WAL 单写多读，宪法第 4 条配套）；
- 订单/成交/持仓先落库后广播的「落库」半边在这里（广播在 services/engine 侧）。
"""

import json
import threading
from datetime import datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy import func
from sqlmodel import Session, select

from quantdesk.core.enums import OrderStatus, OrderType, Side
from quantdesk.core.models import Order, Position, Trade
from quantdesk.core.timeutil import now_ms
from quantdesk.store.db import get_session
from quantdesk.store.models_db import (
    BacktestDetailRow,
    BacktestRunRow,
    ComplianceRow,
    OrderRow,
    PositionSnapshotRow,
    TradeRow,
    WatchlistRow,
)

_write_lock = threading.RLock()

# 回测运行的终止态：进入这些状态时补 finished_at（queued/running 之外）。
# cancelled/interrupted 是本轮新增的取值，completed/failed 语义不变。
_BACKTEST_TERMINAL_STATUS = ("completed", "failed", "cancelled", "interrupted")

# 明细块大小：每块最多这么多条。200 条成交 ≈ 40KB JSON，
# 一次上万笔的运行也就是几十行记录，单块既不撑爆一页 SQLite 页，
# 也不至于切出上千个碎块（每块一行一次 INSERT）。
_DETAIL_CHUNK_ROWS = 200


def _json_default(o):
    """明细序列化兜底：numpy 标量 → python 标量，时间 → ISO 文本，其余 → str。

    为什么要兜底：明细来自引擎与 pandas，历史上这里最容易混进 np.int64
    （json.dumps 直接抛 TypeError）。写库这一步抛异常会让整次回测「跑完了但没落库」，
    比明细里一个字段变成字符串严重得多——所以宁可在类型上降级也不许写失败。
    """
    if isinstance(o, datetime):
        return o.isoformat()
    item = getattr(o, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:                        # noqa: BLE001 换 str 兜底
            return str(o)
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    return str(o)


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=_json_default)


def _side_str(v: Side | str) -> str:
    return v.value if isinstance(v, Side) else str(v)


def _otype_str(v: OrderType | str) -> str:
    return v.value if isinstance(v, OrderType) else str(v)


def _status_str(v: OrderStatus | str) -> str:
    return v.value if isinstance(v, OrderStatus) else str(v)


def _enum_side(v: str) -> Side:
    return Side(v)


# ---------- 订单 ----------
def _order_to_row(o: Order) -> OrderRow:
    return OrderRow(
        order_id=o.order_id,
        broker_order_id=o.broker_order_id,
        strategy_id=o.strategy_id,
        symbol=o.symbol,
        side=_side_str(o.side),
        price=o.price,
        volume=o.volume,
        order_type=_otype_str(o.order_type),
        status=_status_str(o.status),
        filled_volume=o.filled_volume,
        avg_filled_price=o.avg_filled_price,
        message=o.message,
        created_at=o.created_at,
        updated_at=o.updated_at,
    )


def _row_to_order(r: OrderRow) -> Order:
    return Order(
        order_id=r.order_id,
        broker_order_id=r.broker_order_id,
        strategy_id=r.strategy_id,
        symbol=r.symbol,
        side=Side(r.side),
        price=r.price,
        volume=r.volume,
        order_type=OrderType(r.order_type),
        status=OrderStatus(r.status),
        filled_volume=r.filled_volume,
        avg_filled_price=r.avg_filled_price,
        message=r.message,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def upsert_order(order: Order, db_path=None) -> None:
    with _write_lock, get_session(db_path) as s:
        row = s.get(OrderRow, order.order_id)
        if row is None:
            s.add(_order_to_row(order))
        else:
            fresh = _order_to_row(order)
            for k in ("broker_order_id", "strategy_id", "symbol", "side", "price",
                      "volume", "order_type", "status", "filled_volume",
                      "avg_filled_price", "message", "created_at", "updated_at"):
                setattr(row, k, getattr(fresh, k))
        s.commit()


def get_order(order_id: str, db_path=None) -> Order | None:
    with get_session(db_path) as s:
        row = s.get(OrderRow, order_id)
        return _row_to_order(row) if row else None


def list_orders(status: OrderStatus | str | None = None, strategy_id: str | None = None,
                limit: int = 1000, db_path=None) -> list[Order]:
    with get_session(db_path) as s:
        stmt = select(OrderRow)
        if status is not None:
            stmt = stmt.where(OrderRow.status == _status_str(status))
        if strategy_id is not None:
            stmt = stmt.where(OrderRow.strategy_id == strategy_id)
        rows = s.exec(stmt.order_by(OrderRow.created_at.desc()).limit(limit)).all()
        return [_row_to_order(r) for r in rows]


# ---------- 成交 ----------
def _trade_to_row(t: Trade) -> TradeRow:
    return TradeRow(
        trade_id=t.trade_id,
        order_id=t.order_id,
        broker_trade_id=t.broker_trade_id,
        symbol=t.symbol,
        side=_side_str(t.side),
        price=t.price,
        volume=t.volume,
        amount=t.amount,
        traded_at=t.traded_at,
    )


def _row_to_trade(r: TradeRow) -> Trade:
    return Trade(
        trade_id=r.trade_id,
        order_id=r.order_id,
        broker_trade_id=r.broker_trade_id,
        symbol=r.symbol,
        side=Side(r.side),
        price=r.price,
        volume=r.volume,
        amount=r.amount,
        traded_at=r.traded_at,
    )


def append_trade(trade: Trade, db_path=None) -> None:
    with _write_lock, get_session(db_path) as s:
        if s.get(TradeRow, trade.trade_id) is None:
            s.add(_trade_to_row(trade))
        s.commit()


def list_trades(start_ms: int | None = None, limit: int = 1000, db_path=None) -> list[Trade]:
    with get_session(db_path) as s:
        stmt = select(TradeRow)
        if start_ms is not None:
            stmt = stmt.where(TradeRow.traded_at >= start_ms)
        rows = s.exec(stmt.order_by(TradeRow.traded_at.desc()).limit(limit)).all()
        return [_row_to_trade(r) for r in rows]


# ---------- 持仓快照 ----------
def save_positions_snapshot(positions: list[Position], snapshot_at: int | None = None,
                            db_path=None) -> int:
    """全量快照（W1 简化：整表替换，snapshot_at 统一）。"""
    ts = snapshot_at if snapshot_at is not None else now_ms()
    with _write_lock, get_session(db_path) as s:
        s.exec(sa_delete(PositionSnapshotRow))
        for p in positions:
            s.add(PositionSnapshotRow(
                symbol=p.symbol, volume=p.volume, available=p.available,
                avg_cost=p.avg_cost, market_value=p.market_value,
                strategy_id=p.strategy_id, snapshot_at=ts,
            ))
        s.commit()
        return len(positions)


def latest_positions(db_path=None) -> list[Position]:
    with get_session(db_path) as s:
        max_ts = s.exec(select(func.max(PositionSnapshotRow.snapshot_at))).one()
        if max_ts is None:
            return []
        rows = s.exec(
            select(PositionSnapshotRow).where(PositionSnapshotRow.snapshot_at == max_ts)
        ).all()
        return [Position(
            symbol=r.symbol, volume=r.volume, available=r.available,
            avg_cost=r.avg_cost, market_value=r.market_value, strategy_id=r.strategy_id,
        ) for r in rows]


# ---------- 自选股 ----------
def watchlist_add(symbol: str, name: str = "", note: str = "", db_path=None) -> None:
    with _write_lock, get_session(db_path) as s:
        row = s.exec(select(WatchlistRow).where(WatchlistRow.symbol == symbol)).first()
        if row is None:
            s.add(WatchlistRow(symbol=symbol, name=name, note=note))
        else:
            row.name, row.note = name or row.name, note or row.note
        s.commit()


def watchlist_remove(symbol: str, db_path=None) -> bool:
    with _write_lock, get_session(db_path) as s:
        row = s.exec(select(WatchlistRow).where(WatchlistRow.symbol == symbol)).first()
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def watchlist_list(db_path=None) -> list[WatchlistRow]:
    with get_session(db_path) as s:
        return list(s.exec(select(WatchlistRow).order_by(WatchlistRow.added_at)).all())


# ---------- 合规清单 ----------
def compliance_upsert(item: str, category: str = "general", checked: bool = False,
                      note: str = "", db_path=None) -> None:
    with _write_lock, get_session(db_path) as s:
        row = s.exec(select(ComplianceRow).where(ComplianceRow.item == item)).first()
        if row is None:
            s.add(ComplianceRow(item=item, category=category, checked=checked, note=note))
        else:
            row.category, row.checked = category, checked
            if note:
                row.note = note
            row.updated_at = now_ms()
        s.commit()


def compliance_list(db_path=None) -> list[ComplianceRow]:
    with get_session(db_path) as s:
        return list(s.exec(select(ComplianceRow).order_by(ComplianceRow.id)).all())


# ---------- 回测运行 ----------
def backtest_create(strategy_name: str, symbol: str, freq: str, start: str, end: str,
                    params_json: str = "{}", db_path=None) -> int:
    with _write_lock, get_session(db_path) as s:
        row = BacktestRunRow(strategy_name=strategy_name, symbol=symbol, freq=freq,
                             start=start, end=end, params_json=params_json)
        s.add(row)
        s.commit()
        s.refresh(row)
        return int(row.id)


def backtest_finish(run_id: int, metrics_json: str, equity_json: str = "",
                    remark: str = "", status: str = "completed", db_path=None) -> None:
    """写「跑到了终点」的运行：状态终值 + 指标 + 净值 + 结束时刻。

    status 允许 completed 与 cancelled（以及理论上带产物的 failed 收尾）。
    取消的调用必须传 metrics_json="{}"——半截成交算出来的指标不是这次回测的结论，
    写进去就等于冒充 completed；净值/成交可以留（部分是事实），指标不行。
    """
    with _write_lock, get_session(db_path) as s:
        row = s.get(BacktestRunRow, run_id)
        if row is not None:
            row.metrics_json = metrics_json
            row.equity_json = equity_json
            row.remark = remark
            row.status = status
            row.finished_at = now_ms()
            s.commit()


def backtest_get(run_id: int, db_path=None) -> BacktestRunRow | None:
    with get_session(db_path) as s:
        return s.get(BacktestRunRow, run_id)


def backtest_set_status(run_id: int, status: str, remark: str = "", db_path=None) -> bool:
    """异步队列的中间态（queued/running）与终止态改写。

    completed / cancelled 这类要带指标与净值的终态走 backtest_finish；
    failed / interrupted / 排队中取消走这里（只改状态与备注）。
    返回是否真的改到了这一行（run_id 不存在 → False，不静默假装成功）。
    """
    with _write_lock, get_session(db_path) as s:
        row = s.get(BacktestRunRow, run_id)
        if row is None:
            return False
        row.status = status
        if remark:
            row.remark = remark[:2000]
        if status in _BACKTEST_TERMINAL_STATUS:
            row.finished_at = now_ms()
        s.commit()
        return True


def backtest_reconcile_orphans(remark: str = "", db_path=None) -> list[int]:
    """把库里悬挂的 queued|running 一律标 interrupted（进程重启后队列与线程都不在了）。

    为什么必须在启动时做：异步回测的队列活在服务进程内存里。进程被杀 / 重启后，
    库里那条 run 还写着 running——前端轮询会永远转圈，清单里也永远挂个「运行中」，
    这是「看起来在跑其实早就没了」的假状态。宁可 interrupted 也不许挂着。
    返回被标记的 run_id 列表（调用方据此记日志）。
    """
    ts = remark or "服务重启：该运行由旧进程发起，队列已随进程消失，未跑完（明细不可回看）"
    with _write_lock, get_session(db_path) as s:
        rows = s.exec(select(BacktestRunRow).where(
            BacktestRunRow.status.in_(("queued", "running")))).all()
        ids = [int(r.id) for r in rows]
        for r in rows:
            r.status = "interrupted"
            r.remark = ts[:2000]
            r.finished_at = now_ms()
        s.commit()
        return ids


def backtest_delete_detail(run_id: int, db_path=None) -> int:
    """删除某次运行的全部明细块（重跑覆盖前调用；返回删除块数）。"""
    with _write_lock, get_session(db_path) as s:
        rows = s.exec(select(BacktestDetailRow).where(
            BacktestDetailRow.run_id == run_id)).all()
        for r in rows:
            s.delete(r)
        s.commit()
        return len(rows)


def backtest_detail_write(run_id: int, parts: dict[str, object],
                          chunk_rows: int = _DETAIL_CHUNK_ROWS, db_path=None) -> dict:
    """回测明细分块落库（**按 kind 幂等覆盖**：只删重写的那几类，不碰其它 kind）。

    parts 的 value 两种形态：
    - list → 按 chunk_rows 切块，seq 从 0 递增，payload_json 是该块的数组；
    - dict → 单块（seq=0），用于 meta（bars/config/counts 这类运行元信息）。
    返回 {kind: {"rows": 条数, "chunks": 块数}}，调用方据此自检并塞进日志。

    两个坑都在实现里钉死了：
    ① 删旧块必须在插入之前 flush——SQLAlchemy 的 unit of work 在同一次 flush 里
       先 INSERT 后 DELETE，直接 add+delete 会撞 (run_id, kind, seq) 唯一索引
       （实测 IntegrityError: UNIQUE constraint failed）。
    ② 只删本次写入的 kind——曾经写成「先删该 run 的全部明细」，
       于是补写 trades 会顺手抹掉 logs/meta，重跑一类的局部覆盖变成静默丢数据。

    为什么 value 直接收 python 对象而不是调用方自己 dumps：序列化必须和
    「分块 + 唯一索引 + 条数」这件事在同一个事务语义里做，调用方各写各的
    dumps 容易漏 kind 或写坏 seq；体积失控的责任也就没地方承担了。
    """
    written: dict[str, dict] = {}
    with _write_lock, get_session(db_path) as s:
        kinds = tuple(parts.keys())
        if kinds:
            for r in s.exec(select(BacktestDetailRow).where(
                    BacktestDetailRow.run_id == run_id,
                    BacktestDetailRow.kind.in_(kinds))).all():
                s.delete(r)
            s.flush()                             # 删除先落地，再插新块（见 docstring ①）
        for kind, payload in parts.items():
            if isinstance(payload, dict):
                s.add(BacktestDetailRow(run_id=run_id, kind=kind, seq=0, rows=1,
                                        payload_json=_dumps(payload)))
                written[kind] = {"rows": 1, "chunks": 1}
                continue
            items = list(payload or [])
            size = chunk_rows if chunk_rows and chunk_rows > 0 else (len(items) or 1)
            seq = 0
            for i in range(0, len(items), size):
                block = items[i:i + size]
                s.add(BacktestDetailRow(run_id=run_id, kind=kind, seq=seq,
                                        rows=len(block), payload_json=_dumps(block)))
                seq += 1
            written[kind] = {"rows": len(items), "chunks": seq}
        s.commit()
    return written


def backtest_detail_read(run_id: int, kinds: tuple[str, ...] | list[str] | None = None,
                         db_path=None) -> dict:
    """按 (kind, seq) 升序把块拼回原始结构；没落库的运行返回 {}。

    返回 {kind: list|dict}：写时是 list 的拼回 list，写时是 dict 的（meta）取回 dict。
    旧块缺失（半途被删）时按 seq 顺序拼接已有的部分，不补占位元素——
    宁可少几条也不要用假数据凑一个看起来完整的列表。
    """
    with get_session(db_path) as s:
        stmt = select(BacktestDetailRow).where(BacktestDetailRow.run_id == run_id)
        if kinds:
            stmt = stmt.where(BacktestDetailRow.kind.in_(tuple(kinds)))
        rows = s.exec(stmt.order_by(BacktestDetailRow.kind.asc(),
                                    BacktestDetailRow.seq.asc())).all()
        out: dict = {}
        for r in rows:
            try:
                payload = json.loads(r.payload_json or "null")
            except (TypeError, ValueError):        # 单块损坏不拖垮整份明细
                continue
            if isinstance(payload, dict):
                out[r.kind] = payload
            elif isinstance(payload, list):
                out.setdefault(r.kind, []).extend(payload)
        return out


def backtest_detail_stats(run_id: int, db_path=None) -> dict:
    """明细落库概况（块数/条数按 kind 汇总）——回看前的「有没有、多不多」判据。"""
    with get_session(db_path) as s:
        rows = s.exec(select(BacktestDetailRow).where(
            BacktestDetailRow.run_id == run_id)).all()
        out: dict[str, dict] = {}
        for r in rows:
            e = out.setdefault(r.kind, {"rows": 0, "chunks": 0})
            e["rows"] += int(r.rows or 0)
            e["chunks"] += 1
        return out


def backtest_list(limit: int = 50, db_path=None) -> list[dict]:
    with get_session(db_path) as s:
        rows = s.exec(select(BacktestRunRow).order_by(
            BacktestRunRow.created_at.desc()).limit(limit)).all()
        return [{"run_id": r.id, "strategy": r.strategy_name, "symbol": r.symbol,
                 "freq": r.freq, "start": r.start, "end": r.end, "status": r.status,
                 "metrics": json.loads(r.metrics_json or "{}"),
                 "created_at": r.created_at, "finished_at": r.finished_at}
                for r in rows]


# ---------- 运行日志（落库可检索） ----------
def logs_append(rows: list[dict], db_path=None) -> int:
    """批量写入（LogSink 每批一次事务，避免高频事件把库压垮）。"""
    from quantdesk.store.models_db import LogRow

    if not rows:
        return 0
    with _write_lock, get_session(db_path) as s:
        for r in rows:
            s.add(LogRow(ts=r["ts"], level=r.get("level", "INFO"),
                         source=r.get("source", ""),
                         strategy_id=r.get("strategy_id", ""),
                         message=str(r.get("message", ""))[:4000]))
        s.commit()
    return len(rows)


def logs_query(limit: int = 200, offset: int = 0, source: str | None = None,
               level: str | None = None, contains: str = "", strategy_id: str | None = None,
               since_ms: int | None = None, until_ms: int | None = None,
               db_path=None) -> dict:
    from sqlalchemy import func as sa_func

    from quantdesk.store.models_db import LogRow

    with get_session(db_path) as s:
        stmt = select(LogRow)
        count_stmt = select(sa_func.count(LogRow.id))
        conds = []
        if source:
            conds.append(LogRow.source == source)
        if level:
            conds.append(LogRow.level == level)
        if strategy_id:
            conds.append(LogRow.strategy_id == strategy_id)
        if contains:
            conds.append(LogRow.message.contains(contains))
        if since_ms:
            conds.append(LogRow.ts >= since_ms)
        if until_ms:
            conds.append(LogRow.ts <= until_ms)
        for c in conds:
            stmt = stmt.where(c)
            count_stmt = count_stmt.where(c)
        total = int(s.exec(count_stmt).one())
        rows = s.exec(stmt.order_by(LogRow.ts.desc(), LogRow.id.desc())
                      .offset(offset).limit(min(limit, 1000))).all()
        return {"total": total, "items": [
            {"id": r.id, "ts": r.ts, "level": r.level, "source": r.source,
             "strategy_id": r.strategy_id, "message": r.message} for r in rows]}


def logs_sources(db_path=None) -> list[dict]:
    """已有日志的来源与条数（历史日志页「来源」过滤的事实源，不让用户手填字符串）。"""
    from sqlalchemy import func as sa_func

    from quantdesk.store.models_db import LogRow

    with get_session(db_path) as s:
        rows = s.exec(select(LogRow.source, sa_func.count(LogRow.id))
                      .group_by(LogRow.source)
                      .order_by(sa_func.count(LogRow.id).desc())).all()
        return [{"source": r[0], "count": int(r[1])} for r in rows if r[0]]


def logs_purge(before_ms: int, db_path=None) -> int:
    """保留期清理（盘后任务调用，防止日志表无限增长）。"""
    from sqlalchemy import delete as sa_delete

    from quantdesk.store.models_db import LogRow

    with _write_lock, get_session(db_path) as s:
        result = s.exec(sa_delete(LogRow).where(LogRow.ts < before_ms))
        s.commit()
        return int(result.rowcount or 0)


def reconcile_purge(before_ms: int, db_path=None) -> int:
    """删除保留期之前的「本轮无差异」心跳行（真差异一条不动）。

    为什么只删 clean：无差异行是运行噪音，30s 一条、一天 2880 条，留着只会让
    对账页翻不到有内容的日期；而差异行是事故档案——事故档案不许被定时任务悄悄删掉，
    要删得有人在这条记录上按下「退回/处置」或手工清库。回测明细与运行记录同理，不受影响。
    """
    from sqlalchemy import delete as sa_delete

    from quantdesk.store.models_db import ReconcileDiffRow

    with _write_lock, get_session(db_path) as s:
        result = s.exec(sa_delete(ReconcileDiffRow).where(
            ReconcileDiffRow.kind == "clean",
            ReconcileDiffRow.ran_at < before_ms))
        s.commit()
        return int(result.rowcount or 0)


# ---------- 交易日（SQLModel 侧，与 core/calendar 共表） ----------
def trading_days_upsert(rows: list[tuple[str, int]], db_path=None) -> None:
    from quantdesk.store.models_db import TradingDayRow

    with _write_lock, get_session(db_path) as s:
        for d, is_open in rows:
            row = s.get(TradingDayRow, d)
            if row is None:
                s.add(TradingDayRow(date=d, is_open=is_open))
            else:
                row.is_open = is_open
        s.commit()


def trading_days_open(db_path=None) -> set[str]:
    from sqlmodel import select as _select
    from quantdesk.store.models_db import TradingDayRow

    with get_session(db_path) as s:
        rows = s.exec(_select(TradingDayRow).where(TradingDayRow.is_open == 1)).all()
        return {r.date for r in rows}


# ---------- 账户状态（D09） ----------
def account_get(db_path=None) -> "AccountStateRow | None":
    from quantdesk.store.models_db import AccountStateRow

    with get_session(db_path) as s:
        return s.get(AccountStateRow, 1)


def account_set(cash: float, initial_cash: float, db_path=None) -> None:
    from quantdesk.store.models_db import AccountStateRow

    with _write_lock, get_session(db_path) as s:
        row = s.get(AccountStateRow, 1)
        if row is None:
            row = AccountStateRow(id=1, cash=cash, initial_cash=initial_cash)
            s.add(row)
        else:
            row.cash = cash
            row.initial_cash = initial_cash
        row.updated_at = now_ms()
        s.commit()


# ---------- 对账差异（差异即事故，必须可回溯） ----------
def reconcile_record(diffs: list[dict], channel: str = "", ran_at: int | None = None,
                     db_path=None) -> int:
    """一轮对账的差异整体落库（空差异也记一轮：无差异同样是审计证据）。"""
    from quantdesk.store.models_db import ReconcileDiffRow

    ts = ran_at if ran_at is not None else now_ms()
    with _write_lock, get_session(db_path) as s:
        if not diffs:
            s.add(ReconcileDiffRow(ran_at=ts, channel=channel, kind="clean", ref="",
                                   detail_json="{}"))
        for d in diffs:
            s.add(ReconcileDiffRow(ran_at=ts, channel=channel, kind=d["kind"],
                                   ref=str(d.get("ref", "")),
                                   detail_json=json.dumps(d.get("detail", {}),
                                                          ensure_ascii=False)))
        s.commit()
    return len(diffs)


def reconcile_recent(limit: int = 100, kind: str | None = None,
                     db_path=None, unresolved_only: bool = False,
                     diffs_only: bool = False) -> list[dict]:
    """对账差异清单。

    `diffs_only=True` 把 `kind="clean"` 的空跑行排除掉：一轮无差异也落库是刻意的
    （「无差异同样是审计证据」），但它**不是差异**。混进来会让「待处置 N 项」变成
    心跳计数——30s 一轮一天就是 2880 项，真事故被淹在噪音里，红条于是失去意义。
    审计视角（看全部）仍然保留 clean 行。
    """
    from quantdesk.store.models_db import ReconcileDiffRow

    with get_session(db_path) as s:
        stmt = select(ReconcileDiffRow)
        if kind:
            stmt = stmt.where(ReconcileDiffRow.kind == kind)
        if diffs_only:
            stmt = stmt.where(ReconcileDiffRow.kind != "clean")
        if unresolved_only:
            stmt = stmt.where(ReconcileDiffRow.resolved == False)  # noqa: E712 SQLModel 条件写法
        rows = s.exec(stmt.order_by(ReconcileDiffRow.ran_at.desc(),
                                    ReconcileDiffRow.id.desc()).limit(limit)).all()
        out = []
        for r in rows:
            out.append({"id": r.id, "ran_at": r.ran_at, "channel": r.channel,
                        "kind": r.kind, "ref": r.ref,
                        "detail": json.loads(r.detail_json or "{}"),
                        "resolved": bool(r.resolved), "resolve_note": r.resolve_note or "",
                        "resolved_at": int(r.resolved_at or 0)})
        return out


def reconcile_resolve(diff_id: int, note: str = "", resolve: bool = True,
                     db_path=None) -> dict | None:
    """把一条对账差异标记为已处置（或退回未处置），留一句话原因。

    「差异即事故」的闭环在于有人认领并写下结论；只列不处理，红条会在页面上变成噪音，
    噪音的后果是下一次真差异没人看。
    """
    from quantdesk.store.models_db import ReconcileDiffRow

    with _write_lock, get_session(db_path) as s:
        row = s.get(ReconcileDiffRow, diff_id)
        if row is None:
            return None
        row.resolved = bool(resolve)
        row.resolve_note = (note or "").strip()[:200]
        row.resolved_at = now_ms() if resolve else 0
        s.add(row)
        s.commit()
        s.refresh(row)
        return {"id": row.id, "resolved": bool(row.resolved),
                "resolve_note": row.resolve_note, "resolved_at": int(row.resolved_at or 0)}


def reconcile_last_run(db_path=None) -> dict:
    """最近一轮对账概览（供面板显示「上次核对：时间 / 通道 / 差异数」）。"""
    from quantdesk.store.models_db import ReconcileDiffRow

    with get_session(db_path) as s:
        last = s.exec(select(ReconcileDiffRow).order_by(
            ReconcileDiffRow.ran_at.desc()).limit(1)).first()
        if last is None:
            return {"ran_at": None, "channel": "", "diffs": 0}
        ran_at = last.ran_at
        rows = s.exec(select(ReconcileDiffRow).where(
            ReconcileDiffRow.ran_at == ran_at)).all()
        return {"ran_at": ran_at, "channel": last.channel,
                "diffs": sum(0 if r.kind == "clean" else 1 for r in rows)}
def flow_upsert(row: dict, db_path=None) -> None:
    """当日买卖量定格（按 trade_date+symbol 幂等覆盖——盯盘一天里反复定格不应堆重复行）。"""
    from quantdesk.store.models_db import DailyFlowRow

    with _write_lock, get_session(db_path) as s:
        existing = s.exec(select(DailyFlowRow).where(
            DailyFlowRow.trade_date == row["trade_date"],
            DailyFlowRow.symbol == row["symbol"])).first()
        target = existing or DailyFlowRow(trade_date=row["trade_date"], symbol=row["symbol"])
        for k, v in row.items():
            if k in ("trade_date", "symbol", "id"):
                continue
            if hasattr(target, k):
                setattr(target, k, v)
        s.add(target)
        s.commit()


def flow_latest(trade_date: int, db_path=None) -> list[dict]:
    """某交易日全部标的的买卖量定格（盯盘复盘列表）。"""
    from quantdesk.store.models_db import DailyFlowRow

    with get_session(db_path) as s:
        rows = s.exec(select(DailyFlowRow).where(DailyFlowRow.trade_date == trade_date)
                      .order_by(DailyFlowRow.amount.desc())).all()
        return [r.model_dump() for r in rows]


def flow_history(symbol: str, limit: int = 60, db_path=None) -> list[dict]:
    """单标的近 N 日的主动买卖结构（看资金是持续流入还是单日脉冲）。"""
    from quantdesk.store.models_db import DailyFlowRow

    with get_session(db_path) as s:
        rows = s.exec(select(DailyFlowRow).where(DailyFlowRow.symbol == symbol)
                      .order_by(DailyFlowRow.trade_date.desc())
                      .limit(min(int(limit), 500))).all()
        return [r.model_dump() for r in rows]


def flow_dates(db_path=None, limit: int = 40) -> list[int]:
    """已有定格记录的交易日（倒序），供"哪些天回看过"判断。"""
    from quantdesk.store.models_db import DailyFlowRow

    with get_session(db_path) as s:
        rows = s.exec(select(DailyFlowRow.trade_date).distinct()
                      .order_by(DailyFlowRow.trade_date.desc()).limit(limit)).all()
        return [int(r[0] if isinstance(r, tuple) else r) for r in rows]
