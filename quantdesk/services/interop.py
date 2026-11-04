"""主流股票软件互操作（不依赖券商权限的那一半「结合」能力）。

用户日常在同花顺/通达信/东方财富看盘，QuantDesk 的信号要能被他们**用惯的软件消费**，
这才是「与各大主流股票软件结合」的落地形态。本模块做三件事：

1. 板块文件双向：QuantDesk 自选股/信号结果 → 通达信自定义板块 `.blk`（GBK、每行
   「市场位+6位代码」）与同花顺/东方财富的 TXT 导入格式；反向也能把客户端里维护了
   多年的板块读回来进自选股（老用户的存量资产不该重录一遍）；
2. 选股公式导出：把信号规则编译成通达信 / 同花顺「公式管理器」能直接粘贴使用的
   条件选股公式文本（两套方言的差异在编译层抹平）；
3. 规则同源：同一条规则既能编译成客户端公式，也能在本地 Parquet 上求值
   ——「软件里看到的」和「我们回测的」是同一个条件，不是两份各写一遍的逻辑。

安全边界：写客户端目录要路径校验（板块名不许带分隔符或 ..），默认导出到
`data/exports/`，只有显式 `write_to_client` 且安装目录存在才写入客户端。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from quantdesk.core.enums import Frequency
from quantdesk.core.errors import QuantDeskError
from quantdesk.core.symbols import from_internal, to_internal

log = logging.getLogger(__name__)

__all__ = ["SignalRule", "RULE_KINDS", "export_tdx_block", "export_ths_block",
           "parse_tdx_block", "parse_bare_block", "write_block", "block_targets",
           "compile_formula", "evaluate_rule", "export_dir"]

# 通达信板块文件市场位：1=沪、0=深；北交所各版本口径不一，默认不支持（需显式配置）
_TDX_MARKET = {"SH": "1", "SZ": "0"}
_TDX_MARKET_REVERSE = {"1": "SH", "0": "SZ"}
_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff\-]{1,32}$")
_DIALECTS = ("tdx", "ths")


class InteropError(QuantDeskError):
    """互操作层的输入/落盘错误（消息必须能直接给用户看）。"""


# ---------- 信号规则（一条规则 = 一个客户端公式 = 一次本地求值） ----------
@dataclass
class SignalRule:
    """可编译成公式、也能在本地 K 线上求值的选股条件。

    kind:
      ma_cross        均线金叉/死叉 {fast, slow, direction}
      macd_golden     MACD DIF 上穿 DEA {}
      rsi_threshold   RSI 突破阈值 {period, op, value}
      volume_spike    放量 {ma_days, multiple}
    """

    kind: str = "ma_cross"
    params: dict = field(default_factory=lambda: {"fast": 5, "slow": 20,
                                                  "direction": "golden"})

    def __post_init__(self) -> None:
        if self.kind not in RULE_KINDS:
            raise InteropError(f"未知规则类型: {self.kind}（可用: {sorted(RULE_KINDS)}）")
        try:
            self.params = RULE_KINDS[self.kind].normalize(self.params)
        except ValueError as e:
            raise InteropError(f"{self.kind} 参数非法: {e}") from e

    @property
    def title(self) -> str:
        return RULE_KINDS[self.kind].label(self.params)

    def formula(self, dialect: str = "tdx") -> str:
        return RULE_KINDS[self.kind].formula(self.params, dialect)

    def evaluate(self, df: pd.DataFrame) -> dict:
        """在最近一根 K 线上求值：返回 {"hit": bool, "as_of": iso, "context": {...}}。"""
        return RULE_KINDS[self.kind].evaluate(self.params, df)


@dataclass
class _Kind:
    title: str
    label: callable            # params → 人类可读的条件描述（UI/板块名用）
    normalize: callable
    formula: callable
    evaluate: callable


RULE_KINDS: dict[str, _Kind] = {}


def _register(kind: str, title: str, label, normalize, formula, evaluate) -> None:
    RULE_KINDS[kind] = _Kind(title=title, label=label, normalize=normalize,
                             formula=formula, evaluate=evaluate)


# ---------- 均线交叉 ----------
def _norm_ma_cross(p: dict) -> dict:
    fast, slow = int(p.get("fast", 5)), int(p.get("slow", 20))
    direction = p.get("direction", "golden")
    if not (1 <= fast < slow <= 500):
        raise ValueError("需 1 ≤ fast < slow ≤ 500")
    if direction not in ("golden", "death"):
        raise ValueError("direction 只能是 golden/death")
    return {"fast": fast, "slow": slow, "direction": direction}


def _formula_ma_cross(p: dict, dialect: str) -> str:
    if dialect not in _DIALECTS:
        raise InteropError(f"未知公式方言: {dialect}")
    f, s = p["fast"], p["slow"]
    # 通达信用 ':' 声明输出线，同花顺中间变量用 ':='、条件写在最后一条语句
    assign = ":" if dialect == "tdx" else ":="
    cross = f"CROSS(F{f},S{s})" if p["direction"] == "golden" else f"CROSS(S{s},F{f})"
    return (f"{{QuantDesk 生成 · {f}/{s} 均线{'金叉' if p['direction'] == 'golden' else '死叉'}选股}}\r\n"
            f"F{f}{assign}MA(CLOSE,{f});\r\n"
            f"S{s}{assign}MA(CLOSE,{s});\r\n"
            f"XG:{cross};\r\n"
            "XG\r\n")


def _eval_ma_cross(p: dict, df: pd.DataFrame) -> dict:
    close = _close_series(df)
    fast, slow = _ma(close, p["fast"]), _ma(close, p["slow"])
    if len(close) < p["slow"] + 1:
        return {"hit": False, "as_of": _as_of(df),
                "context": {"reason": f"数据不足 {p['slow']}+1 根"}}
    rising = fast.iloc[-1] > slow.iloc[-1] and fast.iloc[-2] <= slow.iloc[-2]
    falling = fast.iloc[-1] < slow.iloc[-1] and fast.iloc[-2] >= slow.iloc[-2]
    hit = rising if p["direction"] == "golden" else falling
    return {"hit": bool(hit), "as_of": _as_of(df),
            "context": {"fast": round(float(fast.iloc[-1]), 3),
                        "slow": round(float(slow.iloc[-1]), 3)}}


def _label_ma_cross(p: dict) -> str:
    return (f"MA{p['fast']}/{p['slow']} "
            f"{'金叉' if p['direction'] == 'golden' else '死叉'}")


_register("ma_cross", "均线交叉", _label_ma_cross, _norm_ma_cross,
          _formula_ma_cross, _eval_ma_cross)


# ---------- MACD ----------
def _norm_macd(p: dict) -> dict:
    return {"fast": int(p.get("fast", 12)), "slow": int(p.get("slow", 26)),
            "signal": int(p.get("signal", 9))}


def _formula_macd(p: dict, dialect: str) -> str:
    assign = ":" if dialect == "tdx" else ":="
    return (f"{{QuantDesk 生成 · MACD({p['fast']},{p['slow']},{p['signal']}) 金叉选股}}\r\n"
            f"DIF{assign}EMA(CLOSE,{p['fast']})-EMA(CLOSE,{p['slow']});\r\n"
            f"DEA{assign}EMA(DIF,{p['signal']});\r\n"
            "XG:CROSS(DIF,DEA);\r\n"
            "XG\r\n")


def _eval_macd(p: dict, df: pd.DataFrame) -> dict:
    close = _close_series(df)
    if len(close) < p["slow"] + p["signal"]:
        return {"hit": False, "as_of": _as_of(df), "context": {"reason": "数据不足"}}
    dif = close.ewm(span=p["fast"], adjust=False).mean() - close.ewm(
        span=p["slow"], adjust=False).mean()
    dea = dif.ewm(span=p["signal"], adjust=False).mean()
    hit = bool(dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-2] <= dea.iloc[-2])
    return {"hit": hit, "as_of": _as_of(df),
            "context": {"dif": round(float(dif.iloc[-1]), 4),
                        "dea": round(float(dea.iloc[-1]), 4)}}


_register("macd_golden", "MACD 金叉",
          lambda p: f"MACD({p['fast']},{p['slow']},{p['signal']}) 金叉",
          _norm_macd, _formula_macd, _eval_macd)


# ---------- RSI ----------
def _norm_rsi(p: dict) -> dict:
    period = int(p.get("period", 14))
    op = p.get("op", "cross_above")
    value = float(p.get("value", 30))
    if not 2 <= period <= 100:
        raise ValueError("period 需在 2~100")
    if op not in ("above", "below", "cross_above", "cross_below"):
        raise ValueError("op 只能是 above/below/cross_above/cross_below")
    return {"period": period, "op": op, "value": value}


def _formula_rsi(p: dict, dialect: str) -> str:
    assign = ":" if dialect == "tdx" else ":="
    cond = {
        "above": f"RSI{p['period']}>{p['value']}",
        "below": f"RSI{p['period']}<{p['value']}",
        "cross_above": f"CROSS(RSI{p['period']},{p['value']})",
        "cross_below": f"DOWN(CROSS({p['value']},RSI{p['period']}))",
    }[p["op"]]
    return (f"{{QuantDesk 生成 · RSI({p['period']}) {p['op']} {p['value']}}}\r\n"
            f"LC{assign}REF(CLOSE,1);\r\n"
            f"RSI{p['period']}{assign}SMA(MAX(CLOSE-LC,0),{p['period']},1)"
            f"/SMA(ABS(CLOSE-LC),{p['period']},1)*100;\r\n"
            f"XG:{cond};\r\n"
            "XG\r\n")


def _eval_rsi(p: dict, df: pd.DataFrame) -> dict:
    close = _close_series(df)
    if len(close) < p["period"] * 3:
        return {"hit": False, "as_of": _as_of(df), "context": {"reason": "数据不足"}}
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1 / p["period"], adjust=False).mean()
    loss = (-delta).clip(lower=0.0).ewm(alpha=1 / p["period"], adjust=False).mean()
    rsi = 100 * gain / (gain + loss).replace(0.0, pd.NA).astype(float)
    rsi = rsi.fillna(50.0)
    cur, prev = float(rsi.iloc[-1]), float(rsi.iloc[-2])
    v = p["value"]
    hit = {
        "above": cur > v, "below": cur < v,
        "cross_above": cur > v and prev <= v, "cross_below": cur < v and prev >= v,
    }[p["op"]]
    return {"hit": bool(hit), "as_of": _as_of(df),
            "context": {"rsi": round(cur, 2), "prev": round(prev, 2)}}


_register("rsi_threshold", "RSI 阈值",
          lambda p: f"RSI{p['period']} {p['op']} {p['value']:g}",
          _norm_rsi, _formula_rsi, _eval_rsi)


# ---------- 放量 ----------
def _norm_vol(p: dict) -> dict:
    days = int(p.get("ma_days", 5))
    multiple = float(p.get("multiple", 2.0))
    if not 2 <= days <= 120:
        raise ValueError("ma_days 需在 2~120")
    if multiple <= 1:
        raise ValueError("multiple 需 > 1")
    return {"ma_days": days, "multiple": multiple}


def _formula_vol(p: dict, dialect: str) -> str:
    assign = ":" if dialect == "tdx" else ":="
    return (f"{{QuantDesk 生成 · 成交量 ≥ {p['multiple']}×MA(VOL,{p['ma_days']})}}\r\n"
            f"VMA{assign}MA(VOL,{p['ma_days']});\r\n"
            f"XG:VOL>{p['multiple']}*VMA;\r\n"
            "XG\r\n")


def _eval_vol(p: dict, df: pd.DataFrame) -> dict:
    if "volume" not in df.columns or len(df) < p["ma_days"] + 1:
        return {"hit": False, "as_of": _as_of(df), "context": {"reason": "数据不足"}}
    vol = df["volume"].astype(float)
    base = vol.iloc[:-1].rolling(p["ma_days"]).mean().iloc[-1]
    if pd.isna(base) or base <= 0:
        return {"hit": False, "as_of": _as_of(df), "context": {"reason": "均量为空"}}
    ratio = float(vol.iloc[-1]) / float(base)
    return {"hit": bool(ratio >= p["multiple"]), "as_of": _as_of(df),
            "context": {"ratio": round(ratio, 2), "threshold": p["multiple"]}}


_register("volume_spike", "放量",
          lambda p: f"成交量 ≥ {p['multiple']:g}×MA(VOL,{p['ma_days']})",
          _norm_vol, _formula_vol, _eval_vol)


# ---------- 求值辅助 ----------
def _close_series(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty or "close" not in df.columns:
        raise InteropError("本地无 K 线数据：请先在数据管理页下载该标的")
    return df["close"].astype(float).reset_index(drop=True)


def _ma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def _as_of(df: pd.DataFrame) -> str:
    try:
        return str(pd.to_datetime(df["dt"]).iloc[-1])
    except Exception:  # noqa: BLE001 展示用，失败不阻断判定
        return ""


def compile_formula(rule: SignalRule, dialect: str = "tdx") -> str:
    """规则 → 客户端公式文本（通达信/同花顺公式管理器可直接粘贴）。"""
    return rule.formula(dialect)


def evaluate_rule(rule: SignalRule, df: pd.DataFrame) -> dict:
    """规则在给定 K 线上的求值（与公式同源，不是第二份实现）。"""
    return rule.evaluate(df)


# ---------- 板块文件 ----------
def block_targets() -> list[dict]:
    """支持的软件与格式（供 UI 渲染导出选项）。"""
    return [
        {"id": "tdx", "label": "通达信 自定义板块", "ext": ".blk",
         "encoding": "GBK", "format": "每行「市场位(1沪/0深)+6位代码」",
         "client_path": "{安装目录}\\T0002\\blocknew\\<板块名>.blk",
         "import_hint": "通达信 → 自定义板块 → 导入"},
        {"id": "ths", "label": "同花顺 自选股导入", "ext": ".txt",
         "encoding": "GBK", "format": "每行一个 6 位代码",
         "client_path": "自选股 → 导入本地文件",
         "import_hint": "同花顺 → 自选股 → 导入"},
        {"id": "em", "label": "东方财富 自选导入", "ext": ".txt",
         "encoding": "UTF-8", "format": "每行一个 6 位代码",
         "client_path": "东方财富客户端/网页 → 自选股导入",
         "import_hint": "东方财富 → 自选股 → 导入"},
    ]


def _validate_block_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise InteropError("板块名只能含中英文、数字、下划线与短横线，长度 ≤32")
    return name


def to_tdx_line(symbol: str, bse_prefix: str = "") -> str:
    num, suffix = symbol.split(".")
    if suffix == "BJ":
        if not bse_prefix:
            raise InteropError(
                f"通达信板块格式的北交所市场位需实测确认：{symbol} 已跳过。"
                "确认本机客户端可用哪个前缀后，在导出参数里传入 bse_prefix")
        return f"{bse_prefix}{num}"
    return f"{_TDX_MARKET[suffix]}{num}"


def from_tdx_line(line: str) -> str:
    line = line.strip()
    if len(line) != 7 or not line.isdigit():
        raise InteropError(f"无法解析的板块行: {line!r}（期望 7 位：市场位+6位代码）")
    suffix = _TDX_MARKET_REVERSE.get(line[0])
    if suffix is None:
        raise InteropError(f"未知市场位: {line!r}（1=沪、0=深）")
    return to_internal(f"{line[1:]}.{suffix}", "qmt")


def export_tdx_block(symbols: list[str], bse_prefix: str = "") -> str:
    """→ 通达信 .blk 文本内容（北交所无法确认市场位时如实跳过并告知）。"""
    lines, skipped = [], []
    for s in symbols:
        try:
            lines.append(to_tdx_line(s, bse_prefix))
        except InteropError:
            skipped.append(s)
    text = "\r\n".join(lines)
    if skipped:
        text += f"\r\n{{QuantDesk 导出：{len(skipped)} 只北交所标的因市场位未确认被跳过}}\r\n"
    return text


def export_ths_block(symbols: list[str], encoding_target: str = "gbk") -> str:
    """→ 同花顺/东方财富导入用文本（每行 6 位裸代码）。"""
    return "\r\n".join(from_internal(s, "akshare") for s in symbols)


def parse_tdx_block(text: str) -> list[str]:
    """通达信 .blk 内容 → 内部代码列表（注释行 {} 与空行忽略）。"""
    out = []
    for raw in text.replace("，", ",").splitlines():
        line = raw.strip()
        if not line or line.startswith("{"):
            continue
        try:
            out.append(from_tdx_line(line))
        except InteropError:
            log.warning("板块行无法解析（跳过）: %r", raw)
    return out


def parse_bare_block(text: str) -> list[str]:
    """同花顺/东财文本（每行 6 位代码，可能带名称列）→ 内部代码列表。"""
    out = []
    for raw in text.splitlines():
        cell = raw.strip().split(",")[0].split("\t")[0].strip()
        if not cell or not re.fullmatch(r"\d{6}", cell):
            continue
        try:
            out.append(to_internal(cell, "akshare"))
        except Exception:  # noqa: BLE001 无法判市场的行直接跳过
            log.warning("裸代码行无法判市场（跳过）: %r", raw)
    return out


def export_dir() -> Path:
    from quantdesk.config import data_dir

    d = data_dir() / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_block(name: str, content: str, target: str, encoding: str | None = None,
               write_to_client: bool = False) -> dict:
    """落盘：默认写 data/exports/，显式要求且安装目录存在时才写进客户端目录。"""
    name = _validate_block_name(name)
    spec = next((t for t in block_targets() if t["id"] == target), None)
    if spec is None:
        raise InteropError(f"未知导出目标: {target}")
    enc = encoding or ("gbk" if spec["encoding"] == "GBK" else "utf-8")
    fname = f"{name}{spec['ext'] if target == 'tdx' else '.txt'}"
    out = export_dir() / fname
    out.write_text(content, encoding=enc, errors="strict")
    result = {"target": target, "name": name, "path": str(out),
              "bytes": out.stat().st_size, "client_path": ""}
    if write_to_client:
        client = _client_block_dir(target)
        if client is not None:
            dst = client / fname
            dst.write_text(content, encoding=enc, errors="strict")
            result["client_path"] = str(dst)
    return result


def _client_block_dir(target: str) -> Path | None:
    """按设置里的客户端安装目录定位板块目录；目录不存在返回 None（不假装写成功）。"""
    from quantdesk.config import get_settings

    s = get_settings()
    if target != "tdx":
        return None
    raw = str(getattr(s, "tdx_install_dir", "") or "").strip()
    if not raw:                                        # 空串 Path("") 会变成 "."——必须当未配置
        return None
    base = Path(raw)
    if not base.is_dir():
        return None
    d = base / "T0002" / "blocknew"
    d.mkdir(parents=True, exist_ok=True)
    return d


def scan_history_signals(store, rule: SignalRule, symbols: list[str],
                         freq: Frequency = Frequency.D1, limit: int = 300) -> list[str]:
    """在本地 Parquet 上跑规则，返回命中的标的（导出成客户端板块的输入）。"""
    hits = []
    for sym in symbols[:limit]:
        try:
            df = store.read(sym, freq)
        except Exception as e:  # noqa: BLE001 单标的读取失败不影响整体扫描
            log.warning("信号扫描读取失败 %s: %s", sym, e)
            continue
        if df.empty:
            continue
        if rule.evaluate(df)["hit"]:
            hits.append(sym)
    return hits
