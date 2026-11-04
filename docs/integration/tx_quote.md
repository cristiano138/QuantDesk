# 接入卡 · 腾讯财经公开行情通道（`tx_quote`）

注册名 `tx_quote` · UI 名「腾讯财经公开行情（A股/港股，只读，非官方接口）」· 类型 `data`
适配器文件：`quantdesk/adapters/data/tx_quote.py`（类 `TencentQuoteFeed`，模块级 `ADAPTER` 导出在 `quantdesk/adapters/data/tx_quote.py:372`）
消费方：`quantdesk/services/watch.py:252`（盯盘快照主源）· `quantdesk/services/scheduler.py:119`（每日 K 线补齐）·
`quantdesk/api/routes_watch.py:171`（K 线接口回源标记）
离线单测：`tests/test_quote_adapters.py`（假 `httpx.MockTransport` 按下标表造行情串，锁单位/批量分片/能力边界/探活失败）·
服务层替身 `tests/test_watch.py:33`（`FakeTx`）· `tests/test_scheduler.py:68`

> 证据标注约定：本文里「实测」= 2026-11-04 收盘后在本机用项目 venv（`uv run python`）直连真接口跑出来的结果；
> 「待核对」= 代码/文档里靠推断、尚未在盘中复核的条目，全部汇总在第 9 节清单。

## 1. 定位与边界

只读看盘产品（不交易）的**实时主源 + 日线源**，A股与港股一视同仁：

| 用途 | 本通道角色 | 依据 |
|---|---|---|
| 盯盘池整池快照（价、量、额、**五档**、**外盘/内盘**、涨跌停、均价、市值） | 主源：一次 HTTP 请求覆盖 ≤60 只（`tx_quote.py:45`） | `services/watch.py:332` |
| 买卖量主口径 | 官方外盘/内盘直接取自本通道 `[7]/[8]`（`tx_quote.py:51`） | 第 8 节交叉对数 |
| 日/周/月 K 线（本地库补齐 + 盯盘图） | A股+港股日线源（`tx_quote.py:65`） | `scheduler.py:130-132` |
| 分钟线 | **不提供**（`tx_quote.py:278-280` 直接抛 `AdapterError`） | 分钟口径请走 `em_quote` 分时 |
| 逐笔成交 | **不提供**（capabilities 无 `TRANSACTION`） | 逐笔在 `em_quote` |
| 财务数据 | **不提供**（`tx_quote.py:309-312` 抛 `AdapterError`） | 财务链：iFinD → Tushare → AKShare |

路由事实：本通道在 `feed_priority_realtime = "qmt_xtdata,tdx_aidata,tx_quote"`（`quantdesk/config.py:53`）里排第三，
QMT/通达信未就绪时才是实时主源；**故意不进 `feed_priority_history`**，理由是日线没有成交额，
混进历史降级链会静默写入 `amount=0` 的降级数据（`quantdesk/config.py:57-58` 已把这条写死）。
港股日线不经过降级链，由盯盘服务直取本通道（`services/watch.py:263-265`）。

通达信为什么退到第二位：本机实测公开 pytdx 服务器（24 台）只回 `get_security_count`，K 线/快照/分笔一律 0 行
（`docs/integration/tdx_aidata.md` 第 3、8 节）。**公开服务器可用性随时间变化，需在交易时段重测**，
不能据此判定通达信通道永久不可用。

## 2. 前置与安装（无 SDK、无凭据、无新依赖）

- **无需任何可选 SDK**：`requires = ()`（`tx_quote.py:100`）。全部外呼只用 `httpx`，
  而 httpx 是 `pyproject.toml:20` 里的主依赖（`httpx>=0.27`）——本通道**没有新增任何依赖**，
  也不存在 `import_optional` 惰性导入分支（宪法第 8 条「可选依赖不进 pyproject」在这里不适用）。
- **无凭据**：`connect()` 不读保险库，`services/channels.py` 的 `_FEED_CREDENTIALS`（`channels.py:51-58`）里没有本通道条目，
  设置页不会出现表单，这是对的——公开接口没有账号可填。
- **`default_enabled = True` + `official = False`**（`tx_quote.py:99-101`）：注册表源头可见，
  但 UI 必须显示红字「非官方接口，无文档无 SLA」（宪法第 9 条），`describe()` 也回带 `official: False`（`tx_quote.py:368`）。
- 网络前置（唯一一条）：本机能出站访问 `https://qt.gtimg.cn` 与 `https://web.ifzq.gtimg.cn`。
  代理/防火墙拦掉时 `connect()` 抛带引导的 `ConnectionLostError`（`tx_quote.py:131-133`），不静默返回 None。

自检命令（实测可用）：

```bash
uv run python -c "from quantdesk.adapters.data.tx_quote import TencentQuoteFeed as F; f=F(); f.connect(); \
print(f.get_snapshot(['600000.SH','00700.HK'])['600000.SH'])"
```

## 3. 接口与参数映射

| 用途 | 真实 URL / 参数 | 代码位置 |
|---|---|---|
| 批量实时快照 | `GET https://qt.gtimg.cn/q=sh600000,sz000001,hk00700`（逗号拼接，**响应是 GBK**，`decode="gbk"`） | `tx_quote.py:41` / `tx_quote.py:194` |
| 日/周/月 K 线 | `GET https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},{day\|week\|month},{start:YYYY-MM-DD},{end:YYYY-MM-DD},{count},{qfq\|hfq\|空}` | `tx_quote.py:42` / `tx_quote.py:283-285` |
| 请求头 | `User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)` + `Referer: https://gu.qq.com/` | `tx_quote.py:43-44` |

批量与分页纪律：`_MAX_BATCH = 60`（`tx_quote.py:45`）——接口本身能吃更长列表，这里主动收敛，
防「一次带太多被裁包」（返回行数变少比报错更难查）。盯盘池默认 8 只（`config.py:61-62`），
整池一次请求即覆盖（实测 4 只 = 1 次外呼）。

K 线条数估算：`count = min(max(区间天数 × 1.6 + 10, 10), 800)`（`tx_quote.py:281-282`），
返回后按 `[start, end]` 二次过滤 + `sort_values("dt")` + `drop_duplicates("dt")`（`tx_quote.py:306-307`）。
**`count` 上限 800 与 1.6 倍系数是推断值**（第 9 节清单第 6 条）。

代码格式互转（只在 `core/symbols.py` 与本适配器内发生，宪法核心契约）：

| 内部代码 | 腾讯码（`from_internal(s,"tx")`，`symbols.py:166-167`） | 说明 |
|---|---|---|
| `600000.SH` | `sh600000` | 沪市 |
| `000001.SZ` | `sz000001` | 深市 |
| `830799.BJ` | `bj830799` | 前缀支持（`symbols.py:36`），**北交所本通道未实测** |
| `00700.HK` | `hk00700` | 港股统一 5 位（`symbols.py:88`） |

入码方向（用户在盯盘页手输的写法）由 `normalize()` 兜住（`symbols.py:56-79`）：
`hk00700` / `116.00700` / `700.HK` / `0700` / `00005` 一律归一成 `00700.HK` / `00005.HK`
（裸 1–5 位数字按港股处理并 `zfill(5)`，`symbols.py:77-78`；实测用例见 `tests/test_watch.py:128-143`）。
快照回读时同样归一：A股走 `normalize(parts[2])`，港股走 `parts[2].zfill(5) + ".HK"`（`tx_quote.py:223-224`），
最终键由 `get_snapshot` 用请求侧内部代码覆盖（`tx_quote.py:261`），保证「请求什么码就回什么码」。

## 4. 字段下标表（行情串按 `~` 切分，0 基）

改口径只改这两张表（`tx_quote.py:48` 的注释就是这么约定的），不动逻辑。
`_f()`（`tx_quote.py:69-79`）对缺失/空串/非数字一律回默认值——**接口给空位不会抛**，
所以字段漂移表现为「数字变小/变 0」而不是报错，第 9 节清单第 2 条专门盯这件事。

### 4.1 A股 `_TX_LAYOUT_A`（`tx_quote.py:49-56`）

| 下标 | 语义 | 单位 | 实测值（600000.SH，2026-11-04 收盘后） |
|---|---|---|---|
| 1 | 证券简称 | — | 浦发银行 |
| 2 | 6 位裸代码 | — | 600000 |
| 3 | 最新价 | 元 | 10.09 |
| 4 | 昨收 | 元 | 10.09 |
| 5 | 今开 | 元 | 10.05 |
| 6 | 成交量 | **手** | 824086 |
| 7 | **外盘（主动买入累计）** | 手 | 458432 |
| 8 | **内盘（主动卖出累计）** | 手 | 365654 |
| 9~18 | 买一~买五（价,量 交替） | 元 / 手 | 10.09/325、10.08/482、10.07/1996、10.06/3434、10.05/3552 |
| 19~28 | 卖一~卖五（价,量 交替） | 元 / 手 | 10.10/320 … 10.14/135 |
| 30 | 行情时间戳 | `YYYYMMDDHHMMSS` 文本 | 20261104161457 |
| 33 / 34 | 当日最高 / 最低 | 元 | 10.23 / 10.05 |
| 37 | 成交额 | **万元**（`amount_unit=10000.0` → 归一成元） | 83502 → 835020000.0 |
| 44 / 45 | 总市值 / 流通市值 | 亿元（`mv_unit=1e8`） | 3360.56 / 3360.56 |
| 46 | 市净率 | 倍 | 0.44 |
| 47 / 48 | 涨停 / 跌停 | 元 | 11.10 / 9.08 |
| 51 | 均价 | 元 | 10.13 |

**未被表使用、但本次实测看到的下标**（写下来供下次核对，不要靠它们做事）：
31/32 涨跌与涨跌幅（收盘后为 `0.00`/`0.00`，与 `last/prev_close-1` 一致）、35 `最新/量/额(元)` 三段串
（`10.09/824086/835024728`，可用于校验 `[37]` 的万元截断）、36 量（手）、38 换手率（0.25）、
43 振幅（1.78）、49 量比（0.98）、52/53 两个市盈率类字段（6.21 / 6.72；东财 f162 市盈(动)=6.21，
推测 52 为市盈(动)，**含义待核对**）。

### 4.2 港股 `_TX_LAYOUT_HK`（`tx_quote.py:57-64`）

| 下标 | 语义 | 单位 | 实测值（00700.HK，2026-11-04 收盘后） |
|---|---|---|---|
| 1 / 2 | 简称 / 5 位代码 | — | 腾讯控股 / 00700 |
| 3 / 4 / 5 | 最新 / 昨收 / 今开 | 港元 | 401.600 / 397.200 / 397.800 |
| 6 | 成交量 | **股**（港股无统一整手概念） | 18500105.0 |
| 7 / 8 | 外盘 / 内盘 | — | **接口给 0**，表里写 `None` → 归一后为 `None`（`tx_quote.py:218-219`），买卖量改由东财补（第 6 节） |
| 9~18 / 19~28 | 五档 | 港元 / 股 | 收盘后整档为 0 → `_levels()` 丢弃（`tx_quote.py:82-89`），实测 `bids/asks` 为空 |
| 30 | 行情时间戳 | `YYYY-MM-DD HH:MM:SS`（**与 A股格式不同**） | 2026-11-04 16:08:16 |
| 31 / 32 | 涨跌额 / 涨跌幅 | 港元 / % | 4.400 / 1.11 |
| 33 / 34 | 最高 / 最低 | 港元 | 402.200 / 391.600 |
| 37 | 成交额 | **港元**（`amount_unit=1.0`，不换算） | 7372563093.200 |
| 44 / 45 | 总市值 / 流通市值 | 亿港元 | 36516.0616 |
| 48 / 49 | 52 周最高 / 最低 | 港元 | 656.700 / 375.200 |

港股串共 78 段、A股串 88 段（实测）；`_raw_quotes` 用 `len(parts) > 30 and parts[2]` 作最低门槛（`tx_quote.py:202`），
门槛以上才入表——**接口改版缩字段时，本通道是「少标的缺席」而不是「整批报错」**，
缺席在 `WatchService.list_rows()` 里表现为 `stale=True`（`services/watch.py:422`）。
港股下标 46 是英文简称 `TENCENT`（实测），与 A股表的 46=市净率不可互换。

归一后的行结构（`tx_quote.py:222-243`，盯盘服务与前端就认这套键）：
`symbol / tx_code / name / last / prev_close / open / high / low / volume / volume_unit(lot|share) /
amount / outer_volume / inner_volume / bids / asks / total_mv / float_mv / limit_up / limit_down /
avg_price / pct_chg / quote_time / ts_ms`。`pct_chg` 由 `(last/prev_close-1)*100` 现算，不取接口字段（`tx_quote.py:240`）。
`to_tick()`（`tx_quote.py:265-269`）只搬 宪法 `Tick` 认识的字段，内外盘在 Tick 里丢失——
**所以买卖量口径必须走 `WatchService`，不能从 Tick 反推**。

## 5. 能力矩阵与通道元数据

| 项 | 值 | 代码位置 |
|---|---|---|
| `capabilities` | `{REALTIME_TICK, HISTORY_BAR}` | `tx_quote.py:103` |
| `official` | **False**（公开网页接口 → UI 红字） | `tx_quote.py:99` |
| `requires` / `requires_any` | `()` / 未声明（False） | `tx_quote.py:100` |
| `default_enabled` / `enable_flag` | True / ""（注册表源头可见） | `tx_quote.py:101-102` |
| `prereq` | 一句话说清「能出站访问两个域名即可 + 字段变了要改两张表」 | `tx_quote.py:97-98` |
| `label` | 中文名带「只读、非官方接口」字样 | `tx_quote.py:96` |

注册表实跑回显（本机 `uv run python` + `describe_adapters()`，2026-11-04）：

```text
tx_quote | data | ready | official=False | ['history_bar', 'realtime_tick']
```

**没有 `REALTIME_BAR`**：本通道 tick 转 bar 由上游 `RealtimePump`/子进程按周期起点聚合，适配器不自造 bar。
**没有 `TRANSACTION`**：逐笔在东财。元数据读取入口是 `adapters/base/optional.py:119` 的 `meta_of()`；
`capabilities` 不含 `HISTORY_BAR` 的通道进不了历史降级链（`services/channels.py:187-189`），本通道含之，
但因第 1 节的 `amount=0` 理由，只被 `watch`/`scheduler` 直取，不进默认历史链。

## 6. 单位与价格口径

| 项 | A股 | 港股 | 换算发生在哪 |
|---|---|---|---|
| 成交量 | **手**（1 手 = 100 股），`volume_unit="lot"` | **股**，`volume_unit="share"` | `tx_quote.py:229-230`，不换算 |
| 成交额 | 接口给万元 → 归一为**元**（×10000） | 接口直接给港元，×1 | `tx_quote.py:53` / `tx_quote.py:61` / `tx_quote.py:217` |
| 市值 | 亿元 → 归一为元 | 亿港元 → 归一为元（口径仍是港币金额，币种另说） | `mv_unit=1e8`，`tx_quote.py:235-236` |
| 币种 | CNY | HKD（本通道不回报币种字段） | 服务层按 `.HK` 判定 `currency`（`services/watch.py:498`） |
| 价格 | **不复权原始市场价**（宪法第 5 条） | 同左 | 快照不做任何复权 |
| 历史复权 | 请求 `qfq/hfq` 时取 `qfqday/hfqday` 键 | **港股无 qfq/hfq 键**（实测） | `tx_quote.py:293-295` 的回退分支，见第 10 节坑 1 |

三条必须让下游知道的诚实话：

1. **日线没有成交额**：腾讯 K 线每行只有 6 列 `[dt,open,close,high,low,volume]`（实测，A股与港股都是），
   归一化把 `amount` 一律填 `0.0`（`tx_quote.py:301`），**不做价格×量的近似**（那是伪造）。
   需要金额口径请用 akshare/tushare（`tx_quote.py:276` 的 docstring 明写）。
   本地库现状实测：`data/parquet/SH/600000.SH/1d/forward/2026.parquet` 201 行里 200 行有 amount、
   唯独 2026-11-04（当天由腾讯补的那行）为 0——这正是 `services/data_store.py:96-107`
   允许「把 `amount=0` 升级成有金额」的原因。
2. **万元截断**：`[37]=83502` 万元归一后是 835,020,000 元，东财 f48 给 835,024,728 元，
   差 4,728 元（相对 0.00057%，在宪法 ≤0.1% 之内）。要精确成交额以**东财/akshare 的元口径**为准。
3. **收盘后字段行为因市场不同**：本文件顶部注释写「收盘后五档为 0 属正常」，
   实测 A股收盘后 `[9]~[18]` **仍保留最后一档挂单量**（600000 买一 10.09/325），港股整档为 0。
   「五档是否只保留盘中状态」按市场分别核对（第 9 节清单第 3 条），不要拿一条注释当两市场共识。

## 7. 限流与韧性纪律

| 机制 | 取值 | 代码位置 | 为什么这么定 |
|---|---|---|---|
| 最小请求间隔 | `RateLimiter(0.25)` + 20% 抖动 | `tx_quote.py:46` / `tx_quote.py:109` / `core/ratelimit.py:16` | 整池一次请求，0.25s 足够；抖动防「同相位齐发」 |
| 单次超时 | `timeout=8.0`（连接+读） | `tx_quote.py:106` / `tx_quote.py:126-127` | 公开接口 8s 不回就当死 |
| 连接池 | `max_connections=8` | `tx_quote.py:127` | 单进程桌面应用，不给它开大池的机会 |
| 重试 | **最多 1 次**，间隔 0.3s，仍失败抛 `ConnectionLostError` | `tx_quote.py:172-187` | 宪法 DataFeed 约定：重试风暴归 `FederatedFeed` |
| 探活 | 取 `sz000001` 一行，任何异常→False | `tx_quote.py:137-142` / `tx_quote.py:156-162` | `connect()` 即探活，失败不给「假连接」状态 |
| 装配冷却 | 失败后 60s 内不再重新实例化+connect | `services/channels.py:49` / `channels.py:131-165` | 面板/盯盘反复点不致于把外呼打满；`force=True` 绕过（「测试连接」按钮） |
| 轮询线程 | 订阅式：`tx-quote-poll` 线程，默认 3s，下限 0.5s | `tx_quote.py:314-330` / `tx_quote.py:339-358` / `tx_quote.py:360-361` | 接口无推送，tick 的 `dt` 是**采集时刻**（`ts_ms`），不是交易所撮合时刻 |
| 单标的解析失败 | 记 warning 后跳过，不拖垮整批 | `tx_quote.py:258-260` / `tx_quote.py:354-357` | 一只解析错不能让整个盯盘池空 |
| 观测 | `stats{requests,retries,errors,rows}` + `last_ok_at` + `describe()` | `tx_quote.py:119` / `tx_quote.py:364-369` | `stats.requests` **包含 connect 探活那一次**（实测 1 次探活 + 1 次批量 4 只 = `requests=2, rows=5`） |

失败路径是**如实上报**而不是静默退化：`WatchService.refresh()` 拿到 `QuantDeskError` 时
把缓存旧行标 `stale=True` 返回、写 `last_error`、`stats["errors"]+=1`（`services/watch.py:331-337`），
前端因此能看到「这是最后一次已知值」而不是「数字变成了 0」。

自动外呼的总闸：`refresh_auto()` 在 A股与港股**都休市**时不外呼，返回当日最后一次快照；
但缓存为空（当天第一次打开页面）先种一次（`services/watch.py:379-398`）。
时段判定在 `services/watch.py:53-63`（A股 09:15-09:30 / 09:30-11:30 / 11:30-13:00 / 13:00-15:00 / 15:00-15:59，
港股 09:00-09:30 / 09:30-12:00 / 12:00-13:00 / 13:00-16:00 / 16:00-16:59），`post_close` 也算「活着」，
因为盘后定价成交仍在动（实测 600000 逐笔最后一笔 15:29:44）。

## 8. 与其他源交叉对数（2026-11-04 收盘后，本机重跑；样本 600000.SH / 000001.SZ / 00700.HK / 09988.HK）

| 对项 | 本通道 | 对照源 | 结果 | 判定 |
|---|---|---|---|---|
| 外盘（主动买入累计） | `[7]` = 458432 手 | 东财 `f49` = 458432 | **逐位相等** | 实测 ✓ |
| 内盘（主动卖出累计） | `[8]` = 365654 手 | 东财 `f47 − f49` = 824086 − 458432 = 365654 | **逐位相等** | 实测 ✓ |
| 总量恒等式 | 458432 + 365654 = 824086 = `[6]` | 000001.SZ 同样成立：723215 + 714691 = 1437906 | 成立 | 实测 ✓ |
| 成交量（日线） | 日线 `[5]` = 824086 手 | 东财日线 `klines` 末行量 = 824086；快照 `f47` = 824086 | 逐位相等 | 实测 ✓ |
| 成交额 | 快照 `[37]` → 835,020,000 元 | 东财 `f48` = 835,024,728 元 | 差 0.00057%（万元截断） | 实测 ✓（≤0.1%） |
| 均价 / 换手 / 振幅 / 量比 / 涨跌停 / 市净率 | `[51]`10.13 / `[38]`0.25 / `[43]`1.78 / `[49]`0.98 / `[47]`11.10 `[48]`9.08 / `[46]`0.44 | 东财 `f71` `f168` `f171` `f50` `f51` `f52` `f167` | 全部相等 | 实测 ✓ |
| 日线收盘价（27 个共同交易日） | `get_history(D1, qfq)` 与 `get_history(D1, 原始)` | 东财 `push2his` 日线 `fqt=1` 与 `fqt=0` | **最大收盘差 0.0000%** | 实测 ✓ |
| 港股日线收盘价（31 个共同交易日） | 同上 | 同上 | 最大收盘差 0.0000% | 实测 ✓（但复权键见坑 1） |
| 港股成交量 / 成交额 | `[6]` = 18,500,105 股 / `[37]` = 7,372,563,093.2 港元 | 东财 `f47` = 18,500,105 / `f48` = 7,372,563,200 | 量逐位相等，额差 106.8 港元（1.4e-6） | 实测 ✓ |
| 港股内外盘 | `[7]/[8]` = 0/0（不提供） | 东财 `f49`=10,473,682、内盘 `f47−f49`=8,026,423 | 本通道不提供 → 由东财补 | 实测 ✓ |

**这一节是「买卖量主口径=官方外盘/内盘」的全部依据**（`services/watch.py:505-511` 把口径与 caveat 写死在服务层）。
宪法第 9 条要求的「新数据源交叉对数 ≤0.1%」在上述常规项上都过了；
**除权除息日单列报告这一项本轮未做**（本窗口 2026-09-20~11-04 内 600000/00700 未出现复权分离，属偶然，
不能当结论）——列为第 9 节清单第 7 条。

## 9. 在线核对清单（下一个交易日 09:30 起逐条勾选；未勾选项一律视为「靠推断」）

命令模板（不写进代码、临时跑）：

```bash
uv run python -c "from quantdesk.adapters.data.tx_quote import TencentQuoteFeed as F; f=F(); f.connect(); \
import json; s=f.get_snapshot(['600000.SH','000001.SZ','00700.HK','09988.HK']); \
[print(k, {x:s[k][x] for x in ('last','volume','volume_unit','outer_volume','inner_volume','amount','bids','asks')}) for k in s]"
```

| # | 时刻 | 勾选项 | 预期（今天的实测基线） | 代码位置 |
|---|---|---|---|---|
| 1 | 09:30 / 10:30 / 14:00 各一次 | 盘中 `[6]/[7]/[8]` 与东财 `f47/f49` 是否同步跳动（同一秒取两源） | 全程 `外盘相等 且 外盘+内盘=总量`；今日基线 458432/365654/824086 | `tx_quote.py:51-52` |
| 2 | 09:30 首笔后 | 逐字段下标是否漂移：`[3]` 最新、`[6]` 量、`[30]` 时间戳格式（A股 `YYYYMMDDHHMMSS`、港股带 `-`） | 与第 4 节两张表逐一相符 | `tx_quote.py:49-64` |
| 3 | 09:30 与 15:30 | 五档是否齐 5 档：A股 `len(bids)==5 and len(asks)==5`；港股 00700/09988 同样检查 | 盘中共 5 档；收盘后 A股仍留挂单量、港股清零（今日实测） | `tx_quote.py:82-89` |
| 4 | 09:30 | 集合竞价阶段（09:15-09:30）本通道给什么：`[7]/[8]` 是否已计入竞价量 | 与东财 `f49` 口径一致（今日 09:25 后逐笔已含竞价，官方内外盘是否含需确认） | `tx_quote.py:51-52` |
| 5 | 收盘后（15:05 / 16:10） | 数字定格时刻：本通道 `[30]` 时间戳停在哪一秒、A股盘后定价（15:00-15:30）是否继续动 `[6]/[7]` | 今日 A股最后行情时间 16:14:57（已含盘后更新），港股 16:08:16 | `tx_quote.py:52` / `services/watch.py:57` |
| 6 | 全天 | K 线 `count` 上限与 1.6 倍系数：请求 800 根与更长区间，核对返回条数/缺口 | 单次 ≤800 根；区间过滤后无重复 dt | `tx_quote.py:281-282` |
| 7 | 除权日 | 抽 1 只当日除权的 A股，比 `[3]` 原始价与东财 `fqt=0` 日线收盘 | 完全相等（都是原始价）；复权口径差异单列，不与常规偏差混 | 第 6 节口径 |
| 8 | 盘中 | 港股 qfq 请求是否真的只回 `day`（今日实测如此）→ 若是，港股复权必须显式声明不可用 | 期望：返回键只有 `day`；此时 `get_history(adjust=FORWARD)` 给的是原始价 | `tx_quote.py:293-295` |
| 9 | 全天 | 限流触发条件：盯盘 5s 一次整池（默认 8 只）连跑一天，看 `stats.errors`/`requests` 与是否出现空返回 | 0 错误、无「整批空返回」；出现即把 `watch_poll_interval_s` 调大并记录 | `tx_quote.py:119` / `config.py:60` |
| 10 | 全天 | 单位换算复核：A股 `volume_unit=="lot"`、港股 `=="share"`；前端展示单位由服务层统一成「手/股」 | 与第 6 节表一致；错一次就是 100 倍 | `tx_quote.py:229-230` / `services/watch.py:68-76` |
| 11 | 15:25 / 16:25 | `watch_kline_*` 任务后自检：本地库该标的是否新增当日 dt、dt 单调不重复、原行不丢 | 无「自检失败」日志（自检本身在 `data_store.py:109-122`） | `scheduler.py:107-139` |
| 12 | 盘前 | 北交所 `bj` 前缀代码是否可用（本通道唯一未测的市场，`normalize` 支持） | 取到 `bj8xxxxx` 快照且字段齐全；取不到就在路由里排除 | `symbols.py:36` / `symbols.py:166-167` |

## 10. 已知坑

1. **港股「前复权」拿到的是原始价**（实测 + 代码可证）：腾讯港股 K 线只回 `day` 键，
   而 `get_history` 的取值是 `node.get(f"{adjust}day") or node.get("day")`（`tx_quote.py:293-295`）——
   回退分支会把「请求 qfq、拿到原始」这件事静默吞掉。本地库实测证据：
   `data/parquet/HK/00700.HK/1d/forward/2026.parquet` 与 `.../none/2026.parquet` 207 行**完全相同**。
   后果与处置：`scheduler.py:130-132` 每天对港股同时跑 FORWARD 与 NONE 两个口径，港股的「前复权桶」实际是原始价序列。
   除权日前后做指标/复盘时**不得把港股 forward 桶当复权价用**；
   根治要么在服务层对 `.HK` 只取 NONE 口径，要么让 `get_history` 在键缺失时如实报「该市场不支持复权」——
   两者都需要改代码，本轮按任务边界只登记坑，不动实现（第 9 节清单第 8 条复确认）。
2. **日线没有成交额**（实测）：`amount` 恒 0.0（`tx_quote.py:301`）。任何「用日线 amount 算换手/资金流」的下游
   都必须先看这条，`routes_watch.py:174` 已经把 note 写给前端了。
3. **无推送**：tick 的 `dt` 是采集时刻（`tx_quote.py:266`），固有延迟≈一个轮询周期；
   盘中延迟敏感的场景（策略驱动）不要把本通道当第一实时源，它在路由里排第三是有意的（`config.py:52-53`）。
4. **字段空位不报错**：`_f()` 一律回默认（`tx_quote.py:69-79`），接口改版/字段错位时表现为「数字偏小或为 0」，
   而不是异常。巡检只能靠第 9 节清单第 1、2 条的**跨源同刻比对**，别指望日志。
5. **两条轮询路径叠加**：面板/策略订阅实时时，本通道内部 3s 轮询线程会被起来（`tx_quote.py:339-358`），
   与 `watch_poll` 任务（默认 5s，`scheduler.py:47-51` + `services/watch.py:379-398`）各打各的；
   限速器只保证相邻两次外呼 ≥0.25s，不保证「整池一秒只打一次」。
   盯盘池扩到几十只且开着实时监控时，先看第 9 节清单第 9 条的计数。
6. **非官方接口、无 SLA**：`official=False`（`tx_quote.py:99`）。接口改版不设通知，
   两张下标表就是唯一的口径锚点；改了表必须同步改第 4 节，并把那次实测值写进卡片。
7. **收盘后五档为 0 不能一概而论**：港股为 0、A股保留最后一档（实测）。前端若按「五档为空即断线」显示，
   港股收盘后会一直红——判据应当是 `quote_time`/`fetched_at`，不是 `len(bids)`。
8. **下标漂移拦不住在 CI 里**：适配器级离线用例已经有了（`tests/test_quote_adapters.py:91-164`：单位/五档/内外盘
   置 None/批量分片/能力边界/探活失败），但夹具是**按 `_TX_LAYOUT_*` 同一张表拼出来的**
   （`tests/test_quote_adapters.py:76-81`）——接口把整表字段往后挪一位时，用例照样绿。
   所以「字段漂移」只能靠第 9 节清单第 1、2 条的**跨源同刻比对**兜住；
   下次维护时把第 4 节两张表的实测原始串（`v_sh600000="1~浦发银行~600000~…"` 整行）当固定字符串存进夹具，
   而不是按下标表组装，才能把这件事变成回归锁。
9. **北交所未测**：`tx` 前缀支持 `bj`（`symbols.py:36`），但未实跑，路由要能容错（清单第 12 条）。

## 11. 退回路径

本通道是**纯新增文件 + 注册表目录扫描自动发现**（`quantdesk/adapters/__init__.py:79` / `:86`，无集中注册点需回滚）：

- 改了什么（相对 v1.1.0 之后）：
  - 新增 `quantdesk/adapters/data/tx_quote.py`（未修改任何既有 `core/engine/services/store` 文件来迁就它）；
  - `quantdesk/config.py:53` 的默认值 `feed_priority_realtime` 从 `"qmt_xtdata,tdx_aidata"` 改为
    `"qmt_xtdata,tdx_aidata,tx_quote"`（工作区未提交改动，原值见 `git diff quantdesk/config.py`）；
  - 盯盘相关新增文件（`services/watch.py`、`api/routes_watch.py`、前端 `WatchPage.tsx`、`store` 的 `daily_flow` 表）
    是同一轮的产品改动，不属于本通道退回范围。
- 怎么退回去（两条，按需选一）：
  1. **只停用本通道，保留代码**：把实时主源改回原值即可，无需删文件——
     设置页改 `feed_priority_realtime`（写入 `data/config_override.json`，本机当前不存在该文件，
     停用后删掉该键即恢复代码默认），或环境变量覆盖：
     `set QUANTDESK_FEED_PRIORITY_REALTIME=qmt_xtdata,tdx_aidata`；
     同时把 `watch_enabled` 置 `False`（`config.py:59`）停掉盯盘定时任务（`scheduler.py:45-51`）。
  2. **完全删除本通道**：删 `quantdesk/adapters/data/tx_quote.py`，并把上面那条 `feed_priority_realtime`
     改回 `"qmt_xtdata,tdx_aidata"`。删干净后自检：
     `uv run python -c "from quantdesk.adapters import describe_adapters; print([r['name'] for r in describe_adapters()])"`
     里不应再出现 `tx_quote`。
- 删除后的行为（已按代码路径确认，非猜测）：`WatchService._feeds()` 拿 `hub.feed("tx_quote")` 返回 None，
  `feed_errors` 里写「未注册或被禁用」（`services/channels.py:144-146`），
  `refresh()` 直接返回空并记 `last_error`（`services/watch.py:327-330`），`status()` 里 `tx_quote.ready=False`——
  **不崩、不假装连上，但盯盘页与港股日线补齐会失效**。
- 本文档本身（`docs/integration/tx_quote.md`）随通道一起删除即可，没有别处引用它的硬依赖。
