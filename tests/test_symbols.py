"""D02：统一证券代码体系测试（DoD：≥20 用例，五格式互转/板块/ST/非法输入）。"""

import pytest

from quantdesk.core.enums import Board
from quantdesk.core.errors import SymbolError
from quantdesk.core.symbols import (
    board_of,
    currency_of,
    from_internal,
    is_st,
    normalize,
    set_st_names,
    to_internal,
)


# ---------- to_internal：五格式 ----------
def test_to_internal_qmt_identity():
    assert to_internal("600000.SH", "qmt") == "600000.SH"


def test_to_internal_ifind_identity():
    assert to_internal("000001.SZ", "ifind") == "000001.SZ"


def test_to_internal_qmt_lowercase_suffix_normalized():
    assert to_internal("600000.sh", "qmt") == "600000.SH"


def test_to_internal_akshare_sh():
    assert to_internal("600000", "akshare") == "600000.SH"


def test_to_internal_akshare_sz():
    assert to_internal("000001", "akshare") == "000001.SZ"


def test_to_internal_akshare_gem():
    assert to_internal("300750", "akshare") == "300750.SZ"


def test_to_internal_akshare_bse():
    assert to_internal("830001", "akshare") == "830001.BJ"


def test_to_internal_tdx_sh():
    assert to_internal("1.600000", "tdx") == "600000.SH"


def test_to_internal_tdx_sz():
    assert to_internal("0.000001", "tdx") == "000001.SZ"


def test_to_internal_gm_sh():
    assert to_internal("SHSE.600000", "gm") == "600000.SH"


def test_to_internal_gm_sz():
    assert to_internal("SZSE.000001", "gm") == "000001.SZ"


def test_to_internal_gm_bse():
    assert to_internal("BSE.830001", "gm") == "830001.BJ"


# ---------- from_internal：五格式往返 ----------
def test_roundtrip_all_sources():
    for source, expected in [
        ("qmt", "600000.SH"),
        ("ifind", "600000.SH"),
        ("akshare", "600000"),
        ("tdx", "1.600000"),
        ("gm", "SHSE.600000"),
    ]:
        assert from_internal("600000.SH", source) == expected


def test_from_internal_tdx_sz():
    assert from_internal("000001.SZ", "tdx") == "0.000001"


def test_from_internal_tdx_bse_rejected():
    with pytest.raises(SymbolError):
        from_internal("830001.BJ", "tdx")


def test_from_internal_gm_bse():
    assert from_internal("830001.BJ", "gm") == "BSE.830001"


# ---------- board_of ----------
def test_board_main_sh():
    assert board_of("600000.SH") is Board.MAIN


def test_board_main_sz():
    assert board_of("000001.SZ") is Board.MAIN


def test_board_gem():
    assert board_of("300750.SZ") is Board.GEM


def test_board_star():
    assert board_of("688111.SH") is Board.STAR


def test_board_bse():
    assert board_of("830001.BJ") is Board.BSE


def test_board_bse_92():
    assert board_of("920001.BJ") is Board.BSE


# ---------- ST 注册接口 ----------
def test_st_registry():
    set_st_names(["000001", "600000.SH"])
    assert is_st("000001.SZ")
    assert is_st("600000.SH")
    assert not is_st("600519.SH")


# ---------- 非法输入 ----------
def test_illegal_internal_missing_suffix():
    with pytest.raises(SymbolError):
        to_internal("600000", "qmt")


def test_illegal_internal_bad_suffix():
    with pytest.raises(SymbolError):
        to_internal("600000.XX", "qmt")


def test_illegal_akshare_short():
    with pytest.raises(SymbolError):
        to_internal("60000", "akshare")


def test_illegal_tdx_format():
    with pytest.raises(SymbolError):
        to_internal("2.600000", "tdx")


def test_unknown_source():
    with pytest.raises(SymbolError):
        to_internal("600000", "wind")


def test_board_of_unknown_prefix():
    with pytest.raises(SymbolError):
        board_of("999999.SH")


def test_board_of_malformed():
    with pytest.raises(SymbolError):
        board_of("600000")  # 缺后缀


# ---------- B 股 / 新增代码段：曾经这些代码在交易链路上直接抛「无法识别市场前缀」 ----------

def test_b_share_codes_map_to_their_exchange():
    assert normalize("900921") == "900921.SH"
    assert normalize("200725") == "200725.SZ"
    assert normalize("sh900921") == "900921.SH"
    assert board_of("900921.SH") is Board.MAIN
    assert board_of("200725.SZ") is Board.MAIN


def test_b_share_currency():
    assert currency_of("900921.SH") == "USD"      # 沪B 以美元报价
    assert currency_of("200725.SZ") == "HKD"      # 深B 以港币报价
    assert currency_of("600000.SH") == "CNY"
    assert currency_of("00700.HK") == "HKD"


def test_new_code_segments():
    """创业板新增段 30xxxx、北交所新增段 92xxxx 都要认，别用「猜不到就拒」。"""
    assert board_of("302132.SZ") is Board.GEM
    assert board_of("920770.BJ") is Board.BSE
    assert board_of("689009.SH") is Board.STAR
    assert normalize("920770") == "920770.BJ"


def test_source_roundtrip_for_b_shares():
    assert from_internal("900921.SH", "tx") == "sh900921"
    assert from_internal("200725.SZ", "em") == "0.200725"
    assert to_internal("200725", "akshare") == "200725.SZ"
