# QuantDesk

Windows 本地 A 股量化交易工作台（Python 3.11+ · FastAPI · React + TypeScript）。

核心体验：行情面板 → Monaco 策略工作台 → 回测 → 模拟盘 → 实盘（QMT 为主力通道），
同一份策略代码零修改跑通三种模式；实盘下单必经服务端三道闸。
项目宪法见 [AGENTS.md](AGENTS.md)。

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
```

桌面壳（D29）：双击 `启动.bat`——自动起后端 + 打开 pywebview 窗口。

## 权限开通指引（各适配器前置条件）

| 适配器 | 前置条件 | 默认状态 |
|---|---|---|
| AKShare | 无（免 token） | **可用**（注意反爬限速：`QUANTDESK_AKSHARE_MIN_INTERVAL`，默认 1s） |
| Tushare | [tushare.pro](https://tushare.pro) 注册 → 项目根 `.env` 写 `QUANTDESK_TUSHARE_TOKEN=<token>` | 待 token；配置后即用（含本地精确 qfq 计算，**推荐回测口径**） |
| miniQMT (xtquant) | 券商开通 QMT 权限 → 登录 miniQMT 终端 | 未接入（D13/D14 顺延，见进度日志） |
| 通达信 TdxAiData | 通达信官方 SDK | 未接入（D15 顺延） |
| iFinD | 同花顺商务开通试用 | 未接入（D26 顺延，不阻塞） |
| 掘金 gm | 掘金终端注册并绑定券商 | 未接入（D24 顺延） |
| easytrader | 同花顺客户端 | **默认禁用**（非官方接口，稳定性低；启用需设置页确认文本） |

## 实盘安全（宪法红线，任何代码不得绕过）

1. **全局模拟盘开关**默认 ON——一切下单路由内置模拟撮合；
   切实盘需在「设置」页输入确认文本（`我知晓并承担实盘交易风险`）；
2. **A 股规则校验**（闸②）：T+1 可用 / 整手 / 委托价 ∈ [跌停,涨停]（复权价误用兜底拦截）/
   资金充足 / 单票市值上限 / 日委托数上限；
3. **实盘二次确认**（闸③）：先落库 `PENDING_CONFIRM` → WS 推送弹窗 → 10s 超时
   `EXPIRED_CONFIRM` 自动作废；通道断线一律 fail-closed 拒单（不排队不重试）；
4. 全部数据本地存储（SQLite WAL + Parquet）；敏感信息只进 `.env`（不入库不入 git）；
   后端只绑 127.0.0.1（写死）。

## 常见故障排查

| 现象 | 处置 |
|---|---|
| AKShare 拉数中断/报 `ConnectionLostError` | 触发了限流熔断（封禁期内重试会延长封禁）。等 5/15/30 分钟冷却后重跑；长期放缓：`QUANTDESK_AKSHARE_MIN_INTERVAL=3.0` |
| miniQMT 掉线 | 通道状态灯变红；RiskGate fail-closed 拒单属预期。重启 miniQMT 登录后适配器重连并重放订阅 |
| easytrader 控件丢失 | 适配器自动截图到 `data/logs/`；锁定同花顺客户端版本、操作期间勿动鼠标键盘、建议独立 Windows 账户 |
| 后端 8787 被占用 | `netstat -ano \| findstr 8787` 找 PID 结束旧进程；端口经 `QUANTDESK_PORT` 修改（监听地址不可改） |
| 回测报「本地无数据」 | 先在数据管理页/命令行下载对应标的与周期（K 线全部走 Parquet 本地缓存，不外呼） |
| 待确认单重启后还在？ | 在。重启后 `restore_pending()` 从库重载，确认窗口重新起算 |

## 目录

- `quantdesk/` 后端（core / adapters / engine / ipc / services / store / api）
- `frontend/` React 前端（Vite + TS）
- `desktop/` pywebview 桌面壳
- `scripts/` 数据与运维脚本（含 `e2e_v1.py` 全链路验收、`dod_d27_align.py` 数据源审计）
- `strategies/` 用户策略（只依赖 BaseStrategy，零适配器依赖）
- `data/` 运行时数据（SQLite + Parquet + logs，不入 git）
- `docs/` 审计文档（下单路径审计等）

## 版本

- **v1.1.0**（D30）：数据源口径审计 + 合规/运维页 + 桌面壳 + 盘后刷新任务
- **v1.0.0**（D20）：数据底座 / 回测 / 撮合 / 模拟盘 / 三道闸 / 策略运行时 / 全部前端页
