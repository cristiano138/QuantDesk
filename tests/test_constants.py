"""D02：A 股规则常量测试（K1：涨跌停/整手/费率）。"""

import pytest

from quantdesk.core.constants import (
    COMMISSION_RATE,
    LIMIT_PCT,
    MIN_COMMISSION,
    STAMP_TAX_SELL,
    ST_PCT,
    TRANSFER_FEE,
    is_valid_lot,
    limit_down,
    limit_pct,
    limit_up,
)
from quantdesk.core.enums import Board


# ---------- 涨跌停 ----------
def test_limit_pct_table():
    assert LIMIT_PCT[Board.MAIN] == 0.10
    assert LIMIT_PCT[Board.GEM] == 0.20
    assert LIMIT_PCT[Board.STAR] == 0.20
    assert LIMIT_PCT[Board.BSE] == 0.30
    assert ST_PCT == 0.05


def test_limit_up_main():
    # 9.99 × 1.1 = 10.989 → 10.99
    assert limit_up(9.99, Board.MAIN) == 10.99


def test_limit_down_main():
    assert limit_down(10.00, Board.MAIN) == 9.00


def test_limit_up_gem():
    assert limit_up(10.00, Board.GEM) == 12.00


def test_limit_up_star():
    assert limit_up(50.00, Board.STAR) == 60.00


def test_limit_up_bse():
    assert limit_up(10.00, Board.BSE) == 13.00


def test_limit_st_override():
    assert limit_up(10.00, Board.MAIN, is_st=True) == 10.50
    assert limit_pct(Board.MAIN, is_st=True) == ST_PCT


def test_limit_up_floating_point_boundary():
    # 4.44 × 1.1 = 4.884 → 4.88（浮点边界不出 4.889999 错值）
    assert limit_up(4.44, Board.MAIN) == 4.88


# ---------- 整手 ----------
def test_lot_main_ok():
    assert is_valid_lot(100, Board.MAIN)
    assert is_valid_lot(1500, Board.GEM)


def test_lot_main_odd():
    assert not is_valid_lot(101, Board.MAIN)
    assert not is_valid_lot(50, Board.MAIN)


def test_lot_star_min_200():
    assert not is_valid_lot(199, Board.STAR)
    assert is_valid_lot(200, Board.STAR)
    assert is_valid_lot(201, Board.STAR)  # 科创板 200 股后 1 股递增


def test_lot_bse():
    assert not is_valid_lot(99, Board.BSE)
    assert is_valid_lot(100, Board.BSE)
    assert is_valid_lot(101, Board.BSE)


def test_lot_zero_and_fraction():
    assert not is_valid_lot(0, Board.MAIN)
    assert not is_valid_lot(100.5, Board.MAIN)


# ---------- 费率默认值 ----------
def test_fee_defaults():
    assert COMMISSION_RATE == 2.5e-4
    assert MIN_COMMISSION == 5.0
    assert STAMP_TAX_SELL == 5e-4
    assert TRANSFER_FEE == 1e-4


def test_lot_rejects_negative():
    assert not is_valid_lot(-100, Board.MAIN)
