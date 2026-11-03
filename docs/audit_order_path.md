# 下单路径审计清单（D27 · 宪法第 5 条：下单价格永远是原始市场价，不复权）

审计日期：2026-11-04 ｜ 审计方式：grep 全链路 + 逐点核对 ｜ 结论：**通过（含 1 项已记录的口径说明）**

## 链路核对表

| # | 环节 | 文件:行 | 价格来源 | 口径 |
|---|---|---|---|---|
| 1 | API 入参 | `quantdesk/api/routes_trading.py:39-44` | 前端/调用方提交的委托价 | 用户输入，进入闸门校验 |
| 2 | 意图构造 | `quantdesk/services/trading.py:56-70` | `price` 原样进 Order，不参与任何复权换算 | ✓ 透传 |
| 3 | 闸②涨跌停校验 | `quantdesk/engine/riskgate.py:193-209` | 昨收取 Parquet **raw 口径**（`AdjustType.NONE`，`riskgate.py:_prev_close`），按板块/ST 计算 [跌停,涨停] | ✓ 原始价口径；**复权价委托在此被拦截**（tests/test_riskgate.py::test_adjusted_price_caught_by_band） |
| 4 | 模拟撮合 | `quantdesk/adapters/brokers/paper_broker.py:85`（tick 撞价 `match_on_tick`） | 撮合参考价 = 实时 Tick.last/五档（原始价），成交价取对手价±滑点 | ✓ 原始价 |
| 5 | 实盘投递 | `quantdesk/engine/riskgate.py:270` | Order.price 原样交 BrokerAdapter（适配器合同：只执行不换算） | ✓ 原始价 |
| 6 | 回测路径 | `quantdesk/engine/backtest.py:_submit` | 策略在回测中给出的价格来自 **qfq 行情序列**（宪法：回测用前复权） | ▷ 见口径说明 |

## 口径说明（回测内部自洽）

回测宇宙中数据、委托价、成交价、净值全部使用同一 qfq 序列——内部闭环一致，
不存在「qfq 委托价 × raw 成交价」的混算。回测不走 RiskGate 涨跌停闸（涨跌停
区间按 raw 昨收计算，对 qfq 序列会全量误杀）——该差异为有意设计并在此存档。

**实盘/模拟盘路径绝不接受 qfq 价格**：闸②以 raw 昨收的 ±10/20%/ST±5% 区间
硬校验，复权价委托必被拒（兜底生效，测试在案）。

## 数据源口径警示（D27 审计发现）

AKShare(东财) qfq 序列存在日度收益分离（10/10 标的中位 0.08%~0.25%，详见
`data/logs/source_align_audit_*.csv`）——**该源 qfq 仅作回测的降级口径**；
Tushare qfq 为本地计算（raw × factor/最新factor，精确因子），token 就位后
应切为回测主口径（`adapters/data/tushare_feed.py` 已具备该实现）。

## 遗留

- [ ] 策略子进程经 IPC 上行的 order_intent 价格由策略计算（`engine/runner.py:_submit` 透传）——子进程策略若误用 qfq 数据计算实盘委托价，闸②兜底拦截；策略开发规范已要求只用 ctx（raw tick）。
