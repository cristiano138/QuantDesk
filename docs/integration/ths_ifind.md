# 同花顺 iFinD 数据通道接入卡

> 定位：宪法里的**付费专业数据源**——财务数据主源 + 历史行情 + 实时快照 + 板块/资讯。
> 适配器 `quantdesk/adapters/data/ths_ifind.py`（`ADAPTER = ThsIFindFeed`，name=`ths_ifind`，
> label=`同花顺 iFinD（付费）`，requires=`("iFinDPy",)`，official=True，
> capabilities=`HISTORY_BAR · FINANCIAL · REALTIME_TICK · TRANSACTION`）。
> 财务降级链首位：`feed_priority_financial = "ths_ifind,tushare,akshare"`（config.py 既有字段）。
> 敏感信息（账号/密码/refresh token）只进本地 DPAPI 加密保险库（`core/secrets.py`），
> 不入库明文、不进日志（只打前 4 位 + `***`）、不进 git、不进提示词。

## 1. 行政前置与安装

1. **商务开通**：向同花顺 iFinD 商务申请试用（一般 7–14 天）或正式账号。
   开通时需要指定**出口 IP / 机器码**——iFinD 是 IP 绑定的，换机器、换宽带、
   家里↔公司切换都会导致登录被拒（见 §9 已知坑）。
2. **拿 whl**：随账号分发 `iFinDPy-*.whl`（同花顺私有包，PyPI 上没有）。装法
   （**不改 pyproject 主依赖**，宪法第 8 条：SDK 属可选依赖）：

    ```bat
    uv pip install "D:\iFinD\iFinDPy-xxx-py3-none-any.whl"
    ```

3. **凭据录入**：设置页 → 凭据保险库，写 `ifind.username` + `ifind.password`
   （账号密码方式），或 `ifind.refresh_token`（token 方式，优先）。
   主线程在 `Settings` 落地 `ifind_*` 字段后，也支持 `QUANTDESK_IFIND_*` 环境变量注入。
4. **注册表行为**：whl 未安装时适配器仍然登记，`describe_adapters()` 报
   `status=unready / missing=["iFinDPy"]`；实例化调用 `connect()` 才抛带安装引导的
   `AdapterError`（可观测性优于静默消失，宪法 optional.py 约定）。

## 2. 凭据与登录态（代码收口点）

| 环节 | 实现位置 | 说明 |
|---|---|---|
| 取 refresh token | `ThsIFindFeed._resolve_token()` | 顺序：构造参数 `refresh_token` → 注入的 `token_provider()` callable → `_secret_lookup("ifind.refresh_token", "ifind_refresh_token")` |
| 取账号密码 | `ThsIFindFeed._resolve_credentials()` | 构造参数优先，其余走 `_secret_lookup("ifind.username"/"ifind.password", …)` |
| 唯一查找出口 | 模块级 `_secret_lookup(key, settings_attr)` | 内部只调 `core/secrets.resolve()`；DPAPI 不可用/保险库损坏 → 按「未配置」处理并抛 `ConfigError` 引导，**绝不退化成明文兜底** |
| 登录动作 | `ThsIFindFeed._login()` | 两种方式在此收口，二选一；成功记 `_login_date`/`_login_count` |
| 会话保证 | `_ensure_session()`（每次外呼前） | 未登录 → 登录；**跨自然日 → 自动重登** |
| 定时重登 | `refresh_session()`（公开、幂等） | 供 services 层 APScheduler 每日盘前（建议 08:30）调用；失败只记 warning 不崩调度 |

refresh token 方式的登录链（真实签名待核对，见 §7）：

```
iFinDGetAccessToken(refresh_token) → data.access_token → THS_iFinDLogin(access_token, "")
whl 不提供 iFinDGetAccessToken 时退化为 THS_iFinDLogin(refresh_token, "")
无 token 时走 THS_iFinDLogin(username, password)
凭据全空 → ConfigError（含录入引导）
```

**会话失效自动重登**：响应 `errorcode` 命中 `_SESSION_CODES` 或 `errmsg` 命中
`_SESSION_KEYWORDS` 关键字 → 重登一次 → 重试一次；再失败即抛 `ConnectionLostError`
并把 `_logged_in` 置 False（通道标记断线，交 FederatedFeed 降级 + ReconnectWatchdog 重连）。
**一次调用最多重试 1 次**（宪法 DataFeed 约定），不做静默降级、不无限重登。

**登录态与请求互斥**：iFinDPy 是**进程级单会话** SDK，登录态全局，多线程并发调用不安全。
本适配器用 `self._api_lock`（RLock）串行化「SDK 调用 + 重登」，重登线程不可能与
在途请求竞争登录态；实时推送回调不进这把锁（只入队）。

## 3. 能力矩阵

| 能力 | 支持 | 接口 | 说明 |
|---|---|---|---|
| 历史 K 线（1d/1w/1M/1m/5m/15m/30m/60m） | 是 | `THS_HistoryQuotes` | 复权由服务端参数控制（不像 tushare 本地乘因子） |
| 历史 tick 回放 | 否 | — | 抛 `FeedCapabilityError`；逐笔走 `THS_HF/THS_DS`（§7 待核对） |
| 财务三表 + 指标集 | 是 | `THS_FeedBatch` / `THS_BasicData` | 归一化为统一长表（§5） |
| 实时快照（五档） | 是 | `THS_RealTimeQuotes`（候选名探测） | 回调只入队 + 工作线程归一化 `Tick` |
| 逐笔/分笔成交 | 声明支持 | 接口待核对 | capabilities 含 `TRANSACTION`，实际拉取方法在在线核对后补 |
| 板块/资讯 | 待接入 | `THS_DataPool` / `THS_NEWS` | 本卡只登记规划，未落到代码（不写没验证过的东西） |

## 4. 历史行情：指标与参数映射（核对后只改常量，不动逻辑）

调用形态：`THS_HistoryQuotes(codes, indicators, func_params, start_time, end_time)`

| 适配器入参 | iFinD 参数 | 映射常量 | 取值 |
|---|---|---|---|
| —（固定） | `indicators` | `_HQ_INDICATORS` | `thstime,open,high,low,close,volume,amount` |
| `AdjustType.NONE` | `func_params["CTP"]` | `_CTP` | `none`（原始市场价——宪法：下单价不复权） |
| `AdjustType.FORWARD` | `func_params["CTP"]` | `_CTP` | `front`（前复权——回测与指标口径） |
| `AdjustType.BACKWARD` | `func_params["CTP"]` | `_CTP` | `back` |
| `Frequency.D1/W1/MO1` | `func_params["Interval"]` | `_INTERVAL` | `D` / `W` / `M` |
| `Frequency.M1/M5/M15/M30/H1` | `func_params["Interval"]` | `_INTERVAL` | `1` / `5` / `15` / `30` / `60` |
| —（固定） | `func_params["is_local"]` | 代码内 | `0`（走服务器，本地库不保证新鲜） |

- 证券代码：**与内部规范完全一致**（`600000.SH`），零转换（`core/symbols.py` 已登记 ifind 口径）。
- 返回：严格 `HISTORY_COLUMNS = [dt, open, high, low, close, volume, amount]`；
  `dt` 为 `datetime64[ns]` **升序**（源乱序会被排好）；数值列统一 `float64`（防 Parquet 分片 dtype 漂移）；
  无数据返回带列空 DataFrame（不抛异常）；字段缺失抛 `FeedCapabilityError`（接口变动的信号，不静默补 0）。
- 解析函数 `_parse_history_response(resp)` 是纯函数，离线单测直接覆盖，无需真账号。
- **口径红线**：iFinD 历史价入库前后必须与已接入源交叉对数（误差 ≤ 0.1%，除权日单列报告，
  宪法第 9 条）。参照 `docs/audit_order_path.md`：东财 qfq 已实测存在日收益分离缺陷，
  iFinD 的 `front` 口径正是用来替代它的候选主口径——**但替代结论必须由对数结果给出，不预设**。

## 5. 财务归一化契约（降级链第一优先源的主要代价）

`get_financial_data(symbol, report_type)` 的返回**永远是统一长表**：

| 列 | 含义 | 口径 |
|---|---|---|
| `symbol` | 内部规范代码 | `600000.SH` |
| `report_type` | 报表类型 | `income` / `balance` / `cashflow` / `indicator`（其余值抛 `FeedCapabilityError`） |
| `period` | 报告期/统计期 | 字符串原样保留源口径（如 `2023-12-31`），不猜不换算 |
| `indicator` | 指标 ID | iFinD 指标代码（如 `ths_revenue_stock`），**不做跨源改名** |
| `value` | 指标值 | 能转数值转 `float`，否则保留字符串；**空值行直接丢弃，绝不补 0/插值** |
| `unit` | 单位 | 源给了才填；源没给留空串（单位待 §7 核对，宁可空也不臆造） |
| `update_time` | 数据更新时间 | 取记录级 `updatetime`/`endtime`，缺省空串 |

iFinD 不同报表返回结构不一致，适配器 `_walk_financial_record()` 支持四种形态并抹平：

| 形态 | 结构 | 典型来源 |
|---|---|---|
| A | `data[i].table = {指标ID: [各期值]}` + `time` 标量/数组 | `THS_FeedBatch` 三张表 |
| B | `data[i].tbl = [{indicator, value, period, unit}, …]` | 长表型返回 |
| C | `data[i].tbl = [{time, 指标ID: 值}, …]`（每期一个 dict） | 多期批量 |
| D | 无 `table/tbl`，指标直挂 `data[i]` | `THS_BasicData` 单点指标 |

`report_type → 指标集 ID / 功能参数` 见文件顶部 `_FIN_SHEET` 与 `_FIN_FUNC_PARAMS`
（`F1` 报表类型码 / `F2` 频率 / `F3` 数据类型 / `F4` 截止日期 / `F5` 返回条数）——**这些 ID
是文档口径的占位映射，真实取值必须在线核对**（§7）。空结果**不写缓存**：不把「本期暂无」
固化成永久暂无。

> 代价声明：iFinD 上位成财务主源后，`indicator` 列是 iFinD 指标代码，与 tushare/akshare 的
> 列名体系不同。跨源比较必须先在 `indicator` 层建映射表（规划在 services 层财务落库时做），
> 并且**三张表入库后仍要与 tushare/akshare 交叉对数**（同 `report_type` + 同 `period`
> 的同名指标误差 ≤ 0.1%），对数不通过的指标降级用 tushare。

## 6. 配额纪律（付费源核心）

| 项 | 实现 | 默认值 |
|---|---|---|
| 当日阈值 | `daily_quota` 构造参数；缺省读 `Settings.ifind_daily_quota` | 字段未落地 → 常量 `_DEFAULT_DAILY_QUOTA = 3000` |
| 限速 | `min_interval`；缺省读 `Settings.ifind_min_interval` | 字段未落地 → 常量 `_DEFAULT_MIN_INTERVAL = 0.2s` |
| 当日计数 | `quota.json`（`{cache_dir}/quota.json`）落盘，跨重启不丢；跨自然日自动清零 | — |
| 计数时机 | **只有真正外呼才计数**（`_request()` 内，重试的那次也计数——服务端确实消耗了） | — |
| 超阈值行为 | **只读缓存，不发请求**；缓存也没覆盖到 → 抛 `AdapterError`，消息含「明日重跑/上调阈值」引导 | — |
| 缓存命中 | 同一请求第二次命中缓存 → **计数不增加**（DoD，`test_same_request_second_hit_does_not_consume_quota` 锁死） | — |
| 对外快照 | `quota_status()` → `{date, used, limit, left, exceeded}`，供适配器健康面板显示「iFinD 配额余量」 | — |

三层缓存：

1. **进程内**（`_mem`，最多 `_MAX_MEM_FRAMES = 64` 帧，FIFO 淘汰，读写都 copy 防污染）；
2. **Parquet 强缓存**（历史行情）：`ParquetStore(root=data/cache/ifind/parquet)`——
   **与主 parquet（data/parquet）分目录**，避免不同源在同一
   `{market}/{symbol}/{freq}/{adjust}` 路径下按 dt 去重互相覆盖（口径混淆）。
   命中判定要求本地已覆盖请求区间，端点落在非交易日时
   给区间容差（日线族 10 天 / 分钟族 4 天，`_COVERAGE_TOL_DAYS`）；
3. **长表缓存**（财务）：`{cache_dir}/fin_{symbol}_{report_type}.parquet`，TTL
   `_FIN_CACHE_TTL = 6h`，列不符 `FINANCIAL_COLUMNS` 视为 miss（防旧结构缓存毒化）。

## 7. 在线核对清单（真实账号到位后逐条打勾，改常量不改逻辑）

| # | 核对项 | 现状（代码里的占位口径） | 核对方法 |
|---|---|---|---|
| 1 | `errorcode` 取值表 | `_SESSION_CODES = {-1010,-1011,-1014,-1302}`、`_CONNECTION_CODES`、关键字兜底 | 官方 errorcode 文档 + 故意用错参数/断网各打一次，把真实码登记进本表并补进集合 |
| 2 | 登录接口签名 | `THS_iFinDLogin(user, pwd)`；token 方式先 `iFinDGetAccessToken(token)` | 试用文档/whl 内 `dir(iFinDPy)`；确认 access_token 是当账号还是当参数传 |
| 3 | `THS_HistoryQuotes` 参数顺序/形态 | `(codes, indicators, func_params, start, end)`，`func_params` 为 dict | 实调一次并打印 `type/keys`；确认日期格式（`YYYY-MM-DD` vs `YYYYMMDD`） |
| 4 | `CTP` 复权取值 | `none` / `front` / `back` | 拉一只除权股（如 601899）三口径对照，与 tushare `adj_factor` 手算结果比 |
| 5 | `Interval` 取值 | `D/W/M` + `1/5/15/30/60` | 分钟线实调，核对条数与时间戳粒度 |
| 6 | `table` 字段名 | `_HQ_FIELDS`：`thstime/open/high/low/close/volume/amount` | 打印真实 `data[0]["table"].keys()`；确认 `time` 在记录级还是 table 内 |
| 7 | 成交量/额单位 | 原样透传（不换算） | 与 tushare `vol`(手)/`amount`(千元)、akshare(手) 对齐登记，换算写在使用层而非适配器 |
| 8 | 财务指标 ID 清单 | `_FIN_SHEET` 四个报表指标集 + `_FIN_FUNC_PARAMS` 的 `F1..F5` | 用官方「指标查询工具」导出三张表指标 ID 全清单，落成 `docs/integration/ifind_financial_indicators.csv`（本期未建，核对后补） |
| 9 | 财务报表返回结构 | 兼容 A/B/C/D 四形态 | 四类 `report_type` 各拉一次，把真实 JSON 样本贴进本卡，回写单测脚本 |
| 10 | 实时订阅接口名 | 候选探测 `_RT_FUNC_NAMES = (THS_RealTimeQuotes, THS_RealtimeQuotes, THS_SubscribeSecurity)`；退订 `_RT_UNSUB_FUNC_NAMES` | 核对 whl 实际导出与回调签名（回调是 dict 还是 (codes,data) 元组） |
| 11 | 五档字段名 | `_RT_LEVELS`：`bidPrice1..5 / bidVolume1..5 / askPrice1..5 / askVolume1..5` | 盘中实推一条快照，比对真实键名；缺失档位数不补 0（宁缺毋造） |
| 12 | 逐笔成交（TRANSACTION） | 未落地取数方法 | 核对 `THS_HF`/`THS_DS` 参数后补方法；能力位先声明、接口先不假实现 |
| 13 | 板块/资讯 | 未接入（`THS_DataPool` / `THS_NEWS`） | 规划项，落到独立任务，不与本卡混做 |

## 8. 验收标准

1. **离线**：`uv run pytest tests/test_ths_ifind.py -q` 全绿（假 SDK `tests/mocks/iFinDPy.py`，
   不触网、不消耗真实配额）。已覆盖：历史解析归一化（列/dt 升序/dtype）、复权与周期参数映射、
   财务四形态抹平成同一长表、会话失效自动重登且只重登 1 次、配额超限走缓存不增计数、
   token 明文不进日志与异常、`health_check` 异常返回 False、能力不支持抛 `FeedCapabilityError`、
   实时回调只入队 + 工作线程归一化。
2. **在线（试用账号）**：
   - 三张表入库：`income/balance/cashflow`（+ `indicator`）四调用各自返回非空长表，
     列严格等于 `FINANCIAL_COLUMNS`；
   - 缓存命中不消耗配额：同一请求第二次调用后 `quota_status()["used"]` 不变，
     抓包/SDK 流水确认无外呼；
   - 交叉对数：与已接入源（tushare 优先，其次 akshare）同区间同口径日线误差 **≤ 0.1%**，
     除权日单列报告；差异超标先查复权参数口径（`CTP`）再查成交量单位；
   - 会话失效演练：手动把 token 置失效（或等 token 过期）后首次调用应重登成功且只重登一次；
   - 配额超限演练：把 `daily_quota` 调小跑一批请求，确认超限后只读缓存、报错消息带引导；
   - 日志脱敏复核：`data/logs/*.log` 全量 grep 账号/token 明文，命中即视为红线事故。
3. **不动共享文件**：本通道只新增 `ths_ifind.py` + 假 SDK + 单测 + 本卡；
   `core/`、`engine/`、`services/`、`api/`、`pyproject.toml`、`config.py` 零改动。
   装配走既有中枢：`services/channels.py` 按 `feed_priority_financial`
   （`ths_ifind,tushare,akshare`）逐个装配，whl 未装/凭据缺失时 `feed()` 返回 None
   并留痕原因（`feed_errors`），链条自动跳过它——**不需要为本通道改任何上游代码**。
   中枢以 `cls()` 构造（无凭据参数），凭据一律由 §2 的收口链路从保险库取。

## 9. 已知坑

- **不同报表结构不同**：三张表与指标集的返回形态不一致（§5 的 A/B/C/D），甚至同一接口
  不同指标集形态也不同。归一化必须留在适配器内；上层拿到非 `FINANCIAL_COLUMNS`
  就是接口变动信号（`FeedCapabilityError`），不要在下游打补丁。
- **登录态竞争**：iFinDPy 会话是进程级全局的，两个线程一个在重登、一个在发请求 →
  随机「未登录」。本适配器全程持 `_api_lock` 串行化；**新增方法必须走 `_request()`**，
  不要绕过它直接调 SDK。
- **IP 绑定 / 多机登录**：iFinD 按授权 IP/机器登录，同一账号多机并发通常被拒
  （错误码形如「登录数超限」）。换宽带/换机/热点上网都要重新找商务改授权；
  不要把 QuantDesk 的 iFinD 会话当成可在多进程间共享的资源（策略子进程只 import
  BaseStrategy/StrategyContext，适配器实例永不进入策略进程——宪法第 6 条本来就是对策）。
- **token 有效期**：refresh token/access token 有时效，「每日定时重登」不是可选优化：
  长期跑实时订阅会在某天集中报未登录。部署时把 `refresh_session()` 挂进盘前调度。
- **复权口径不可混用**：`CTP=front` 的前复权价随除权日整体平移，历史区间不同会得到
  不同的前复权价——**缓存按 `adjust` 子目录分存**
  （`data/cache/ifind/parquet/{market}/{symbol}/{freq}/{adjust}/{part}.parquet`），
  下单价一律 `AdjustType.NONE`（宪法第 5 条）。
- **配额是钱**：批量回补历史前先算 `quota_status()["left"]`；缓存未覆盖 + 已超阈值时
  适配器会硬拒（不外呼），这是设计不是 bug，别为了让它跑而把阈值调成无限。
- **空值不是 0**：财务长表丢弃空值行；上游若发现某指标缺期，那是源缺披露，
  不要用 `fillna(0)` 补回去（会污染估值类指标）。
- **单位口径**：`unit` 只在源给了才填，留空不代表「元/股」。跨源比较前先做单位对齐，
  否则误差 ≤ 0.1% 的校验会以倍数级差异失败（东财 qfq 审计已经踩过同类坑）。
