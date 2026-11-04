# 接入卡 · 通达信行情通道（`tdx_aidata`）

注册名 `tdx_aidata` · UI 名「通达信（官方 SDK / pytdx）」· 类型 `data`
适配器文件：`quantdesk/adapters/data/tdx_aidata.py`（类 `TdxAiDataFeed`，模块级 `ADAPTER` 导出）
离线单测：`tests/test_tdx_aidata.py`（假 SDK 在 `tests/mocks/pytdx/`、`tests/mocks/TdxAiData.py`）

## 1. 定位

通达信是国内市占率最高的行情软件。本通道做两件事：

1. **历史 K 线**（未复权）与**实时快照/分笔**的第二来源，主源仍是 QMT（`feed_priority_realtime
   = "qmt_xtdata,tdx_aidata"`）；
2. 在**没有官方 SDK 的机器上装就能用**——所以做成双传输层 + 自动选择：

| 传输层 | 类 | 模块 | 前置 | 定位 |
|---|---|---|---|---|
| `TdxOfficialTransport` | 官方 AI 数据接口 | `TdxAiData` | 装通达信官方 SDK（随官方客户端分发，需申请权限） | 优先 |
| `TdxPytdxTransport` | 开源 pytdx | `pytdx.hq` | `uv pip install pytdx` | 缺失/官方握手失败时自动降级；社区最通用路径 |

实际用的是哪一层，`connect()` 记日志、`describe()` 如实上报（`transport` / `transport_reason`
/ `transport_official` / `resolved_methods`）。两层都没有 → `import_optional` 抛带安装引导的
`AdapterError`（不静默、不返回 None）。

**`requires` 语义（择一即可）**
类上声明 `requires = ("TdxAiData", "pytdx")` 是**可选 SDK 全集**，并配 `requires_any = True`
告知注册表「任一存在即就绪」。因此只装了 pytdx 时，通道页显示 `ready`、`missing` 为空；
两层都没装才显示 `unready` 并列出两个候选。实例侧另有 `describe()` 上报本次实际选中的传输层
（`transport` / `transport_reason` / `transport_official`），面板口径以它为准——
`official=True` 表示存在官方路径，不代表当前跑的是官方层。

## 2. 安装与前置

```powershell
# 当前 venv 里装 pytdx（不进 pyproject：可选依赖，宪法第 8 条）
uv pip install pytdx
```

- 不改 `pyproject.toml`、不改 `quantdesk/config.py`：pytdx 是运行期可选依赖，
  顶层不 import，注册表扫描不会崩；
- 官方 SDK：装完通达信官方 AI 数据组件后，Python 里能 `import TdxAiData` 即可，
  安装目录可从 `QUANTDESK_TDX_INSTALL_DIR` 提供（`config.tdx_install_dir`，非凭据）；
- 网络：需要能出站到行情服务器 7709/7727 端口（公司网/防火墙要先放行）。

## 3. 行情服务器与换机策略

`TDX_HQ_SERVERS`（适配器文件顶部常量）是公开 vip 服务器候选列表，`connect()` **依次试连**，
第一台握手成功即用，`describe()["server"]` 报告实际连上的 `host:port`。

- 全部试连失败 → `ConnectionLostError`，消息里带「更新常量或传 `servers=`」的引导；
- 临时换机（不改代码）：

  ```python
  from quantdesk.adapters.data.tdx_aidata import TdxAiDataFeed
  feed = TdxAiDataFeed(servers=[("新的host", 7709), ("另一个host", 7709)])
  feed.connect()
  ```

- 长期换机：改 `TDX_HQ_SERVERS` 常量（服务器地址可从通达信客户端「行情测试/服务器列表」
  或社区维护的 pytdx 服务器清单获取；地址会失效，属日常运维项）；
- 试连失败的机器数会写进日志（`试连失败 N 台`），排查时先看这行。

## 4. 能力矩阵

| 能力 | 支持 | 说明 |
|---|---|---|
| `HISTORY_BAR` | 是 | 分钟族/日线/周线/月线，**仅未复权**（见第 6 节） |
| `REALTIME_TICK` | 是 | pytdx 无推送 → 轮询 `get_security_quotes` 合成 `Tick`（默认 3s，`poll_interval=`） |
| `TRANSACTION` | 是 | `get_transactions(symbol, count=1000, start=0)` → `[dt, price, volume, num, direction]` |
| `REALTIME_BAR` | 否 | 本通道不合成实时 K 线；实时 bar 驱动请走 QMT 主源 |
| `FINANCIAL` | 否 | `get_financial_data` 抛 `FeedCapabilityError`（财务链：iFinD → Tushare → AKShare） |

生命周期：`connect()` / `disconnect()`（幂等）/ `health_check()`（探活=取股票数量，结果缓存
60s，任何异常返回 False，可被 `ReconnectWatchdog` 探测）。

## 5. 周期映射表（freq → category / period）

| `Frequency` | pytdx `category` | 官方 SDK `period`（假设值） |
|---|---|---|
| `M1` 1 分钟 | 7 | `1m` |
| `M5` 5 分钟 | 0 | `5m` |
| `M15` 15 分钟 | 1 | `15m` |
| `M30` 30 分钟 | 2 | `30m` |
| `H1` 60 分钟 | 3 | `60m` |
| `D1` 日线 | 4 | `1d` |
| `W1` 周线 | 5 | `1w` |
| `MO1` 月线 | 6 | `1M` |
| `TICK` | 不支持 | 走 `get_transactions()` |

pytdx 另有 `8=1分钟选`、`9=3分钟` 等取值（不同版本口径不同），本表未使用；
**表以 pytdx 文档/源码为准，需在线核对**（第 8 节清单第 1 条）。
分页：`BAR_PAGE_SIZE = 700`（单次上限约 800），`MAX_BAR_PAGES = 60`，按「从最新往回数的偏移」
翻页，够到区间左端或取完即停；偏移语义与预期不符（整页时间戳重复）时立即停止而不是死循环。

## 6. 单位与价格口径

- **成交量单位是「手」**（1 手 = 100 股）：`get_security_bars` 的 `vol`、快照的 `vol`、
  分笔的 `vol` 都是手；`amount` 是元。归一化层**不做换算**（与 AKShare 适配器一致，
  口径混着改是最难查的数据 bug），需要「股」由使用层显式 ×100。
  本通道数据与 QMT/掘金（量为股）交叉对数时，**必须先把通达信的量 ×100**。
- **复权**：`adjust=FORWARD/BACKWARD` 一律 `FeedCapabilityError`。
  pytdx 只给原始价，本地没有通达信复权因子；按比例折算=伪造数据（宪法第 5 条价格口径 +
  已实锤的 P1 复权红线：AKShare 的 qfq 日收益分离已证明猜出来的复权价会静默污染回测）。
  要复权口径请用 Tushare 精确 qfq / QMT。拒绝发生在任何 SDK 请求发出之前（单测断言「一次请求都不发」）。
- **日线族 dt 截到 00:00**：pytdx 日线返回 "YYYY-MM-DD 15:00"，归一化后去掉时刻，
  否则与 AKShare/Tushare 逐行对数会因 dt 不等而失配。分钟线保留当日时刻。

## 7. 线程与韧性纪律

- **单工作线程独占连接对象**：pytdx/官方 SDK 的连接对象**非线程安全**（多线程共用会错包/串包）。
  适配器起一个 `tdx-worker` 线程，所有请求经 `queue.Queue` 串行消费，调用方同步等待结果
  （`request_timeout`，默认 10s；超时抛 `ConnectionLostError`，**不重发**——结果不确定时
  在同一连接上重发可能拿到上一条的包）。`max_workers` 参数只接受 1，传别的直接 `ValueError`。
- **单次调用内部最多重试 1 次**（宪法 DataFeed 约定）：取数抛断线 → 重连 → 重试一次，
  再失败就抛 `ConnectionLostError`；重试风暴与跨源降级归 `FederatedFeed`。
  异常时**丢弃连接对象**，重试用的是重连后的新连接（不复用坏 socket）。
- **回调线程只入队**：轮询线程取快照 → `Tick` 入投递队列；回调在独立的 `tdx-tick` 线程里执行，
  单个订阅者抛异常被隔离，不影响其他订阅者。
- **引用计数**：多策略订同一标的 → 外部只一份快照请求；`unsubscribe_realtime` 减计数，
  **归零才停轮询线程**；`disconnect()` 一并停轮询与投递线程。
- **断线自愈**：`ReconnectWatchdog(probe=_probe, reconnect=_reconnect_transport,
  on_reconnected=_replay_subscriptions)`。轮询线程只 `mark_lost()`（不重连不 sleep）；
  watchdog 退避重连成功后**重放订阅集合**并确保 1 个周期内有数（宪法明文义务，可测）。

## 8. 在线核对清单（接入验证日必做，逐条回写代码常量）

代码里所有「需在线核对」的假设都集中在下面这张表。核对方式：装了 pytdx 的机器上
`python -c "import pytdx.hq, inspect; ..."` 看签名 + 真连一台服务器打印返回字段；
官方 SDK 用 `dir(module)` / 官方文档核对类名与方法名。

| # | 待核对项 | 代码位置 | 影响 |
|---|---|---|---|
| 1 | `get_security_bars` 的 category 全取值（尤其 7/8/9 与 1 分钟口径） | `PYTDX_BAR_CATEGORY` | 错则取到别的周期 |
| 2 | `get_security_bars` 的 `start` 偏移语义（是否「从最新往回数」）与单次上限 | `BAR_PAGE_SIZE` / `MAX_BAR_PAGES` / 翻页循环 | 错则历史缺口或死循环（已有「整页时间戳重复即停」兜底） |
| 3 | bar 记录字段名：`open/high/low/close/vol/amount/datetime` 或 `year/month/day/hour/minute` | `normalize_bars` / `bar_datetime` | 错则空表 + 告警 |
| 4 | 快照字段名与单位：`price/last_close/open/high/low/vol/amount/bid1~5/bid_vol1~5/ask1~5/ask_vol1~5` | `quote_to_tick` / `make_quotes` | 错则 Tick 缺档 |
| 5 | 分笔接口名与形参：`get_transaction_data(market, code, start, count)` 还是首参 `category`；历史分笔是否要用 `get_history_transaction_data(..., date)` | `_TX_CATEGORY` / `TdxPytdxTransport.transactions`（按签名自适配） | 错则分笔不可用 |
| 6 | 分笔时间字段（`datetime` 还是 `time` + `year/month/day`）与方向取值 | `_tx_datetime` / `normalize_transactions` | 错则丢行（缺日期时不猜，直接丢） |
| 7 | `get_security_count` 作为探活是否可接受（是否有更轻的服务器时间接口） | `TdxPytdxTransport.probe` / `_PROBE_MARKET` | 影响探活开销 |
| 8 | `TdxHq_API(heartbeat=...)` / `connect(ip, port, timeout=)` 各版本形参 | `TdxPytdxTransport._try_connect`（按签名自适配） | 错则握手失败 |
| 9 | 公开行情服务器清单是否仍可用 | `TDX_HQ_SERVERS` | 错则连不上（有多机试连兜底） |
| 10 | 官方 SDK 真实类名（候选 `TdxAiDataAPI/TdxAiDataApi/TdxDataAPI/TdxAPI/Api`） | `_OFFICIAL_API_NAMES` | 错则官方层不可用，自动降级 pytdx |
| 11 | 官方 SDK 各方法真实名（`get_kline/get_snapshot/get_transaction/get_security_count` …） | `_OFFICIAL_METHOD_NAMES` | 同上 |
| 12 | 官方 SDK 各方法真实形参名/顺序 | `TdxOfficialTransport._candidate_forms`（关键字/位置/省略 start 三种形态逐个试）；`describe()["resolved_methods"]` 与 `_forms` 报告实际生效形态 | 核对后把真实签名写死，删掉候选探测 |
| 13 | 官方 SDK 的复权能力（若真提供复权，可放开本通道 adjust 分支） | `get_history` 的拒绝分支 | 影响能否提供 qfq/hfq |

## 9. 验收标准（D15 卡片口径）

1. **交叉对数**：同一批标的（建议 10 只，覆盖沪深主板/创业板/科创板）近 1 年日线，
   与 QMT（主源）/AKShare 对数：`|价差| ≤ 0.1%`，**除权除息日单列报告**（未复权价在除权日
   必然与复权源分离，属预期，不算失败）；成交量对数前先统一「手 vs 股」。
   脚本沿用 `scripts/verify_sources.py` 的模式，产物落 `data/logs/tdx_align_<date>.csv`。
2. **断线重连重放**：杀掉连接（拔网/服务器失效/SDK 报错）→ `health_check()` 转 False →
   watchdog 判死并退避重连 → 重连成功后订阅集合自动重放（本项已由
   `tests/test_tdx_aidata.py::test_poller_marks_lost_on_disconnect_and_watchdog_replays_subscription`
   在离线 mock 下证明；真机复跑同一剧本即可）。
3. **串行性**：多策略线程并发取数时，SDK 只被一个工作线程触碰
   （`test_requests_are_serialized_on_one_worker_thread` 用 mock 的并发计数断言 `overlaps == 0`）。
4. **口径纪律**：复权请求必须被明确拒绝而不是拿到「看起来对」的价格
   （`test_adjust_requests_refused_not_fabricated`）。
5. pytest 全绿、无回归；本通道用例全离线（不挂 `network` 标记也能跑）。

## 10. 已知坑

- **非线程安全**：见第 7 节。任何「把 `self._api` 暴露出去」的改法都是错的——连接对象只能在
  `tdx-worker` 线程里被触碰。
- **复权缺失**：只有未复权价。回测/指标层需要前复权时不要用本通道作基准源。
- **轮询实时的延迟与限连**：pytdx 无推送，快照轮询 → Tick 的 `dt` 是**采集时刻**，不是交易所
  推送时刻，固有延迟≈一个轮询周期（默认 3s）。轮询间隔不要设到 1s 以下（公开服务器有并发/频率
  限制，可能被踢）；本通道实时只作 QMT 的补充/兜底，不作主实时源。
- **公开服务器会失效**：`TDX_HQ_SERVERS` 属运维项；连不上先看日志里的试连明细。
- **pytdx 是社区实现**（逆向通达信私有协议），非官方接口：稳定性与字段口径随版本变化，
  升级 pytdx 后必须重跑第 8 节清单 1–9 条。
- **单次调用上限**：K 线一次约 800 根（本适配器按 700 分页）；快照一次几十只，订阅标的多时
  注意 `get_security_quotes` 的批量上限。
- 北交所代码在本通道不可用（`core/symbols` 的 tdx 口径不支持），路由层要排除。

## 11. 退回路径（本卡片对应的改动只新增文件，未改共享文件）

新增：`quantdesk/adapters/data/tdx_aidata.py`、`tests/mocks/pytdx/{__init__.py,hq.py}`、
`tests/mocks/TdxAiData.py`、`tests/test_tdx_aidata.py`、本文件。
删除这些文件即完全还原（注册表是目录扫描，没有集中注册点需要回滚）。
