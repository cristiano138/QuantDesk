# 新增主流股票软件通道 · 开发 SOP（adapter_dev_guide）

> 适用对象：要给 QuantDesk 接一个新数据源（行情/财务）或新券商通道（交易）的人或 Agent。  
> 阅读顺序：第 1 节看边界 → 第 2 节照骨架写文件 → 第 3 节填元数据合同 → 第 4~7 节接线 →
> 第 8 节写离线单测 → 第 9 节写在线核对卡 → 第 10~12 节过门禁。  
> 配套事实源：`docs/商业落地工程方案.md`（架构决策 ADR-6~ADR-12）、`docs/integration/*.md`（六个已接入通道的卡）。

---

## 1. 先确认边界（违反任一条即返工）

| 约束 | 出处 | 落地形态 |
|---|---|---|
| 只新增文件，不改 `core/` `engine/` `services/` | 宪法第 1 条 | 新适配器 = `adapters/data/xxx.py` 或 `adapters/brokers/xxx.py` + 模块级 `ADAPTER` 导出 |
| SDK 是可选依赖，不进 `pyproject` 主依赖 | 宪法第 8 条 | 顶层禁止 `import xtquant` 之类；在 `connect()` 里 `import_optional()` |
| 交易适配器只执行、不风控 | 宪法第 3 条 | 收到的 `Order` 已过三道闸；不调用 `riskgate`，不自作限价改单 |
| 提交失败必须抛 `BrokerError`，断线抛 `ConnectionLostError` | 宪法第 3 条 | 禁止静默失败、禁止返回假成功快照 |
| 下单价永远是未复权原始市场价 | 宪法第 5 条 | 不做任何复权换算；行情侧复权只用于历史/回测 |
| `Position.available` 遵守 T+1（当日买入为 0） | 宪法第 3 条 | 通道不给可用量时自己按成交日期算 |
| 账号/token/资金路径只进本地加密保险库 | 宪法第 7 条 | `core/secrets.py`；不入库明文、不进日志、不进 git、不进提示词 |
| 非官方通道默认禁用 + UI 红字 | 宪法第 9 条 | `default_enabled = False` + `official = False`，注册表源头不可见 |
| 新数据源必须交叉对数（误差 ≤0.1%，除权日单列） | 宪法第 9 条 | 第 11 节流程，未过不得进 `feed_priority_*` |
| 单次调用内部最多重试 1 次，降级链归 `FederatedFeed` | 适配器合同 | 适配器不自建重试风暴 |

**不做的东西**（防止范围膨胀）：不给适配器加"自动选源"（那是 `FederatedFeed` 的职责）、不在适配器里写
风控判断、不加数据库迁移、不做服务器部署假设（本机单进程）。

---

## 2. 目录与命名（30 秒落位）

```
quantdesk/adapters/
  data/     <vendor>_<product>.py     # 例：qmt_xtdata.py, tdx_aidata.py, ths_ifind.py
  brokers/  <vendor>_broker.py        # 例：qmt_broker.py, gm_broker.py, easytrader_broker.py
  base/     datafeed.py broker.py optional.py     # 已存在，一般不动
tests/
  mocks/    <sdk_package>/            # 假 SDK（第 8 节）
  test_<vendor>_feed.py / test_<vendor>_broker.py
docs/integration/<vendor>.md          # 接入卡（第 9 节）
```

- 注册名 `name` 用**小写短名**（`qmt` / `gm` / `ths_ifind`），它会出现在配置 `broker_channel`、
  `feed_priority_*` 列表、UI 下拉与订单快照里，改名等于破坏用户配置。
- 文件名 ≠ 注册名。曾有面板用文件名比对注册名导致常红（已修）；一律以 `meta_of(cls).name` 为准。
- 模块级必须 `ADAPTER = <YourClass>`。**没有这一行 = 注册表发现不了**（`paper_broker` 就漏过一次，
  修好后系统面板才第一次显示内置模拟撮合）。

---

## 3. 通道元数据合同（UI 与路由的事实源）

声明为类属性，由 `quantdesk/adapters/base/optional.py:119` 的 `meta_of()` 归一化读取，未声明的取安全默认值。

| 属性 | 类型 | 默认 | 语义（谁消费） |
|---|---|---|---|
| `name` | str | 类名 | 注册名（路由、配置、面板） |
| `label` | str | = name | 中文通道名，面板与设置页展示 |
| `prereq` | str | "" | 一句话说清**用户要做完哪一步本通道才能用**（开通权限→装客户端→录凭据）。面板直接显示，不要写技术黑话 |
| `official` | bool | True | False → UI 红字「非官方接口，稳定性低」（宪法第 9 条） |
| `requires` | tuple[str,...] | () | 依赖的 Python 模块名；缺失 → 状态 `unready` + `missing` 列表 |
| `requires_any` | bool | False | True → `requires` 任一可用即就绪（通达信：官方 SDK 或 pytdx 择一） |
| `default_enabled` | bool | True | False → 注册表源头不可见（`get_adapter_class` 抛 KeyError、`list_adapters` 不返回） |
| `enable_flag` | str | "" | `default_enabled=False` 时对应的 Settings 布尔字段名（如 `enable_easytrader`） |
| `is_paper` | bool | False | True → 只用于模拟撮合，永远不作为实盘通道解析 |
| `capabilities` | frozenset[FeedCapability] | 空 | 数据侧能力；`HISTORY_BAR / REALTIME_TICK / REALTIME_BAR / FINANCIAL / TRANSACTION` |

三条易踩的规则：

1. **禁用必须在注册表源头生效**，前端藏按钮不算禁用——通道选择与下单路由都从
   `quantdesk/adapters/__init__.py:79` 的 `is_visible()` 过。
2. **`requires` 缺失不隐藏通道**，而是如实上报 `unready`。理由：用户要看到"这个通道存在、缺什么、
   怎么装"，静默消失会让面板看起来"产品不支持"。实例化时才抛带引导的 `AdapterError`。
3. **`capabilities` 若是 property**（组合式适配器，如 `FederatedFeed` 动态聚合子源），
   `_static_capabilities()` 会登记成 `("dynamic",)` 哨兵而不是崩扫描。**别在类上直接迭代 property。**

---

## 4. 代码骨架

### 4.1 行情适配器

```python
# quantdesk/adapters/data/acme_feed.py
from quantdesk.adapters.base.datafeed import DataFeedAdapter, FeedCapability
from quantdesk.adapters.base.optional import import_optional
from quantdesk.core.enums import AdjustType, Frequency, OrderStatus
from quantdesk.core.errors import AdapterError, ConnectionLostError
from quantdesk.core.symbols import from_internal       # 统一代码 "600000.SH" → 通道代码，只在适配器内部转

_SDK = "acme_sdk"
_INSTALL = "安装：券商/供应商官网下载 SDK 后 uv pip install ./acme_sdk-*.whl，并登录其客户端。"


class AcmeFeed(DataFeedAdapter):
    name = "acme"
    label = "ACME 行情"
    prereq = "完成 ACME 账号开通 → 安装并登录 ACME 客户端 → 设置页凭据保险库录入 acme.token。"
    official = True
    requires = (_SDK,)
    capabilities = frozenset({FeedCapability.HISTORY_BAR, FeedCapability.REALTIME_TICK})

    def connect(self) -> None:
        sdk = import_optional(_SDK, channel=self.name, install_hint=_INSTALL)   # 惰性，绝不在顶层 import
        self._api = sdk.Api(...)              # 连接失败抛 ConnectionLostError，不抛裸异常
        self._ensure_watchdog()

    def health_check(self) -> bool: ...       # 轻量、不抛
    def get_history(self, symbol, freq, start, end, adjust=AdjustType.FORWARD) -> pd.DataFrame: ...
    def subscribe_realtime(self, symbols, callback) -> None: ...   # callback 内只入队，禁止阻塞

ADAPTER = AcmeFeed
```

`get_history` 返回必须归一为宪法列：`["dt","open","high","low","close","volume","amount"]`，
`dt` 为 `datetime64[ns]` **升序**，且带 `drop_duplicates()`。历史口径用前复权（宪法第 5 条），
除权日的复权因子单独说明，不静默插值。

### 4.2 交易适配器

```python
# quantdesk/adapters/brokers/acme_broker.py
class AcmeBroker(BrokerAdapter):
    name = "acme"
    label = "ACME 实盘"
    prereq = "开通 ACME 程序化权限（需报备）→ 登录客户端 → 录入凭据。"
    official = True
    requires = ("acme_sdk",)
    is_paper = False
    default_enabled = True

    def connect(self, **credentials) -> None:
        # 凭据缺失 → 抛 BrokerError，不猜默认值、不静默降级成只读
        ...

    def place_order(self, order: Order) -> Order:
        """收到的 order 已确认（order_id 由引擎预生成）。这里只补 broker_order_id。"""
        if not self._connected:
            raise ConnectionLostError("ACME 通道未连接：请登录客户端后重试")   # fail-closed，不排队不重试
        ...

    def get_position(self, symbol=None) -> list[Position]: ...   # available 必须 T+1
    def poll(self) -> None: ...                                  # 无回调能力通道：差异生成事件后广播
    def snapshot_for_reconcile(self) -> tuple[list[Order], list[Trade], list[Position], Account]: ...

ADAPTER = AcmeBroker
```

`snapshot_for_reconcile()` 不是可选装饰：`services/reconcile.py` 优先调它拿**一致性快照**。
回调能力弱的通道（easytrader 类）必须实现，否则对账要逐项查、期间还能被 `poll()` 改动而误报。

### 4.3 线程模型三条硬规定

1. **回调线程只入队**（`queue.put_nowait`），处理在独立线程；SDK 回调线程里做 DB/网络 = 死锁与丢数。
2. **状态变更集中在锁内**：成交回报与 API 线程并发修改同一 book/现金会算错（`paper_broker` 曾因
   无锁被复现实锤，全程 `RLock` 后才稳）。
3. **ZMQ socket 非线程安全**：不要让两个线程 `recv` 同一 socket（历史"偶发 kill9 抖动"的真根因之一）。

---

## 5. 凭据接入（商业产品必须做对的一段）

1. 键名约定 `<channel>.<field>`，全小写：`qmt.userdata_path`、`gm.token`、`ifind.refresh_token`。
2. 写入只有两条路：设置页表单 → `PUT /api/channels/credentials` → `set_secret()`，或代码内
   `set_secret(key, value)`。**没有读取明文的 API**——`describe()` 返回掩码（前 4 位 + `***`），
   `list_keys()` 只给键名。这是设计，不是偷懒：DPAPI 密文与本机 Windows 账户绑定，能读出明文
   的接口就等于把风险摊开。
3. 在 `services/channels.py:39` 登记：
   - 交易通道 → `_CREDENTIALS[channel] = [(密钥, connect 参数名, 是否必填), ...]`
   - 行情通道（`connect()` 无参、自己 `secrets.resolve()` 取）→ `_FEED_CREDENTIALS[channel]`
   
   登记后 `credential_spec()` 自动渲染设置页表单，**前端零改动**。
4. 凭据缺失时 `ChannelHub` 如实失败（不假装连接），状态里给 `missing` 与原因，并进入 15s 冷却，
   避免每单都去撞一次连接。
5. 日志与异常文本只允许 `mask()` 后的值。禁止 `log.info("token=%s", token)`。

---

## 6. 韧性接入（断线自愈）

```python
from quantdesk.core.resilience import Backoff, ReconnectWatchdog

def _ensure_watchdog(self) -> None:
    if self.watchdog is not None:
        return
    s = get_settings()
    self.watchdog = ReconnectWatchdog(
        name=self.name, probe=self._probe, reconnect=self._reconnect,
        on_reconnected=self._after_reconnect, bus=self.bus,
        interval=s.watchdog_interval_s,
        backoff=Backoff(base=s.backoff_base_s, cap=s.backoff_cap_s),
        probe_timeout_note=f"ACME 探活失败：{LOGIN_GUIDE}")
```

- `probe()` 必须轻量且不抛（抛了按失败处理）。
- `on_reconnected()` 是**义务**：重放订阅列表 + 全量刷新持仓/资金对账。重连后不重放订阅 = 行情
  静默停摆，用户看不出差别，等到发现已经错过一整段。
- 迁移事件只在真变化时广播：`channel_down` / `channel_up`；重放义务失败广播 `channel_replay_failed`
  （监控页红条的数据来源）。退避 1→2→4…封顶 60s。

---

## 7. 路由接入（`FederatedFeed` 与配置）

- 历史降级链、实时主源、财务链分别由 `config.feed_priority_history` / `feed_priority_realtime` /
  `feed_priority_financial` 控制；把新通道名加进去即完成选路，**不改代码**。
- **实时主源单一（ADR-5：宁停勿错）**：断线不换源。两个源拼一条实时序列会造成 tick 时间轴抖动与
  价差跳变，回测与撮合都会被污染；历史查询才允许降级。
- 订阅侧走引用计数：`subscribe_realtime(symbols, callback, subscriber="panel")`；
  策略/面板各自注册，退订只减自己的引用（`drop_subscriber`）。**不要**为"多一个消费者"再开一条
  订阅通道，那是 SDK 连接数的浪费，也是重复扇出的来源。
- 策略启动时 `RealtimePump._seed_history` 会回灌本地日线（250 根），新通道只要 `HISTORY_BAR`
  能力真实可用，这条链自动成立。

---

## 8. 离线单测（双层验证协议 · 第一层）

**原则：假 SDK 证我们自己的代码，真 SDK 证口径。CI 里永远不打真网络、不依赖真客户端。**

1. `tests/mocks/` 下放一份与真 SDK **同名**的包（目录或单文件模块均可）：
   ```
   tests/mocks/xtquant/{__init__.py, xtconstant.py}
   tests/mocks/pytdx/hq.py
   tests/mocks/iFinDPy.py
   ```
   假 SDK 只提供适配器实际调用的表面，并**故意带上真 SDK 的怪癖**（列名不统一、状态是字符串、
   数量为整手、价格放大 1000 倍之类）——假得越像，测试越有用。
2. 用例统一走 `tests/mocks/__init__.py` 的 `fake_sdk()`：
   ```python
   from tests.mocks import fake_sdk

   def test_acme_history_normalizes():
       with fake_sdk("acme_sdk"):
           feed = AcmeFeed()
           feed.connect()
           df = feed.get_history("600000.SH", Frequency.D1, start, end)
           assert list(df.columns) == ["dt","open","high","low","close","volume","amount"]
   ```
   退出时按包名前缀清 `sys.modules` 并还原 `sys.path`。**假包泄漏给后续用例 = 注册表把未就绪的
   通道报成就绪**，这类污染极难查。
3. 每个通道必须覆盖的 8 类用例（缺一类即视为该通道未完成）：
   | # | 覆盖点 | 断言形态 |
   |---|---|---|
   | 1 | 列名/单位归一 | 宪法列、`dt` 升序去重；价格/手数换算 |
   | 2 | 参数映射 | 周期、复权、代码格式映射到 SDK 侧调用的实参 |
   | 3 | 订单状态机 | SDK 状态码 → `OrderStatus` 全分支，含 `PART_FILLED` |
   | 4 | 回调只入队 | 回报回调内不做 IO；处理在消费线程 |
   | 5 | 断线 | `ConnectionLostError` + `health_check()` 可探测 |
   | 6 | 重连重放 | 重连后订阅被重放、持仓/资金被刷新 |
   | 7 | 凭据缺失 | 结构化失败 + 原因，不静默退化、不假装连接 |
   | 8 | 可见性 | `default_enabled=False` 时注册表源头不可见，开 flag 后可见 |
4. 网络用例一律打标记，默认跳过：`pytest -m "not network"`（发布门禁跑这条）。
5. **不要碰真实开发库**。夹具改 `data_dir` 时注意 `store/db.py` 早期是模块级绑定（已改惰性取
   `data_dir()`）；涉及单例（`_service` / `_hub` / `_pump`）的测试要在夹具里复位，否则会跨用例串状态。

---

## 9. 在线核对卡（双层验证协议 · 第二层）

`docs/integration/<vendor>.md` 是"接入完成"与"接入可交付"的分界线。十节模板（照 `qmt.md` 写）：

1. 前置条件（行政 + 环境，缺一不可：权限、开通门槛、报备）
2. SDK 安装（含版本来源：客户端自带 whl 优先于 PyPI 社区构建）
3. 凭据与 `connect` 参数约定（表：通道 / 调用 / 参数 / 来源 / 说明）
4. 能力矩阵（对齐 `capabilities`，注明哪些是实测、哪些是推断）
5. 线程模型与回调投递路径
6. 参数与状态映射表（SDK 码 ↔ 本方枚举，逐值列）
7. 单位与口径（价格是否放大、手数/股、金额单位、复权因子来源）
8. **在线核对清单**（可勾选：本机装好 SDK 后逐条验证的真实调用与预期返回）
9. 验收标准（端到端：历史对数、实时 tick 到图 <1s、下单到回报链路）
10. 已知坑与错误码处置（含"客户端未登录/版本不匹配"这类最常见现场问题）

**规则**：任何"SDK 表面靠猜"的结论必须写进第 8 节清单并在真机勾选后才能升为"已实测"；
文档、代码注释、commit 里都不得声称未验证的行为。

---

## 10. 前端可见性（不该写前端的部分）

- 通道全景：`GET /api/system/adapters` → `describe_adapters()` 驱动，**新增通道零前端改动**。
- 凭据表单：`GET /api/channels/credentials` → `credential_spec()`。
- 探活：`POST /api/channels/probe/{name}`；状态：`GET /api/channels`。
- 只有当新通道带来**新的交互**（例如需要用户在客户端里做手势/二次确认）才动前端。

---

## 11. 交叉对数（新数据源放行的前置门槛）

1. `scripts/verify_sources.py` 就绪（Tushare 需 `QUANTDESK_TUSHARE_TOKEN`）。
2. 同一标的、同一周期、同一区间，与已接入任一源比对：**日收益偏差 ≤0.1%**。
3. **除权日单列报告**，不与常规偏差混在一起——qfq 口径缺陷正是在这里暴露的：
   东财(AKShare) qfq 的 10/10 标的中位偏差 0.08%~0.25%，601899 局部放大到 4.6×，
   因此**不能作精确回测基准**（详见 `docs/audit_order_path.md` 与
   `data/logs/source_align_audit_*.csv`）。
4. 未过对数的通道：可以登记在注册表（用户可见、状态如实），但**不得进入 `feed_priority_history` 主口径**。

---

## 12. 提交前门禁清单

```bash
uv run pytest -q -m "not network"        # 全绿，不留红测试过夜
uv run python -c "from quantdesk.adapters import describe_adapters; \
  [print(r['name'], r['status']) for r in describe_adapters()]"   # 新通道出现在这里
```

- [ ] 只新增文件；若确实改了 `core/engine/services`，说明为什么这不是错误设计
- [ ] 顶层无 SDK import；缺失时 `unready` + `missing` 如实上报
- [ ] 模块级 `ADAPTER` 已导出，注册名唯一（冲突会直接 `RuntimeError: 适配器注册名冲突`）
- [ ] 交易侧：失败抛 `BrokerError`、断线抛 `ConnectionLostError`、`available` 符合 T+1
- [ ] 凭据进保险库、日志脱敏、不进 git
- [ ] 非官方通道 `official=False` + `default_enabled=False` + `enable_flag` 指向真实 Settings 字段
- [ ] 8 类离线用例齐备；`fake_sdk` 无泄漏
- [ ] `docs/integration/<vendor>.md` 十节齐全，未实测项进第 8 节清单
- [ ] 新数据源：交叉对数结论（或未对数说明）
- [ ] `git commit` 信息格式 `D<N>-<序号>: <做了什么>`

---

## 13. 本轮六个通道踩过的坑（照抄可省一天）

| 现象 | 真根因 | 修法 |
|---|---|---|
| 启动即崩：`适配器注册名冲突: akshare` | `meta_of` 迭代 property 型 `capabilities` 抛 `TypeError`，扫描中断留下半状态 | `getattr_static` + `DYNAMIC` 哨兵；meta 解析放注册前；单适配器失败只跳过 |
| 面板里看不到内置模拟撮合 | `paper_broker` 没有模块级 `ADAPTER` 导出 | 补导出 + `label/prereq/official` 元数据 |
| `on_tick` 后事件总线上 `source`/`freq` 是 `"Frequency.D1"` | 序列化用 `str(enum)` | 统一 `.value`（`bar_to_dict` / `_sv`） |
| 实时 bar 在子进程历史缓冲里刷重复行 | bar 的 `dt` 取最后一个 tick 时间 | `_floor_dt` 取周期起点 + 子进程按 `dt` upsert |
| 64 位下 DPAPI 崩 `OverflowError: int too long` | `ctypes` 默认返回类型截断 `LocalFree` 指针 | 显式绑定 `argtypes`/`restype` |
| 测试把数据写进真实开发库 / 仓库根长出 `T0002/` | `store/db.py` 模块级绑定 `data_dir`；`Path("")` 变成 `.` 且 `is_dir()` 为真 | 惰性取 `data_dir()`；空串显式当"未配置" |
|  kwargs 全部没传进适配器 | `_instantiate` 用 `k in params` 而 `params` 是 `ValuesView` | 改 `inspect.signature(cls).parameters` 字典判断 |

判据式的教训：**"能实例化"不等于"已接通"**。本轮最大的产品级缺陷是策略 `ctx.buy()` 生成的
`order_intent` 从未进入 `RiskGate`（意图落到队列后无人消费）——单测全绿、界面能启动、真实链路
却不发单。新增通道时把"端到端一条链"（订阅 → 行情 → 策略 → 意图 → 闸门 → 撮合 → 回报 → 回投）
当作验收对象，而不是只测组件。
