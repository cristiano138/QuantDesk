# QuantDesk

Windows 本地 A 股量化交易工作台（Python 3.11+ · FastAPI · React + TypeScript）。

核心体验：行情面板 → Monaco 策略工作台 → 回测 → 模拟盘 → 实盘（QMT 为主力通道），
同一份策略代码零修改跑通三种模式；实盘下单必经服务端三道闸。
项目宪法见 [AGENTS.md](AGENTS.md)。

## 快速开始（W1 现状）

```bash
uv sync                          # 安装依赖（Python 3.12）
uv run pytest                    # 测试（network 用例默认跳过）
uv run python -m quantdesk.app   # 启动 API：http://127.0.0.1:8787
```

数据下载（AKShare 免 token）：

```bash
uv run python scripts/download_history.py --symbols 600000.SH --start 2019-01-01
```

## 目录

- `quantdesk/` 后端（core / adapters / engine / ipc / services / store / api）
- `frontend/` React 前端（Vite + TS）
- `scripts/` 数据与运维脚本
- `strategies/` 用户策略（只依赖 BaseStrategy）
- `data/` 运行时数据（SQLite + Parquet，不入 git）
