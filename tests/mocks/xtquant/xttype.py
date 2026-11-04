"""xttype 假实现：只用到 StockAccount（资金账号句柄）。"""

from __future__ import annotations

__all__ = ["StockAccount", "ACCOUNT_TYPE_FUTURE", "ACCOUNT_TYPE_STOCK"]

ACCOUNT_TYPE_STOCK = "STOCK"
ACCOUNT_TYPE_FUTURE = "FUTURE"


class StockAccount:
    """资金账号对象：真实 xttype 的 account_id 是字符串，账户类型可选。"""

    def __init__(self, account_id: str, account_type: str = ACCOUNT_TYPE_STOCK,
                 alias: str = "") -> None:
        self.account_id = str(account_id)
        self.account_type = account_type
        self.alias = alias

    def __repr__(self) -> str:  # 日志/调试永不回显敏感全文
        return f"StockAccount({str(self.account_id)[:4]}***)"
