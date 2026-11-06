"""SQLite 表模型（宪法存储层）。

七张业务表：strategies / orders / trades / positions_snapshot / watchlist /
compliance / backtest_runs；另含 schema_version 与 trading_days（D02 日历共用）、
account_state / reconcile_diff / run_log / daily_flow / backtest_detail（回测明细分块）。
时间戳统一 INTEGER 毫秒（UTC）；orders.status 直接存 OrderStatus 枚举值
（含 pending_confirm / expired_confirm，为 D17 二次确认铺路）。
"""

from sqlmodel import Field, Index, SQLModel

from quantdesk.core.timeutil import now_ms


class SchemaVersion(SQLModel, table=True):
    __tablename__ = "schema_version"

    id: int | None = Field(default=None, primary_key=True)
    version: int = 0
    applied_at: int = Field(default_factory=now_ms)


class StrategyRow(SQLModel, table=True):
    __tablename__ = "strategies"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    path: str
    params_json: str = "{}"
    enabled: bool = True
    created_at: int = Field(default_factory=now_ms)
    updated_at: int = Field(default_factory=now_ms)


class OrderRow(SQLModel, table=True):
    __tablename__ = "orders"

    order_id: str = Field(primary_key=True)
    broker_order_id: str | None = Field(default=None, index=True)
    strategy_id: str = Field(default="manual", index=True)
    symbol: str = Field(index=True)
    side: str
    price: float
    volume: int
    order_type: str
    status: str = Field(default="pending_confirm", index=True)
    filled_volume: int = 0
    avg_filled_price: float = 0.0
    message: str = ""
    created_at: int = 0
    updated_at: int = 0
    # ---- 闸③ 确认判据（K01）：判定要靠这份落库的事实，不靠进程内存 ----
    # 老库由 store/db.ensure_columns 补列，补出来是默认值 = 「从未武装过」，
    # 这种待确认单确认时一律 fail-closed 作废，绝不猜身份、不重开确认窗口。
    confirm_deadline: int = Field(default=0)   # UTC 毫秒绝对截止；0 = 无截止时间
    confirm_mode: str = ""                     # live（模拟路径没有确认流）
    confirm_channel: str = ""                  # 通道标识（适配器 name）
    confirm_account: str = ""                  # 账户指纹（sha256 截断，不存明文账号）
    confirm_rules: str = ""                    # 武装时的规则口径版本
    result_uncertain: int = Field(default=0)   # 1 = 委托结果不明，重启先核对券商不盲重发


class TradeRow(SQLModel, table=True):
    __tablename__ = "trades"

    trade_id: str = Field(primary_key=True)
    order_id: str = Field(index=True)
    broker_trade_id: str | None = Field(default=None, index=True)
    symbol: str = Field(index=True)
    side: str
    price: float
    volume: int
    amount: float
    traded_at: int = Field(index=True)


class PositionSnapshotRow(SQLModel, table=True):
    __tablename__ = "positions_snapshot"

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(index=True)
    volume: int
    available: int  # T+1：当日买入为 0
    avg_cost: float
    market_value: float
    strategy_id: str = Field(default="manual", index=True)
    snapshot_at: int = Field(index=True)


class WatchlistRow(SQLModel, table=True):
    __tablename__ = "watchlist"

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(unique=True)
    name: str = ""
    note: str = ""
    added_at: int = Field(default_factory=now_ms)


class ComplianceRow(SQLModel, table=True):
    __tablename__ = "compliance"

    id: int | None = Field(default=None, primary_key=True)
    item: str = Field(unique=True)
    category: str = "general"
    checked: bool = False
    note: str = ""
    updated_at: int = Field(default_factory=now_ms)


class BacktestRunRow(SQLModel, table=True):
    """回测运行记录。

    status 取值（六个，前四个是既有语义，后两个本轮新增，不改写旧值含义）：
    queued 排队中 / running 运行中 / completed 跑完并出指标 / failed 异常终止
    / cancelled 用户取消（排队中直接作废，运行中在 bar 边界安全停下）
    / interrupted 服务重启时发现库里仍挂着 queued|running——进程已经不在了，
    这条运行事实上没跑完，如实标 interrupted 而不是让它永远挂着（明细多半不可回看）。
    """

    __tablename__ = "backtest_runs"

    id: int | None = Field(default=None, primary_key=True)
    strategy_name: str = Field(index=True)
    symbol: str
    freq: str
    start: str
    end: str
    params_json: str = "{}"
    status: str = Field(default="running", index=True)
    metrics_json: str = "{}"
    equity_json: str = ""
    remark: str = ""
    created_at: int = Field(default_factory=now_ms)
    finished_at: int | None = None


class BacktestDetailRow(SQLModel, table=True):
    """回测运行明细（成交/委托/日志/元信息）分块落库。

    为什么要单独一张表，而不是往 backtest_runs 塞一个大 JSON 列：
    - 体积：分钟线/多标的回测一次可产生上万笔成交与委托，整块 JSON 能到几 MB。
      backtest_runs 是「清单页每行都要读」的表，把大对象挂在它身上，
      列表接口会把明细一起读进内存（SQLite 不做列裁剪意义上的懒取）；
    - 分块：按 (run_id, kind, seq) 切块，每块最多 _DETAIL_CHUNK_ROWS 条，
      详情页按 kind 只取自己要的那几块，删运行记录时按 run_id 整体删；
    - 生命周期不同：runs 行是「运行事实」，明细是「可重算的派生物」，
      同一 run 重跑就是覆盖（先删同 kind 旧块再写），不与 runs 的语义纠缠。

    kind 取值：trades / orders / logs（列表，按 seq 升序拼接）
             meta（单块，dict：bars/config/partial/logs_total/counts）。
    唯一索引 (run_id, kind, seq)：写入路径是「先删旧块再插」，
    万一并发重跑同一 run，索引层就是最后一道防线（会抛约束冲突而不是静默双份）。
    """

    __tablename__ = "backtest_detail"
    __table_args__ = (
        Index("ux_backtest_detail_run_kind_seq", "run_id", "kind", "seq", unique=True),
    )

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(index=True)
    kind: str = Field(index=True, default="trades")
    seq: int = 0
    rows: int = 0                       # 本块条目数（logs/meta 为逻辑条数）
    payload_json: str = "[]"
    created_at: int = Field(default_factory=now_ms)


class TradingDayRow(SQLModel, table=True):
    __tablename__ = "trading_days"

    date: str = Field(primary_key=True)  # YYYYMMDD
    is_open: int


class AccountStateRow(SQLModel, table=True):
    """账户状态（D09，模拟盘/实盘共用的资金快照；单行 id=1）。

    七表之外的新增本地表：D09 DoD「重启后持仓/资金恢复」需要持久化资金，
    持仓已有 positions_snapshot，资金在此落地。持仓/成交明细仍在原表。
    """

    __tablename__ = "account_state"

    id: int | None = Field(default=None, primary_key=True)
    cash: float = 0.0
    initial_cash: float = 0.0
    updated_at: int = Field(default_factory=now_ms)


class ReconcileDiffRow(SQLModel, table=True):
    """对账差异（七表外新增本地表）。

    理由：「差异即事故」要求事故可回溯——只发 WS 事件的话，页面关掉就再没人知道
    昨晚少了一笔成交。差异按轮次记录，前端红条与运维导出都读这张表。
    """

    __tablename__ = "reconcile_diff"

    id: int | None = Field(default=None, primary_key=True)
    ran_at: int = Field(index=True)
    channel: str = Field(index=True)
    kind: str = Field(index=True)
    ref: str = ""
    detail_json: str = "{}"
    created_at: int = Field(default_factory=now_ms)
    # 处置闭环：差异不是列出来就完了，要能标"已核实/已处置"并留一句话原因。
    # 这三列由 store.db.ensure_columns() 在老库上自动补齐（升级不许让软件打不开）。
    resolved: bool = False
    resolve_note: str = ""
    resolved_at: int = 0


class LogRow(SQLModel, table=True):
    """运行日志落库（策略 stdout 之外的可检索痕迹）。

    落库理由：supervisor 只在内存留 10 条、崩溃才写文件——「昨天 14:20 那次拒单
    到底说了什么」这种问题必须能被回答。写入走异步批量（LogSink），不拖慢事件总线。
    """

    __tablename__ = "run_log"

    id: int | None = Field(default=None, primary_key=True)
    ts: int = Field(index=True)
    level: str = Field(default="INFO", index=True)
    source: str = Field(default="", index=True)
    strategy_id: str = Field(default="", index=True)
    message: str = ""


class DailyFlowRow(SQLModel, table=True):
    """当日买卖量定格（盯盘页收盘后仍可回看历史某天的主动买卖结构）。

    为什么要落库：盯盘的实时买卖量只活在内存里，收盘后刷新页面就变成"今天没数据"；
    而"某天主买占比 62%、尾盘大单流出"这类看盘结论必须能复盘。
    口径列全部保留：官方内外盘与逐笔聚合各自留痕，谁和谁差多少一目了然，不做合并掩盖。
    """

    __tablename__ = "daily_flow"

    id: int | None = Field(default=None, primary_key=True)
    trade_date: int = Field(index=True)          # YYYYMMDD 整数，便于按日查
    symbol: str = Field(index=True)
    name: str = ""
    market: str = ""                             # SH/SZ/BJ/HK
    close: float = 0.0
    pct_chg: float = 0.0
    volume: float = 0.0                          # A股=手，港股=股（volume_unit 标注）
    amount: float = 0.0
    outer_volume: float | None = None            # 官方主动买入量（外盘）
    inner_volume: float | None = None            # 官方主动卖出量（内盘）
    tick_buy_volume: float = 0.0                 # 逐笔聚合的买量（含盘后，与官方可有差）
    tick_sell_volume: float = 0.0
    tick_neutral_volume: float = 0.0
    buy_amount: float = 0.0
    sell_amount: float = 0.0
    big_buy_volume: float = 0.0                  # 大单分层（阈值见 services/watch.py）
    big_sell_volume: float = 0.0
    side_map: str = ""                           # 校准后的方向映射，如 {"2":"buy","1":"sell"}
    flow_source: str = ""                        # official_outer_inner / em_tick
    staled: bool = False                         # 定格时数据是否已过时（接口断过）
    created_at: int = Field(default_factory=now_ms)
