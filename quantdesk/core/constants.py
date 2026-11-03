"""A 股规则常量（K1 交易规则速查落地）。

规则会变：涨跌停/整手/费率全部集中于此，且可被 config/设置页覆盖（D19）。
费率以券商实际为准。
"""

from quantdesk.core.enums import Board

# 涨跌停幅度：主板 ±10% / 创业板·科创板 ±20% / 北交所 ±30%；ST ±5%
LIMIT_PCT: dict[Board, float] = {
    Board.MAIN: 0.10,
    Board.GEM: 0.20,
    Board.STAR: 0.20,
    Board.BSE: 0.30,
}
ST_PCT = 0.05
PRICE_TICK = 0.01

# 整手规则：主板·创业板 100 股整数倍；科创板 ≥200 股后 1 股递增；北交所 ≥100 股
LOT_SIZE = 100
STAR_MIN_VOLUME = 200
BSE_MIN_VOLUME = 100

# 费率默认值（config 可覆盖）
COMMISSION_RATE = 2.5e-4   # 佣金万 2.5（双边）
MIN_COMMISSION = 5.0       # 佣金最低 5 元
STAMP_TAX_SELL = 5e-4      # 印花税，卖出单边 0.05%
TRANSFER_FEE = 1e-4        # 过户费万 0.1（双边，按成交金额）


def limit_pct(board: Board, is_st: bool = False) -> float:
    return ST_PCT if is_st else LIMIT_PCT[board]


def _round_price(p: float) -> float:
    # 涨跌停价按分取整；+1e-9 抵消浮点边界（如 9.99 * 1.1 = 10.989 → 10.99）
    return round(p + 1e-9, 2)


def limit_up(prev_close: float, board: Board, is_st: bool = False) -> float:
    """涨停价 = 昨收 × (1 + 幅度)，四舍五入到分。"""
    return _round_price(prev_close * (1 + limit_pct(board, is_st)))


def limit_down(prev_close: float, board: Board, is_st: bool = False) -> float:
    """跌停价 = 昨收 × (1 - 幅度)，四舍五入到分。"""
    return _round_price(prev_close * (1 - limit_pct(board, is_st)))


def is_valid_lot(volume: float, board: Board) -> bool:
    """整手校验（D16 RiskGate 第②道闸使用）。

    主板/创业板：100 股整数倍；科创板：≥200 股（其后 1 股递增，任意整数成立）；
    北交所：≥100 股（1 股递增）。卖出零股申报为 W1 简化，D16 细化。
    """
    if volume <= 0 or volume != int(volume):
        return False
    v = int(volume)
    if board is Board.STAR:
        return v >= STAR_MIN_VOLUME
    if board is Board.BSE:
        return v >= BSE_MIN_VOLUME
    return v % LOT_SIZE == 0
