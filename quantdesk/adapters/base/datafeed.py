"""行情数据插件基类（宪法核心契约：签名一个不改）。

插件约定（新增数据源 = 在 adapters/data/ 下新增一个文件并导出模块级 ADAPTER 类）：
1. symbol 一律使用内部规范 "600000.SH" / "000001.SZ"，
   与源格式互转只发生在适配器内部（用 core/symbols.py 工具函数）。
2. 接口为同步风格（国内数据 SDK 基本是同步/回调式），由 services 层
   放入线程池驱动；实时数据经 callback 推送，callback 内只入队、禁止阻塞。
3. 单次调用内部最多重试 1 次；重试风暴与降级由上层 FederatedFeed 负责。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime

import pandas as pd

from quantdesk.core.enums import AdjustType, FeedCapability, Frequency
from quantdesk.core.models import Bar, Tick

HISTORY_COLUMNS = ["dt", "open", "high", "low", "close", "volume", "amount"]


class DataFeedAdapter(ABC):
    """行情数据插件基类。"""

    name: str = "base"                                     # 注册名，全局唯一
    capabilities: frozenset[FeedCapability] = frozenset()  # 能力声明，供路由

    # ---------- 生命周期 ----------
    @abstractmethod
    def connect(self) -> None:
        """建立连接 / 登录。失败抛 ConnectionError/AdapterError，由调用方决定重试。"""

    @abstractmethod
    def disconnect(self) -> None:
        """断开并释放资源。要求幂等。"""

    def __enter__(self) -> DataFeedAdapter:
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()

    # ---------- 核心接口 ----------
    @abstractmethod
    def get_history(
        self,
        symbol: str,
        freq: Frequency,
        start: datetime,
        end: datetime,
        adjust: AdjustType = AdjustType.FORWARD,
    ) -> pd.DataFrame:
        """历史 K 线。列固定为 HISTORY_COLUMNS，dt 为 datetime64[ns] 升序。
        无数据返回空 DataFrame（不抛异常）；能力不支持抛 FeedCapabilityError。"""

    @abstractmethod
    def subscribe_realtime(
        self, symbols: list[str], callback: Callable[[Tick | Bar], None]
    ) -> None:
        """订阅实时行情（增量追加调用）。tick / 分钟 K 经 callback 推出。
        不支持实时能力的实现抛 FeedCapabilityError（注册表按 capabilities 排除）。"""

    def unsubscribe_realtime(self, symbols: list[str]) -> None:
        """按需实现：仅具备实时能力的插件。"""
        raise NotImplementedError(f"{self.name} 不支持取消订阅")

    @abstractmethod
    def get_financial_data(self, symbol: str, report_type: str = "income") -> pd.DataFrame:
        """财务数据。report_type: income | balance | cashflow | indicator。
        不支持时抛 FeedCapabilityError。"""

    # ---------- 运维 ----------
    def health_check(self) -> bool:
        """轻量探活（取交易日历/服务器时间等），供 UI 状态灯与自动重连使用。"""
        return False
