# QuantDesk — 项目宪法（AGENTS.md）

你正在开发 QuantDesk：一个 Windows 本地 A 股量化交易工作台（Python 3.11 + FastAPI + React + TypeScript）。  
核心体验：行情面板 → Monaco 策略工作台 → 回测 → 模拟盘 → 实盘（QMT 为主力通道），  
**同一份策略代码零修改跑通三种模式**；实盘下单必须经服务端三道闸。

## 硬性约束（每天、每个任务都必须遵守，违反即返工）

1. **适配器模式**：新增数据源/券商 = `quantdesk/adapters/data/` 或 `adapters/brokers/`  
   新增一个文件 + 模块级 `ADAPTER` 类导出（注册表自动扫描发现）。任何为接入新源  
   而修改 `core/`、`engine/`、`services/` 的设计都是错误设计。
2. **订单三道闸（服务端强制）**：一切下单意图必经 `engine/riskgate.py`——  
   ① 全局模拟盘开关（config.simulation_only，默认 True；ON → 一律路由内置模拟撮合）；  
   ② A股规则校验（T+1 可用 / 整手 / 委托价 ∈ 涨跌停区间 / 单票仓位 / 日委托数 / 资金充足）；  
   ③ 实盘二次确认（先落库 PENDING_CONFIRM → WS 推送 → 10s 超时 EXPIRED_CONFIRM 自动作废）。  
   通道断线 → fail-closed 直接拒单（不排队不重试）。前端弹窗只是表现层，直连 API 也必须过闸。
3. **适配器合同**：BrokerAdapter 只执行不风控；place_order 收到的 Order 一定已确认  
   （order_id 由引擎预生成，适配器只补 broker_order_id）；提交失败必须抛 BrokerError，  
   禁止静默失败；断线必须抛 ConnectionLostError 且可被 health_check 探测；  
   重连成功后必须自行重放订阅 / 刷新对账。
4. **先落库后广播**：订单/成交/持仓变化先写 SQLite（WAL）再发 EventBus——  
   重启可对账，待确认单重启不丢。
5. **价格口径**：下单价格永远是原始市场价（不复权）；回测与指标用前复权。  
   「委托价 ∈ 涨跌停区间」校验天然拦截复权价误用（兜底）。
6. **策略隔离**：每策略一个子进程（ZMQ IPC）；策略代码只 import  
   BaseStrategy / StrategyContext 与 core 类型，禁止 import 任何适配器；  
   适配器实例永不进入策略进程。
7. **本地与安全**：uvicorn 只绑 127.0.0.1（写死，不提供配置项）；全部数据本地存储  
   （SQLite + Parquet），重启不丢；敏感信息（账号/token/资金路径）只进本地 config  
   （加密存储），不入库明文、不进日志、不进提示词与 git。
8. **UI 与栈纪律**：K线 lightweight-charts、指标图 ECharts、编辑器 Monaco；  
   禁止 PyQt/Tkinter；后端禁止 Java/C#；禁止服务器部署；不引入 pyproject 之外的  
   重量级依赖，新增前说明理由并征得同意。
9. **合规**：程序化交易报备清单为常驻页面（导航入口，非弹窗刷屏）；easytrader 类  
   非官方通道默认禁用 + UI 红字「非官方接口，稳定性低」；新数据源接入必须交叉对数  
   （与已接入任一源误差 ≤ 0.1%，除权日单列报告）。

## 技术栈（已锁定，不讨论替换）

Python 3.11+ · uv · FastAPI(REST + WebSocket) · SQLModel(SQLite, WAL) · pyarrow(Parquet) ·  
pyzmq(XPUB/SUB 事件 + PUSH/PULL 订单意图) · msgpack · pandas · pytest · APScheduler(盘后任务)  
前端：Vite + React 18 + TypeScript(strict) · zustand · lightweight-charts · echarts ·  
@monaco-editor/react · 桌面壳：pywebview  
交易：xtquant/QMT（主力）· 掘金 gm（备选）· easytrader（默认禁用）· 内置模拟撮合

## 架构分层与职责

- UI 层 frontend/src：行情面板 / 策略工作台(Monaco) / 回测报告 / 实盘监控 / 数据管理 /  
  报备清单 / 设置（全局模拟盘开关）
- API 网关层 quantdesk/api：routes\_* + ws.py（/ws/events 统一事件流）；只依赖 services
- 服务层 quantdesk/services：market_data / trading / strategy_mgr / backtest_svc / data_store
- 引擎层 quantdesk/engine：EventBus(core) · RiskGate（订单唯一咽喉）· runner（策略子进程装载）  
  · supervisor（心跳 5s / 判死 15s / 重启上限 3 次每小时）· backtest + matching（回测与模拟共用撮合）
- IPC 层 quantdesk/ipc：ZMQ XPUB（事件下行）/ PULL（订单意图上行）+ 心跳信道
- 适配器层 quantdesk/adapters：base/（两个 ABC）+ data/（qmt_xtdata · tdx_aidata · ths_ifind ·  
  akshare_feed · tushare_feed · federated 路由降级）+ brokers/（paper_broker · qmt_broker ·  
  gm_broker · easytrader_broker）+ 注册表自动扫描
- 存储层 quantdesk/store：db.py(SQLite WAL) / models_db.py（strategies / orders / trades /  
  positions_snapshot / watchlist / compliance / backtest_runs）/ parquet_store.py  
  （data/parquet/{market}/{symbol}/{freq}/{YYYY}.parquet，日线按年、分钟线按月分文件）
- 用户策略 strategies/：只依赖 BaseStrategy；引擎 importlib 动态加载

## 核心契约（必须原样实现，不得改名或删减字段）

统一证券代码：`"600000.SH"` / `"000001.SZ"`（各源格式互转只发生在 core/symbols.py 与适配器内部）。  
K 线统一列：`["dt","open","high","low","close","volume","amount"]`，dt 为 datetime64[ns] 升序。  
事件类型：TICK · BAR · ORDER · TRADE · POSITION · ACCOUNT · LOG · TIMER · RISK

DataFeedAdapter（adapters/base/datafeed.py）：  
name: str / capabilities: frozenset[FeedCapability]  
connect() / disconnect() / health_check() -> bool  
get_history(symbol, freq, start, end, adjust=AdjustType.FORWARD) -> pd.DataFrame  
subscribe_realtime(symbols: list[str], callback: Callable[[Tick | Bar], None])  # 增量追加  
unsubscribe_realtime(symbols) / get_financial_data(symbol, report_type="income") -> pd.DataFrame  
约定：同步风格（调用方放线程池）；callback 内只入队禁止阻塞；  
单次调用内部最多重试 1 次，重试风暴与降级归 FederatedFeed 管。

BrokerAdapter（adapters/base/broker.py）：  
name: str / is_paper: bool  
connect(\*\*credentials) / disconnect()  
place_order(order: Order) -> Order   # 返回补齐 broker_order_id 的快照；失败抛 BrokerError  
cancel_order(order_id) -> bool       # 返回是否受理；最终状态以回报为准  
get_position(symbol=None) -> list[Position]   # available 必须遵守 T+1（当日买入为 0）  
get_account() -> Account / get_orders(status=None) -> list[Order]  
get_trades(start=None) -> list[Trade] / poll()  # 无回调能力通道（easytrader）实现轮询

RiskGate（engine/riskgate.py）：  
submit(intent: Order) -> Order   # 三道闸唯一入口  
confirm(order_id) / reject(order_id)  # 二次确认回调（前端按钮 → API → 此处）

核心数据模型（core/models.py，dataclass，字段不得删减）：  
Bar{symbol, freq, dt, open, high, low, close, volume, amount}  
Tick{symbol, dt, last, open, high, low, volume, amount, bids: [(price,vol)]×5, asks: [(price,vol)]×5}  
Order{order_id, broker_order_id?, strategy_id, symbol, side, price, order_type, volume,  
status, filled_volume, avg_filled_price, message, created_at, updated_at}  
OrderStatus: PENDING_CONFIRM · SUBMITTED · PART_FILLED · FILLED · CANCELLED ·  
REJECTED · FAILED · EXPIRED_CONFIRM  
Position{symbol, volume, available, avg_cost, market_value, strategy_id}  
Trade{trade_id, order_id, broker_trade_id?, symbol, side, price, volume, amount, traded_at}  
Account{total_asset, cash, market_value, frozen, updated_at}

## 代码与工作流约定

- 测试先行：每日 DoD 中的用例必须落 tests/；每完成一个模块跑 pytest，不留红测试过夜。
- 提交纪律：每完成一个小任务即 git commit，信息格式 `D<N>-<序号>: <做了什么>`。
- 日度提示词开头的【状态检查】必须认真执行：昨日 DoD 缺失先报告（含顺延预案）再继续，  
  不跳步。
- 外部调用（数据源/券商）必须带超时、结构化异常、日志；回调线程只入队。
- 日志脱敏：账号/token 只打前 4 位 + \*\*\*。
- 中文 UI 文案；代码注释可中文；标识符一律英文。
- 时间戳统一存 INTEGER 毫秒（UTC），展示层再本地化。

## 当前项目状态（每日更新此节）

2026-11-04 · W2–W6（D06–D30）连续执行完成：撮合引擎 / 回测引擎（无未来函数）/
指标与回测 API / 模拟盘（tick 撮合+SQLite 回放重启恢复）/ 回测·行情·监控·设置·合规·数据前端 /
ZMQ IPC 桥（往返 P95≈0.55ms）/ 守护进程+生命周期门禁 / RiskGate 三道闸全量 /
二次确认流 / 端到端验收 / 数据源口径审计 / 桌面壳+启动.bat。pytest 236 用例全绿
（network 默认跳过）。tag：v1.0.0（D20）→ v1.1.0（D30）。

- M1 部分（AKShare vs Tushare 交叉对数待 token：`scripts/verify_sources.py` 就绪，
  配置 QUANTDESK_TUSHARE_TOKEN 后重跑）；M2 达成；M3 部分（IPC/守护达成，实时驱动待 QMT）；
  M4 达成（v1.0.0）；M5 未启动（需用户在场）；M6 可交付项达成（v1.1.0）。
- **关键发现（P1）**：东财(AKShare) qfq 口径日收益分离（10/10 标的中位 0.08%~0.25%，
  601899 局部 4.6× 放大）——不能作精确回测基准；Tushare 本地精确 qfq 待 token 切主口径。
  详见 `docs/audit_order_path.md` 与 `data/logs/source_align_audit_*.csv`。
- 顺延（外部前置未具备）：D13-D15（QMT 权限/通达信 SDK）、D21-D26（实盘通道，
  需用户在场/掘金/iFinD/easytrader）。恢复路径见 README 权限指引表。
- 实测教训：高负载下子进程 spawn+import 可达十几秒（守护测试等待窗口已放宽；
  supervisor 首次心跳前不做心跳判死）；RateLimiter 已修睡眠唤醒 undersleep 缺陷。
- 下一日（权限就位后）：QMT 行情接入（D13 卡片）→ 实盘通道（D21-23 卡片，只读起步）；
  token 就位后先跑 verify_sources.py 交叉对数。
