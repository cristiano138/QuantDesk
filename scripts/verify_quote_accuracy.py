"""行情准确度跨源核对（用户要求：数据准确度 100%）。

对一批横跨各板块的标的，把腾讯 / 东方财富 / 同花顺三个公开源的同名字段
逐位比对（价格、今开、昨收、最高、最低、涨跌停、成交额、换算后的成交量），
并给腾讯五档盘口做结构校验（买卖价单调、相对最新价的位置、落在涨跌停区间内）。

口径纪律：
- 成交量：东财 A股=手；腾讯/同花顺 A股原始量可能是手也可能是股（科创板是股）——
  一律按「成交额 ÷ 股数 落在当日高低区间」反推换算成股后再比；
- 东财快照不提供五档，五档为腾讯单源 → 只做结构校验并如实标注单源；
- 任何不一致都写入 CSV 并计入失配清单，不允许"看起来对"。

用法：
  uv run python scripts/verify_quote_accuracy.py                # 默认抽样
  uv run python scripts/verify_quote_accuracy.py --per-board 10 # 每板块抽 10 只
输出：data/logs/verify_quote_accuracy_<ts>.csv + 控制台结论（全过=100% 才算过）。
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx

from quantdesk.config import data_dir

_UA = {"Referer": "https://quote.eastmoney.com/"}
_UT = "fa5fd1943c7b386f172d6893dbfba10b"
_BOARDS = [
    ("沪市主板", "m:1+t:2"), ("深市主板", "m:0+t:6"), ("创业板", "m:0+t:80"),
    ("科创板", "m:1+t:23"), ("北交所", "m:0+t:81+s:2048"), ("港股主板", "m:128+t:3"),
]
_ALWAYS = ["600000.SH", "000001.SZ", "600519.SH", "300750.SZ", "688836.SH",
           "00700.HK", "09988.HK", "03690.HK", "01810.HK"]
# 价格容差：两家都给两位小数，相等应逐位一致；留半分容差吸收浮点
_PX_TOL = 0.0051
# 金额相对容差：腾讯金额给「万元」精度，换算后有 ≤50 元绝对误差
_AMT_TOL = 0.0005
# 成交量（股）容差：东财给整数手，换算后有 ≤1 手舍入差
_VOL_TOL = 0.006


def _pad_hk(code: str) -> str:
    return code.zfill(5) + ".HK"


def fetch_universe(client: httpx.Client, per_board: int) -> list[str]:
    """每个板块按代码序抽前 N 只（确定性抽样），外加必查清单。

    clist 按代码升序会带出退市/长期停牌的旧代码（600001 邯钢等）——
    它们没有当日行情（东财无数据），不进核对样本；选股器同理应过滤。
    """
    out: list[str] = list(_ALWAYS)
    for _label, fs in _BOARDS:
        r = client.get("https://push2.eastmoney.com/api/qt/clist/get", params={
            "pn": 1, "pz": per_board, "po": 0, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f12", "fs": fs, "fields": "f2,f12,f13", "ut": _UT}, timeout=10)
        diff = ((r.json().get("data") or {}).get("diff")) or []
        for it in diff:
            code, mkt = str(it.get("f12", "")), str(it.get("f13", ""))
            px = it.get("f2")
            if not code or not isinstance(px, (int, float)) or px <= 0:
                continue  # 无当日行情（退市/停牌）不进样本
            if mkt == "128":
                out.append(_pad_hk(code))
            else:
                out.append(code + (".SH" if mkt == "1" else ".SZ" if mkt == "0" else ".BJ"))
    seen: set[str] = set()
    return [s for s in out if not (s in seen or seen.add(s))]


def _tx_parse(client: httpx.Client, symbols: list[str]) -> dict[str, dict]:
    """腾讯批量快照。量：按成交额反推是手还是股，统一换算成「股」。"""
    out: dict[str, dict] = {}
    codes = {("hk" + s[:5] if s.endswith(".HK")
              else ("sh" if s.endswith(".SH") else "bj" if s.endswith(".BJ") else "sz") + s[:6]): s
             for s in symbols}
    for i in range(0, len(codes), 60):
        chunk = list(codes)[i:i + 60]
        text = client.get("https://qt.gtimg.cn/q=" + ",".join(chunk), timeout=10,
                          headers={"Referer": "https://gu.qq.com/"}).content.decode("gbk", "ignore")
        for line in text.splitlines():
            if '="' not in line:
                continue
            key = line.split("=", 1)[0].strip()[2:]
            parts = line.split('="', 1)[1].strip().strip('";').split("~")
            sym = codes.get(key)
            if not sym or len(parts) < 40:
                continue
            f = lambda k: float(parts[k]) if k < len(parts) and parts[k].strip() else 0.0  # noqa: E731
            last, prev = f(3), f(4)
            hk = sym.endswith(".HK")
            high, low = f(33), f(34)
            amount = f(37) * (1.0 if hk else 1e4)   # 港股字段已是港元；A股是万元
            raw_vol = f(6)
            div = _vol_divisor(raw_vol, amount, low, high, "share" if hk else "auto")
            vol_shares = raw_vol * div
            bids = [(f(9 + 2 * k), f(10 + 2 * k)) for k in range(5)]
            asks = [(f(19 + 2 * k), f(20 + 2 * k)) for k in range(5)]
            lu, ld = f(47), f(48)
            out[sym] = {
                "name": parts[1].strip(), "last": last, "prev_close": prev,
                "open": f(5), "high": high, "low": low,
                "vol_shares": vol_shares, "amount": amount, "hk": hk,
                # 腾讯对无涨跌停（港股/退市）给 -1 占位，不能当真值
                "limit_up": lu if lu > 0 else None, "limit_down": ld if ld > 0 else None,
                "bids": [(p, v) for p, v in bids if p > 0 and v > 0],
                "asks": [(p, v) for p, v in asks if p > 0 and v > 0],
            }
    return out


def _vol_divisor(raw_vol: float, amount: float, low: float, high: float, mode: str) -> float:
    """原始量 → 股的乘数：1 表示原始已是股，100 表示原始是手。

    判据（与手机端同一套）：成交额÷股数 必须落在 [低×0.985, 高×1.015]。
    mode='auto' 两种解释都成立时按手（绝大多数 A 股），mode='share' 按股（港股/已知股源）。
    """
    if raw_vol <= 0 or amount <= 0 or high <= 0 or low <= 0:
        return 1.0

    def ok(price: float) -> bool:
        return low * 0.985 <= price <= high * 1.015

    as_share = ok(amount / raw_vol)             # 原始当股
    as_lot = ok(amount / (raw_vol * 100))       # 原始当手
    if as_share and not as_lot:
        return 1.0
    if as_lot and not as_share:
        return 100.0
    return 100.0 if mode in ("auto", "lot") else 1.0


def _em_parse(client: httpx.Client, symbol: str) -> dict | None:
    mkt = "116" if symbol.endswith(".HK") else ("1" if symbol.endswith(".SH") else "0")
    r = client.get("https://push2.eastmoney.com/api/qt/stock/get", params={
        "secid": f"{mkt}.{symbol[:6] if not symbol.endswith('.HK') else symbol[:5]}",
        "invt": 2, "fltt": 2, "ut": _UT,
        "fields": "f43,f44,f45,f46,f47,f48,f60,f51,f52"}, timeout=10)
    d = (r.json().get("data") or {})
    if not d:
        return None
    num = lambda k: (float(d[k]) if d.get(k) not in (None, "-") else None)  # noqa: E731
    hk = symbol.endswith(".HK")
    raw_vol = num("f47") or 0.0
    amount = num("f48") or 0.0
    high, low = num("f44") or 0.0, num("f45") or 0.0
    div = 1.0 if hk else _vol_divisor(raw_vol, amount, low, high, "lot")
    return {
        "last": num("f43") or 0.0, "prev_close": num("f60") or 0.0,
        "open": num("f46") or 0.0, "high": high, "low": low,
        "vol_shares": raw_vol * div, "amount": amount,
        "limit_up": None if hk else num("f51"), "limit_down": None if hk else num("f52"),
    }


def _ths_parse(client: httpx.Client, symbol: str) -> dict | None:
    """同花顺 realhead（A股）。量给「股」；下标 6=昨收 7=今开（2026-11-05 钉死）。"""
    import json

    if symbol.endswith(".HK"):
        return None
    u = "https://d.10jqka.com.cn/v6/realhead/hs_" + symbol[:6] + "/last.js"
    t = client.get(u, timeout=10, headers={"Referer": "https://stockpage.10jqka.com.cn/"}).text
    m = re.search(r'"items":(\{.*?\})', t, re.S)
    if not m:
        return None
    it = json.loads(m.group(1))

    def g(k: str) -> float:
        try:
            v = float(it.get(k, "") or 0)
            return v
        except ValueError:
            return 0.0

    if g("10") <= 0:
        return None
    return {
        "last": g("10"), "prev_close": g("6"), "open": g("7"),
        "high": g("8"), "low": g("9"), "vol_shares": g("13"), "amount": g("19"),
        "limit_up": g("69") or None, "limit_down": g("70") or None,
    }


def _close(a: float | None, b: float | None, tol_abs: float = _PX_TOL,
           tol_rel: float = 0.0) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if a == b:
        return True
    return abs(a - b) <= max(tol_abs, tol_rel * max(abs(a), abs(b)))


def _px_or_none(v: float | None) -> float | None:
    """0 是「来源未提供」（当日无成交的港股，开高低留空），不是价格 0 元。"""
    return v if v and v > 0 else None


def compare(symbol: str, tx: dict | None, em: dict | None, ths: dict | None) -> list[dict]:
    """逐字段比对，返回失配行（一致的不落 CSV，只计数）。"""
    rows: list[dict] = []
    pairs = [("tx", tx, "em", em), ("tx", tx, "ths", ths), ("em", em, "ths", ths)]

    def chk(field: str, va, vb, sa: str, sb: str, *, tol_abs=_PX_TOL, tol_rel=0.0) -> None:
        if va is None and vb is None:
            return
        if va is None or vb is None:
            rows.append({"symbol": symbol, "field": field, "src_a": sa, "val_a": va,
                         "src_b": sb, "val_b": vb, "diff": "单侧缺失"})
            return
        if not _close(va, vb, tol_abs, tol_rel):
            rows.append({"symbol": symbol, "field": field, "src_a": sa, "val_a": round(va, 4),
                         "src_b": sb, "val_b": round(vb, 4),
                         "diff": round(abs(va - vb), 4)})

    for sa, A, sb, B in pairs:
        if A is None or B is None:
            continue
        for fld in ("last", "prev_close"):
            chk(fld, A.get(fld), B.get(fld), sa, sb)
        for fld in ("open", "high", "low"):
            # 开高低：0 = 来源未提供（无成交日），单侧缺失如实单列，不算数值冲突
            chk(fld, _px_or_none(A.get(fld)), _px_or_none(B.get(fld)), sa, sb)
        chk("vol_shares", A.get("vol_shares"), B.get("vol_shares"), sa, sb,
            tol_abs=100.0, tol_rel=_VOL_TOL)
        chk("amount", A.get("amount"), B.get("amount"), sa, sb,
            tol_abs=50.0, tol_rel=_AMT_TOL)
        if not symbol.endswith(".HK"):
            chk("limit_up", A.get("limit_up"), B.get("limit_up"), sa, sb)
            chk("limit_down", A.get("limit_down"), B.get("limit_down"), sa, sb)
    return rows


def check_book(tx: dict) -> list[str]:
    """腾讯五档结构校验（单源，无第二源可比 → 校验结构合理性）。"""
    problems: list[str] = []
    bids, asks = tx.get("bids") or [], tx.get("asks") or []
    bp = [p for p, _ in bids]
    ap = [p for p, _ in asks]
    if bp != sorted(bp, reverse=True):
        problems.append("买档价格不是从高到低")
    if ap != sorted(ap):
        problems.append("卖档价格不是从低到高")
    last = tx.get("last") or 0.0
    if bp and ap and last > 0:
        if bp[0] > last + _PX_TOL:
            problems.append(f"买一 {bp[0]} 高于最新价 {last}")
        if ap[0] < last - _PX_TOL:
            problems.append(f"卖一 {ap[0]} 低于最新价 {last}")
    lu, ld = tx.get("limit_up"), tx.get("limit_down")
    for p in bp + ap:
        if lu and p > lu + _PX_TOL:
            problems.append(f"档位价 {p} 超过涨停 {lu}")
            break
        if ld and p < ld - _PX_TOL:
            problems.append(f"档位价 {p} 低于跌停 {ld}")
            break
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="行情准确度跨源核对")
    ap.add_argument("--per-board", type=int, default=25, help="每板块抽样数")
    ap.add_argument("--no-ths", action="store_true", help="跳过同花顺源")
    args = ap.parse_args()

    client = httpx.Client(headers=_UA, timeout=10)
    symbols = fetch_universe(client, args.per_board)
    print(f"待核对 {len(symbols)} 只（每板块 {args.per_board} + 必查 {len(_ALWAYS)}）")

    tx_all = _tx_parse(client, symbols)
    print(f"腾讯快照取到 {len(tx_all)} 只")

    fields_checked = 0
    mismatches: list[dict] = []
    book_problems: list[tuple[str, str]] = []
    checked = 0
    for i, sym in enumerate(symbols, 1):
        tx = tx_all.get(sym)
        if tx is None or tx.get("last", 0) <= 0:
            continue  # 腾讯都没有有效行情（退市/停牌）→ 无从核对
        try:
            em = _em_parse(client, sym)
        except Exception as e:  # noqa: BLE001
            em = None
            print(f"[{i}/{len(symbols)}] {sym} 东财取数失败 {type(e).__name__}")
        if em is None or em.get("last", 0) <= 0:
            continue  # 东财无该标的数据（退市/停牌）→ 跳过，不算失配
        checked += 1
        ths = None
        if not args.no_ths and not sym.endswith(".HK"):
            try:
                ths = _ths_parse(client, sym)
            except Exception:  # noqa: BLE001
                ths = None
            import time
            time.sleep(0.25)
        rows = compare(sym, tx, em, ths)
        fields_checked += 10  # 每标的固定比 10 类字段（三源两两）
        mismatches.extend(rows)
        for p in check_book(tx):
            book_problems.append((sym, p))
        if i % 25 == 0:
            print(f"  进度 {i}/{len(symbols)}，当前失配 {len(mismatches)}")

    logs = data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    out = logs / f"verify_quote_accuracy_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "field", "src_a", "val_a", "src_b", "val_b", "diff"])
        for r in mismatches:
            w.writerow([r["symbol"], r["field"], r["src_a"], r["val_a"], r["src_b"],
                        r["val_b"], r["diff"]])
        for sym, p in book_problems:
            w.writerow([sym, "盘口结构", "tx", p, "", "", ""])

    conflicts = [r for r in mismatches if r["diff"] != "单侧缺失"]
    missing = [r for r in mismatches if r["diff"] == "单侧缺失"]
    acc = 100.0 if fields_checked == 0 else (1 - len(conflicts) / fields_checked) * 100
    print(f"\n核对 {checked} 只 × 10 类字段 ≈ {fields_checked} 项")
    print(f"数值冲突 {len(conflicts)} 项，单侧缺失 {len(missing)} 项（来源不提供，如实标注不算冲突），"
          f"盘口结构问题 {len(book_problems)} 项")
    print(f"准确度 {acc:.3f}%（要求 100%）")
    print(f"明细: {out}")
    ok = not conflicts and not book_problems
    print("结论:", "通过（100%）" if ok else "未通过——冲突明细见 CSV，必须修到零冲突")
    client.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
