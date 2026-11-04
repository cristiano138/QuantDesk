"""统一证券代码体系。

内部规范（宪法核心契约）："600000.SH" / "000001.SZ" / "830001.BJ" / "00700.HK"。
与各源格式互转只发生在本模块与适配器内部，禁止散落别处。

支持源格式（K3）：
  qmt    : "600000.SH"（与内部一致，零转换）
  ifind  : "600000.SH"（与内部一致）
  akshare: "600000"（6 位裸代码）
  tdx    : "1.600000"（1=沪 0=深；W1 不支持北交所）
  gm     : "SHSE.600000" / "SZSE.000001" / "BSE.830001"
  em     : "1.600000" / "0.000001" / "0.830001" / "116.00700"（东方财富 secid，含港股）
  tx     : "sh600000" / "sz000001" / "hk00700"（腾讯行情代码，含港股）

港股口径说明：港股代码统一补足 5 位（700 → 00700）。本项目港股**只读**
（无涨跌停/T+1/整手概念，且不接券商下单通道），风控闸口对 .HK 一律拒绝下单。
"""

import re
import threading
from collections.abc import Iterable

from quantdesk.core.enums import Board
from quantdesk.core.errors import SymbolError

_INTERNAL_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")
_HK_RE = re.compile(r"^(\d{1,5})\.(HK)$")
_BARE_RE = re.compile(r"^\d{6}$")
_TDX_RE = re.compile(r"^([01])\.(\d{6})$")
_GM_RE = re.compile(r"^(SHSE|SZSE|BSE)\.(\d{6})$", re.IGNORECASE)
_EM_RE = re.compile(r"^(1|0|116)\.(\d{1,6})$")
_TX_RE = re.compile(r"^(sh|sz|hk|bj)(\d{1,6})$", re.IGNORECASE)

# 东财 secid 市场位 → 内部后缀
_EM_MARKET = {"1": "SH", "0": "SZ", "116": "HK"}
_TX_MARKET = {"sh": "SH", "sz": "SZ", "hk": "HK", "bj": "BJ"}

# 北交所代码段（K1：8xxxxx 等；92xxxx 为新增段）
_BJ_PREFIXES = ("43", "83", "87", "88", "92", "4", "8")

_ST_LOCK = threading.Lock()
_ST_SYMBOLS: set[str] = set()


def _suffix_of_bare(code6: str) -> str:
    """6 位裸代码 → 市场后缀（AKShare 口径推断）。"""
    if code6.startswith("6"):
        return "SH"
    if code6.startswith(("0", "3")):
        return "SZ"
    if code6.startswith(_BJ_PREFIXES):
        return "BJ"
    raise SymbolError(f"无法识别市场前缀: {code6!r}")


def normalize(symbol: str) -> str:
    """宽容归一：接受内部/裸码/东财/腾讯/掘金/通达信写法，输出内部规范代码。

    用户在手填框里打 "00700"、"hk00700"、"116.00700"、"700.HK" 都应当认得——
    拒绝歧义比猜市场更安全，猜错等于看错股票。
    """
    s = str(symbol or "").strip().upper()
    if not s:
        raise SymbolError("空代码")
    if _INTERNAL_RE.match(s) or _HK_RE.match(s):
        return _validate_internal(s)
    if _EM_RE.match(s):
        return to_internal(s, "em")
    if _TX_RE.match(s):
        return to_internal(s, "tx")
    if _TDX_RE.match(s):
        return to_internal(s, "tdx")
    if _GM_RE.match(s):
        return to_internal(s, "gm")
    if _BARE_RE.match(s):
        return f"{s}.{_suffix_of_bare(s)}"
    if re.match(r"^\d{1,5}$", s):                    # 裸港股码（4~5 位，可省略前导零）
        return f"{s.zfill(5)}.HK"
    raise SymbolError(f"无法识别的证券代码: {symbol!r}")


def _validate_internal(symbol: str) -> str:
    s = symbol.strip().upper()
    if _INTERNAL_RE.match(s):
        return s
    m = _HK_RE.match(s)
    if m:
        return f"{m.group(1).zfill(5)}.HK"           # 港股统一 5 位
    raise SymbolError(f"非法内部代码: {symbol!r}（期望如 600000.SH / 00700.HK）")


def to_internal(code: str, source: str) -> str:
    """各源代码 → 内部规范代码。"""
    src = source.strip().lower()
    c = code.strip()
    if src in ("qmt", "ifind"):
        return _validate_internal(c)
    if src == "akshare":
        if not _BARE_RE.match(c):
            raise SymbolError(f"akshare 源期望 6 位裸代码: {code!r}")
        return f"{c}.{_suffix_of_bare(c)}"
    if src == "tdx":
        m = _TDX_RE.match(c)
        if not m:
            raise SymbolError(f"tdx 源期望如 1.600000 / 0.000001: {code!r}")
        mkt, num = m.group(1), m.group(2)
        if mkt == "1":
            return f"{num}.SH"
        if num.startswith(_BJ_PREFIXES):
            raise SymbolError(f"tdx 源 W1 不支持北交所代码: {code!r}")
        return f"{num}.SZ"
    if src == "gm":
        m = _GM_RE.match(c.upper())
        if not m:
            raise SymbolError(f"gm 源期望如 SHSE.600000: {code!r}")
        suffix = {"SHSE": "SH", "SZSE": "SZ", "BSE": "BJ"}[m.group(1).upper()]
        return f"{m.group(2)}.{suffix}"
    if src == "em":
        m = _EM_RE.match(c)
        if not m:
            raise SymbolError(f"em 源期望如 1.600000 / 116.00700: {code!r}")
        suffix, num = _EM_MARKET[m.group(1)], m.group(2)
        if suffix == "HK":
            return f"{num.zfill(5)}.HK"
        if len(num) != 6:
            raise SymbolError(f"A股代码应为 6 位: {code!r}")
        return f"{num}.{suffix}"
    if src == "tx":
        m = _TX_RE.match(c)
        if not m:
            raise SymbolError(f"tx 源期望如 sh600000 / hk00700: {code!r}")
        suffix, num = _TX_MARKET[m.group(1).lower()], m.group(2)
        if suffix == "HK":
            return f"{num.zfill(5)}.HK"
        if len(num) != 6:
            raise SymbolError(f"A股代码应为 6 位: {code!r}")
        return f"{num}.{suffix}"
    raise SymbolError(f"未知数据源: {source!r}（支持 qmt/ifind/akshare/tdx/gm/em/tx）")


def from_internal(symbol: str, source: str) -> str:
    """内部规范代码 → 各源代码。"""
    s = _validate_internal(symbol)
    num, suffix = s.split(".")
    src = source.strip().lower()
    if src in ("qmt", "ifind"):
        return s
    if src == "akshare":
        if suffix == "HK":
            return num                       # 东财/新浪港股接口用 5 位裸码
        return num
    if src == "tdx":
        if suffix == "SH":
            return f"1.{num}"
        if suffix == "SZ":
            return f"0.{num}"
        raise SymbolError(f"tdx 源 W1 不支持北交所/港股: {symbol!r}")
    if src == "gm":
        if suffix == "HK":
            raise SymbolError(f"gm 通道未接入港股行情: {symbol!r}")
        prefix = {"SH": "SHSE", "SZ": "SZSE", "BJ": "BSE"}[suffix]
        return f"{prefix}.{num}"
    if src == "em":
        market = {"SH": "1", "SZ": "0", "BJ": "0", "HK": "116"}[suffix]
        return f"{market}.{num}"
    if src == "tx":
        return f"{suffix.lower()}{num}"
    raise SymbolError(f"未知数据源: {source!r}")


def is_hk(symbol: str) -> bool:
    """是否港股（只读标的：不得进入下单路径，见 engine/riskgate 的显式拒绝）。

    容忍非规范写法（hk00700 / 116.00700 / 700.HK）：风控咽喉在拿到规范代码之前就要判定，
    这里若抛异常会把"拒单"变成 500。认不出来的代码返回 False，交给 board_of 去报非法。
    """
    try:
        return normalize(symbol).endswith(".HK")
    except SymbolError:
        return False


def board_of(symbol: str) -> Board:
    """板块识别（K1 代码段：60xxxx 沪主板 / 00xxxx 深主板 / 30xxxx 创业板 /
    688xxx 科创板 / 8xxxxx·43xxxx·92xxxx 北交所；港股单列 Board.HK）。"""
    s = _validate_internal(symbol)
    if s.endswith(".HK"):
        return Board.HK
    num = s.split(".")[0]
    if num.startswith(("688", "689")):
        return Board.STAR
    if num.startswith(("300", "301", "302")):
        return Board.GEM
    if num.startswith(_BJ_PREFIXES):
        return Board.BSE
    if num.startswith(("60", "00", "001", "002", "003")):
        return Board.MAIN
    raise SymbolError(f"无法识别板块: {symbol!r}")


def set_st_names(names: Iterable[str]) -> None:
    """注册 ST/*ST 证券（按内部代码或 6 位裸代码）。

    ST 判定依赖证券简称，简称由行情源回填（W3+）；此处先留映射接口（D02 预案）。
    """
    normalized: set[str] = set()
    for n in names:
        t = n.strip().upper()
        if _INTERNAL_RE.match(t):
            normalized.add(t)
        elif _BARE_RE.match(t):
            normalized.add(f"{t}.{_suffix_of_bare(t)}")
        else:
            raise SymbolError(f"非法代码: {n!r}")
    global _ST_SYMBOLS
    with _ST_LOCK:
        _ST_SYMBOLS |= normalized


def is_st(symbol: str) -> bool:
    """是否 ST（未注册返回 False；涨跌停校验 D16 使用）。"""
    return _validate_internal(symbol) in _ST_SYMBOLS
