# 接入卡 · 东方财富公开行情通道（`em_quote`）

注册名 `em_quote` · UI 名「东方财富公开行情（A股/港股，只读，非官方接口）」· 类型 `data`
适配器文件：`quantdesk/adapters/data/em_quote.py`（类 `EastmoneyQuoteFeed`，模块级 `ADAPTER` 导出在 `quantdesk/adapters/data/em_quote.py:391`）
消费方：`quantdesk/services/watch.py:253`（逐笔/分时/个股扩展字段 + 港股主动买卖量）·
`quantdesk/services/watch.py:344-354`（腾讯不给内外盘时用本通道补字段）
离线单测：`tests/test_quote_adapters.py`（假 `httpx.MockTransport`，参数映射与单位口径都钉成断言；服务层替身在 `tests/test_watch.py:51`）

> 证据标注约定：「实测」= 2026-11-04 收盘后在本机用项目 venv（`uv run python`）直连真接口跑出来的结果；
> 「待核对」= 靠推断、尚未复核的条目，全部汇总在第 9 节清单。
> 与 `tx_quote` 的分工一句话：**腾讯管「一批标的现在什么价」，东财管「一只标的今天怎么成交的」**。
> 这既是产品分工也是反爬分工——本通道的每一次外呼都比腾讯贵（逐笔单日数千到两万行）。

## 1. 定位与边界

只读看盘的**明细源**。三件事只有它能给：

| 用途 | 接口 | 代码位置 |
|---|---|---|
| 当日**逐笔成交**（带方向码、笔数）→ 买卖量分层与逐笔翻页 | `push2 …/stock/details/get` | `em_quote.py:240-281` |
| 当日**分时曲线**（1 分钟 + 均价线）→ 盯盘图 | `push2 …/stock/trends2/get` | `em_quote.py:284-302` |
| **个股扩展字段**（股本/市值/市盈(动)/市净/换手/量比/涨跌停/振幅/板块码） | `push2 …/stock/get` | `em_quote.py:191-232` |
| 港股主动买卖量（腾讯港股接口不给内外盘） | `stock/get` 的 `f49` | `em_quote.py:198-199` + `services/watch.py:344-354` |

它**不是**日线主口径：`capabilities` 里刻意不含 `HISTORY_BAR`（`em_quote.py:77-78`），
因此 `feed_priority_history` 的能力过滤会直接跳过它（`quantdesk/services/channels.py:165-179`、`channels.py:187-189`），
即使有人把名字写进降级链也进不去。`get_history`（`em_quote.py:305-329`）已实现且**本机实测可用**，
但目前**无调用方**：K 线接口只走腾讯（`quantdesk/api/routes_watch.py:171`、`services/watch.py:263-265`）。
要拿它的分钟线必须显式点名，别指望自动降级过来。

它**也不是**实时主源：`feed_priority_realtime = "qmt_xtdata,tdx_aidata,tx_quote"`（`quantdesk/config.py:53`）里没有它。
`realtime_feed()` 的入选条件是 `REALTIME_TICK` 或 `REALTIME_BAR`（`channels.py:201-202`），本通道两者都声明——
**所以把 `em_quote` 写到链首是可行的，而且是错的**：它的 `to_tick` 永远给空五档（`em_quote.py:237`），
盘口会整列消失。详见第 10 节坑 3。

## 2. 前置与安装（无 SDK、无凭据、无新依赖）

- **无需任何可选 SDK**：`requires = ()`（`em_quote.py:74`）。全程只用 `httpx`，
  它是 `pyproject.toml:20` 里的主依赖（`httpx>=0.27`）——本通道**没有新增依赖**，
  没有 `import_optional` 分支，`status` 永远是 `ready`（宪法第 8 条「可选依赖不进 pyproject」在这里不适用）。
  注册表实跑回显（本机 `describe_adapters()`，2026-11-04）：

  ```text
  em_quote | data | ready | official=False | ['realtime_bar', 'realtime_tick', 'transaction']
  ```

  并由 `tests/test_quote_adapters.py:310-326` 锁住两件事：`official is False`（UI 必须红字）+
  **不出现在凭据清单**（公开接口没有账号可填，`credential_spec()` 里不该有它，否则设置页渲染出空表单）。
- **`default_enabled = True` + `official = False`**（`em_quote.py:73-76`）：注册表源头可见，
  不配 `enable_flag`（不是 easytrader 那种「默认禁用 + 需开 flag」的通道）。
- 网络前置：本机能出站访问 `https://push2.eastmoney.com`（快照/逐笔/分时）与
  `https://push2his.eastmoney.com`（历史 K 线，**不同主机、冷却分别计**，`em_quote.py:149-152`）。
- **本通道历史上真被限流过**（模块 docstring `em_quote.py:20` 记着「本机 IP 被 push2 限流过，熔断提示需等待」）。
  所以第 7 节那套限速 + 按主机冷却不是装饰，是这个源能否长期活着的条件。

自检命令（实测可用）：

```bash
uv run python -c "from quantdesk.adapters.data.em_quote import EastmoneyQuoteFeed as F; f=F(); f.connect(); \
q=f.get_quote('600000.SH'); print(q['outer_volume'], q['inner_volume'], q['volume'], q['limit_up'], q['turnover_rate']); \
print(f.describe()['stats'])"
```

## 3. 接口与参数映射

| 调用 | 端点与关键参数 | 返回形态（实测样本） | 代码位置 |
|---|---|---|---|
| `get_quote(symbol)` | `push2/api/qt/stock/get`：`secid`、`fields`（28 个 f 码）、`invt=2`、`fltt=2` | `data` 字典：`f43:10.09 f47:824086 f48:835024728.0 f49:458432 f198:"BK0475"` | `em_quote.py:191-195` |
| `get_transactions(symbol, count=3000)` | `push2/api/qt/stock/details/get`：`secid`、`pos=0` 与 `pos=-count` **各一次**、`fields1=f1..f4`、`fields2=f51..f57` | `details: ["09:15:01,10.00,503,0,4,\u0000,0", …]`（7 段） | `em_quote.py:248-253` |
| `get_intraday(symbol)` | `push2/api/qt/stock/trends2/get`：`ndays=1`、`iscr=0`、`fields2=f51..f58` | `trends: ["2026-11-04 09:30,10.05,10.05,10.05,10.05,5387,5413935.00,10.050"]`（8 段） | `em_quote.py:287-290` |
| `get_history(symbol, freq, start, end, adjust)` | `push2his/api/qt/stock/kline/get`：`klt`、`fqt`、`beg/end=YYYYMMDD`、`fields2=f51..f58` | `klines: ["2026-11-04,10.05,10.09,10.23,10.05,824086,835024728.00,1.78"]` | `em_quote.py:315-319` |
| 请求头 | `User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)` + `Referer: https://quote.eastmoney.com/` | — | `em_quote.py:49-50` |
| 探活 | `secid=0.000001`（深市平安银行）取 `f43` | `f43` 非空即算通 | `em_quote.py:104-105`、`em_quote.py:129-137` |

外呼次数换算（实测）：`connect()` 探活 1 次；`get_quote` 1 次；`get_intraday` 1 次；
**`get_transactions` = 2 次**（头段 + 回溯段，`em_quote.py:251`）。
本轮冒烟脚本共 6 只标的/接口调用 → `stats["requests"] == 6`，逐笔只占其中 2 次。

代码格式互转（只在 `core/symbols.py` 与本适配器内发生，宪法核心契约）：

| 内部代码 | secid（`from_internal(s,"em")`，`quantdesk/core/symbols.py:163-165`） | 实测回读 |
|---|---|---|
| `600000.SH` | `1.600000` | `f57="600000"`、`f58="浦发银行"` |
| `000001.SZ` | `0.000001` | 探活样本 |
| `830799.BJ` | `0.830799` | 北交所映射到市场位 `0`（`symbols.py:164`），**本通道未实测** |
| `00700.HK` | `116.00700` | `f57="00700"`、`f198="HK28"` |

入码方向由 `normalize()` 兜住（`symbols.py:56-79`）：`hk00700` / `116.00700` / `700.HK` / `0700` / `00005`
归一成 `00700.HK` / `00005.HK`（裸 1–5 位按港股并 `zfill(5)`，`symbols.py:77-78`；`em` 分支 `symbols.py:118-127`）。
本通道所有出参的键都是**内部代码**（`em_quote.py:188`、`em_quote.py:214`），不做二次转换。

**周期映射表 `_KLT`（`em_quote.py:60-62`）—— 这里踩过一个静默缺陷，留档**

| `Frequency` | `klt` | 本机实测（`1.600000`，2026-11-01~11-04） |
|---|---|---|
| `M1` | `1` | 240 根，间隔中位 1 分钟 |
| `M5` | `5` | 144 根，间隔中位 5 分钟 |
| `M15` | `15` | 48 根，间隔中位 15 分钟 |
| `M30` | `30` | 24 根，间隔中位 30 分钟 |
| `H1` | `60` | 12 根，间隔中位 60 分钟 |
| `D1` / `W1` / `MO1` | `101` / `102` / `103` | 日线 3 根（区间内交易日），间隔中位 1440 分钟 |
| —（曾写过） | `900` / `1800` / `3600` | **一律 0 行**（接口不认识，不报错，只回空） |

`klt` 的分钟级口径是**分钟数**不是秒数。按秒写的时候有两档后果，第二档才是危险的：
`900/1800/3600` 不认识 → 空表（看起来像「这个标的没数据」）；
而 `M5→"60"` 接口**认识** 60 → **静默返回 60 分钟线**，列名、行数、时间轴全都「看起来正常」，
图上看不出错，回测会拿 1 小时线当 5 分钟线跑完并出一份像样的报告。
现在由 `tests/test_quote_adapters.py:168-193` 把 8 个周期的 `klt` 钉成断言，
外加「映射表不得出现重复值」与「`900/1800/3600` 不得出现在请求里」两条判据。
`_FQT`（`em_quote.py:63`）：不复权 `0` / 前复权 `1` / 后复权 `2`，实测 `fqt=1` 与 `fqt=0` 请求都能拿到数。

## 4. f 码字段表（接口变更只改这一处）

请求字段清单在 `_SNAPSHOT_FIELDS`（`em_quote.py:53-54`），解析在 `get_quote`（`em_quote.py:213-232`）。
「实测值」列是 2026-11-04 收盘后 `secid=1.600000` 的真返回，**每一项都与腾讯同一标的对比过**（第 8 节）。

| f 码 | 语义 | 单位 | 实测（600000.SH） | 港股实测（00700.HK） |
|---|---|---|---|---|
| f43 | 最新价 | 元 / 港元 | 10.09 | 401.6 |
| f60 | 昨收 | 元 / 港元 | 10.09 | 397.2 |
| f46 | 今开 | 元 / 港元 | 10.05 | 397.8 |
| f44 / f45 | 最高 / 最低 | 元 / 港元 | 10.23 / 10.05 | 402.2 / 391.6 |
| f47 | 成交量 | **手**（A股）/ **股**（港股） | 824086 | 18500105 |
| f48 | 成交额 | 元 / 港元 | 835024728.0 | 7372563200.0 |
| f49 | **外盘（主动买入累计量）** | 手 / 股 | 458432 | 10473682 |
| — | 内盘（**接口不给，代码算** `f47 − f49`） | 手 / 股 | 365654 | 8026423 |
| f50 | 量比 | 倍 | 0.98 | 0.93 |
| f51 / f52 | A股：涨停 / 跌停；**港股：52 周最高 / 最低** | 元 / 港元 | 11.1 / 9.08 | 656.7 / 375.2 |
| f71 | 均价 | 元 / 港元 | 10.13 | 398.515 |
| f168 | 换手率 | % | 0.25 | 0.2 |
| f169 / f170 | 涨跌额 / 涨跌幅 | 元 / % | — | 4.4 / 1.11 |
| f171 | 振幅 | % | 1.78 | 2.67 |
| f84 / f85 | 总股本 / 流通股本 | 股 | 33305838300 | 9092644829 |
| f116 / f117 | 总市值 / 流通市值 | 元 / 港元 | 336055908447 | 3651606163326.4 |
| f162 | 市盈(动) | 倍 | 6.21 | **`"-"` → `None`** |
| f167 | 市净率 | 倍 | 0.44 | 2.79 |
| f92 | 每股净资产 | 元 | — | — |
| f105 | 净利润 | 元 | 40572000000.0 | 实测 `0.0`（不作依据） |
| f57 / f58 | 代码 / 名称 | — | 600000 / 浦发银行 | 00700 / 腾讯控股 |
| f198 | 所属板块码 | — | `BK0475` | `HK28` |
| f107 | 已请求**但未使用** | — | 1 | — | 

`f107` 在请求串里（`em_quote.py:54`）但 `get_quote` 不读它——**语义未核对**，删或留都不影响出参，
别把它当板块码（板块码是 `f198`）。非数字占位一律走 `num()` → `None`（`em_quote.py:204-211`），
判据是「去掉小数点与负号后仍是数字」；`"-"` 变 `None` 而不是 0，
因为 0 的含义是「没有换手/市盈」，与「接口不给」必须区分（`tests/test_quote_adapters.py:227-232` 锁住）。
接口字符串型涨跌幅另有出口 `pct_display`（`em_quote.py:231`，只在值为字符串时给出，如新股的 `-`）。

### 4.1 逐笔行格式（`details` 的 7 段，实测）

`时间, 价格, 成交量, 笔数, 方向码, 第6段, 第7段`——解析只取前 5 段（`em_quote.py:261-270`），
第 6/7 段原样丢弃：

| 段 | A股实测 | 港股实测 | 说明 |
|---|---|---|---|
| 1 | `09:15:01` | `09:00:00` | 只有时间没有日期 → 日期由 `pd.Timestamp.now()` 拼（`em_quote.py:276-277`），见坑 2 |
| 2 | `10.00` | `397.200` | 价格（元 / 港元） |
| 3 | `503` | `26600` | **量：A股=手、港股=股**（与 `f47` 同口径） |
| 4 | `0` | `0` | 笔数（部分行为 0，如实保留） |
| 5 | `1` / `2` / `4` | `1` / `2` | 方向码；**港股实测无 `4`（无集合竞价码）** |
| 6 | `\u0000`（空字符） | `P` / `U` | 未使用；港股该段是成交类型标记，**语义未核对** |
| 7 | `0` | `80` / `85` | 未使用 |

归一后的 DataFrame 列：`dt / time / price / volume / deals / side_code / amount`（`em_quote.py:281`），
`amount = price × volume × vol_mult`，`vol_mult` 对 `116.` 前缀取 1、其余取 100（`em_quote.py:247`）。
实测首行：`10.00 × 503 × 100 = 503000.0` 元 ✓；港股不乘 100 由 `tests/test_quote_adapters.py:235-245` 锁住。

### 4.2 分时行格式（`trends2` 的 8 段，实测）

`p[0]` 日期时间 / `p[1]` 价 / `p[2..4]` 未使用 / `p[5]` 分钟量（手） / `p[6]` 分钟额（元） / `p[7]` 均价
→ 出参 `dt / price / avg_price / volume / amount`（`em_quote.py:297-299`、`em_quote.py:302`）。
实测 A股当日 241 行（09:30→15:00 含首尾）。

### 4.3 方向码语义（本通道最容易做错的一处）

`_SIDE_CODES = ("1","2","4")` 只是「原样给出」（`em_quote.py:55-56`），**代码里不写死谁是买谁是卖**：
`services/watch.py:144-175` 的 `calibrate_side_map()` 每天用官方外盘/内盘反解——
对两种候选映射各算一次误差 `|买合计−外盘| + |卖合计−内盘|`（`watch.py:165-170`）取小的那个，
再要求它「真的明显更小」（`watch.py:174`：`best/worst < 0.6` 且 `best/total < 0.08`）才宣布校准成功。
默认兜底映射 `1=卖 / 2=买 / 4=中性`（`watch.py:141`）**只在没校准上时使用，且一定带 `calibrated=False`**。

本机实测（2026-11-04，600000.SH）：

| 量 | 值 |
|---|---|
| 逐笔行数（`pos=0` 单次） | 4654 行，覆盖 09:15:01 → 15:29:44 |
| 方向码分布 | `4`:76 笔 / `1`:1863 笔 / `2`:2715 笔 |
| 码 `2` 合计量 | 469283 手（官方外盘 458432，+2.37%） |
| 码 `1` 合计量 | 355116 手（官方内盘 365654，−2.88%） |
| 校准结论 | `{'1':'sell','2':'buy','4':'neutral'}`，`calibrated=True` |
| 误差对比 | 选定映射 21389（占官方总量 2.60%）vs 落选映射 206945（25.11%）→ 判据 0.10 < 0.6 ✓ |

盘前/半截数据时校准不成立 → `_ensure_calibration()` **不把失败结论写进当日缓存**
（`services/watch.py:448-471`，理由写在注释里：盘前一次残缺不该钉住全天分层）。

## 5. 能力矩阵与元数据合同

| 能力 | 声明 | 状态 | 说明 |
|---|---|---|---|
| `REALTIME_TICK` | ✓（`em_quote.py:77`） | **已实测** | 个股快照转 Tick；**`bids/asks` 恒为空**（`em_quote.py:237`）——五档只能看腾讯 |
| `REALTIME_BAR` | ✓（`em_quote.py:77`） | 已实测（分时可取） | 但通道**不产 `Bar` 对象**：`_poll_loop` 只回投 Tick（`em_quote.py:361-377`）；分时是服务层显式调 `get_intraday`（`services/watch.py:592`） |
| `TRANSACTION` | ✓（`em_quote.py:78`） | **已实测** | 当日逐笔，含方向码与笔数 |
| `HISTORY_BAR` | **不声明**（`em_quote.py:77-78`） | 代码可用、路由不可达 | 分钟/日/周/月都能取（第 3 节实测表）；不声明是为了不被自动降级链选中，见第 1 节 |
| `FINANCIAL` | 不声明 | 明确拒绝 | `get_financial_data` 抛 `AdapterError` 并指路 akshare/iFinD/tushare（`em_quote.py:331-334`） |

元数据：`official=False`、`requires=()`、`default_enabled=True`、`enable_flag=""`、`label` 带「只读、非官方接口」
（`em_quote.py:69-76`），`prereq` 一句话说清「能出站到 push2 即可 + 连续猛打会被临时封」
（`em_quote.py:71-72`）。读取入口 `adapters/base/optional.py:119` 的 `meta_of()`，面板由元数据驱动、前端零改动。

生命周期：`connect()`（`em_quote.py:97-115`，失败抛 `ConnectionLostError` 并把 client 关掉，不留半连接状态）/
`disconnect()`（幂等，`em_quote.py:117-127`）/ `health_check()`（轻量取 `f43`，任何异常返回 False，
`em_quote.py:129-137`，可被 `ReconnectWatchdog` 探测）。

## 6. 单位与价格口径

| 项 | A股 | 港股 | 依据 |
|---|---|---|---|
| 价格 | 元，**未复权原始市场价**（宪法第 5 条） | 港元 | `fltt=2` 直接给小数，接口不做百倍放大（`em_quote.py:194-195`） |
| 快照成交量 `f47` | **手**（1 手=100 股），`volume_unit="lot"` | **股**，`volume_unit="share"` | `em_quote.py:196-197`、`em_quote.py:217` |
| 快照成交额 `f48` | 元 | 港元 | `em_quote.py:218` |
| 逐笔量 | 手 → 金额 ×100 | 股 → 金额 ×1 | `vol_mult`，`em_quote.py:247` |
| 币种 | CNY | HKD | 本通道不回报币种；服务层按 `.HK` 判定（`services/watch.py:498`） |
| 展示单位 | 「手」 | 「股」 | 由服务层统一成中文并保留原码（`services/watch.py:68-76`）——单位猜错一次就差 100 倍 |
| 复权 | 历史 K 线支持 `fqt=0/1/2`（`em_quote.py:63`） | 同左 | 快照与逐笔**永远是原始价** |

**买卖量主口径 = 官方外盘/内盘，逐笔只做明细与分层**（`services/watch.py:505-511` 把口径与 `caveat` 写死在服务层）：

- 实测 600000.SH 当日逐笔合计 **890953 手 > 官方总量 824086 手**（`watch.tick_vs_official_pct = +8.11%`，
  计算在 `services/watch.py:545-547`）。逐笔时间跨度 09:15:01 → **15:29:44**，
  即含 15:00-15:30 的盘后定价成交，而官方内外盘不含——这是**口径差**，不是谁算错了；
- 因此界面同时给 `flow_source`（`official_outer_inner` / `em_tick` / `quote_only`）与
  `tick_vs_official_pct`，**不偷偷取其中一个、不抹平**；
- 港股对照：逐笔合计 18500119 股 vs 官方 18500105 股（+0.008%）——**港股逐笔几乎等于官方量**，
  与 A股差 8% 的行为不同（港股竞价/收市竞价都计入了 f47）。

**分层阈值 `_BUCKET_EDGES = (4万, 20万, 100万)`**（`services/watch.py:45`），按**单笔成交额**分
小/中/大/特大（`services/watch.py:178-203`）。
A股算的是人民币、港股算的是港币，**同一套绝对值阈值不折算币种**（`em_quote.py:280` 的 `amount` 直接进分层）——
港币 4 万 ≈ 人民币 3.7 万，量级接近但不等价，港股的分层结论要带着这句话看。

## 7. 限速、熔断与冷却纪律（这个源的重点）

| 机制 | 取值 | 代码位置 |
|---|---|---|
| 全局最小间隔 | `RateLimiter(0.35)` + 20% 抖动（`jitter` 默认值见 `quantdesk/core/ratelimit.py:16`） | `em_quote.py:51`、`em_quote.py:84` |
| 单次超时 | `timeout=8.0`，连接池 `max_connections=8` | `em_quote.py:81`、`em_quote.py:101-102` |
| 重试 | **最多 1 次**（`for attempt in (0,1)`），非致命错间隔 0.4s 再试 | `em_quote.py:155`、`em_quote.py:175-177` |
| 致命错判定 | 仅 `RemoteProtocolError / ConnectError / ConnectTimeout / ReadTimeout` 视为「对端在挡」 | `em_quote.py:173-174` |
| **按主机冷却** | 致命错后该 host 冷却 `_COOLDOWN_S = 60s`；冷却期内**直接抛、一次都不外呼** | `em_quote.py:52`、`em_quote.py:149-152`、`em_quote.py:178-180` |
| 冷却只锁出事的主机 | `push2` 被冷却不影响 `push2his`（host 分别计，成功一次即 `pop`） | `em_quote.py:149`、`em_quote.py:168` |
| 空 `data` **不冷却** | 那是「这个标的没数据/代码写错」→ 抛 `AdapterError`；否则用户敲错一个代码就撞 60 秒墙 | `em_quote.py:141-145`、`em_quote.py:166-167` |
| 轮询线程遇限流 | `break` 整轮（不逐只往下撞），下个周期再来 | `em_quote.py:371-373` |
| 单标的异常 | `AdapterError/ValueError` → 记 warning 后 `continue`，不拖垮整轮 | `em_quote.py:374-376` |
| 装配冷却（上一层） | 通道 `connect()` 失败后 60s 内不重新实例化，`force=True` 绕过（面板「测试连接」） | `services/channels.py:49`、`channels.py:131-165` |
| 观测 | `stats{requests,retries,errors,rate_limited}` + `describe()["cooldown"]` 给出还剩几秒 | `em_quote.py:94`、`em_quote.py:382-388` |

服务层再加一层 TTL，避免「多个前端标签页把明细请求放大成 N 倍外呼」：

| 路径 | 盘中 | 休市 | 代码位置 |
|---|---|---|---|
| 逐笔取数缓存 | 20s | 15min | `services/watch.py:473-490`（`_FLOW_TTL_S=20`，`watch.py:48`） |
| 买卖量重算 | 20s | 1h | `services/watch.py:430-446` |
| 单标的详情（快照+逐笔+分时+扩展字段） | 20s | 15min | `services/watch.py:564-574` |
| 整池自动刷新 | 交易时段才外呼 | 两个市场都休市时**返回当日最后一次快照、不外呼**（缓存为空的首次除外） | `services/watch.py:379-398` |
| 调度节奏 | `watch_poll` 5s（`quantdesk/config.py:60`、`services/scheduler.py:47-51`） | — | — |

实测：本轮冒烟 6 次外呼 `stats = {requests:6, retries:0, errors:0, rate_limited:0}`、`cooldown={}`
（未触发冷却）。触发条件只能靠**主动复现**验证（第 9 节清单第 6 条），
离线用假 `RemoteProtocolError` 已锁住「冷却期内一次都不再外呼」这条行为
（`tests/test_quote_adapters.py:257-269`）。

## 8. 与其他源交叉对数（2026-11-04 收盘后，本机重跑；对照源=腾讯 `tx_quote`）

同一交易日、同一标的逐位核对（宪法第 9 条要求 ≤0.1%）：

| 项 | 本通道 | 腾讯 | 结果 |
|---|---|---|---|
| 外盘 | `f49` = 458432 | `[7]` = 458432 | **逐位相等** ✓ |
| 内盘 | `f47 − f49` = 824086 − 458432 = 365654 | `[8]` = 365654 | **逐位相等** ✓ |
| 总量恒等式 | 外盘 + 内盘 = `f47` | `[7] + [8] == [6]` | 000001.SZ 同样成立（723215 + 714691 = 1437906）✓ |
| 成交量 | `f47` = 824086 手 | 日线最后一根量 824086、`[6]` 824086 | 逐位相等 ✓ |
| 成交额 | `f48` = 835024728 元 | `[37]` = 83502 **万元** → 835020000 元 | 差 4728 元 = **0.00057%** ✓（腾讯按万元截断） |
| 均价 | `f71` = 10.13 | `[51]` = 10.13 | 相等 ✓ |
| 换手 / 振幅 / 量比 | `f168` 0.25 / `f171` 1.78 / `f50` 0.98 | `[38]` / `[43]` / `[49]` | 相等 ✓ |
| 涨停 / 跌停 | `f51` 11.10 / `f52` 9.08 | `[47]` / `[48]` | 相等 ✓ |
| 市净率 | `f167` 0.44 | `[46]` 0.44 | 相等 ✓ |
| 日线收盘价（A股 27 个共同交易日） | `push2his` `fqt=1` / `fqt=0` | 腾讯 `qfqday` / `day` | **最大收盘差 0.0000%** ✓ |
| 日线收盘价（港股 31 个共同交易日） | 同上 | 同上（腾讯港股只有 `day`，见 `docs/integration/tx_quote.md` 第 10 节坑 1） | 最大收盘差 0.0000% ✓ |
| 港股成交量 / 成交额 | `f47` 18500105 / `f48` 7372563200 | `[6]` 18500105 / `[37]` 7372563093.2 | 量逐位相等；额差 106.8 港元 = 1.4e-6 ✓ |
| 港股内外盘 | `f49` 10473682（内盘 8026423） | `[7]/[8]` = 0/0（不提供） | 本通道是港股内外盘的**唯一来源** ✓ |

**逐笔口径与官方口径的差**（不是对数失败，是口径不同）：+8.11%（A股盘后成交），
+0.008%（港股）——见第 6 节。
**除权除息日单列报告本轮未做**（对数窗口 2026-10-01~11-04 内 600000/00700 未出现除权事件，属偶然），
列为第 9 节清单第 8 条。

## 9. 在线核对清单（下一个交易日 09:30 起逐条勾选；未勾选项一律视为「靠推断」）

| # | 时刻 | 勾选项 | 预期（今日基线） | 代码位置 |
|---|---|---|---|---|
| 1 | 09:30 / 10:30 / 14:00 | 同一秒内取东财 `f49` 与腾讯 `[7]`，**盘中是否仍逐位相等**（今天只对的是收盘后定格值） | 三时点全等；出现分叉先查两源更新时间戳字段 | `em_quote.py:198-199` |
| 2 | 09:30 起 | 逐笔方向码是否仍只有 `1/2/4`，`calibrate_side_map` 是否仍给 `{'1':'sell','2':'buy','4':'neutral'}` 且 `calibrated=True` | 今日基线：4654 笔 / +8.11% / calibrated=True；映射翻转要能在日志里立刻看到（`services/watch.py:467`） | `services/watch.py:144-175` |
| 3 | 09:30 后每 20s | 逐笔**分页覆盖**：`pos=0` 单次到底给多少行？A股 600000 今天一次给完全日（4654 行），**港股 00700 一次给 22695 行**——注释里「约 5000 行上限」与实测不符，需要重新量一次并改注释 | 高成交标的核对：头段 + `pos=-count` 段的并集无中间空洞（比较相邻 `dt` 最大间隔） | `em_quote.py:243-244`、`em_quote.py:251` |
| 4 | 09:30 | 分时点数：A股 241（今日实测）、**港股点数未测**（应为 331 上下，含早市与收市竞价段）；`avg_price` 非空且平滑 | 服务层只取 `dt/price/avg_price/volume`（`services/watch.py:593-597`） | `em_quote.py:287-302` |
| 5 | 09:30 | 字段号漂移：`f43/f47/f49/f51/f198` 语义不变；非数字仍回 `None` | 与第 4 节表逐一相符；整批变 `None` 就是接口改版 | `em_quote.py:53-54`、`em_quote.py:204-211` |
| 6 | 全天任意 | **限流触发条件**：连续猛打（例如 30 次 `get_quote`，间隔压到 0）→ 观察首次出现 `RemoteProtocolError` 的门槛，之后应立即抛「东财 push2.eastmoney.com 限流冷却中，还需 Ns」且 **N 递减、不外呼**（`stats["requests"]` 停在冷却那一刻） | 冷却 60s 内 `stats.requests` 不增长；`push2his` 仍可请求 | `em_quote.py:149-152`、`em_quote.py:173-180` |
| 7 | 任意 | 休市后定格时刻：逐笔最后时间戳（今日 A股 15:29:44、港股 16:08:08）、快照 `f43` 最后变动时刻；与 `watch_freeze_cn` 15:05 / `watch_freeze_hk` 16:10 对齐（`services/scheduler.py:53-58`） | 定格任务跑在逐笔停止之后；若港股 16:10 仍取不到收市竞价后数据，把 `watch_freeze_hk` 往后挪 | `em_quote.py:240-281` |
| 8 | 除权日 | 除权除息日单列：当日原始价与前一收盘的复权关系不比对，只报差异 | 宪法第 9 条要求单列报告，不与常规偏差混 | 第 8 节 |
| 9 | 盘前 | 空数据不冷却：`get_quote('999999.SZ')` 抛 `AdapterError`，紧接着正常标的**立即可用** | 已由离线用例锁住（`tests/test_quote_adapters.py:248-254`），真机再确认一次 | `em_quote.py:166-167` |
| 10 | 盘中 | 港股 `f51/f52` 是否恒为 52 周高/低（今日实测 656.7/375.2 与腾讯 52 周字段一致），而非涨跌停 | 若确认恒为 52 周，出参键名 `limit_up/limit_down` 对 `.HK` 必须改名或在服务层屏蔽 | `em_quote.py:222` |
| 11 | 盘中 | 单位换算复核：A股逐笔 `amount = price×volume×100`、港股 ×1；`volume_unit` 分别为 `lot`/`share` | 抽样 10 笔与成交明细肉眼一致 | `em_quote.py:247`、`em_quote.py:280` |
| 12 | 任意 | 分钟线周期真机复核（**必做**，静默错周期只能真机暴露）：1/5/15/30/60 各取一次，`dt` 间隔中位数 = 请求周期 | 今日实测 240/144/48/24/12 根、间隔 1/5/15/30/60 分钟 ✓（已复核过一轮，接口变更后重跑） | `em_quote.py:60-62` |
| 13 | 任意 | `push2his` 断连时：`get_history` 抛 `ConnectionLostError` 且 `push2` 不受牵连（分别冷却） | 本机 venv 内今日 5/5 成功；**外来解释器曾出现 `RemoteProtocolError`** → 正是冷却分支，换 venv 复现一次 | `em_quote.py:173-180`、`em_quote.py:309-310` |

## 10. 已知坑

| # | 现象 | 原因 | 处置 |
|---|---|---|---|
| 1 | 分钟线周期不对但「看着正常」 | `klt` 曾按秒写；`M5→"60"` 被接口当成 60 分钟 | 已改为分钟数（`em_quote.py:57-62`），第 3 节实测表逐项复核，离线锁在 `tests/test_quote_adapters.py:168-193`。**接口变更后必须重跑第 9 节清单第 12 条** |
| 2 | 隔夜/跨日补跑逐笔会把昨日成交打成今日 `dt` | `details` 只给时间不给日期，日期用 `pd.Timestamp.now()` 拼（`em_quote.py:276-277`） | **本通道只能当日取数**。收盘定格必须当日跑（`services/scheduler.py:53-58`）；不要指望"明天补昨天的逐笔"，那是另一条接口（历史分笔，本通道未实现） |
| 3 | 把 `em_quote` 提到实时链首 → 盯盘/监控五档整列消失 | `to_tick` 的 `bids/asks` 恒为空（`em_quote.py:237`），而 `realtime_feed()` 只看 `REALTIME_TICK`/`REALTIME_BAR`（`channels.py:201-202`） | **不要**把 `em_quote` 写进 `feed_priority_realtime` 的前列（默认串里没有它，`config.py:53`）；要五档请用腾讯，本通道的价值是逐笔/分时/扩展字段 |
| 4 | 港股出参里 `limit_up/limit_down` 是 52 周高/低 | 东财对 `116.` 标的复用 f51/f52 给 52 周高低（实测 656.7/375.2，与腾讯 52 周字段逐位相同） | 展示层不得对 `.HK` 标「涨停/跌停」；判据取 `is_hk`（计算在 `em_quote.py:196`、出参在 `em_quote.py:230`）。第 9 节清单第 10 条复核多标的后决定是否改键名 |
| 5 | 「逐笔量 > 官方量」被当成 bug | 东财逐笔含盘后成交/竞价段，官方内外盘不含 | 口径差，如实显示 `tick_vs_official_pct`（`services/watch.py:545-547`），**不改数、不截断**；主口径永远是官方外内盘（`services/watch.py:505-511`） |
| 6 | 换手/市盈/净利润为 0 | 接口对部分标的给 `"-"` | `num()` 统一转 `None`（`em_quote.py:204-211`），前端显示「—」而不是 0；港股 `f105` 实测 0，**不作财务依据**（财务口径走 iFinD/Tushare/AKShare） |
| 7 | A股逐笔金额少两个数量级 | 逐笔量单位是手不是股 | `vol_mult=100`（`em_quote.py:247`），港股 `116.` 前缀为 1；离线断言 `tests/test_quote_adapters.py:235-245` |
| 8 | 整源 60 秒打不开 | 连接被掐后进入冷却（设计如此） | 面板显示「限流冷却中，还需 Ns」（`describe()["cooldown"]`）；服务层休市期不外呼（`services/watch.py:379-398`），不会叠加撞墙 |
| 9 | `data` 为空也把整源锁 60 秒 | 早期把「标的没数据」也当断连 | 已改为抛 `AdapterError` 且**不冷却**（`em_quote.py:141-145`、`em_quote.py:166-167`），原因写在 `_json` docstring 里 |
| 10 | 分时最后一点缺失 | 当分钟未走完 | 按接口原样返回不补零（补零会在图上出现假下垂，`em_quote.py:291-301`） |
| 11 | 一次 `get_transactions` = 2 次外呼，港股单日可达 2 万+ 行 | 头段 + 回溯段拼接（`em_quote.py:251`） | 服务层共享缓存（`services/watch.py:473-490`）：翻页、筛买卖、算分层都用同一份取数，**别在循环里调它** |

## 11. 退回路径

本通道是**纯新增文件 + 注册表目录扫描自动发现**（`quantdesk/adapters/__init__.py:79` / `:86`，无集中注册点需要回滚）：

- 改了什么：新增 `quantdesk/adapters/data/em_quote.py`（`quantdesk/adapters/data/` 多一个文件即多一个通道）；
  **未修改** `core/` / `engine/` / `services/` / `store/` 来迁就它；本文档 `docs/integration/em_quote.md` 随之新增。
  注意：与它配对的产品改动（`services/watch.py`、`api/routes_watch.py`、`tests/test_watch.py`、前端 `WatchPage.tsx`、
  `store/models_db.py` 里的 `daily_flow` 表、`config.py` 的 `watch_*` 字段、`feed_priority_realtime` 加 `tx_quote`）
  属于同一轮的看盘功能，**不在本通道退回范围内**。
- 怎么退回去：
  1. **停用而不删码**（推荐，可逆）：盯盘服务是硬取 `hub.feed("em_quote")`（`services/watch.py:253`），
     因此停用方式是让装配失败——最干净的做法是删文件（见下条），或者临时把 `RateLimiter` 之外的一切
     明细调用绕开：在设置页关掉 `watch_enabled`（`config.py:59`）停掉定时任务，页面明细区自行降级。
  2. **完全删除本通道**：删 `quantdesk/adapters/data/em_quote.py`，并同步删
     `tests/test_quote_adapters.py` 里东财那部分用例（同一文件也覆盖腾讯，整删会连带腾讯的离线锁消失）。
     自检：`uv run python -c "from quantdesk.adapters import describe_adapters; print([r['name'] for r in describe_adapters()])"`
     里不应再出现 `em_quote`。
- 删除后的行为（按代码路径确认，不是猜测）：
  `WatchService._feeds()` 拿 `hub.feed("em_quote")` 返回 None、`feed_errors` 写「未注册或被禁用」
  （`services/channels.py:144-146`），于是——
  A股买卖量仍能给出（官方内外盘来自腾讯，`services/watch.py:505-511`）；
  逐笔/分时/扩展字段区如实报错：`res.caveat += "；东财逐笔通道未就绪，无明细分层"`（`services/watch.py:513-516`）、
  `ticks()` 返回 `{rows:[], total:0, error:...}`（`services/watch.py:616-618`）、
  `detail()` 把异常原样写进 `quote_extra_error` / `intraday_error`（`services/watch.py:585-600`）；
  **港股主动买卖量退化为无口径**（腾讯港股不给内外盘、东财补字段这一路断了，`services/watch.py:344-354`），
  界面按 `flow_source='quote_only'` + caveat 显示，不拿五档变动去凑一个看起来像的数。
- 反向恢复：把文件放回原路径即可，注册表自动发现，无需改任何其它文件。
