# QuantDesk 开发提示词全套（总提示词 + 30 天日度提示词）

> 配套文档（同目录）：《AGENTS.md》项目宪法（第 1 天复制进仓库）、《量化交易系统架构师提示词.md》架构设计与里程碑方案、《量化交易架构师Pro提示词包.md》参数化通用版（跨项目复用 + T1–T10 任务模板）。

## 〇、使用机制（先读这个）

这套提示词按 AI 编码工具的**无状态特性**设计——每天开新会话，AI 没有昨天的记忆。因此采用「**宪法文件 + 日度指令**」两层结构：

```
第 1 天：新建代码仓库后，把同目录的《AGENTS.md》复制到仓库根目录（或把下方【总提示词】
         发给 AI，让它原样写入仓库根目录 AGENTS.md 作为第一个 commit）
         ——ZCode / Claude Code / Codex 都会自动读取仓库根目录的 AGENTS.md / CLAUDE.md
第 N 天：从本文件复制【当天的日度提示词】粘贴给 AI → AI 先读 AGENTS.md 重建全局上下文，
         按【状态检查】核对昨日进度，然后开工
收工：   AI 按【自检】输出 DoD 核对表 → 追加《进度日志.md》→ git commit
```

三个设计原则（也是为什么提示词长这样）：

1. **全局约定只写一次**——放 AGENTS.md，日度提示词不重复，避免规则在 30 份提示词里版本漂移；
2. **每天以【状态检查】开头**——AI 先自验昨日 DoD 是否达成，缺项就上报（各日已内置顺延预案），防止偏差静默累积（换任何 AI 工具都能接上）；
3. **每天以【自检】收尾**——AI 交付前必须自查并给出证据，你只需人工验最终体验与资金相关操作。

两条纪律：

- **行政先行**：D1 当天提交 QMT 权限与 iFinD 试用申请（券商流程以工作日计，D13 与 W5 依赖它）；
- **没做完不跳**：第二天重发同一天的提示词即可，【状态检查】会兜住缺口。

本手册放在项目外（`F:\量化交易\`），每天复制一段。

---

## 一、总提示词（第 1 天发送，随后成为仓库根目录 `AGENTS.md`）

> 同目录已备好现成的《AGENTS.md》，第 1 天直接复制进仓库即可；下面这份内容与该文件完全一致，留作备用与查阅。

```markdown
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
- API 网关层 quantdesk/api：routes_* + ws.py（/ws/events 统一事件流）；只依赖 services
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
  connect(**credentials) / disconnect()
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
- 日志脱敏：账号/token 只打前 4 位 + ***。
- 中文 UI 文案；代码注释可中文；标识符一律英文。
- 时间戳统一存 INTEGER 毫秒（UTC），展示层再本地化。

## 当前项目状态（每日更新此节）

（第 1 天：尚无代码，从零开始。行政前置：QMT 权限 / iFinD 试用申请待提交。）
```

---

## 二、第 1 周 · 数据底座（D1–D5）

### D1 · 项目脚手架 + 核心类型（行政启动日）

```
你是 QuantDesk 的开发者。先完整阅读仓库根目录 AGENTS.md（若尚不存在，把本手册同目录
的《AGENTS.md》原样写入仓库根目录作为第一个 commit）。

【状态检查】确认这是全新项目，无既有代码。今日同时启动行政动作（见任务 7）。

【今日目标】项目骨架可启动，统一类型系统落地。
完成定义：uv run pytest 全绿；GET /api/system/health 返回 {"status":"ok","simulation_only":true}；
服务仅监听 127.0.0.1。

【任务】
1. uv init；按 AGENTS.md 架构分层建目录：quantdesk/{core,adapters/{base,data,brokers},
   engine,ipc,services,store,api} + strategies/examples + scripts + tests + frontend + desktop + data
2. pyproject 声明依赖：fastapi uvicorn[standard] pyzmq pandas pyarrow sqlmodel akshare
   tushare pytest httpx msgpack
3. quantdesk/config.py：pydantic-settings——simulation_only: bool = True、broker_channel、
   费率参数引用、data_dir
4. quantdesk/core/enums.py（Side/OrderType/OrderStatus/Frequency/AdjustType/FeedCapability）、
   models.py（Bar/Tick/Order/Trade/Position/Account，字段按宪法核心契约）、
   errors.py（AdapterError/ConnectionLostError/BrokerError/FeedCapabilityError）
5. quantdesk/app.py：最小 FastAPI + GET /api/system/health；uvicorn 启动 host="127.0.0.1"
   （写死，不提供配置项）
6. tests/test_health.py 冒烟
7. 行政动作（提醒我完成，代码不阻塞）：向券商提交 QMT 权限申请；向同花顺商务提交
   iFinD 试用申请；注册 Tushare 并核对积分档位

【关键点与坑】
- Order.order_id 字段位今天留好（引擎预生成，适配器不许自造主键）
- OrderStatus 必须含 PENDING_CONFIRM / EXPIRED_CONFIRM（D17 要用，今天进枚举）
- Position 必须区分 volume 与 available（T+1 语义）

【不做】不接任何数据源；不写引擎；不做前端。

【自检】uv run pytest 全绿；netstat -ano | findstr 8787 确认仅 127.0.0.1 监听；
AGENTS.md 已作为第一个 commit 入仓库。
```

### D2 · 代码体系 + 事件总线

```
先读 AGENTS.md（重点：核心契约的代码规范与事件类型）。

【状态检查】确认 D1 health 冒烟通过、类型系统就位。

【今日目标】统一证券代码体系 + A股规则常量 + 交易日历 + 事件总线。
完成定义：pytest tests/test_symbols.py tests/test_events.py 全绿。

【任务】
1. core/symbols.py：to_internal(code, source) / from_internal(symbol, source)——支持
   qmt "000001.SZ"、akshare "000001"、tdx "0.000001"、gm/ifind 变体；
   board_of(symbol) → MAIN/GEM/STAR/BSE
2. core/constants.py：涨跌停（主板0.10 / 创业板·科创板0.20 / ST0.05 / 北交所0.30）、
   整手规则（100 整数倍；科创板 ≥200 后 1 股递增）、费率（commission_rate=2.5e-4、
   min_commission=5、stamp_tax_sell=5e-4、transfer_fee=1e-4）——全部可被 config 覆盖
3. core/calendar.py：trade_cal 拉取缓存 SQLite 表 trading_days；is_trading_day/prev_day/next_day
4. core/events.py：EventType（TICK/BAR/ORDER/TRADE/POSITION/ACCOUNT/LOG/TIMER/RISK）+
   Event{type,data,source,ts} + EventBus（线程安全；subscribe(etype|None, handler)->sub_id；
   单 handler 异常只记日志不扩散）
5. tests/test_symbols.py ≥20 用例（五格式互转/688xxx/300xxx/非法输入）；
   test_events.py（10k 事件无丢失 / handler 异常隔离）

【关键点与坑】
- ST 判定依赖证券简称：symbols.py 预留 set_st_names(names) 注册接口，简称后续从行情源回填
- calendar 的 token 从 config 读；缺失时抛结构化错误并给注册引导，不静默

【不做】不写任何适配器实现；不做存储层。

【自检】pytest 全绿；EventBus 压测脚本通过并记录耗时。
```

### D3 · SQLite 持久化层

```
先读 AGENTS.md（重点：先落库后广播、核心数据模型字段）。

【状态检查】确认代码体系与 EventBus 就位。

【今日目标】交易数据全部落 SQLite（WAL），重启不丢。
完成定义：插入样例订单/成交/持仓 → 新起子进程读取 → 数据完整；init_db 幂等。

【任务】
1. store/db.py：SQLModel 引擎（PRAGMA journal_mode=WAL + foreign_keys=ON）；get_session()
2. store/models_db.py 七表：strategies / orders / trades / positions_snapshot / watchlist /
   compliance / backtest_runs（trading_days 由 D2 写入）
   - orders 必含 order_id(PK) / broker_order_id(索引) / strategy_id / symbol / side / price /
     volume / order_type / status / filled_volume / avg_filled_price / message / created_at / updated_at
   - status 直接存 OrderStatus 枚举值（含 pending_confirm / expired_confirm）
3. scripts/init_db.py（幂等：create_all + schema_version 版本表）
4. store/repo.py：dataclass↔DB 转换与基础 CRUD（upsert_order / append_trade /
   replace_positions / latest_positions ...）；内部单写锁
5. tests/test_store.py：写入 → multiprocessing 子进程重读比对；幂等建库

【关键点与坑】
- 时间戳统一存 INTEGER 毫秒（UTC），展示层再本地化——今天定死，前后端都遵守
- WAL 允许单写多读：所有写走 repo 单写锁，不要到处开连接

【不做】不做 Parquet；不做任何适配器。

【自检】pytest 全绿；重启进程两次数据完整；init_db 重复执行无报错。
```

### D4 · Parquet + 插件注册表 + AKShare

```
先读 AGENTS.md（重点：适配器模式、DataFeedAdapter 契约）。

【状态检查】确认 SQLite 层可用。

【今日目标】Parquet 历史库 + 插件注册表 + 第一个真实数据源 AKShare。
完成定义：000001.SZ 2019-2026 日线(qfq) 入库且 schema 校验通过；断点重跑无重复行。

【任务】
1. adapters/base/datafeed.py、broker.py：按宪法核心契约原样落地两个 ABC
   （含 HISTORY_COLUMNS 常量），签名一个不改
2. adapters/__init__.py：registry——启动扫描 adapters/data/ 与 adapters/brokers/，
   收集模块级 ADAPTER 类属性注册（name 冲突即报错）
3. store/parquet_store.py：路径 {data_dir}/parquet/{market}/{symbol}/{freq}/{YYYY}.parquet；
   read / write（append + 按 dt 去重）；写前 schema 校验（列与 dt 类型）
4. adapters/data/akshare_feed.py：ADAPTER=AkshareFeed——connect/health_check
   （轻量探活）、get_history（stock_zh_a_hist，adjust none/qfq/hfq 映射；列名映射层隔离
   AKShare 接口变动）、get_financial_data、capabilities={HISTORY_BAR, FINANCIAL}；
   内置 TokenBucket（1 req/s + 均匀抖动）
5. tests/test_parquet_store.py（schema/去重/增量）+ test_akshare.py（标记 network）

【关键点与坑】
- AKShare 只做历史/兜底：capabilities 不含 REALTIME_*，后续路由层据此排除它做实时
- 列名映射写成适配器文件顶部常量——接口变动只改这一个文件（宪法适配器模式）

【不做】不写 Tushare；不做 federated 路由；不做前端页面。

【自检】pytest 全绿；拉数 → 重启 → 再拉无重复行；连续 200 请求观察无封禁。
```

### D5 · Tushare + FederatedFeed + 数据服务（本周里程碑 M1）

```
先读 AGENTS.md（重点：FederatedFeed 是 services 唯一直接使用的行情源）。

【状态检查】确认 AKShare 可拉数且 Parquet 就位；Tushare token 已配置（没有先配 config）。

【今日目标】第二数据源 + 路由降级 + 下载服务 → M1。
完成定义：50 只标的批量入库 + 缺口报告正确；M1 对数：AKShare vs Tushare 误差 ≤ 0.1%
（除权日单列）。

【任务】
1. adapters/data/tushare_feed.py：pro.daily 按 trade_date 循环批量（省积分）+ pro.adj_factor；
   按积分档配置最小请求间隔限速
2. adapters/data/federated.py：FederatedFeed(DataFeedAdapter)——get_history：
   本地 Parquet 命中直接返回 → miss 按优先级外呼 → 成功结果回写 Parquet；
   get_financial_data 按降级链逐源尝试（FeedCapabilityError → 下一个源）
3. services/data_store.py：batch_download(symbols, freq, start, end)（进度回调）+
   gap_report（对照 trading_days 求缺口）
4. scripts/download_history.py CLI：--symbols --freq --start --end
5. 前端骨架：pnpm create vite frontend --template react-ts；路由 7 空页
   （market/strategy/backtest/monitor/data/compliance/settings）；
   src/api/client.ts（axios 实例 + WebSocket 封装带指数退避重连）；
   vite.config.ts 代理 /api → 127.0.0.1:8787
6. M1 对数脚本 scripts/verify_sources.py（输出 CSV 到 data/logs/）

【关键点与坑】
- FederatedFeed 之外禁止任何代码直接 import 具体数据源适配器
- 对数在除权日必有差异：verify_sources 用 calendar 找除权日单列，不算失败

【不做】不做实时订阅；不做回测。

【自检】pytest 全绿；M1 剧本跑通、对数报告存档；pnpm dev 打开 7 页壳。
本周验收：数据底座（双源 + Parquet + SQLite + 前端壳）达成。
```

---

## 三、第 2 周 · 回测 + 模拟盘（D6–D10）

### D6 · 撮合引擎

```
先读 AGENTS.md（重点：价格口径、费率参数化）。

【状态检查】确认 M1 对数报告已存档。

【今日目标】撮合引擎（回测与模拟盘共用同一实现）。
完成定义：test_matching ≥15 用例全绿；一笔含印花税的卖出手工核算一致。

【任务】
1. engine/matching.py：
   - match_bar(order, bar)：limit buy 成交条件 bar.low ≤ price / sell bar.high ≥ price；
     成交价 = min(price, bar.open)（开盘价优先）；成交量受 bar.volume 上限约束（可配置比例）
     → 支持部分成交
   - match_tick(order, tick)：buy last ≤ price / sell last ≥ price；市价单按对手一档价 + slippage
   - FeeCalculator：读 constants——佣金 max(amount*rate, min)；印花税仅卖出；过户费双边
   - T+1：成交后 Position.available 不增加（当日买入不可卖），次日由 calendar 驱动释放
2. tests/test_matching.py：≥15 用例——当日买不可卖 / 佣金 min5 边界 / 印花税单边 /
   部分成交 / 市价单滑点 / 涨停价挂买单成交边界

【关键点与坑】
- 撮合是纯函数：只改订单与持仓状态，不落库不发事件（调用方负责）——回测与模拟盘才能复用

【不做】不做回测循环；不做 API。

【自检】pytest 全绿；手工核算案例与过程写进测试注释。
```

### D7 · 回测引擎

```
先读 AGENTS.md（重点：策略隔离、BaseStrategy 契约）。

【状态检查】确认 matching 全绿。

【今日目标】回测事件循环跑通。
完成定义：fake 策略跑 1000 bar 无事件丢失，bar 时间严格单调，现金守恒断言通过。

【任务】
1. engine/strategy_base.py：StrategyContext（抽象：subscribe/history/buy/sell/cancel/
   get_position/get_account/log + run_mode 属性）与 BaseStrategy（on_init/on_bar 必须实现；
   on_tick/on_order/on_trade/on_timer/on_stop 可选）——签名按宪法
2. engine/backtest.py：BacktestConfig{symbols, freq, start, end, initial_cash, slippage}；
   HistoricalDataFeed（包装 parquet_store 按时间升序吐 Bar）；虚拟时钟循环：
   bar → matching → 策略 on_bar → 记 equity
3. 回测版 StrategyContext：history 直读 parquet_store；buy/sell → matching（paper 路径）
4. tests/test_backtest.py：fake 双均线策略 1000 bar；断言事件数 / 时间单调 /
   现金守恒（现金+市值 = 初始资金 ± 费用）

【关键点与坑】
- 现金守恒是回测正确性的锚，写进测试
- history() 返回序列是否含当前 bar 要在 docstring 写明（回测含、实盘同样语义——同构要求）

【不做】不做指标计算；不做 API 与前端。

【自检】pytest 全绿；equity 序列导出 CSV 抽查首尾值。
```

### D8 · 指标 + 示例策略 + 回测 API

```
先读 AGENTS.md（重点：策略文件位置与动态加载）。

【状态检查】确认回测循环可用。

【今日目标】绩效指标 + 示例策略 + 回测 REST API。
完成定义：dual_ma 回测 600000.SH 2020-2026 返回完整指标 JSON；与 backtrader 同参数
数量级一致（差异写说明）。

【任务】
1. services/backtest_svc.py：compute_metrics(equity, trades)——total_return /
   annual(252) / max_drawdown / sharpe(rf 参数化) / win_rate / profit_factor / trade_count
2. strategies/examples/dual_ma.py：params{fast:5, slow:20}；history 算均线 → 金叉买/死叉卖；
   卖前查 available（T+1 感知）；grid.py 骨架
3. engine/runner.py 轻量版：importlib 动态加载 strategies/ 下策略文件（校验 BaseStrategy 子类）
4. api/routes_backtest.py：POST /api/backtest（同步小任务版，参数校验）+
   GET /api/backtest/{id}（结果读 backtest_runs 表）
5. 对照脚本 scripts/compare_backtrader.py（独立 venv 或 optional 依赖，不入主依赖）

【关键点与坑】
- 策略 import 失败必须给结构化报错（文件/类名/原因），不能 500 裸抛
- backtrader 对照结论与差异原因写进 backtest_runs.remark 与进度日志

【不做】不做前端页面；不做模拟盘实时版。

【自检】pytest 全绿；curl POST /api/backtest 拿到完整指标 JSON。
```

### D9 · 模拟盘通道 + RiskGate 最小版

```
先读 AGENTS.md（重点：订单三道闸第①道、先落库后广播）。

【状态检查】确认回测 API 可用。

【今日目标】内置模拟盘上线（与实盘共用同一套库表与监控数据面）。
完成定义：API 下单 → 撮合 → 落库 → 查询闭环；重启后持仓/资金恢复；
开关 ON 时实盘通道缺位不炸。

【任务】
1. adapters/brokers/paper_broker.py：ADAPTER=PaperBroker（is_paper=True）——实现
   BrokerAdapter 全部抽象方法；place_order → matching → 生成 Trade → 更新
   Position（available 按 T+1）/ Account；全部写宪法指定的表；订单状态机完整
2. engine/riskgate.py 最小版：submit(intent) —— simulation_only=True → 全部路由 paper；
   否则（实盘 broker 未接）直接 REJECTED（fail-closed）
3. services/trading.py：下单/撤单/订单列表/持仓/资金 API；先落库后广播顺序强制
4. tests/test_paper_broker.py：下单成交闭环 / T+1 available / 重启恢复（子进程重读）

【关键点与坑】
- paper 与未来实盘共用同一套表：strategy_id 语义今天定死（"manual" = 手工单）
- RiskGate 是唯一入口：API 层不许绕过它直接调 broker（写一条守卫测试）

【不做】不做确认弹窗（D17）；不接真实行情 tick（用定时 spot 模拟）。

【自检】pytest 全绿；curl 下单 → 查询 → 重启 → 查询一致；绕过 RiskGate 的调用被守卫测试拦截。
```

### D10 · 回测前端（本周里程碑 M2）

```
先读 AGENTS.md（重点：UI 栈纪律）。

【状态检查】确认模拟盘闭环可用。

【今日目标】浏览器完成回测闭环 → M2。
完成定义：页面上跑 dual_ma 回测看到收益曲线与指标；模拟盘下一单 → 落库 → 重启恢复。

【任务】
1. frontend/src/pages/backtest/BacktestPage.tsx：策略选择（GET /api/strategies）/ 参数表单 /
   运行按钮 / EquityCurve（ECharts，封装 components/charts/EquityCurve.tsx）/ 指标卡片 /
   成交明细表
2. WS 日志流接入：回测过程 LOG 事件实时显示
3. api/routes_strategy.py：GET /api/strategies（扫描 strategies/ 返回清单与默认参数）
4. git tag m2-backtest-paper-loop；AGENTS.md「当前项目状态」更新

【关键点与坑】
- ECharts 按需引入（echarts/core + LineChart），不整包
- 指标格式化：百分比/回撤 2 位小数，夏普 3 位

【不做】不做行情面板；不做策略编辑器。

【自检】pnpm build 通过；M2 剧本逐项过并写入进度日志。
本周验收：回测 + 模拟盘闭环（原 v1 内核）达成。
```

---

## 四、第 3 周 · 策略运行时 + 实时行情（D11–D15）

### D11 · ZMQ IPC 桥

```
先读 AGENTS.md（重点：策略隔离、IPC 层职责）。

【状态检查】确认 M2 已达成。

【今日目标】策略子进程与主进程 IPC 通道建立。
完成定义：子进程收到主进程 bar 事件并回调 on_bar；本机事件往返 P95 < 5ms。

【任务】
1. ipc/bridge.py：
   - 主进程：ZMQ XPUB（事件广播，topic=EventType）+ PULL（收策略订单意图 → 交 RiskGate）
   - 子进程端 Client：SUB（按需订阅 topic）+ PUSH（发订单意图）+ 心跳（5s 一次）
   - 序列化：msgpack + {topic, payload} 信封；core/serde.py 统一 dataclass/datetime 编解码
2. engine/runner.py：StrategyProcess——multiprocessing(spawn) 目标函数：
   importlib 加载策略文件 → 实例化（注入 IPC 版 StrategyContext）→ 事件循环分发
   on_bar/on_tick/... ；子进程崩溃向上抛
3. 基准脚本 scripts/bench_ipc.py：10k 事件往返统计 P50/P95/P99
4. 集成测试：主进程发 bar → 子进程 on_bar 触发 → 回发订单意图 → RiskGate 收到

【关键点与坑】
- Windows spawn：策略加载必须在子进程函数内，模块入口加 __main__ 保护
- 序列化 datetime 用毫秒整数，别让 msgpack 报 unknown object

【不做】不做 supervisor（明天）；不接真实行情。

【自检】集成测试全绿；bench P95 < 5ms 结果记录进进度日志。
```

### D12 · Supervisor + 生命周期门禁 + 策略工作台

```
先读 AGENTS.md（重点：策略隔离、先落库后广播）。

【状态检查】确认 IPC 双向通路可用。

【今日目标】崩溃隔离闭环 + 生命周期门禁 + 策略工作台前端。
完成定义：kill -9 策略进程 15s 内自动重启且状态从 SQLite 恢复；无模拟记录时实盘启动 403。

【任务】
1. engine/supervisor.py：心跳 5s / 判死 15s；自动重启（滚动窗口 3 次/小时，
   超限标 failed 待人工）；崩溃前最后 10 条日志落 data/logs/crash/
2. services/strategy_mgr.py：策略 CRUD（strategies/ 文件读写）+ 启/停 API +
   生命周期门禁——start(mode="live") 前查运行记录，无 completed 模拟记录 → 403 带原因
3. 重启恢复：重放订阅列表 + 从 SQLite 注入当前持仓快照（防状态漂移）
4. 前端 strategy/StrategyPage.tsx：Monaco（@monaco-editor/react，python）+ 文件树 +
   启停按钮 + 日志流面板（WS 流式）

【关键点与坑】
- 策略 import 失败要结构化回前端（文件/类名/异常栈），Monaco 只管编辑不管校验
- 策略停止/崩溃都必须注销订阅引用，防泄漏

【不做】不做参数热更新；不接实时行情。

【自检】pytest 全绿（含 403 门禁用例）；手动 kill -9 实测重启与状态恢复。
```

### D13 · QMT 行情·历史（接入验证日 1）

```
先读 AGENTS.md（重点：适配器合同、交叉对数红线）。

【状态检查】关键前置：券商 QMT 权限已生效、miniQMT 已登录、userdata 路径已知。
若未生效 → 顺延预案：今天改做 D14 中不依赖本日的部分（订阅封装可先写）+
D15 的 SDK 安装调研；D13 顺延并在进度日志预警（影响 M3 与 W5）。

【今日目标】QMT 历史数据接入并通过交叉对数。
完成定义：600000.SH 日线+分钟线取得；与已入库 AKShare 数据误差 ≤ 0.1%（除权日单列）；
二次 download 秒级返回。

【任务】
1. uv add xtquant（或券商提供的 whl）
2. adapters/data/qmt_xtdata.py：ADAPTER=QmtXtdataFeed——connect（xtdata 自动发现本机
   miniQMT；失败抛 ConnectionLostError 带引导「请登录 miniQMT 客户端」）/
   health_check（get_stock_list_in_sector('沪深A股') 探活，结果缓存 60s）
3. get_history：先 xtdata.download_history_data（增量落 QMT 本地）→ get_market_data_ex 读取；
   dividend_type 映射（front→qfq / back→hfq / none→none；front_ratio/back_ratio 不支持时
   回退 front 并告警日志）；freq 映射（1m/5m/15m/30m/60m/1d）
4. capabilities={HISTORY_BAR}（实时明天加）；交叉对数脚本 scripts/verify_qmt.py
   （输出格式与 verify_sources 一致）

【关键点与坑】
- QMT 代码格式与内部规范一致（"600000.SH"），零转换
- download_history_data 是阻塞的：放线程池执行，别卡服务

【不做】不接实时订阅；不接交易。

【自检】pytest 全绿（mock xtquant 单测 + network 标记用例）；对数报告存档 data/logs/。
```

### D14 · QMT 行情·实时 + 断线自愈

```
先读 AGENTS.md（重点：适配器合同的重连重放义务、回调线程纪律）。

【状态检查】确认 QMT 历史可用；miniQMT 在线。

【今日目标】实时行情接入，断线可自愈。
完成定义：订阅 10 标的实时 tick；手动断开 miniQMT → 状态灯红 → 重连后 10s 内自动恢复推送；
两个策略订阅同一标的外部仅一份订阅。

【任务】
1. qmt_xtdata 补齐：subscribe_realtime（subscribe_quote 逐标的）——回调线程只入队
   （queue.Queue），专用工作线程归一化 Tick（五档 bids/asks 解析）→ callback；
   unsubscribe_realtime
2. 断线自愈：health_check 周期探活（60s）+ 回调异常检测 → 标记 disconnected →
   EVENT_RISK 广播 → 指数退避重连（1s/2s/4s...cap 60s）→ 成功后重放订阅列表
   （宪法：重连不会自动恢复订阅）
3. federated.py 补实时路由：qmt 为主实时源 + 订阅引用计数
   （dict[symbol, set[subscriber]]，计数归零才 unsubscribe）
4. services/market_data.py：快照缓存（每 symbol 最新 Tick）+ WS 广播
   （/ws/events，tick 节流 4/s 合并）
5. tests/test_resubscribe.py：模拟 disconnect → reconnect → 断言订阅重放被调用

【关键点与坑】
- 回调里禁止任何阻塞/重逻辑——只入队（QMT 回调线程契约）
- 引用计数清理时机：策略停止/崩溃都要注销

【不做】不做通达信；不接交易。

【自检】pytest 全绿；手动退出 miniQMT 实测断线→重连→恢复全流程；前端临时页接 WS 验证推送。
```

### D15 · 通达信 TdxAiData + 降级链（本周里程碑 M3）

```
先读 AGENTS.md（重点：适配器模式、ADR-5 实时宁停勿错）。

【状态检查】确认 QMT 实时可用；通达信官方 SDK 已安装。

【今日目标】第二实时源通达信 + 财务降级链 → M3。
完成定义：tdx 日线与 qmt 对数误差 ≤ 0.1%；杀连接恢复后订阅自动重放；
dual_ma 以 paper 模式在子进程被实时 bar 驱动产生模拟交易。

【任务】
1. adapters/data/tdx_aidata.py：ADAPTER=TdxAiDataFeed——单例 + 内部请求队列
   （工作线程串行消费，SDK 非线程安全）；get_history（K线）+ 分笔独立方法；
   "0.000001" ↔ "000001.SZ" 互转走 symbols.py；SDK 重连后手动重放订阅（重连钩子）
2. federated.get_financial_data 降级链落定：tushare → akshare（iFinD W6 插最前）
3. 串联验证：strategy_mgr 启动 dual_ma（paper 模式）→ 实时 bar 驱动 → 模拟成交落库 →
   前端日志流可见
4. git tag m3-runtime-realtime；AGENTS.md「当前项目状态」更新

【关键点与坑】
- tdx 与 qmt 同时在线时，federated 实时仍只走主源（ADR-5），tdx 作为分笔补充按需调用

【不做】不接 iFinD；不接实盘。

【自检】pytest 全绿；M3 剧本逐项过（含 kill 策略进程自动恢复）并记录。
本周验收：策略隔离运行 + QMT/通达信双源接入达成。
```

---

## 五、第 4 周 · 安全闸 → v1.0（D16–D20）

### D16 · RiskGate 规则校验

```
先读 AGENTS.md（重点：三道闸第②道、价格口径与涨跌停兜底）。

【状态检查】确认 M3 已达成。

【今日目标】第 2 道闸：A 股规则校验全量落地。
完成定义：test_riskgate ≥12 边界用例全绿；每条拦截 order.message 可查原因。

【任务】
1. engine/riskgate.py validate(intent) -> list[str]：
   - T+1：卖出量 ≤ positions.available（读最新快照）
   - 整手：board_of + constants（MAIN/GEM 100 整数倍；STAR ≥200 后 1 股递增）
   - 价格：委托价 ∈ [跌停, 涨停]——昨收取「未复权日线」最后一根（parquet_store,
     AdjustType.NONE），按板块 ±10%/±20%/ST±5%；复权价下单会被此拦截（防误用兜底）
   - 单票市值 ≤ config.max_position_pct × 总资产；资金充足（cash ≥ 预估金额+费用）
   - 日委托数 ≤ config.max_daily_orders（按策略计）
2. 拦截路径：status=REJECTED + message=规则名+详情 → EVENT_RISK 广播 → 落库
3. tests/test_riskgate.py：≥12 用例（超 available / 碎股 / 科创板 199 股 / 价格越界 /
   复权价 / 仓位超限 / 资金不足 / 超频）

【关键点与坑】
- 昨收必须取未复权价——取错口径会让涨跌停区间整体漂移

【不做】不做二次确认（明天）；不接实盘 broker。

【自检】pytest 全绿；手工构造违规单 → API 返回 REJECTED 且 message 明确。
```

### D17 · 二次确认流（服务端强制）

```
先读 AGENTS.md（重点：三道闸第③道、先落库后广播、fail-closed）。

【状态检查】确认规则校验全绿。

【今日目标】第 3 道闸：实盘二次确认闭环（服务端强制，前端只是表现层）。
完成定义：确认中 kill 主进程 → 重启后待确认单仍在且可处理；超时/取消/断线拒单用例通过。

【任务】
1. riskgate 确认状态机：实盘路径校验通过 → order.status=PENDING_CONFIRM 先落库 →
   WS 推送 confirm_request{order, 倒计时} → 等 confirm(order_id)/reject(order_id) API
   或超时 → 10s 无响应 EXPIRED_CONFIRM 落库+广播
2. fail-closed：submit 时检查主实盘通道 health_check——断线状态直接 REJECTED
   （message=通道离线），不排队不重试
3. tests/mocks/mock_live_broker.py：可脚本化的假实盘通道（回报延迟/拒单/断线可配），
   供本日与 W5 前全部实盘路径测试
4. api/routes_trading.py：POST /api/orders/{id}/confirm、/reject
5. 前端 components/ConfirmDialog.tsx：10s 倒计时 + 策略名/代码/方向/价量/通道红字

【关键点与坑】
- 超时判定以落库的 created_at 为准（重启后补算），不能只靠内存 timer
- 穿透测试：curl 直连 API 绕过前端下单 → 必须仍被闸拦住要求确认（写成用例）

【不做】不接真实 QMT 交易（W5）。

【自检】pytest 全绿（含穿透测试与重启恢复用例）；前端演练一次完整确认流。
```

### D18 · 行情面板前端

```
先读 AGENTS.md（重点：UI 栈纪律）。

【状态检查】确认三道闸用例全绿。

【今日目标】行情可视化达到日常看盘可用。
完成定义：实时 tick 到图端到端 < 1s；切周期请求走 Parquet 缓存不重复外呼。

【任务】
1. components/charts/KLine.tsx：lightweight-charts——日/周/月切换（周/月由日线聚合）/
   成交量副图 / 十字光标；增量更新用 series.update()
2. 分时图（area series，当日分钟聚合）+ OrderBook.tsx 五档
   （Tick.bids/asks 渲染表格 + 买卖压力条）
3. Watchlist.tsx：自选股 CRUD（watchlist 表 + api/routes_market.py）
4. services/market_data.py 补：K 线查询 API（Parquet 优先 + 当日实时 bar 拼接）

【关键点与坑】
- lightweight-charts 别整段 setData 重刷，增量 update
- 周线聚合按 A 股周界定（周一起始），不用库默认周

【不做】不做监控页；不做设置页。

【自检】pnpm build 通过；打点实测 tick→图延迟 < 1s 并记录。
```

### D19 · 监控页 + 全局安全开关

```
先读 AGENTS.md（重点：三道闸第①道全局开关的服务端强制）。

【状态检查】确认行情面板可用。

【今日目标】监控页与全局模拟盘开关。
完成定义：开关切换 WS 即时生效；OFF（模拟）时所有下单路由 paper，实盘 broker 缺位不炸。

【任务】
1. MonitorPage.tsx：持仓 / 当日委托 / 成交记录 / 资金概览（REST 轮询 2s + WS 事件增量刷新）
   + 通道状态灯（feed/broker health）
2. SettingsPage.tsx：全局模拟盘开关（OFF 时展示影响清单 + 要求输入「确认切实盘」文本）/
   通道选择 / 费率参数 / 限速参数
3. 后端：设置持久化 + PUT /api/settings + 变更 WS 广播 settings_changed
4. tests/test_settings_flow.py：开关切换 → 后续下单路由断言（含 broker 缺位场景）

【关键点与坑】
- 「确认切实盘」文本校验在服务端做，前端只是交互
- RiskGate 每单读最新开关值，不缓存（运行时热切）

【不做】不做报备清单页（D28）；不接实盘。

【自检】pytest 全绿；前端切开关 → 下单路由实测；实盘缺位场景不报 500。
```

### D20 · 端到端回归（本周里程碑 M4 = v1.0）

```
先读 AGENTS.md 全文，做发布前总检查。

【状态检查】确认 D19 全绿。

【今日目标】v1.0 全链路验收发布。
完成定义：全链路剧本通过；三道闸/重启不丢/长时间模拟运行无未捕获异常；tag v1.0.0。

【任务】
1. 全链路剧本（逐项记录）：下载数据 → 回测 → dual_ma 模拟盘以实时行情运行 ≥2 小时 →
   持仓/费用核算（抽 3 笔逐项对佣金/印花税/过户费）→ 中途 kill 策略进程验证恢复 →
   重启主程序验证数据完整（含待确认单）
2. 回归：pytest 全量 + pnpm build + 手工冒烟清单
3. 修复暴露问题（只修阻断级，其余记 docs/backlog.md）
4. 更新 AGENTS.md「当前项目状态」为 v1.0；git tag v1.0.0

【不做】任何新功能。

【自检】v1.0 验收报告（DoD 核对表）写入进度日志。
本周验收：模拟盘 + 回测 + 安全闸 + 持久化全闭环。
```

---

## 六、第 5 周 · 实盘通道（D21–D25）

### D21 · QMT 交易·连接与查询（只读日）

```
先读 AGENTS.md（重点：适配器合同、敏感信息纪律）。

【状态检查】v1.0 已达成；用户在场（可登录券商账户）；miniQMT 交易权限可用。

【今日目标】QMT 交易通道打通——只查询，不下单。
完成定义：实盘资金/持仓与券商 APP 人工核对一致；重启程序 session 不冲突。

【任务】
1. uv add xtquant（D13 已装则跳过）
2. adapters/brokers/qmt_broker.py：ADAPTER=QmtBroker——XtQuantTrader(userdata_path,
   session_id=日期+随机后缀) connect + StockAccount(account_id)；connect 失败抛
   ConnectionLostError（引导检查 miniQMT 登录）
3. 回调类（继承 XtQuantTraderCallback）：OnStockOrder / OnStockTrade → 归一化 Order/Trade
   → 回调线程只入队 → 工作线程发 EventBus；disconnected → ConnectionLostError → EVENT_RISK
4. get_account / get_position / get_orders / get_trades（同步查询封装）
5. 对数脚本 scripts/verify_qmt_account.py：查询结果打印，供人工与券商 APP 核对
6. **place_order 本日抛 NotImplementedError 占位**

【关键点与坑】
- session_id 每次启动不同，防冲突
- QMT 交易回调线程与 xtdata 行情回调线程是两个东西，各自只入队

【不做】任何下单/撤单（明天，需用户在场 + 小资金约定）。

【自检】人工核对记录（金额/持仓截图路径）写入进度日志；重启两次查询正常。
```

### D22 · QMT 交易·下单与对账（需用户在场 + 小资金）

```
先读 AGENTS.md（重点：三道闸、order_remark 全链路追踪、先落库后广播）。

【状态检查】D21 核对一致；用户在场；已约定测试资金（建议 100 股低价股）。

【今日目标】实盘下单全链路（经三道闸）真实验证。
完成定义：挂「不成交价」限价单 → 确认弹窗 → 券商 APP 可见 → 撤单成功；
委托-回报-落库-前端全链路 < 3s；对账无差异。

【任务】
1. place_order 实现：order_stock(account, code, order_type 映射, volume, price_type, price,
   strategy_name=策略ID, order_remark=本地 order_id)——同步返回后以回调为准更新状态
2. 市价类型映射表：dict 常量放适配器顶部，注释写明沪深市价申报类型差异
3. cancel_order：以回报为准更新状态
4. 对账轮询：每 30s 全量 get_orders/get_trades/get_position 与 SQLite 对账，
   差异 → EVENT_RISK 告警 + 日志（差异即事故，宁可误报）
5. 演练剧本：RiskGate 完整路径（规则校验 → 确认弹窗 → place_order）→ APP 核对 → 撤单 → 对账

【关键点与坑】
- order_remark=order_id 是回报→本地订单关联的唯一锚，不可省略
- 实盘演练只做限价单；市价单类型映射留给单测覆盖

【不做】实盘策略自动跑（明天）；多笔并发下单。

【自检】全链路打点 < 3s；撤单成功；对账零差异记录进日志与进度日志。
```

### D23 · fail-closed 演练 + 策略切实盘

```
先读 AGENTS.md（重点：fail-closed、生命周期门禁）。

【状态检查】D22 全链路验证通过。

【今日目标】异常路径验证 + 策略首次实盘运行（小资金，每单人工确认）。
完成定义：断开 miniQMT → 下一单被拒（状态明确）；恢复 → 对账一致；
dual_ma live 模式跑半日、每单弹确认。

【任务】
1. fail-closed 演练：运行中手动退出 miniQMT → 验证 disconnected → EVENT_RISK →
   UI 红色横幅 → submit 直接拒单；重启 miniQMT → 指数退避重连 → 全量对账 → 状态灯恢复
2. dual_ma 切 live 模式运行半日：每单必弹确认，用户逐单确认；
   期间委托/成交/持仓与券商 APP 实时核对
3. 事件完整性抽查：当日全部委托/成交在 SQLite 可回放（按时间轴重放脚本）

【关键点与坑】
- 实盘首日：低频策略 + 最小仓位——今天验证的是链路，不是收益

【不做】加仓 / 多策略并行 / 无人值守运行。

【自检】fail-closed 用例 + 半日运行记录写入进度日志；AGENTS.md「当前项目状态」更新。
```

### D24 · 掘金通道（备选实盘）

```
先读 AGENTS.md（重点：适配器合同）。

【状态检查】D23 完成；掘金终端已注册并绑定券商（没有 → 本日与 D25 对调，D24 顺延）。

【今日目标】备选实盘通道掘金可用（先仿真账户）。
完成定义：掘金仿真下单成交闭环 < 3s；断开终端 → fail-closed 行为与 QMT 通道一致。

【任务】
1. 安装掘金官方 SDK（包名以官方文档为准）
2. adapters/brokers/gm_broker.py：ADAPTER=GmBroker——set_token（token 走加密 config）；
   专用线程跑最小 live session（订阅一个低活跃标的心跳）维持 SDK 事件循环；
   下单/撤单请求经 queue 投递进该循环执行（SDK 交易函数须在其事件循环内调用）；
   回报桥接 EventBus
3. 仿真账户先测：下单/撤单/查询
4. fail-closed 验证：复用 RiskGate 断线路径（无需新代码，演练确认）

【关键点与坑】
- 不要在主线程直接调 gm 下单函数——会与其事件循环冲突
- 仿真/实盘账户切换走 config，不写死

【不做】easytrader（明天）；实盘资金演练（仿真足够）。

【自检】pytest（mock 版）全绿；仿真闭环实测；fail-closed 对齐验证记录。
```

### D25 · easytrader 通道（本周里程碑 M5）

```
先读 AGENTS.md（重点：合规红线——非官方通道默认禁用）。

【状态检查】D24 完成；同花顺客户端已安装且版本号已记录到设置页。

【今日目标】非官方通道就绪但默认禁用 → M5。
完成定义：默认禁用时适配器不注册、API 不可见；启用后可查余额/持仓（不下单）；
异常截图机制验证。

【任务】
1. uv add easytrader
2. adapters/brokers/easytrader_broker.py：ADAPTER=EasytraderBroker——use('ths') +
   connect(同花顺独立下单 xiadan.exe 路径)；查询走 poll()（balance/position/
   today_entrusts/today_trades）；place_order 有实现但通道禁用时注册表跳过
3. 启用开关：settings 里 broker.easytrader.enabled=False 默认；启用需输入确认文本；
   前端通道卡片红字「非官方接口，稳定性低」+ 客户端锁版本提示
4. 异常处理：pywinauto 找不到控件 → 截图存 data/logs/easytrader/ + AdapterError
5. git tag m5-live-channels；AGENTS.md「当前项目状态」更新

【关键点与坑】
- 从注册表源头跳过 disabled 适配器，保证 API 不可见（不是前端藏按钮）
- 客户端升级会破坏控件：README 与设置页都写明锁版本

【不做】下单演练（查询足够）；多账户。

【自检】pytest 全绿；禁用态 API 不可见验证；启用后人工查余额；
M5 验收记录（三通道状态表）写入进度日志。
本周验收：三条实盘通道就绪（QMT 已实盘验证 / 掘金仿真通过 / easytrader 默认禁用可查）。
```

---

## 七、第 6 周 · 收尾与交付（D26–D30）

### D26 · iFinD 接入（不阻塞主线）

```
先读 AGENTS.md（重点：交叉对数、配额纪律）。

【状态检查】M5 已达成；iFinD 试用已开通。
若未开通 → 顺延预案：今天改做 D27 前半部分（复权验证脚本），D26 顺延。

【今日目标】付费数据源 iFinD 接入，财务降级链升级。
完成定义：三张财务表（income/balance/cashflow）入库；缓存命中不消耗配额（日志验证）。

【任务】
1. 安装 iFinDPy（同花顺私有 whl）；账号密码走加密 config
2. adapters/data/ths_ifind.py：ADAPTER=ThsIFindFeed——thsLogin + 每日定时重登 +
   会话失效自动重登；thsHistoryQuotes / thsBasicData / thsFinancialData / thsNews 封装；
   配额计数器（当日调用超阈值 → 直接读缓存）
3. federated 财务降级链插最前：ifind → tushare → akshare
4. 财务数据 Parquet 强缓存；健康面板加配额展示

【关键点与坑】
- iFinD 不同报表返回结构不同：适配器内归一化为统一列再返回
- 定时重登线程与请求队列互斥，防止登录态竞争

【不做】资讯推送 UI（列表展示即可）。

【自检】pytest 全绿；三表入库抽查；同请求第二次走缓存（配额计数不增）。
```

### D27 · 数据对齐专项

```
先读 AGENTS.md（重点：价格口径、交叉对数红线）。

【状态检查】D26 完成（或补充日完成）。

【今日目标】除权除息与多源一致性达标。
完成定义：10 只除权标的 qfq 连续性报告通过；下单路径原始价审计清单零违规（或有修复记录）。

【任务】
1. scripts/verify_adjust.py：抽 10 只有除权史的标的——qfq 序列除权日无异常跳空
   （阈值参数化）；原始价 × adj_factor 反推 qfq 与 qmt/tushare 交叉一致
2. 盘后任务：APScheduler 每交易日 17:00 刷新复权因子与当日日线增量
3. 下单路径审计：grep 审计 place_order 调用链的价格来源（含前端手工下单路径），
   输出 data/logs/audit_pricing.md——每个产价点标注口径是否原始价
4. 发现问题当日修复

【关键点与坑】
- 审计重点是「价格的产生点」而不是「下单点」——策略取的 bar 口径、前端展示价都要查

【不做】新增功能。

【自检】报告存档；审计清单结论写入进度日志。
```

### D28 · 合规页 + 运维可观测

```
先读 AGENTS.md（重点：合规红线——报备清单常驻非弹窗）。

【状态检查】D27 通过。

【今日目标】合规清单与运维可观测性就绪。
完成定义：报备清单可保存导出；状态灯真实反映断连（复现 D14/D23 场景）。

【任务】
1. CompliancePage.tsx：程序化交易报备清单（账户信息 / 策略类型 / 报备状态勾选 /
   导出 txt）——按证监会《证券市场程序化交易管理规定（试行）》要点生成模板文案；
   常驻导航入口，不做弹窗
2. 适配器健康面板：各源状态灯 / 延迟 / 限速余量 / iFinD 配额
3. 策略日志页：历史查询（落库日志）+ 实时流
4. compliance 表读写 API

【关键点与坑】
- 导出文件含账户信息：默认存 data/exports/ 并提示妥善保管

【不做】自动向券商报送（人工报备）。

【自检】清单保存/导出验证；人为断开某源 → 状态灯红 → 恢复变绿（截图记录）。
```

### D29 · 打包交付

```
先读 AGENTS.md 全文，做交付前检查。

【状态检查】D28 通过。

【今日目标】交付形态：双击可用。
完成定义：装有 QMT 的干净机器（或新 Windows 用户）双击启动跑通 M4 剧本。

【任务】
1. desktop/shell.py：pywebview 窗口（1280x800）同源加载 http://127.0.0.1:8787；
   启动前探活后端 health
2. 启动.bat：起后端（uvicorn，日志重定向 data/logs/backend_&date%.log）→ 轮询 health OK →
   启动壳
3. README.md：权限开通指引（QMT/iFinD/Tushare）/ 各适配器前置条件 / 常见故障排查
   （miniQMT 掉线、AKShare 封禁、easytrader 控件失效、对数差异解释）/ 目录说明
4. 生产形态：pnpm build 前端产物由 FastAPI 静态托管（运行机不再需要 Node）；
   可选 PyInstaller 打包后端

【关键点与坑】
- FastAPI 静态托管与 /api 前缀不冲突（SPA fallback 只拦非 /api 路径）
- 首次启动自动跑 init_db，缺 config 时给引导页而非崩溃

【不做】代码签名；自动更新。

【自检】干净环境双击启动全流程；README 按新手机器视角走查一遍。
```

### D30 · 全量回归 + v1.1 发布（收官）

```
先读 AGENTS.md 全文。

【状态检查】D29 通过。

【今日目标】六周工程收官 v1.1。
完成定义：M1–M5 剧本全部重跑通过；测试全绿；tag v1.1.0。

【任务】
1. 按各周「本周验收」重跑 M1–M5 全部验收项（逐项 DoD 核对表）
2. 修复回归问题（只修阻断级），测试全绿
3. 项目总结写入进度日志：交付物清单 / 遗留优化项（Ptrade 适配器、L2 行情、多账户、
   策略参数优化器等）/ 运维注意事项
4. AGENTS.md「当前项目状态」定稿；git tag v1.1.0

【不做】新功能。

【自检】发布总结 + 全部里程碑 DoD 核对表归档（进度日志 + data/logs/）。
```

---

## 八、附录：计划外情况处理

| 情况 | 处理提示词（直接粘贴） |
|------|----------------------|
| 某天没做完 | 次日重发**同一天**的提示词即可——开头【状态检查】会让 AI 自行核对缺口，不必改词 |
| AI 理解跑偏 | 发：`停。重读 AGENTS.md 的【硬性约束】第 N 条，检查你刚写的代码哪里违反了，列出违反点并给出修正方案，先不要动手改。` |
| 想临时加需求 | 发：`插一个需求：<描述>。先评估它触碰哪些文件、是否违反 AGENTS.md 约束，给出方案等我确认后再做，做完恢复当天原任务。` |
| 行情源接口对不上 | 发：`<源> 的实测返回和预期不符，这是我抓到的原始样本：<粘贴>。只修改 adapters/data/<源>.py 完成归一化，禁止改动其他文件，并在文件顶部更新实测版本注释。` |
| 交易通道回报对不上 | 发：`<通道> 回报与本地订单状态不一致：<描述>。先跑对账轮询打印双方差异，给出根因分析（回调丢失/状态映射错/时序问题），方案确认后再修。` |
| 想新增数据源/券商 | 发：`为 QuantDesk 新增 <名称> 适配器（数据源/券商）。先给《接入评估卡》：前置条件/代码格式/限速配额/已知坑/兜底关系，我确认后再写适配器文件，交叉对数基准源：<指定>` |
| QMT 权限延误 | 发：`QMT 权限预计延误 N 天。重排 D13–D30 顺序，给出不影响 M4（v1.0）的新排期与每天的状态检查调整。` |
| 实盘前最后检查 | 发：`执行实盘前检查清单：三道闸用例全绿？fail-closed 演练过？对账零差异？报备清单已填并导出？逐项回答后再开始今天的任务。` |

---

## 九、使用方案与边界（商业交付说明）

| 方案 | 用法 |
|------|------|
| A · 个人 30 天 MVP（默认） | 按本手册 D1–D30 顺序执行；里程碑 M1–M6 逐周验收 |
| B · 双人小团队 | 后端按本手册；前端领 AGENTS.md 分层契约独立开发 React；每周五合并跑 D20/D30 式回归 |
| C · 存量系统改造 | 先跑附录「实盘前检查」式审计 → 按适配器模式逐源接入 → 每接一源过交叉对数 |
| D · 教学/研究 | 在 AGENTS.md 追加一条硬性约束「实盘路径物理禁用（submit 对 live 直接 403）」，只用免费数据源 |

**边界与免责**：本套提示词交付的是软件工程能力，不构成投资建议、不做收益承诺；股票交易有风险，实盘可能损失本金；程序化交易须按监管要求向券商报备；easytrader 类非官方接口可能违反客户端使用条款，风险自担。对外分发时本段免责声明不得删除。
