# QMT / miniQMT（xtquant）通道接入卡

> 定位：宪法中的**主力通道**——行情 `quantdesk/adapters/data/qmt_xtdata.py`（`ADAPTER = QmtXtdataFeed`，
> name=`qmt_xtdata`）+ 交易 `quantdesk/adapters/brokers/qmt_broker.py`（`ADAPTER = QmtBroker`，
> name=`qmt`，is_paper=False，official=True，default_enabled=True）。
> 本卡覆盖：前置条件、SDK 安装、凭据约定、能力矩阵、线程模型、参数/状态映射、单位口径、
> **在线核对清单**、验收标准、已知坑。
> 敏感信息（资金账号、userdata 路径）只进本地加密保险库（`core/secrets.py`，键名
> `qmt.userdata_path` / `qmt.account_id`），不入库明文、不进日志（日志与异常文本仅前 4 位 + `***`）、不进 git。

## 1. 前置条件（行政 + 环境，缺一不可）

1. **券商开通 QMT/miniQMT 权限**：多数券商要求资产门槛（常见 50 万，可协商）并单独签署
   程序化交易/极速交易协议；未开通时客户端里看不到「极简模式（独立交易）」入口。
   同时按宪法第 9 条完成**程序化交易报备**（报备清单为常驻页面）。
2. **安装并登录 QMT 客户端**：券商官网下载「Xt 它象 QMT」个人版；登录普通账号即可看到行情。
3. **启用 miniQMT（独立交易）**：登录窗口勾选「极简模式」，或在全息/QMT 里登录交易账号后
   选择「独立交易」。miniQMT 是**无界面的 SDK 后台**——它没跑，`xtquant` 一切接口都是空数据/连不上。
4. **找到 userdata 路径**：QMT 安装目录下 `…\{券商}QMT\userdata`（xtquant 读取的就是这个目录，
   不是 `userdata_mini` 就是 `userdata`，取决于客户端形态；两个目录都存在时以客户端「系统-文件路径」
   显示的那个为准）。路径写进保险库 `qmt.userdata_path`。
5. **资金账号**：交易账号（不是股东代码），形如 8~12 位数字，写进保险库 `qmt.account_id`。
6. 版本要求：**xtquant 与客户端必须同源同批**（券商客户端自带的 xtquant 版本最稳）；升级客户端后
   旧 xtquant 常出现「连接返回 -1 / 订阅无回报」，处理见第 10 节。

## 2. xtquant 安装（可选依赖，不进 pyproject）

```bash
# 优先：装券商 QMT 客户端自带的 whl（与本机客户端版本匹配）
#   位置通常在  安装目录\bin\x64\script\rely_versions\  或券商文档给的 reliance 目录
uv pip install "xtquant-<券商版本>.whl"

# 兜底：PyPI 上的社区构建（版本可能与本机客户端不匹配，仅用于冒烟）
uv pip install xtquant
```

- 适配器文件**顶层不 import** xtquant；只在 `connect()` 里 `import_optional("xtquant.xtdata" …)`
  / `import_optional("xtquant.xttrader" …)`。缺失时注册表如实上报 `status=unready /
  missing=["xtquant"]`，实例化时才抛带引导的错误（可观测性优于静默消失）。
- 行情通道的失败统一表现为 `ConnectionLostError`（消息带「请登录 miniQMT 客户端后重试」），
  这样 `FederatedFeed` 的降级链能把请求转给 tushare/akshare，而不是整条链崩掉。
- 离线单测不打真客户端：`tests/mocks/xtquant/` 假 SDK + `tests.mocks.fake_sdk("xtquant")`。

## 3. 凭据与 connect 参数约定

| 通道 | 调用 | 参数 | 来源 | 说明 |
|---|---|---|---|---|
| 行情 | `QmtXtdataFeed.connect()` | 无 | — | 依赖本机 miniQMT 会话，不需要额外凭据 |
| 交易 | `QmtBroker.connect(userdata_path=…, account_id=…)` | 两个都必填 | 保险库 `qmt.*` | 缺一即抛 `BrokerError`（不猜默认值、不静默退化） |

`ChannelHub` 按 `_CREDENTIALS["qmt"]` 自动注入；`simulation_only=True`（默认）时实盘通道根本不会被解析，
下单第①道闸一律路由内置模拟撮合。

## 4. 能力矩阵

| 能力 | qmt_xtdata | qmt | 备注 |
|---|---|---|---|
| 历史 K 线（1m/5m/15m/30m/60m/1d/1w/1M） | ✅ | — | 先 `download_history_data` 增量补本地仓库，再 `get_market_data_ex` |
| 实时 tick（五档） | ✅ | — | `subscribe_quote(period="tick")` → `Tick.bids/asks` 各 5 档 |
| 实时分钟线 | ✅ | — | 默认订阅周期 `("tick","1m")`，只想要 tick 用 `realtime_periods=("tick",)` |
| 财务数据 | ❌ | — | 明确抛 `FeedCapabilityError`（能力声明里没有 FINANCIAL）→ 财务走 ifind/tushare/akshare |
| 委托/撤单 | — | ✅ | 限价 FIX_PRICE；市价按**沪深分支**（见 7.3） |
| 资金/持仓/当日委托/成交查询 | — | ✅ | `query_stock_asset/positions/orders/trades` |
| 回报推送 | — | ✅ | `on_stock_order/on_stock_trade/on_order_error/on_cancel_error/on_disconnected` |

## 5. 线程模型（勿改成直调）

- **行情推送线程**（xtdata）与**交易回报线程**（xttrader）是 SDK 内部两条不同的线程，
  都不是我们的调用线程。两处回调一律「只入队」：
  - 行情：`qmt-rt` 工作线程出队 → 归一化 `Tick`/`Bar` → 调用 `subscribe_realtime` 传入的 callback；
  - 交易：`qmt-report` 工作线程出队 → 归一化 `Order`/`Trade` → 发布注入的 `EventBus`
    （ORDER / TRADE / RISK；撤单失败 → `RISK{action:"cancel_rejected"}`）。
- 适配器**不落库**：先落库后广播是 services 层的顺序保证；本通道只负责归一化与广播，
  `snapshot_for_reconcile()` 给对账服务提供 (orders, trades, positions, account) 全量快照。
- 下单/查询为同步调用，跑在调用方线程（services 线程池）内。**下单不重试**：
  xtquant 抛异常时无法判断柜台是否已受理，重试可能造成重复委托——宁可抛 `BrokerError` 让上层决定。
  查询类可安全重放，故内部最多重试 1 次（宪法 DataFeed/Broker 约定）。
- 断线自愈：两个通道都挂 `ReconnectWatchdog`（`self.watchdog`，`interval=get_settings().watchdog_interval_s`），
  实例化后由 `connect()` 启动：
  - 行情：`probe=_probe`（`get_stock_list_in_sector("沪深A股")`）→ `reconnect=connect` →
    `on_reconnected` **重放当前订阅清单**；
  - 交易：`probe=_probe`（一次 `query_stock_asset`）→ `reconnect` 新建会话（新 session_id）→
    `on_reconnected` **重放账户订阅** + 刷新并发布资金/持仓 + 调用注入的 `reconcile_hook`。
  - 回调线程只允许 `mark_lost()` 做标记，绝不 sleep/重连。

## 6. 单位与价格口径（交叉对数必读）

- `xtdata` 的 `volume` 单位是**股**、`amount` 是**元**；AKShare 日线成交量是**手**（1 手 = 100 股）。
  双源对数必须先换算，否则 100 倍差是口径问题、不是数据质量问题。
- 下单价格永远是原始市场价（`AdjustType.NONE`）。适配器**只做透传**，不做任何复权换算；
  复权只发生在历史查询的 `dividend_type` 参数上（回测/指标用 front）。
- 兜底保护：若误把前复权价当委托价提交，RiskGate 第②道闸的「委托价 ∈ 涨跌停区间」会拦下
  （区间按 raw 昨收计算），见 `docs/audit_order_path.md`。

## 7. 参数映射表（集中在适配器文件顶部，接口变动只改这里）

### 7.1 周期 freq → xtdata period

| Frequency | period | 区间格式 |
|---|---|---|
| `M1/M5/M15/M30/H1` | `1m/5m/15m/30m/60m` | `%Y%m%d%H%M%S` |
| `D1/W1/MO1` | `1d/1w/1M` | `%Y%m%d` |
| `TICK` | — | 走实时订阅，`get_history` 抛 `FeedCapabilityError` |

### 7.2 复权 dividend_type

| AdjustType | dividend_type | 说明 |
|---|---|---|
| `NONE` | `none` | 原始市场价（下单口径） |
| `FORWARD` | `front` | 前复权（回测/指标口径） |
| `BACKWARD` | `back` | 后复权 |
| 其它（如 `front_ratio`） | 回退 `front` + `log.warning` | xtdata 无比率复权；需精确因子走 Tushare 本地计算 |
| SDK 拒绝该口径（本地仓库没做复权数据） | 回退 `front` 再试一次 + `log.warning` | 不直接报错，但一定留痕 |

### 7.3 报价类型 price_type（**沪深市价规则不同，绝不共用常量**）

| 场景 | 常量名（getattr 优先） | 兜底整数 | 说明 |
|---|---|---|---|
| 限价（沪深通用） | `FIX_PRICE` | 11 | 委托价 = `order.price` 原值 |
| 市价 · 沪市 | `MARKET_BEST_5` / `MARKET_BEST_5_SH` | 1 | 最优五档即时成交剩余撤销（沪市没有深市那套「对手方最优」命名） |
| 市价 · 深市 | `MARKET_PEER_PRICE_FIRST` | 42 | 对手方最优价格申报 |
| 市价 · 北交所 | `MARKET_PEER_PRICE_FIRST` | 42 | 暂按深市口径（未在线核对） |

> 兜底整数是**占位值**，只在真机 `xtconstant` 缺该常量名时使用并告警；权威取值一律来自真机
> `xtconstant`。委托方向同理：`STOCK_BUY`(23) / `STOCK_SELL`(24)。

### 7.4 委托状态码 → OrderStatus

| xtquant 状态 | 码 | OrderStatus |
|---|---|---|
| 未报 `ORDER_UNREPORTED` | 48 | SUBMITTED |
| 待报 `ORDER_WAIT_REPORTING` | 49 | SUBMITTED |
| 已报 `ORDER_REPORTED` | 50 | SUBMITTED |
| 已报待撤 `ORDER_REPORTED_CANCEL` | 51 | SUBMITTED（撤单在途，非终态） |
| 部成待撤 `ORDER_PARTSUCC_CANCEL` | 52 | PART_FILLED |
| 部撤 `ORDER_PART_CANCEL` | 53 | CANCELLED |
| 已撤 `ORDER_CANCELED` | 54 | CANCELLED |
| 部成 `ORDER_PART_FILLD` | 55 | PART_FILLED |
| 已成 `ORDER_ALL_FILLDED` | 56 | FILLED |
| 废单 `ORDER_JUNK` | 57 | REJECTED |
| 未识别 | — | **保守 SUBMITTED + `log.warning`**（绝不猜成终态） |

> 同义拼写（`ORDER_PART_FILLED` / `ORDER_CANCELLED` 等）在 `_ORDER_STATUS_DEFS` 里一并覆盖，
> 真机用哪个名字都不影响——但**数值必须以真机为准**（见第 8 节）。

## 8. 在线核对清单（离线 mock 验不到，连真客户端后逐项打勾）

假 SDK 只能验我们自己的归一化/映射/线程逻辑；下列 SDK 表面必须连真实 miniQMT 核对。
不符时**只改适配器文件顶部的映射表与候选字段名**，不改核心逻辑。

- [ ] **`get_full_tick` / tick 推送的五档字段名**：本项目按
      `lastPrice / open / high / low / volume / amount / bidPrice[] / bidVol[] / askPrice[] / askVol[]`
      读取，需确认真实字段名与数组长度（是否恒为 5，价格 0 表示无档位）。
- [ ] **订阅回调 payload 形态**：`subscribe_quote` 回调给的是 `{code: [record]}` 还是
      `{code: record}`（本项目两种都吃）；`record["time"]` 是毫秒 epoch 还是 `YYYYMMDDHHMMSS`。
- [ ] **period 字符串**：`60m` 是否要用 `1h`、`1M` 是否要用 `1mo`；`1w`/`1M` 的历史深度。
- [ ] **`get_market_data_ex` 返回**：index 是否恒为 `str`（本项目按 8 位/14 位数字串解析）、
      是否恒含 `open/high/low/close/volume/amount`、`volume` 单位是股还是手（券商版本有差异，
      直接影响第 6 节口径结论）、`fill_data=True` 在停牌日是否塞入 NaN。
- [ ] **`download_history_data` 签名**：`incrementally` 关键字是否存在（不存在时本项目降级为
      默认补拉并 `log.info`）；是否还有 `download_history_data2`（批量 + 进度回调）更合适。
- [ ] **`xtconstant` 常量名与取值**：`STOCK_BUY/STOCK_SELL`、`FIX_PRICE/LATEST_PRICE`、
      市价申报族（`MARKET_BEST_5`、`MARKET_PEER_PRICE_FIRST`、`MARKET_MINE_PRICE_FIRST`、
      `MARKET_FAK/MARKET_FOK`）与委托状态族 48~57 的**实际数值与拼写**（含历史拼写
      `ORDER_PART_FILLD/ORDER_ALL_FILLDED`）。
- [ ] **`order_stock` 参数顺序与关键字**：本项目按
      `order_stock(account, stock_code, order_type, order_volume, price_type, price, strategy_name=…, order_remark=…)`
      调用；确认真实签名是否接受关键字，以及**市价单是否必须传 price=0**（本项目原样透传，不改写）。
- [ ] **`order_remark` 是否被回传**：这是委托↔回报关联的**唯一锚**。若真实回报不带 remark，
      则完全依赖 `broker_order_id ↔ order_id` 映射表（重启丢失，需 services 层持久化后重建）。
- [ ] **回报对象字段名**：`XtOrder` 的 `order_status` 还是 `status`、`order_volume/traded_volume`、
      `traded_price` 还是均价字段、`order_time` 的形态；`XtTrade` 的 `traded_id`/`traded_amount`/
      `traded_time`（本项目兼容 HHMMSS / YYYYMMDDHHMMSS / epoch 秒 / epoch 毫秒四种）；
      `XtAsset` 的 `frozen_cash` vs `frozen`、`market_value` vs `stock_value`；
      `XtPosition` 的 `can_use_volume`（T+1 落点）vs `available_volume`、`avg_price` vs `open_price`。
- [ ] **`XtQuantTrader` 生命周期**：`connect()` 返回码语义（0 才算成功？各券商是否有非 0 可用值）、
      `subscribe(account)` 返回码语义、`session_id` 的类型要求（本项目用 `YYYYMMDD+4位随机` 整数）、
      同进程多 trader 是否互斥。
- [ ] **断线是否主动回调**：`on_disconnected` 在客户端被登出/进程被杀时是否真的触发；
      若不触发，只能靠 `health_check()` 的轻量查询判死（本项目两者都有，fail-closed 不依赖单一信号）。
- [ ] **回报是否可能乱序/重复**：`已报`→`部成`→`已成` 的时序，以及撤单回报与成交回报竞态；
      services 层落库必须按 `updated_at` 单调收敛（适配器不保证顺序）。
- [ ] **涨跌停/整手等交易所规则回报表现**：违规委托是 `ORDER_JUNK`(废单) 还是走
      `on_order_error`（本项目两条路都归一化为 REJECTED + message）。

## 9. 验收标准（在线，全部通过才算 QMT 通道就位）

1. **历史对数误差 ≤ 0.1%**：与已接入源（Tushare/AKShare）交叉对数 10 只标的 × 日线 + 1 分钟线，
   `close` 与 `volume`（换算单位后）逐日误差 ≤ 0.1%；**除权日单列报告**（不计入合格率，单独核对
   `front` 口径是否按当日复权因子刷新）。命令：`uv run python scripts/verify_sources.py`。
2. **重连 10s 内恢复推送**：kill miniQMT 客户端 → `health_check()` 在下一个探活点返回 False 并广播
   `channel_down`；重启客户端 → 退避重连成功后 **10s 内**收到新的 tick（断言 `qmt-rt` 线程有下行事件），
   且原订阅清单被完整重放（订阅句柄数与断线前一致）。
3. **委托-回报-落库链路 < 3s**：从 RiskGate 二次确认通过 → `order_stock` 返回 broker id →
   `on_stock_order`/`on_stock_trade` 归一化广播 → services 落 SQLite 可对账，端到端 < 3s；
   撤单同样（`cancel_order_stock` 受理 → 终态回报收敛）。
4. **T+1 可用量正确**：当日买入后 `get_position().available == 0`、次日为持仓量；
   与 QMT 客户端「可用股份」面板一致（误差 0）。
5. **fail-closed**：通道断线期间任何下单意图都抛 `ConnectionLostError` → RiskGate 拒单，
   **不排队、不重试、不降级到模拟撮合**。
6. **日志脱敏**：`data/logs/*.log` 全文检索不到 userdata 路径与资金账号明文，只允许前 4 位 + `***`。
7. **重启可恢复**：待确认单与账户状态在重启后从 SQLite 完整恢复（本通道不落库，恢复由 services
   层保证；本卡要求适配器的 `snapshot_for_reconcile()` 与之比对无差异）。

## 10. 已知坑与兜底

1. **`download_history_data` 会长时间阻塞**（首次补几年分钟线可达成分钟级）。
   → 绝不放在事件循环/回调线程里调；services 层走线程池，盘后批量补拉走 APScheduler
   （`DataStoreService.batch_download`）。适配器内部只重试 1 次，重试风暴归 `FederatedFeed`。
2. **session_id 复用报错**：同一 `session_id` 的会话残留时 `connect()` 返回非 0。
   → 每次建会话（含重连）都换 `YYYYMMDD+随机后缀`，重连走「停旧建新」。
3. **行情回调线程 ≠ 交易回报线程**：xtdata 与 xttrader 各有一套 SDK 内部线程；
   把两边的回调混到同一个队列/同一个 handler 里会互相阻塞。
   → 两个通道各自一条工作线程（`qmt-rt` / `qmt-report`），回调里只 `put`。
4. **客户端没登录就取数**：接口不报错但返回空表/全 0（最危险的一类失败：静默）。
   → 空表如实返回空 `DataFrame`（不抛），由上层 gap 报告与交叉对数发现；探活用
   `get_stock_list_in_sector("沪深A股")` 拿非空列表判活，不依赖返回码。
5. **xtquant 与客户端版本不匹配**：升级客户端后报「无法连接」或回报字段缺列。
   → 一律重装与客户端同批的 xtquant whl；缺列时 `normalize_history` 抛
   `FeedCapabilityError` 指名「接口/字段可能变动」，绝不静默补 0。
6. **前复权数据依赖本地仓库**：老仓库没跑复权任务时 `dividend_type="front"` 会失败。
   → 适配器回退一次并 `log.warning`；主口径仍需按 AGENTS.md 的记录（东财 qfq 有分离缺陷，
   Tushare 精确因子待 token）交叉确认。
7. **多实例互斥**：同一 userdata 目录不允许多个进程同时建交易会话（策略子进程必须只经 IPC
   上单，绝不各自 import 适配器——宪法第 6 条已保证适配器实例永不进入策略进程）。
8. **非官方旁路**：easytrader 类同花顺/通达信旁路默认禁用（`default_enabled=False` + UI 红字），
   实盘主通道只认 QMT；`simulation_only=True` 是任何实盘通道就位前的第一道闸。
