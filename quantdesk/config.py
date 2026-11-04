"""全局配置（pydantic-settings）。

安全约定（宪法第 2/7 条）：
- simulation_only 默认 True：任何下单一律路由内置模拟撮合；
- uvicorn 只绑 127.0.0.1（app.py 写死，不提供配置项）；
- 敏感信息（token/账号）只经环境变量 QUANTDESK_* 或本地 .env 注入，不入库不入 git。
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QUANTDESK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # —— 安全开关 ——
    simulation_only: bool = True
    broker_channel: str = "paper"  # paper / qmt / gm / easytrader(默认禁用)

    # —— 运行 ——
    port: int = 8787
    data_dir: Path = Path("data")
    log_level: str = "INFO"

    # —— 费率（默认与 core/constants 一致，可被配置覆盖；以券商实际为准）——
    commission_rate: float = 2.5e-4
    min_commission: float = 5.0
    stamp_tax_sell: float = 5e-4
    transfer_fee: float = 1e-4

    # —— 数据源 ——
    tushare_token: str = ""
    akshare_min_interval: float = 1.0   # AKShare 反爬约 1 req/s
    tushare_min_interval: float = 0.35  # 按积分档限速（200 次/分钟档）

    # —— iFinD（付费源；凭据优先走 DPAPI 保险库，这里只是 env/开发注入位）——
    ifind_refresh_token: str = ""
    ifind_username: str = ""
    ifind_password: str = ""
    ifind_daily_quota: int = 3000       # 当日调用阈值：超阈值只读缓存，不再消耗配额
    ifind_min_interval: float = 0.2

    # —— 主流股票软件通道路由（逗号分隔注册名，靠前的先用；缺失/未就绪自动跳过）——
    feed_priority_history: str = "qmt_xtdata,tushare,akshare"
    feed_priority_realtime: str = "qmt_xtdata,tdx_aidata"
    feed_priority_financial: str = "ths_ifind,tushare,akshare"

    # —— 通道韧性（宪法：断线可探测 + 重连后重放订阅/刷新对账）——
    watchdog_interval_s: float = 60.0    # 健康态探活间隔
    backoff_base_s: float = 1.0          # 重连退避起点
    backoff_cap_s: float = 60.0          # 重连退避上限
    reconcile_interval_s: float = 30.0   # 实盘全量对账周期（差异即事故）

    # —— 风控热参（RiskGate 每单动态读取，改完即时生效不缓存）——
    max_position_value: float = 1_000_000.0   # 单票市值上限（元）
    max_daily_orders: int = 200               # 单策略日委托数上限

    # —— 非官方通道启用位（默认禁用；设置页需输入确认文本才置 True）——
    enable_easytrader: bool = False

    # —— 主流股票软件本地目录（软件互操作：板块文件/公式导出落盘位置，非凭据）——
    tdx_install_dir: str = ""     # 通达信安装目录（T0002 所在）
    ths_install_dir: str = ""     # 同花顺安装目录（StockBlock 等）

    # —— 回测任务队列（异步化：POST 立即返回 run_id，进度经 WS）——
    backtest_async: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


def data_dir() -> Path:
    """运行时数据目录（SQLite/Parquet/logs），不存在则创建。"""
    d = get_settings().data_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- D19 设置热更新（本地覆盖文件 + 缓存失效） ----------

_OVERRIDE_EXCLUDED = {"data_dir"}          # 路径类不进覆盖文件


def _override_path(base: "Settings | None" = None) -> Path:
    d = (base or Settings()).data_dir
    d.mkdir(parents=True, exist_ok=True)
    return d / "config_override.json"


def _load_overrides(base: "Settings | None" = None) -> dict:
    p = _override_path(base)
    if not p.exists():
        return {}
    import json

    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                          # noqa: BLE001 — 损坏文件按无覆盖处理
        return {}


@lru_cache
def get_settings() -> Settings:
    """env/.env 为基座，叠加 data/config_override.json 运行时覆盖。"""
    base = Settings()
    ov = _load_overrides(base)
    if ov:
        clean = {k: v for k, v in ov.items() if k in Settings.model_fields}
        return Settings(**clean)
    return base


def update_settings(**overrides) -> dict:
    """运行时更新设置：写 data/config_override.json + 失效缓存（下次读取生效）。

    宪法：simulation_only 可热切；绑定地址等红线字段不在可更新范围。
    返回更新后的完整设置快照。
    """
    clean = {k: v for k, v in overrides.items()
             if k in Settings.model_fields and k not in _OVERRIDE_EXCLUDED}
    cur = _load_overrides()
    cur.update(clean)
    import json

    _override_path().write_text(json.dumps(cur, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    get_settings.cache_clear()
    return get_settings().model_dump()


def reset_settings_overrides() -> None:
    p = _override_path()
    if p.exists():
        p.unlink()
    get_settings.cache_clear()
