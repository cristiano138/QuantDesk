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
