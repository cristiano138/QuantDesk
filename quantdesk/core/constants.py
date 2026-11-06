"""A 股规则常量（K1 交易规则速查落地）。

规则会变：涨跌停/整手/费率全部集中于此，且可被 config/设置页覆盖（D19）。
费率以券商实际为准。
"""

from quantdesk.core.enums import Board

# 涨跌停幅度：主板 ±10% / 创业板·科创板 ±20% / 北交所 ±30%；ST ±5%
# 港股（Board.HK）不设涨跌幅，这里用 None 明示「没有这个概念」，而不是留 KeyError。
LIMIT_PCT: dict[Board, float | None] = {
    Board.MAIN: 0.10,
    Board.GEM: 0.20,
    Board.STAR: 0.20,
    Board.BSE: 0.30,
    Board.HK: None,
}
ST_PCT = 0.05
PRICE_TICK = 0.01

# 整手规则：主板·创业板 100 股整数倍；科创板 ≥200 股后 1 股递增；北交所 ≥100 股
LOT_SIZE = 100
STAR_MIN_VOLUME = 200
BSE_MIN_VOLUME = 100
# 港股每手股数由发行人自定（100/200/500/1000/2000 都有），公开快照不给这个字段，
# 所以模拟盘按 1 股递增放行，并在界面注明「实际下单须按该股的每手股数」。
HK_MIN_VOLUME = 1

# 交收周期：A 股（含 B 股）T+1 卖出限制；港股 T+0（当日买入当日可卖）
T_PLUS_1_BOARDS = (Board.MAIN, Board.GEM, Board.STAR, Board.BSE)

# 费率默认值（config 可覆盖）
COMMISSION_RATE = 2.5e-4   # 佣金万 2.5（双边）
MIN_COMMISSION = 5.0       # 佣金最低 5 元
STAMP_TAX_SELL = 5e-4      # 印花税，卖出单边 0.05%
TRANSFER_FEE = 1e-4        # 过户费万 0.1（双边，按成交金额）
# 港股三项（港币口径，交易所/证监会公布费率）
HK_STAMP_RATE = 1e-3       # 印花税 0.1% 双边，不足 1 港币按 1 港币计
HK_FRC_RATE = 5.65e-5      # 联交所交易费 0.00565%
HK_AFRC_RATE = 2.7e-5      # 证监会交易征费 0.0027%
HK_SETTLE_RATE = 2e-5      # 股份交收费 0.002%
HK_SETTLE_MIN = 2.0
HK_SETTLE_MAX = 100.0


def limit_pct(board: Board, is_st: bool = False) -> float | None:
    """涨跌停幅度；None = 该板块没有涨跌幅限制（港股）。"""
    if is_st:
        return ST_PCT
    return LIMIT_PCT.get(board)


def has_price_limit(board: Board) -> bool:
    return LIMIT_PCT.get(board) is not None


def is_t_plus_1(board: Board) -> bool:
    return board in T_PLUS_1_BOARDS


def min_volume(board: Board) -> int:
    """该板块买入的最小委托量（前端下单框按这个预填，避免用户猜）。"""
    if board is Board.STAR:
        return STAR_MIN_VOLUME
    if board is Board.BSE:
        return BSE_MIN_VOLUME
    if board is Board.HK:
        return HK_MIN_VOLUME
    return LOT_SIZE


def lot_step(board: Board) -> int:
    """委托量的递增单位：主板/创业板 100 股一档；科创/北交/港股 1 股一档。

    和 min_volume 是两件事——科创 201 股是合法委托，按 100 取整会把它削成 200。
    """
    return LOT_SIZE if board in (Board.MAIN, Board.GEM) else 1


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
    北交所：≥100 股（1 股递增）；港股：任意正整数（每手股数由发行人自定，快照不给，
    模拟盘不因此拒单）。卖出零股申报为 W1 简化，D16 细化。
    """
    if volume <= 0 or volume != int(volume):
        return False
    v = int(volume)
    if board is Board.STAR:
        return v >= STAR_MIN_VOLUME
    if board is Board.BSE:
        return v >= BSE_MIN_VOLUME
    if board is Board.HK:
        return v >= HK_MIN_VOLUME
    return v % LOT_SIZE == 0


def lot_rule_text(board: Board) -> str:
    """委托量规则的中文说明（拒单消息与下单框提示共用一份，避免两处各写一套）。"""
    if board is Board.STAR:
        return "科创板买入须 ≥200 股，其后 1 股递增"
    if board is Board.BSE:
        return "北交所买入须 ≥100 股，其后 1 股递增"
    if board is Board.HK:
        return "港股按 1 股递增模拟（实际下单须凑整到该股每手股数，100~2000 股不等）"
    return "买入须为 100 股整数倍"
