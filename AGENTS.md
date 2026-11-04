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
10. **港股只读红线**：本工作台港股（`.HK`）定位是**看盘**，不提供交易能力。  
    `RiskGate` 在咽喉处对 `is_hk(symbol)` 的下单意图一律拒单（`submit` 与 `validate` 两处都拦，  
    四种代码写法 `00700.HK / 700.HK / hk00700 / 116.00700` 都要命中），`/api/watch/*` 与盯盘前端  
    **不得出现**任何下单/撤单/持仓写入口。这是设计意图，不是待办功能——加港股交易需用户在场另开轮次。
11. **公开接口纪律**：`tx_quote` / `em_quote` 是无 SLA 的网页接口。  
    ① 限速器不许失效（放行间隔锚在实际放行时刻）；② 连接被掐按**主机**冷却，冷却期内零外呼；  
    ③ 休市时自动路径不外呼，只有一线用户手动刷新（`force`）才打；④ 同一份数据多端点共享 TTL 缓存；  
    ⑤ 口径缺失或单源（如港股内外盘只能取东财 `f49`，无第二源互证）只能如实标注，
    不许用别的数凑一个看起来像的结果；运行期要取的字段必须在调用点取（缓存为空时降级＝静默错口径）；
    ⑥ **缓存判据按"要的那批东西缺不缺"算，不按"缓存空不空"算**——`cache.get(k) or {}` 之后继续
    往下走的每一处都要能区分"这个标的本来没这个字段"与"还没取过"，两者动作不同；
    缺失标的不许填成 `0.0`（看着像价格 0 元），要么标 `stale` 要么不出现。

## 技术栈（已锁定，不讨论替换）

Python 3.11+ · uv · FastAPI(REST + WebSocket) · SQLModel(SQLite, WAL) · pyarrow(Parquet) ·  
pyzmq(XPUB/SUB 事件 + PUSH/PULL 订单意图) · msgpack · pandas · pytest · APScheduler(盘后任务)  
前端：Vite + React 18 + TypeScript(strict) · zustand · lightweight-charts · echarts ·  
@monaco-editor/react · 桌面壳：pywebview  
交易：xtquant/QMT·miniQMT（主力）· 掘金 gm（备选）· easytrader（同花顺客户端，默认禁用）· 内置模拟撮合  
行情/数据：QMT xtdata · 通达信（官方 TdxAiData 或 pytdx 择一）· 同花顺 iFinD · AKShare · Tushare ·  
federated 降级路由（以上 SDK 全为**可选依赖**，不进 pyproject，惰性导入，缺失如实上报未就绪）

## 架构分层与职责

- UI 层 frontend/src：实时盯盘(/watch，A股+港股只读) / 新手指引(/guide，术语库) / 行情面板 /  
  策略工作台(Monaco) / 回测报告 / 实盘监控 / 数据管理 / 报备清单 / 设置（全局模拟盘开关）
- API 网关层 quantdesk/api：routes\_* + ws.py（/ws/events 统一事件流）；只依赖 services  
  （routes_channels 通道全景/探活/凭据/对账 · routes_interop 板块与公式互操作 ·  
  routes_system 日志检索与 adapters · routes_backtest 同步+异步队列与进度 ·  
  routes_watch 盯盘池/刷新/详情/逐笔/K线/历史定格（**无任何下单入口**）· routes_guide 术语检索）
- 服务层 quantdesk/services：market_data / trading / strategy_mgr / backtest_svc / data_store  
  （含 **backfill()：只补缺不覆盖 + 写入后自检**）· **watch（只读盯盘：时段表 A股/港股分开、  
  买卖量主口径=官方内外盘、逐笔只做分层与明细、方向码每日校准且只在置信时缓存、  
  休市不外呼 + TTL 缓存 + 收盘定格落 daily_flow）** · **guide（101 条术语 × 10 类，  
  import 期自检重复 slug / 未知分类 / 关联断链）**  
  · **channels（ChannelHub 按配置动态装配：实时主源单一/历史降级链/凭据缺失 fail-closed/  
  实盘通道惰性解析+15s 冷却）** · **realtime（RealtimePump：订阅上行→行情下行→意图过闸→回报回投）** ·  
  **reconcile（周期对账：通道快照 vs SQLite，差异落 reconcile_diff + RISK 广播）** ·  
  **logsink（LOG/RISK 批量落 run_log，可检索）** · **interop（通达信 .blk / 同花顺·东财 TXT 板块双向 +  
  通达信/同花顺条件选股公式方言编译，规则求值与公式同源）**
- 引擎层 quantdesk/engine：EventBus(core) · RiskGate（订单唯一咽喉，含 live_provider 动态实盘解析 +  
  风控热参 + **港股只读红线：is_hk 一律拒单，submit 与 validate 两处都拦**）· runner（策略子进程装载）·  
  supervisor（心跳 5s / 判死 15s / 重启上限 3 次每小时，subscribe 意图上收）· backtest + matching（回测与  
  模拟共用撮合，BacktestEngine.run(on_progress=…)）
- IPC 层 quantdesk/ipc：ZMQ XPUB（事件下行）/ PULL（订单意图上行）+ 心跳信道  
  （bar/order/trade 的跨进程编解码集中在 engine/runner.py 的 *_to_dict / *_from_dict，枚举一律取 .value）
- 公共基建 quantdesk/core：resilience（Backoff + ReconnectWatchdog：探活/判死广播/退避重连/  
  重连后重放义务）· secrets（DPAPI 凭据保险库，只写不读、掩码回显、密文与本机 Windows 账户绑定）·  
  ratelimit（最小间隔限速器：睡眠在锁内补齐到目标时刻，放行间隔锚在**实际放行时刻**——
  限速器失效在公开接口上等于自招封禁）
- 适配器层 quantdesk/adapters：base/（两个 ABC + **optional.py 通道元数据合同与惰性导入**）  
  + data/（qmt_xtdata · tdx_aidata · ths_ifind · akshare_feed · tushare_feed ·  
  **tx_quote / em_quote（公开 HTTP 行情，免账号免 SDK，official=False，用主依赖 httpx）** ·  
  federated 路由降级）  
  + brokers/（paper_broker · qmt_broker · gm_broker · easytrader_broker）  
  · 注册表自动扫描（模块级 `ADAPTER` 导出才登记；SDK 缺失 → 状态 `unready` 如实上报而非隐藏；  
  `default_enabled=False` → **注册表源头不可见**，只有设置页打开 enable_flag 才进入交易 API 视野）
- 存储层 quantdesk/store：db.py(SQLite WAL) / models_db.py（strategies / orders / trades /  
  positions_snapshot / watchlist / compliance / backtest_runs / **reconcile_diff** / **run_log** /  
  **daily_flow（当日买卖量收盘定格）**）/  
  parquet_store.py（data/parquet/{market}/{symbol}/{freq}/{YYYY}.parquet，日线按年、分钟线按月分文件；  
  db 路径惰性取 settings.data_dir）
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
snapshot_for_reconcile() -> (list[Order], list[Trade], list[Position], Account|None)  
# 可选但对账敏感通道应实现：一次性取一致快照，避免逐项查询期间被 poll() 改动而误报

通道元数据合同（adapters/base/optional.py 的 meta_of 读取，UI 与路由的事实源）：  
name / label(中文名) / prereq(用户需完成的前置一句话) / official(False→UI 红字「非官方接口」) /  
requires(可选 SDK 模块名，缺失→status=unready + missing) / requires_any(True→任一依赖可用即就绪) /  
default_enabled(False→注册表源头不可见) / enable_flag(对应 Settings 布尔字段) / is_paper /  
capabilities。SDK 一律惰性导入（import_optional，缺失抛带安装引导的 AdapterError），顶层 import 禁止。

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
- **Windows 启动器一律 CRLF + 纯 ASCII**：`启动.bat` 这类被 `cmd.exe` 执行的脚本，
  正文与注释都不许出现多字节 UTF-8 字符，行尾必须是 CRLF。`cmd` 按**字节偏移**增量读取
  `.bat`，裸 LF 与多字节字符会让它的解析器错位，把一行的一部分当命令执行——本机实测报过
  `'esk.app' 不是内部或外部命令`（`-m quantdesk.app` 被切在中间）。中文提示交给前端做，
  启动器只留 ASCII；改完必须实跑一遍双击路径验收。
- **测试不许污染真实运行目录**：任何会写 `data/`（日志、Parquet、SQLite）的用例，
  必须在夹具里换掉 `QUANTDESK_DATA_DIR` **并清 settings 缓存与引擎**，`init_db()` 默认库也要在
  临时目录建好——否则内部用默认路径的组件（如 `paper_broker`）会去摸真实库，
  用例靠真实库的表"假装通过"。本轮清掉历史污染的 582 个 `crash_*.log`。
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

2026-11-04（同日续）· **主流股票软件接入轮**（Integ-01/02/03）：与各大主流股票软件「结合」
从设想变成注册表里的 10 个通道事实。

- **接入层地基**：`adapters/base/optional.py`（可选 SDK 惰性导入 + 通道元数据合同）、注册表
  启停与状态如实上报、`core/resilience.py`（退避 + 断线判死广播 + 重连后重放义务）。
- **六个通道**：`qmt_xtdata` + `qmt`（miniQMT 主力，行情 tick/分钟 + 交易回调）、
  `tdx_aidata`（官方 SDK / pytdx 择一 + 串行队列）、`ths_ifind`（配额三层缓存）、
  `gm`（终端线程模型 + 请求投递）、`easytrader`（默认禁用 + poll 差异生成事件 + 控件截图）。
  每通道配 `tests/mocks/` 假 SDK 离线单测 + `docs/integration/*.md` 在线核对卡。
- **运行时装配 v2**：`services/channels.py`（ChannelHub）、`services/realtime.py`（RealtimePump）、
  `services/reconcile.py`、`services/logsink.py`、`core/secrets.py`（DPAPI 保险库）、
  `services/interop.py`（通达信 .blk / 同花顺·东财板块双向 + 公式方言编译）。
- **修掉的产品级断点**（此前测试全绿但链路不通）：① 策略 `ctx.buy()` 的 order_intent 从未进
  RiskGate；② subscribe 意图无人消费；③ `/` 404（SPA 未托管，桌面壳打开即白屏）；
  ④ 实盘确认文本只在前端（直连 API 可绕过）；⑤ bar 序列化 `str(enum)`；
  ⑥ 实时 bar dt 不取周期起点导致子进程历史缓冲刷重复行。
- **安全**：凭据只进 DPAPI 保险库（只写不读、掩码回显、密文与本机 Windows 账户绑定）；
  风控热参进设置页；`simulation_only=False` / `enable_easytrader=True` / `broker_channel≠paper`
  三者都要求服务端校验确认文本「我知晓并承担实盘交易风险」。
- pytest **592 passed**（240→592，network 默认跳过，连续两轮）。
- **前端交付**：通道页 `/channels`（全景三态灯 / 探活 / 凭据表单 / 对账 / 实时泵状态）与
  互操作页 `/interop`（板块导入导出 / 信号选股 / 公式编译）；`React.lazy` 首屏分包
  （entry 271KB，gzip 91KB）；`BrowserRouter` + 后端 SPA 回落。浏览器 10 路由遍历 console 零错误，
  UI 点「运行回测」异步链路实测出净值与成交。
- **界面可配置的路由优先级**：`feed_priority_history/realtime/financial` 进设置白名单，
  服务端按注册表校验通道名（允许写未就绪的源，拒绝错名与交易通道名）。
- **事故留档（纪律）**：`store/db.py`、`store/parquet_store.py`、`engine/supervisor.py` 曾在模块顶层
  `from quantdesk.config import data_dir`，夹具替换模块属性对它无效——一次全量跑把合成行情写进
  真实 `data/parquet` 并覆盖了同年真实日线（已按合成特征判据分离、回写真实行、根治并加回归锁）。
  **规则：运行期要取的东西必须在调用点取；改 API 响应结构必须全前端路由遍历**（契约漂移曾致 `/data` 白屏）。
- **接入完成 ≠ 接入可交付**：六个通道凡「SDK 表面靠推断」的条目集中写在
  `docs/integration/*.md` 第 8 节在线核对清单，须真机勾选后才能升为已实测；
  QMT/掘金/iFinD/easytrader 真实下单仍待用户权限与账号（M5 需用户在场）。
- 新增文档：`docs/商业落地工程方案.md`（v2.0 工程方案：产品定位/接入矩阵/运行时架构/
  安全合规强制清单/双层验证协议/交付与备份/路线图/风险登记册/诚实限制）、
  `docs/adapter_dev_guide.md`（新增通道 SOP）。

2026-11-04（同日续）· **只读盯盘轮**（Integ-05）：与主流软件「结合」之外，本机现在有一台
**不需要任何券商权限就能看真实行情**的看盘终端（A股 + 港股）。

- **两个免账号公开行情通道**：`tx_quote`（腾讯批量五档 + 内外盘，一次请求 60 只，盯盘池轮询主源）、
  `em_quote`（东财逐笔/分时/个股扩展字段，明细源）。用主依赖 `httpx` 直连，`requires=()`、
  `official=False`（UI 红字），无凭据表单——与"待权限"的 QMT/通达信是两类通道，故各出接入卡。
- **买卖量口径（本轮最容易自欺的一处，结论钉在这里）**：主口径 = **外盘/内盘**。A股两源互证
  （腾讯 `outer` 与东财 `f49` 实测逐位相等）；**港股只有东财单源**（腾讯港股不给），界面与文档
  一律按单源表述，不借 A股那句抬精度。逐笔只做小/中/大/特大**分层**与明细，因其**含盘后成交**
  （A股 +8.11%、港股 −3.14% vs 官方总量），差值 `tick_vs_official_pct` 直接显示不吞掉。
  方向码 `1/2/4` 每日按官方内外盘反解校准，只有置信结果才写当日缓存；港股逐笔只有 `1/2`。
- **每日自动**：盘中 `watch_poll` → 15:05/16:10 定格当日买卖量（`daily_flow`）→
  15:25/16:25 自动回补日 K（`data_store.backfill()`：只补缺、不覆盖、写入后自检）。
  **休市时自动路径零外呼**（面板显示"休市中：未外呼"），只有手动刷新 `force` 才打。
- **新手指引 `/guide`**：101 条术语 × 10 类，每条含一句话/展开/算例/常见误解/在哪能看到/关联，
  import 期自检重复 slug、未知分类与断链（内容型数据坏了只会静默缺条）。
- **本轮查出 16 处缺陷**（逐个有回归锁，详见进度日志）：其中 2 处属"图上看起来一切正常"——
  ① 东财 `klt` 分钟数写成秒（M5 静默返回 60 分钟线）；② 港股 `f51/f52` 实为 52 周高/低却输出成
  `limit_up/limit_down`（界面会出现"涨停 656.70"）。1 处属**纪律复发**：
  `_compute_flow` 缓存为空即静默降级成逐笔口径，而界面仍写"主口径=官方内外盘"——
  「运行期要取的东西必须在调用点取」这条规则本轮又兑现一次（`_ensure_live`）。
- **公开接口纪律**（宪法第 11 条新增）：限速器不许失效（本轮修掉 undersleep）、按主机冷却、
  休市不外呼、多端点共享 TTL、口径缺失只能如实标注。
- 新增文档：`docs/integration/tx_quote.md`、`docs/integration/em_quote.md`（含真机回显与在线核对清单）。
- **诚实限制**：两市收盘时才交付，**盘中实时跳动尚未真机确认**（链路/结构已验，跳动本身待下一
  交易日 09:15 后复查）；轮询非推送（3s），真 tick 需 QMT/通达信官方 SDK；历史逐笔无法回看。
