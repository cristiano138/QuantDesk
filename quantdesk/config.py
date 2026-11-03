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
