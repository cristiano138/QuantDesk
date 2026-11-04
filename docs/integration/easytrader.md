# easytrader（同花顺客户端 GUI 自动化）交易通道接入卡

> 定位：宪法里的**非官方通道**，默认禁用。适配器 `quantdesk/adapters/brokers/easytrader_broker.py`
> （`ADAPTER = EasytraderBroker`，name=`easytrader`，is_paper=False，official=False，
> `default_enabled=False` + `enable_flag="enable_easytrader"`）。
> 本卡覆盖：风险声明、前置条件、启用流程、线程模型（串行）、能力矩阵与 poll 语义、
> 中文键/状态映射、**在线核对清单**、故障排查、兜底与验收。

## 0. 风险声明（UI 红字内容，启用前必须读）

**非官方接口，稳定性低。** 本通道不用券商 API，而是用 `pywinauto` 操作本机同花顺
「独立下单程序」(`xiadan.exe`) 的界面控件来下单/查单。由此带来：

1. **客户端升级即可能失效**：控件名/层级一变，`ElementNotFoundError` 或
   `NotImplementedError("同花顺版本不匹配")` 就来；必须**锁定同花顺客户端版本**使用。
2. **可能违反客户端使用条款**：自动化操作券商/同花顺客户端属于灰色地带，风险自担。
3. **操作期间不能动鼠标键盘**：GUI 自动化靠抢焦点和模拟输入，人工介入会把点击带偏。
4. **没有推送**：委托/成交/资金只能轮询界面表格读回来，延迟与漏读风险高于 API 通道。
5. **单线程串行**：所有请求排队进一个 GUI 线程，通道吞吐天然受限，且一次卡住会连带
   后续调用超时（这是保护，不是缺陷）。

宪法第 9 条的落地方式：**默认禁用不是前端藏按钮**——`default_enabled=False` 让注册表
源头就跳过它（`list_adapters()` 不含、`get_adapter_class("easytrader")` 抛 `KeyError`、
`describe_adapters(include_disabled=False)` 不返回），下单路由与通道选择根本看不见这个通道；
`services/settings_svc._validate_channel()` 也会拒绝把 `broker_channel` 设成 `easytrader`。
只有在设置页启用后才进入 API 视野。UI 侧同步展示红字「非官方接口，稳定性低」。

## 1. 前置条件

1. 本机（Windows）安装**同花顺下单客户端**并能正常登录；能打开「独立下单程序」`xiadan.exe`
   （一般在安装目录下，如 `C:\同花顺软件\xiadan.exe`；有的版本在 `.install\hq\` 之类子目录）。
2. **锁定客户端版本**：禁用同花顺自动升级（或装完就断网/改更新配置），并记下版本号。
3. **建议独立 Windows 账户**跑交易自动化：与日常使用隔离，避免别人动鼠标；
   该账户需保持登录、屏幕不锁、不进入睡眠（DPAPI 凭据也绑这个账户，见 `core/secrets.py`）。
4. 下单账户在同花顺里已完成券商站点绑定、交易密码可用、当日为交易时间（非交易时段客户端
   多半直接返回空委托号，表现为「未受理」）。
5. 安装 SDK（**不进 pyproject 主依赖**，宪法第 8 条）：

       uv pip install easytrader

   `easytrader` 会带上 `pywinauto`/`pyperclip`/`requests`。适配器顶层不 import 它，
   只在 `connect()` 里 `import_optional("easytrader", ...)`：缺失时注册表如实上报
   `status=unready / missing=["easytrader"]`，实例化时抛带 `pip install` 引导的 `AdapterError`。

## 2. 启用流程（三道口径串起来）

1. 设置页勾选「启用非官方通道 easytrader」→ 输入确认文本
   **`我知晓并承担实盘交易风险`**（`settings_svc.RISK_CONFIRM_PHRASE`）。
   服务端强制校验：文本不对就直接 `ValueError`，curl 直连 `/api/settings` 也绕不过。
2. `enable_easytrader=True` 落 `data/config_override.json`（`update_settings`）→ 注册表
   `_flag_on()` 放行 → `easytrader` 出现在 `list_adapters()` / `describe_adapters(include_disabled=False)`。
3. 凭据保险库录入 `easytrader.ths_path` = `xiadan.exe` 全路径（DPAPI 加密，不回显、不入库、
   不进日志、不进 git）；`ChannelHub._CREDENTIALS["easytrader"]` 会把它作为 `ths_path` 传给 `connect()`。
4. 关掉全局模拟盘（`simulation_only=False`，同样要确认文本）后，`broker_channel=easytrader`
   才会真正走这条通道；否则第①道闸一律路由内置模拟撮合。

关闭即回到源头不可见（不保留任何 API 入口），这是本通道的兜底形态。

## 3. connect 参数约定

`broker.connect(ths_path=r"C:\...\xiadan.exe", client="ths")`

| 参数 | 含义 | 备注 |
|---|---|---|
| `ths_path` | 独立下单程序 `xiadan.exe` 全路径 | 解析顺序：入参 → 保险库 `easytrader.ths_path` → 实例构造参数 → `ths_install_dir` 目录里探测（命中多个则不猜，明确报缺） |
| `client` | `easytrader.use()` 的客户端类型 | 默认 `"ths"`；`"generic"` 是同花顺旧界面/通用形态的备选口径（见第 7 节） |

连接动作 = `easytrader.use(client)` → `user.connect(ths_path)` → **再取一次 `stock_balance()`
探活**（连上窗口但取不到资金数据，视为没连上，抛 `ConnectionLostError`）。失败异常文案固定
给三条引导：①客户端是否已登录 ②路径是否正确（**只回显 `C:\同***` 掩码**）③版本是否匹配（需锁版本）。

## 4. 线程模型（关键，勿改成直调）

pywinauto 抢占焦点、模拟输入，两个线程同时点控件必然互相打断，结果不可知。实现：

- 每个适配器实例一个专用 worker 线程 `easytrader-gui`，所有 SDK 调用经 `queue.Queue`
  投递，**串行消费**；调用方 `threading.Event` 等待，默认 `call_timeout_s=30.0`。
- 并发压测断言：8 个线程同时 `get_account/get_position/get_orders/get_trades/poll`，
  假 SDK 记录的调用线程 id 集合大小为 1（`tests/test_easytrader_broker.py`）。
  **任何绕过队列直接调 `self._user.xxx()` 的改动都会破坏这条纪律。**
- 超时 → `ConnectionLostError`（worker 可能还卡在那次界面调用里，**不强杀**：点了半截的
  鼠标事件比超时更危险；下次 `connect()` 会沿用仍活着的同一线程，保持串行）。
- 下单调用超时 = 委托结果未知：记入 `unconfirmed_orders()` 并 `log.warning` 要求人工对账，
  **不谎报失败**；下一次轮询若在 `today_entrusts` 里找回该合同编号会再留一条痕。
- 截图取证在**调用线程**做（不进队列），避免卡在故障现场时排在自己那条失败调用后面。

## 5. 能力矩阵与 poll 语义

| 能力 | 支持 | 说明 |
|---|---|---|
| 限价下单 | 是 | `order_buy(symbol=6位码, price=浮点, amount=股数)` / `order_sell(...)`，返回**字符串委托号** |
| 市价下单 | **否（fail-closed）** | GUI 面板的市价控件形态未在线核对前直接抛 `BrokerError`，不用 0 价占位瞎点 |
| 空委托号 | **抛 `BrokerError`** | `order_buy/order_sell` 返回空串/None = 未受理，禁止静默失败 |
| 撤单 | 是（受理语义） | `cancel_order(payload)`，payload 同时带 `entrust_no` 与 `合同编号`（版本签名不一）；SDK 无返回值即视为受理，最终状态以回报为准 |
| 批量撤单 | **不做** | `cancel_order_all()` 是「一把撤掉所有单」，与按单意图不符，适配器不暴露 |
| 资金/持仓/委托/成交查询 | 是 | `get_account / get_position / get_orders / get_trades` |
| 回报推送 | **无** | 宪法里 `BrokerAdapter.poll()` 就是为这条通道留的：全靠轮询 |
| 对账成套快照 | 是 | `snapshot_for_reconcile()` → `(orders, trades, positions, account)`，一个 GUI 任务取全四样，供 `services/reconcile.py` 比对 |
| 探活 | 是 | `health_check()`：取一次 `stock_balance()`，**任何异常返回 False 不外抛**（守护据此判死） |
| 重连刷新 | 是 | `connect()` 成功后立即重建 diff 基线（= 宪法「重连后刷新对账」义务） |

**poll 的语义**：一次 GUI 任务里取 `stock_balance / stock_positions / today_entrusts /
today_trades` → 归一化 → 与上一次快照 diff → **只有变化才**经注入的 `EventBus` 发布
`ORDER / TRADE / POSITION / ACCOUNT` 事件（`source="easytrader"`）。适配器**不落库**
（先落库后广播在 services 层），diff 与广播在调用线程做（不占碰界面的时间）。
`connect()` 时已建基线，因此首轮 poll 不会把当日历史委托/成交当成新变化刷屏；若那一刻取数
失败，第一次 `poll()` 自动只建基线并 `log.warning`。

**延迟与限频**：串行执行器本身就是限频器。一次 poll = 4 次界面表格读取，实测口径应控制在
**3–5 秒一轮**（更短会把 GUI 线程占满，其它查询排队）；建议运行时按 `reconcile_interval_s`
或独立轮询线程驱动 `poll()`，与 `ReconnectWatchdog` 的 `health_check()` 探活错开。
> 接线现状：`services/` 尚无稳定的 broker 轮询循环（本任务范围禁改 services），启用后先由
> 运行时/守护按上述节奏调 `poll()`，否则委托状态只在查询时才刷新。

**本地 `order_id` ↔ 合同编号映射**：只存在实例内存（`_bindings` / `_no_to_local`）。
进程重启后映射丢失——本通道无推送、回报靠轮询 `today_entrusts` 比对，因此 `connect()` 会对
「有合同编号但无本地归属」的委托打一条 `log.warning`（含笔数，提示人工对账），并把它们归一为
`order_id = "ths-<合同编号>"`、`broker_order_id = "<合同编号>"`、`strategy_id="manual"`。
`services/reconcile.py` 按 `broker_order_id` 反查本地订单，所以重启后仍能对得上账。

## 6. 中文键与状态映射（置顶于 `easytrader_broker.py`，改口径只改这里）

委托状态（`ENTRUST_STATUS_MAP`）：

| 同花顺「委托状态」 | → OrderStatus |
|---|---|
| 已报 / 已提交 / 正报 / 待报 / 待撤 / 未成 | SUBMITTED |
| 部成 / 部分成交 | PART_FILLED |
| 已成 / 全部成交 / 已成交 | FILLED |
| 已撤 / 已撤销 / 撤单 | CANCELLED |
| 废单 | REJECTED |
| 未报 | FAILED |
| **未识别串** | **SUBMITTED + `log.warning`**（宁可留不确定，绝不误判终态） |

资金（`BALANCE_KEY_MAP`，按优先级取第一个非空键）：`总资产→total_asset`、
`可用资金/可用余额/资金余额→cash`、`股票市值/参考市值/证券市值→market_value`、
`冻结市值/冻结资金→frozen`。

持仓（`POSITION_KEY_MAP`）：`证券代码`、`当前拥股/拥有量/持仓数量→volume`、
**`可用股份/可卖/可用数量→available`（T+1 语义唯一落点）**、`成本价→avg_cost`、
`参考市值→market_value`、`交易所/市场`（含「沪/深/京」直接定后缀，否则按代码段推断）。
**缺「可用」列 → available=0 并 `log.warning`**：宁可不给卖，也不虚增可卖量。

委托行（`ENTRUST_KEY_MAP`）：`合同编号→broker_order_id`、`证券代码`、`委托状态`、
`成交价格`、`委托数量→volume`、`成交数量→filled_volume`、`业务名称→side`
（含「买」/「卖」判定；认不出的原样上报 + warning，不猜方向）、`日期`+`时间`→毫秒时间戳、
`废单原因/备注→message`。

内部代码 ⇄ 客户端代码互转只发生在适配层：`from_internal(sym,"akshare")`（6 位裸代码）出站，
`to_internal(code6,"akshare")` + 交易所列纠偏入站。

## 7. 在线核对清单（首次接真实同花顺客户端时逐条验证）

以下为离线假 SDK（`tests/mocks/easytrader.py`）假设的形态，真实环境不符**只改适配层映射/调用点**：

1. **`order_buy/order_sell` 返回值形态**：是纯字符串委托号，还是 dict / 带前缀串？空串是否
   真表示「未受理」（本项目据此抛 `BrokerError`）。
2. **`use("ths")` 在当前 easytrader 版本里是否还在**、`user.connect()` 的参数名（本项目按
   位置参数传路径）；`ths` 与 `generic` 哪个适配本机客户端版本。
3. **`stock_balance()` 的真实键名**（本项目认「可用资金/参考市值/股票市值/冻结市值/总资产」
   及别名），是否返回 dict 而非 list；`总资产` 是否含冻结。
4. **`stock_positions()` 的可用列真名**：`可用股份` / `可卖` / `可用数量` / `交易能力`；
   「当前拥股」与「参考市值」单位；`交易所` 列取值形态（「上海」/「沪A」/「1」）。
5. **`today_entrusts()` 字段与状态串全集**：尤其「部成/已成/已撤/废单」之外还有哪些
   （如「本方未成」「对手方未成」「正报」）；`成交价格` 是成交均价还是委托价
   （本项目在部成/已成时才把它写进 `avg_filled_price`）。
6. **`cancel_order()` 的真实签名**：收整行 dict（键 `合同编号`）还是 `entrust_no=` 关键字；
   返回值语义（None/True/字符串）；废单/已成交时抛什么异常。
7. **日期/时间列格式**：`2024-01-02` vs `20240102`，`09:30:00` vs `093000`（本项目都做了归一）。
8. **控件类异常的真实类型名与文案**：`pywinauto` 各版本 `ElementNotFoundError`/
   `NoSuchElementError`/`NotImplementedError("同花顺版本不匹配")` —— 本项目按「类名 + 文案关键词」
   判定（`_CONTROL_ERROR_TYPES` / `_CONTROL_ERROR_HINTS`），命中才截图并抛 `AdapterError`。
9. **窗口对象截图属性**：真实 pywinauto 窗口是否具备 `capture_as_image()`（需 PIL），
   属性名是 `client` / `window` / `app` 哪一个（本项目按顺序鸭子类型试）。
10. 非交易时段、可卖不足、资金不足时客户端是弹窗（阻塞 GUI 线程 → 表现为超时）还是返回空委托号
    （表现为 `BrokerError`）——决定运维告警的话术。

## 8. 故障排查

| 现象 | 怎么看 | 处理 |
|---|---|---|
| `AdapterError: easytrader 界面控件定位失败…` | 异常文案末尾带 `data/logs/easytrader/<时间>-<动作>-<hex>.png`（或 `.bmp`）截图路径，**先看截图**：客户端是不是弹了确认框/广告框/切到了别的页签 | 手工把下单窗口恢复到默认页签、关掉弹窗再重连；反复出现即**锁版本**（升级导致控件树变了） |
| 截图只有全屏 `.bmp` | 说明连窗口句柄都没拿到（`xiadan.exe` 没开或找不到）→ 走了 GDI 全屏兜底 | 确认独立下单程序已启动；重设 `easytrader.ths_path`；必要时改 `client="generic"` 让 easytrader 走通用界面口径重试 |
| `ConnectionLostError: …GUI 调用超时…` | worker 还卡在一次界面调用里（弹窗吃焦点、机器卡顿、屏幕锁定） | 人工检查客户端窗口并解除阻塞；期间**不要**再点鼠标键盘；`unconfirmed_orders()` 列出的单子必须与客户端当日委托人工核对 |
| 下单返回空委托号 → `BrokerError` | 客户端未受理 | 核对是否交易时段、资金/可卖是否充足、账户站点是否绑定；RiskGate 侧该单为 FAILED，不会出现「以为下出去了」 |
| `health_check()` 一直 False | 探活（`stock_balance`）取不到数据 | 客户端掉线/被切账号；重连成功后 `connect()` 会自动重建对账基线 |
| 「同花顺版本不匹配」`NotImplementedError` | easytrader 的客户端适配器与本机版本不符 | 降级到 easytrader 支持的版本，或按第 7.2/7.8 条改 `client` 口径；这是结构性失效，不是重试能解决的 |
| 日志/异常里看不到完整路径 | 设计如此：`core/secrets.mask()` 前 4 位 + `***`，且 SDK 异常文案里的路径也被 `_redact()` 洗过 | 排障要完整路径时，直接在客户端属性页看，不要放开脱敏 |

## 9. 兜底与验收

**兜底**

- 禁用即不可见：注册表源头跳过（第 0 节），不存在「藏个按钮但 API 还能调」的形态。
- 失败一律结构化抛出：`BrokerError`（执行失败/未受理）、`ConnectionLostError`（断线/超时/未连接）、
  `AdapterError`（控件与版本类故障，带截图取证路径）；不静默、不重试风暴、不排队下单。
- 通道异常不外溢成进程崩溃：`poll()`/查询抛出的结构化异常由调用方（services/守护）按
  fail-closed 处理，RiskGate 断线直接拒单。
- 适配器不落库、不风控、不二次确认；`simulation_only` 是第一道兜底（ON 时根本路由不到这里）。

**离线验收（已覆盖，`uv run pytest tests/test_easytrader_broker.py`）**

默认禁用 → API 不可见 / 面板可见（`describe_adapters()` 里 `status=disabled`）、启用后可见；
GUI 串行（并发只落一个线程）；调用超时 → `ConnectionLostError`；下单超时 → 计入待对账；
中文键映射（资金/持仓/委托状态/未识别告警）；`available` 取「可用股份」且缺列按 0；
空委托号 → `BrokerError`；市价单拒单；`poll()` 只在变化时发布事件；`snapshot_for_reconcile()`
一次 GUI 任务取全四样；控件异常 → `AdapterError` + 截图真的落在 `data/logs/easytrader/`
（窗口抓图失败 → GDI 全屏 `.bmp` 兜底）；截图失败不掩盖原异常；非控件异常保持 `BrokerError`
且不留图；日志与异常文案不泄漏资金路径（只 `前4位***`）；`health_check()` 异常返回 False；
重启后映射丢失打对账告警并按 `ths-<合同编号>` 归一。

**在线验收（需真机同花顺，用户在场）**

1. 只读闭环：`connect` → `get_account`/`get_position` 与客户端面板逐项一致（误差 0）。
2. 一手限价买（最小可交易单位）→ `poll()` 在 ≤ 2 个轮询周期内产出 `ORDER`（已报）与
   成交后的 `TRADE/POSITION/ACCOUNT`，且 `order_id` 与本地一致。
3. 撤单闭环：挂单未成 → `cancel_order` 受理 → 轮询回报变 `CANCELLED`。
4. 断线 fail-closed：手工关掉 `xiadan.exe` → `health_check()` False → `place_order`
   抛 `ConnectionLostError` → RiskGate 拒单（不排队不重试）。
5. 版本漂移演练：升级同花顺后重复 1–4，确认控件异常带截图且异常文案指向「锁版本」。
