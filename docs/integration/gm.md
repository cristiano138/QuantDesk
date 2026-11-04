# 掘金量化（gm）交易通道接入卡

> 定位：宪法中的**备选实盘通道**。适配器 `quantdesk/adapters/brokers/gm_broker.py`（`ADAPTER = GmBroker`，name=`gm`，is_paper=False，official=True）。
> 本卡覆盖：安装、券商/仿真绑定、SDK 版本差异与本项目探测策略、能力矩阵、参数/状态映射、**在线核对清单**、验收标准、已知坑。
> 敏感信息（token/account）只进本地加密配置（`QUANTDESK_*` 环境变量 / `.env`），不入库明文、不进日志（日志仅前 4 位 + `***`）、不进 git。

## 1. 注册与终端安装

1. 掘金官网（myquant.cn）注册账号，实名并开通量化权限。
2. 下载并安装**掘金终端**（GM Terminal），用掘金账号登录。终端是本地常驻进程，
   SDK 通过它连券商/仿真柜台——**终端没登录 = 通道不可用**，适配器 `connect()` 会失败抛 `BrokerError`。
3. 在终端「账户管理」里绑定券商实盘资金账号，或开通**仿真（模拟）账户**（仿真无需券商，先验证闭环）。

## 2. SDK 安装

- 命令行安装（**不改 pyproject 主依赖**，宪法第 8 条：SDK 属可选依赖）：

    uv pip install gm

- 适配器顶层**不 import** `gm`；仅 `connect()` 内 `import_optional("gm.api", ...)`。
  缺失时注册表如实上报 `status=unready / missing=["gm"]`，实例化时才抛带引导的 `AdapterError`。

## 3. connect 参数约定

`broker.connect(token=..., account_id=..., endpoint=..., mode=...)`（值来自本地配置注入）：

| 参数 | 含义 | 备注 |
|---|---|---|
| `token` | 掘金终端令牌 | 必填；脱敏，绝不写死、绝不进日志全文 |
| `account_id` | 资金账号 ID | 仿真/实盘各自的 account_id |
| `endpoint` | 终端服务地址 | **仿真/实盘切换走此参数，代码绝不写死**（默认由配置给） |
| `mode` | 运行模式标签 | 透传给 SDK；本项目不在适配层判定仿真/实盘 |

> 仿真账户与实盘账户**严禁混用**：endpoint/account_id 成对切换，切错即错下单到真实柜台。

## 4. 新旧 SDK 接口差异与本项目探测策略

掘金 Python 交易接口随版本演进有两套形态，本项目用 `_api_surface()` **一处探测**（`getattr` 判能力），把差异封在适配层：

| 形态 | 探测线索 | 会话构造 | 交易/查询命名空间 |
|---|---|---|---|
| 新版 | 模块有 `Terminal` | `Terminal(token, account_id, endpoint, mode)` | `terminal.order_stock/cancel_order/get_*`（session 即 ns） |
| 旧版 | 模块有 `create_context` | `create_context()` | `context.rest.api.order_*/get_*`（ns = `session.rest.api`） |
| 都不满足 | — | — | **抛 `AdapterError`「掘金 SDK 版本不受支持，请按接入卡核对接口名」，绝不静默用错接口** |

回报回调（`on_execution_report` / `on_order_status` / `on_disconnected`）统一挂在 session 对象上，两版一致。

## 5. 线程模型（关键，勿改成直调）

掘金交易接口**必须在其终端线程内调用**，跨线程直接调会崩。适配器实现：

- `connect()` 起**专用终端线程** `gm-terminal`：`set_token` → 构造会话 → 订阅低活跃标的（`heartbeat_symbol`，默认 `SHSE.600000`）维持事件循环/心跳。
- 下单/撤单/查询经 `queue.Queue` 投递进终端线程执行；调用方 `_submit(fn, timeout)` 带超时等待；
  **终端线程卡死 → 超时抛 `ConnectionLostError` 并置 `_lost`，绝不永久阻塞**（fail-closed）。
- SDK 回报回调线程**只入队**（`_report_q.put`）；独立工作线程 `gm-report` 归一化后发布 `EventBus`
  （ORDER/TRADE；断线信号 → `RISK channel_down`）。回调线程永不广播、永不 sleep、永不重连。

## 6. 能力矩阵

| 能力 | 支持 | 说明 |
|---|---|---|
| 限价/市价下单 | 是 | `place_order` |
| 撤单 | 是（受理语义） | `cancel_order` 返回是否受理，最终状态以回报为准 |
| 资金/持仓/当日委托/成交查询 | 是 | `get_account/get_position/get_orders/get_trades` |
| 回报推送（成交/状态/断线） | 是 | 回调型，`poll()` 空实现 |
| 对账快照 | 是 | `snapshot_for_reconcile()`→(orders,trades,positions,account)，供 services 层比对 |
| 重连刷新 | 是 | `reconnect()` 重建会话 + `refresh_reconcile()`（宪法重连义务） |

适配器**只执行不风控、不落库**：收到的 Order 已过 RiskGate 且已确认；落库在 services 层。

## 7. 参数映射表（置顶于 `gm_broker.py`，常量 getattr 优先取 SDK、缺失回退默认）

| 内部 | 掘金常量 | 值（在线核对） |
|---|---|---|
| Side.BUY / SELL | `ORDER_BUY` / `ORDER_SELL` | 2 / 4 |
| 持仓方向 | `POSITION_LONG` | 1（本项目只做多） |
| OrderType.LIMIT / MARKET | `ORDER_TYPE_LIMIT` / `ORDER_TYPE_MARKET` | `"limit"` / `"market"` |
| 价格类型 | `PRICE_TYPE_LIMIT` / `PRICE_TYPE_LATEST` | `"limit"` / `"latest"`（市价单 price=0） |
| 有效期 | `ORDER_DURATION_DAY` | `"day"` |

证券码：内部 `"600000.SH"` ⇄ gm `"SHSE.600000"`（`from_internal(...,"gm")` / `to_internal(...,"gm")`）。

## 8. 订单状态映射（`_ORDER_STATUS_MAP`，数字+字符串双覆盖）

| 掘金 order_status | → OrderStatus |
|---|---|
| 1 accepted / 2 live / 8 pending / 9 replaced / 12 accepted_by_counter | SUBMITTED |
| 6 part_filled / 7 filled | PART_FILLED / FILLED |
| 3 canceled / 4 expired / 10 canceled_pending / 13 stopped | CANCELLED |
| 5 rejected | REJECTED |
| 11 unknown | FAILED |

**未识别值 → 保守按 SUBMITTED + `log.warning`**（不误判成交、不误判终态）。成交回报 `EXECUTION_*` 归一化为 `Trade`。

## 9. 在线核对清单（首次接真实掘金终端时逐条验证）

以下均为**离线假 SDK 假设的取值**，需在真实掘金 SDK/终端在线确认，不符只改 `gm_broker.py` 顶部映射：

1. 真实常量取值：`ORDER_BUY/SELL`、`POSITION_LONG`、`ORDER_TYPE_*`、`PRICE_TYPE_*`、`ORDER_DURATION_DAY` 的数字/字符串字面值。
2. `order_stock()` 真实签名（**位置参数还是关键字**、参数顺序）——本项目按关键字调用，若真实为位置参数改 `_place` 调用即可（接口名集中一处）。
3. `order_status` 回报的**字段名**（`status` 还是 `order_status`？`filled_volume`/`traded_volume`？`avg_px`/`vwap`？`price`/`limit_price`？）。
4. 成交回报 `execution_report` 字段名（`exec_id`/`trade_id`、`timestamp` 单位秒/毫秒）。
5. **order 关联锚是否可用**：`order_remark`/`cl_order_id` 是否被掘金回传并能在回报里取回。
   - 可用 → `_anchor` 直接用回报里的 remark 关联本地 `order_id`。
   - 不可用 → **靠「`broker_order_id` ↔ 本地 `order_id`」映射表**（`_anchor`），下单返回即登记，回报据 broker 侧 id 反查；跨进程重启映射丢失，需 services 层持久化后重建。
6. 可用量字段真名：`can_use_volume` 还是 `available_volume`（T+1 语义，当日买入应为 0）。
7. 断线信号：`on_disconnected` 是否真实存在、掘金终端掉线是否会主动回调（若否，依赖 `health_check` 轻量探活 + services 层 watchdog）。
8. 事件循环：真实 SDK 是否需要显式 `Terminal.run_forever()` 驱动回调，若是需确认它如何在终端线程内非阻塞挂载。

## 10. 验收标准（在线）

- **仿真下单成交闭环 < 3s**：connect→place_order→（推回报）→get_orders/get_trades 一致，端到端 < 3s。
- **断终端后 RiskGate fail-closed 行为与 QMT 通道一致**：杀掘金终端进程 → `health_check()` 返回 False、
  `place_order()` 抛 `ConnectionLostError`，RiskGate 拒单（不排队不重试），`RISK channel_down` 事件送达前端。
- 交叉对账：适配器查询与掘金终端「资金/持仓」面板一致（误差 0）。

## 11. 已知坑

1. **跨线程调用崩**：掘金接口只在终端线程调；本适配器用队列把一切 SDK 调用投递进 `gm-terminal` 线程，调用方带超时等——任何绕过队列直接调 SDK 的改动都会崩。
2. **事件循环与主进程冲突**：SDK 回调线程 ≠ 我们的线程；回调**只入队**，广播归 `gm-report` 工作线程；在回调里 publish/sleep/重连会死锁或崩溃。
3. **仿真与实盘账户混用**：endpoint/account_id 必须成对切换，切错会把仿真单下到真实柜台（宪法第 2 条 simulation_only 是第一道兜底）。
4. **关联锚不一定存在**：见第 9.5 条，靠映射表兜底但重启不持久，需上层重建。
5. **版本接口漂移**：新旧命名空间不同，探测失败必须抛结构化错误，禁止「try 新失败再 try 旧」的静默乱调。
