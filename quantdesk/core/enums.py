"""统一枚举（宪法核心契约：取值不得删减）。"""

from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(str, Enum):
    """订单状态机。PENDING_CONFIRM/EXPIRED_CONFIRM 为实盘二次确认专用（D17）。"""

    PENDING_CONFIRM = "pending_confirm"
    SUBMITTED = "submitted"
    PART_FILLED = "part_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"
    EXPIRED_CONFIRM = "expired_confirm"


class Frequency(str, Enum):
    """K 线周期。日线族按年分文件、分钟族按月分文件（存储层）。"""

    TICK = "tick"
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "60m"
    D1 = "1d"
    W1 = "1w"
    MO1 = "1M"


class AdjustType(str, Enum):
    """复权口径。

    宪法价格口径：下单价格永远是原始市场价（NONE）；
    回测与指标用前复权（FORWARD）。
    """

    NONE = "none"
    FORWARD = "forward"
    BACKWARD = "backward"


class FeedCapability(str, Enum):
    """数据源能力声明（注册表/路由层据此选择与排除）。"""

    HISTORY_BAR = "history_bar"
    REALTIME_TICK = "realtime_tick"
    REALTIME_BAR = "realtime_bar"
    FINANCIAL = "financial"
    TRANSACTION = "transaction"


class Board(str, Enum):
    """板块（涨跌停/整手规则按板块区分，见 core/constants.py）。"""

    MAIN = "main"    # 沪深主板
    GEM = "gem"      # 创业板
    STAR = "star"    # 科创板
    BSE = "bse"      # 北交所
