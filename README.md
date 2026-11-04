# QuantDesk

Windows 本地 A 股量化交易工作台（Python 3.11+ · FastAPI · React + TypeScript）。

核心体验：行情面板 → Monaco 策略工作台 → 回测 → 模拟盘 → 实盘（QMT 为主力通道），
同一份策略代码零修改跑通三种模式；实盘下单必经服务端三道闸。
与各大主流股票软件结合：QMT/miniQMT、通达信、同花顺（iFinD + 客户端）、掘金、AKShare、Tushare
的**行情通道 + 交易通道 + 板块/公式工作流互操作**（详见下方[通道矩阵](#与主流股票软件结合通道矩阵)）。
项目宪法见 [AGENTS.md](AGENTS.md)；工程方案与决策见 [docs/商业落地工程方案.md](docs/商业落地工程方案.md)。

## 快速开始

```bash
uv sync                          # 安装依赖（Python 3.12）
uv run pytest                    # 测试（network 用例默认跳过）
uv run python -m quantdesk.app   # 启动 API：http://127.0.0.1:8787
```

数据下载（AKShare 免 token）：

```bash
uv run python scripts/download_history.py --symbols 600000.SH --start 2019-01-01
```

前端（开发模式）：

```bash
cd frontend && pnpm install && pnpm dev   # http://localhost:5173（代理 /api /ws → 8787）
cd frontend && pnpm build                 # 产出 frontend/dist（桌面壳与单端口访问需要它）
```

桌面壳（D29）：双击 `启动.bat`——自动起后端 + 打开 pywebview 窗口（先 `pnpm build`，
否则 `/` 返回 503 并提示构建命令）。

## 与主流股票软件结合（通道矩阵）

「结合」有三层：**数据通道**（拿行情/财务）、**交易通道**（下单与回报）、**工作流互操作**
（板块文件与选股公式和客户端互通）。三层都已落地，通道由插件注册表自动发现（宪法第 1 条：
新增通道 = 新增一个文件 + 模块级 `ADAPTER` 导出，核心引擎零改动）。

| 通道（注册名） | 层 | 软件/SDK | 前置条件 | 默认状态 |
|---|---|---|---|---|
| `akshare` | 数据 | 东财 AKShare | 无（免 token） | **可用**（反爬限速 `QUANTDESK_AKSHARE_MIN_INTERVAL`，默认 1s） |
| `tushare` | 数据 | Tushare Pro | tushare.pro 注册 → token 进保险库 | 待 token；含本地精确 qfq，**推荐回测口径** |
| `qmt_xtdata` | 数据 | QMT / miniQMT（xtquant） | 券商开通 QMT 权限 → 客户端登录并启用「极简模式」→ 保险库录入 userdata 目录 + 资金账号 | 代码已接入，**未就绪**（本机无 SDK/客户端） |
| `tdx_aidata` | 数据 | 通达信 | 二者择一：① `pip install pytdx`（社区，免账号）② 通达信官方 SDK | 代码已接入，未就绪时如实上报缺哪个 |
| `ths_ifind` | 数据 | 同花顺 iFinD | 商务开通试用/正式，refresh_token 或账号密码进保险库 | 代码已接入，未就绪（配额三层缓存已实现） |
| `paper` | 交易 | 内置模拟撮合 | 无 | **可用**（`simulation_only=True` 下唯一路由目标） |
| `qmt` | 交易 | QMT / miniQMT | 同 `qmt_xtdata` | 代码已接入，实盘需用户在场 |
| `gm` | 交易 | 掘金终端（gmjsdk） | 掘金注册 → 终端在跑 → Token + 账户 ID 进保险库 | 代码已接入，未就绪 |
| `easytrader` | 交易 | 同花顺客户端 | 同花顺独立下单程序 `xiadan.exe` 路径进保险库 | **默认禁用**（非官方接口，稳定性低；启用需设置页确认文本） |
| `federated` | 数据（路由） | 以上数据通道的降级组合 | — | **可用**（历史可降级；实时主源单一，宁停勿错） |

- 面板入口：`/channels`（通道管理）—— 三态如实上报 `ready / unready(缺哪个 SDK) / disabled`，
  一键探活、凭据表单、对账差异列表。**新增通道不需要改前端**（表单与列表由后端元数据驱动）。
- 谁当主源、降级顺序，在「设置 → 行情通道路由优先级」里改（`feed_priority_history/realtime/financial`），
  服务端按注册表校验通道名：写错名返回 400 并列出可选值；允许先把还没装 SDK 的源写上，装好即用。
  实时只认主源——主源断线不切第二源，避免两条口径拼出一条失真时间轴。
- SDK 全部是**可选依赖**，不进 `pyproject`：适配器顶层不 import，缺失时给安装引导而不是堆栈。
- 「接入完成」≠「接入可交付」：六个通道凡依赖真机 SDK 的口径都归档在
  [`docs/integration/`](docs/integration) 的**在线核对清单**，须在装好 SDK 的机器上逐条勾选。
- 新增通道流程见 [`docs/adapter_dev_guide.md`](docs/adapter_dev_guide.md)。

## 工作流互操作（不需要任何券商权限）

`/interop`（软件互操作）把 QuantDesk 的自选与选股结果**送进**用户已经在用的股票软件，
也把它们的结果**取回来**：

- 通达信 `.blk`（GBK，市场位 1沪/0深 + 6 位代码）导入/导出，可直接写入客户端
  `…\T0002\blocknew\`（需在设置页配置通达信安装目录；未配置时只产出下载文件，不假装写成功）；
- 同花顺/东财 TXT 板块（每行 6 位裸代码）双向；
- 条件选股公式**方言编译**：一条规则同时编译成通达信与同花顺公式，且**回测求值与公式同源**
  （ma_cross / macd_golden / rsi_threshold / volume_spike），避免「软件里选出来、回测里不是」；
- 历史信号扫描：本地 Parquet 上按规则批量出信号，导出成板块文件后即可在客户端里看。

北交所 `.blk` 市场位未实测 → 默认跳过并在结果里显式告知，不猜前缀。

## 凭据保险库

账号 / token / 资金路径只进 `data/credentials.vault`（Windows DPAPI 加密，
`quantdesk/core/secrets.py`）：

- **只写不读**：设置页保存后不再回显明文，只显示前 4 位 + `***`；API 与日志同样拿不到明文；
- 密文与**本机 Windows 账户**绑定——换机或重装系统后**无法还原**，需要重新录入。
  备份 `data/` 时请把这条记住（`credentials.vault` 拷走也没用）；
- 非 Windows 或 DPAPI 不可用 → 抛配置错误并引导「请只用模拟盘」，**绝不退化成明文存储**；
- 历史遗留：AKShare 免 token 通道仍可用 `.env`（`QUANTDESK_TUSHARE_TOKEN` 等环境变量），
  新接入的凭据一律走保险库。

## 实盘安全（宪法红线，任何代码不得绕过）

1. **全局模拟盘开关**默认 ON——一切下单路由内置模拟撮合；
   切实盘需在「设置」页输入确认文本（`我知晓并承担实盘交易风险`）；
2. **A 股规则校验**（闸②）：T+1 可用 / 整手 / 委托价 ∈ [跌停,涨停]（复权价误用兜底拦截）/
   资金充足 / 单票市值上限 / 日委托数上限（后两项是**热参**，改设置即刻生效）；
3. **实盘二次确认**（闸③）：先落库 `PENDING_CONFIRM` → WS 推送弹窗 → 10s 超时
   `EXPIRED_CONFIRM` 自动作废；通道断线一律 fail-closed 拒单（不排队不重试）；
4. **闸门在服务端，不在弹窗**：确认文本由服务端校验（`GATED_FIELDS`）——三类危险写入
   （关模拟盘 / 启用 easytrader / 切实盘通道）缺确认文本直接 400；策略代码 `ctx.buy()` 产生的
   下单意图同样走 `RiskGate`，不存在「程序化就绕过闸」的路径；
5. **周期对账**：`/channels` 页可对账，通道快照 vs 本地库，差异（缺单/状态不符/漏成交/
   持仓或资金不符）落库并以 RISK 事件上报；模拟盘同样自检；
6. 全部数据本地存储（SQLite WAL + Parquet）；敏感信息只进保险库（不入库明文、不入 git、
   不进日志）；后端只绑 127.0.0.1（写死，不提供配置项）。

## 常见故障排查

| 现象 | 处置 |
|---|---|
| AKShare 拉数中断/报 `ConnectionLostError` | 触发了限流熔断（封禁期内重试会延长封禁）。等 5/15/30 分钟冷却后重跑；长期放缓：`QUANTDESK_AKSHARE_MIN_INTERVAL=3.0` |
| miniQMT 掉线 | 通道状态灯变红；RiskGate fail-closed 拒单属预期。重启 miniQMT 登录后适配器重连并重放订阅 |
| easytrader 控件丢失 | 适配器自动截图到 `data/logs/`；锁定同花顺客户端版本、操作期间勿动鼠标键盘、建议独立 Windows 账户 |
| 后端 8787 被占用 | `netstat -ano \| findstr 8787` 找 PID 结束旧进程；端口经 `QUANTDESK_PORT` 修改（监听地址不可改） |
| 回测报「本地无数据」 | 先在数据管理页/命令行下载对应标的与周期（K 线全部走 Parquet 本地缓存，不外呼） |
| 待确认单重启后还在？ | 在。重启后 `restore_pending()` 从库重载，确认窗口重新起算 |
| 通道页显示「未就绪 / 缺 xtquant」等 | 正常状态，不是 bug：SDK 是可选依赖，本机没装就如实上报。按卡片的 `prereq` 一句话做（装 SDK / 登录客户端 / 录凭据），点「探活」复查。装配失败后 15s 冷却，不会每单去撞一次连接 |
| 打不开 `/`（桌面壳空白） | 前端未构建。`cd frontend && pnpm build` 产出 `frontend/dist`，后端才托管 SPA；dist 缺失时 `/` 返回 503 并给构建指引 |
| 互操作导出「客户端目录未配置」 | 设置页填通达信/同花顺安装目录（指向客户端根，代码自己找 `T0002/blocknew`）。未配置时只给下载文件，不假装写成功 |
| 对账差异列表有红项 | 通道与本地库不一致（缺单/状态不符/漏成交/持仓或资金不符）。这是设计出来的告警，不是崩溃：先核对客户端里的真实委托，再决定是否需要人工干预 |
| 凭据保存后看不到明文 | 保险库只写不读，只显示前 4 位 + `***`。忘记只能重录；换机/重装系统后旧密文不可还原 |

## 目录

- `quantdesk/` 后端（core / adapters / engine / ipc / services / store / api）
- `frontend/` React 前端（Vite + TS）：行情 / 策略工作台 / 回测 / 实盘监控 / 数据管理 /
  合规报备 / 通道管理 / 软件互操作 / 设置
- `desktop/` pywebview 桌面壳
- `scripts/` 数据与运维脚本（含 `e2e_v1.py` 全链路验收、`dod_d27_align.py` 数据源审计、
  `verify_sources.py` 交叉对数、`bench_ipc.py` IPC 压测）
- `strategies/` 用户策略（只依赖 BaseStrategy，零适配器依赖）
- `data/` 运行时数据（SQLite + Parquet + logs + credentials.vault，不入 git）
- `docs/` [`商业落地工程方案.md`](docs/商业落地工程方案.md)（v2.0 方案与决策）·
  [`adapter_dev_guide.md`](docs/adapter_dev_guide.md)（新增通道 SOP）·
  [`integration/`](docs/integration)（五个真机通道接入卡与在线核对清单）·
  [`audit_order_path.md`](docs/audit_order_path.md)（数据源口径审计）

## 版本

- **v1.2.0-dev**（2026-11-04 主流股票软件接入轮）：六通道接入（QMT 行情+交易 / 通达信 /
  iFinD / 掘金 / easytrader / 内置模拟撮合）+ 通道元数据合同与注册表启停 + ChannelHub 动态装配 +
  实时链路闭环（订阅上行→行情下行→意图过闸→回报回投）+ 断线自愈重放 + DPAPI 凭据保险库 +
  周期对账 + LOG/RISK 落库检索 + 回测异步队列 + 通达信/同花顺板块与公式互操作 + 服务端确认门禁 +
  SPA 托管。pytest 592 passed
- **v1.1.0**（D30）：数据源口径审计 + 合规/运维页 + 桌面壳 + 盘后刷新任务
- **v1.0.0**（D20）：数据底座 / 回测 / 撮合 / 模拟盘 / 三道闸 / 策略运行时 / 全部前端页

