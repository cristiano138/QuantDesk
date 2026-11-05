"""easytrader 通道（同花顺「独立下单程序」GUI 自动化）——非官方接口，默认禁用。

宪法第 9 条对本通道的定位：非官方、稳定性低、默认禁用，且**启用开关必须从注册表源头
跳过**（`default_enabled=False` + `enable_flag="enable_easytrader"`），不是前端藏按钮。

工程上的三条硬纪律（决定了本文件的形态）：

1. **串行**：pywinauto 走 Windows UI Automation，靠抢焦点/点控件工作。两个线程同时操作
   下单窗口会互相打断（焦点被夺、弹窗吃掉输入），结果不可知。所以整个通道只有一个
   worker 线程能碰 SDK，其他调用一律排队 + 超时等待。
2. **不静默**：easytrader 没有回调能力（宪法 BrokerAdapter 合同里 `poll()` 就是为它留的），
   委托/成交/资金全靠轮询 `today_entrusts / today_trades / stock_positions / stock_balance`
   与上一次快照 diff。任何失败（含「下单返回空委托号」这种看起来像成功的）必须抛结构化异常。
3. **取证**：GUI 通道最容易的故障是客户端升级导致控件找不到（ElementNotFound /
   「同花顺版本不匹配」）。这类异常发生时必须截图落 `data/logs/easytrader/`，
   否则事后完全无法复盘；截图本身失败绝不能掩盖原异常。

适配器不落库（先落库后广播在 services 层）；不做风控、不做二次确认（RiskGate 的事）；
下单价格必须是原始市场价（宪法第 5 条，复权价会被涨跌停闸天然拦下，这里不做二次判断）。
"""

from __future__ import annotations

import ctypes
import logging
import queue
import re
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from quantdesk.adapters.base.broker import BrokerAdapter
from quantdesk.adapters.base.optional import import_optional
from quantdesk.core.enums import OrderStatus, OrderType, Side
from quantdesk.core.errors import (AdapterError, BrokerError, ConnectionLostError,
                                   SymbolError)
from quantdesk.core.events import Event, EventType, EventBus
from quantdesk.core.models import Account, Order, Position, Trade
from quantdesk.core.secrets import get_secret, mask
from quantdesk.core.symbols import from_internal, to_internal
from quantdesk.core.timeutil import now_ms

log = logging.getLogger(__name__)

__all__ = ["EasytraderBroker", "ENTRUST_STATUS_MAP", "BALANCE_KEY_MAP", "POSITION_KEY_MAP"]

# ---------- 中文口径映射表（置顶：真实客户端措辞需逐条在线核对，见 docs/integration/easytrader.md） ----------

# 委托状态中文串 → OrderStatus。未命中 → SUBMITTED + log.warning：
# 宁可保留「不确定」的非终态，也不把看不懂的串误判成已成/已撤（误判终态=资金事故）。
ENTRUST_STATUS_MAP: dict[str, OrderStatus] = {
    "已报": OrderStatus.SUBMITTED,
    "已提交": OrderStatus.SUBMITTED,
    "正报": OrderStatus.SUBMITTED,
    "待报": OrderStatus.SUBMITTED,
    "待撤": OrderStatus.SUBMITTED,
    "未成": OrderStatus.SUBMITTED,
    "部成": OrderStatus.PART_FILLED,
    "部分成交": OrderStatus.PART_FILLED,
    "已成": OrderStatus.FILLED,
    "全部成交": OrderStatus.FILLED,
    "已成交": OrderStatus.FILLED,
    "已撤": OrderStatus.CANCELLED,
    "已撤销": OrderStatus.CANCELLED,
    "撤单": OrderStatus.CANCELLED,
    "废单": OrderStatus.REJECTED,
    "未报": OrderStatus.FAILED,        # 客户端未把委托送出（断线/盘口关闭）——终态失败
}

# Account 字段 → 客户端可能的中文键（按优先级取第一个非空）
BALANCE_KEY_MAP: dict[str, tuple[str, ...]] = {
    "total_asset": ("总资产", "资金总资产", "账户总资产"),
    "cash": ("可用资金", "可用余额", "资金余额", "可用金额"),
    "market_value": ("股票市值", "参考市值", "证券市值", "市值"),
    "frozen": ("冻结市值", "冻结资金", "冻结金额"),
}

# Position 字段 → 客户端可能的中文键。available 的唯一正确落点是「可用股份」
# （T+1：当日买入不计入），缺该键时按 0 处理——宁可不给卖，也不虚增可卖量。
POSITION_KEY_MAP: dict[str, tuple[str, ...]] = {
    "code": ("证券代码", "代码", "股票代码"),
    "name": ("证券名称", "名称", "股票名称"),
    "volume": ("当前拥股", "拥有量", "持仓数量", "股份余额"),
    "available": ("可用股份", "可卖", "可用数量", "股份可用", "交易能力"),
    "avg_cost": ("成本价", "买入价", "参考成本价", "持仓成本"),
    "market_value": ("参考市值", "市值", "证券市值"),
    "exchange": ("交易所", "交易市场", "市场"),
}

# 委托行 → 字段
ENTRUST_KEY_MAP: dict[str, tuple[str, ...]] = {
    "entrust_no": ("合同编号", "委托编号", "entrust_no"),
    "code": ("证券代码", "代码"),
    "status": ("委托状态", "状态", "合同状态"),
    "price": ("成交价格", "委托价格", "价格"),
    "volume": ("委托数量", "业务数量", "数量"),
    "filled_volume": ("成交数量", "已成交数量"),
    "biz": ("业务名称", "委托类别", "业务类型", "操作"),
    "date": ("日期", "委托日期"),
    "time": ("时间", "委托时间"),
    "remark": ("废单原因", "备注", "说明"),
}

# 成交行 → 字段
TRADE_KEY_MAP: dict[str, tuple[str, ...]] = {
    "trade_no": ("成交编号", "合同编号", "委托编号"),
    "entrust_no": ("合同编号", "委托编号"),
    "code": ("证券代码", "代码"),
    "price": ("成交价格", "成交价", "价格"),
    "volume": ("成交数量", "成交量", "数量"),
    "amount": ("成交金额", "金额"),
    "biz": ("业务名称", "委托类别", "操作"),
    "date": ("日期", "成交日期"),
    "time": ("时间", "成交时间"),
}

# easytrader / pywinauto 的控件类异常名（不 import pywinauto，按类名与文案判定更稳）
_CONTROL_ERROR_TYPES = frozenset({
    "ElementNotFoundError", "NoSuchElementError", "NotFoundError", "SpecifiersError",
    "WindowNotFoundError", "InvalidWindowHandle", "ExternalException", "COMError",
    "NotImplementedError", "TextAlreadyExists", "AssertionError",
})
_CONTROL_ERROR_HINTS = ("版本不匹配", "找不到", "控件", "窗口", "Element", "not found",
                        "can't find", "Can't find", "xiadan", "窗口不存在")

# SDK 下单窗口句柄属性名（不同 easytrader 版本叫法不同），按顺序鸭子类型尝试
_WINDOW_ATTRS = ("client", "window", "app")


def _is_control_error(exc: BaseException) -> bool:
    """是否「客户端界面变了」这一类故障（决定要不要截图取证）。"""
    if type(exc).__name__ in _CONTROL_ERROR_TYPES:
        return True
    text = str(exc)
    return any(h in text for h in _CONTROL_ERROR_HINTS)


def _pick(row: dict, keys: tuple[str, ...]) -> Any:
    """按优先级取第一个非空键（真实客户端不同版本用词不一致）。"""
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip() != "":
            return v
    return None


def _to_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[,\s%¥￥]", "", str(v))
    if not s or s in ("-", "--"):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _to_int(v: Any, default: int = 0) -> int:
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return int(v)
    s = re.sub(r"[,\s]", "", str(v))
    try:
        return int(float(s))
    except ValueError:
        return default


def _parse_ts(date_raw: Any, time_raw: Any) -> int | None:
    """「日期」+「时间」两个中文列 → UTC 毫秒（宪法：时间戳统一毫秒）。"""
    d = re.sub(r"\D", "", str(date_raw or ""))
    t = re.sub(r"\D", "", str(time_raw or ""))
    if len(d) != 8:
        return None
    if len(t) == 4:                       # "0930" → "093000"
        t = t + "00"
    if len(t) > 6:                        # 毫秒/微秒尾巴
        t = t[:6]
    if len(t) not in (0, 6):
        return None
    try:
        dt = datetime.strptime(f"{d}{t}" if t else d,
                               "%Y%m%d%H%M%S" if t else "%Y%m%d")
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def _side_from_biz(biz: Any) -> Side | str:
    """「业务名称」（如「证券买入」「限价卖出」）→ Side。认不出的原样返回并告警。"""
    text = str(biz or "")
    if "买" in text:
        return Side.BUY
    if "卖" in text:
        return Side.SELL
    log.warning("easytrader 无法识别业务名称 %r（按原样上报，不猜方向）", text)
    return text


def _symbol_from_row(code_raw: Any, exchange_raw: Any = "") -> str:
    """6 位裸代码（+「交易所」列）→ 内部规范代码。"""
    m = re.search(r"\d{6}", str(code_raw or ""))
    if not m:
        log.warning("easytrader 行数据无法解析证券代码: %r", code_raw)
        return str(code_raw or "")
    code6 = m.group(0)
    ex = str(exchange_raw or "")
    if "沪" in ex or "上海" in ex:
        return f"{code6}.SH"
    if "深" in ex or "深圳" in ex:
        return f"{code6}.SZ"
    if "京" in ex or "北京" in ex or "北交" in ex:
        return f"{code6}.BJ"
    try:                                  # 「akshare」源在这里只表示「6 位裸代码」这一形态
        return to_internal(code6, "akshare")
    except SymbolError:
        log.warning("easytrader 代码无法判定市场: %r", code_raw)
        return code6


def _rows(data: Any) -> list[dict]:
    """SDK 返回 None / dict / list 三种形态都要能归一（不同版本不一致）。"""
    if data is None:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, (list, tuple)):
        return [r for r in data if isinstance(r, dict)]
    log.warning("easytrader 返回了预期外的数据类型: %s", type(data).__name__)
    return []


def _capture_fullscreen_bmp(path: Path) -> bool:
    """GDI 全屏截图（纯 stdlib）。

    为什么需要：控件找不到时往往连下单窗口句柄都拿不到，`capture_as_image()` 这条路
    直接断，此时全屏是唯一取证手段。只依赖 user32/gdi32，非 Windows 返回 False。
    """
    if not (hasattr(ctypes, "windll") and hasattr(ctypes.windll, "gdi32")):
        return False

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32)]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    SRCCOPY = 0x00CC0020
    x = user32.GetSystemMetrics(76)                   # SM_XVIRTUALSCREEN（多显示器全覆盖）
    y = user32.GetSystemMetrics(77)
    w = user32.GetSystemMetrics(78)                   # SM_CXVIRTUALSCREEN
    h = user32.GetSystemMetrics(79)
    if w <= 0 or h <= 0:
        return False
    hdc = mem = bmp = None
    try:
        hdc = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        old = gdi32.SelectObject(mem, bmp)
        if not gdi32.BitBlt(mem, 0, 0, w, h, hdc, x, y, SRCCOPY):
            return False
        gdi32.SelectObject(mem, old)
        row = (w * 3 + 3) & ~3                        # 24bpp 行按 4 字节对齐
        bi = _BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = h                     # 正值 = 自底向上，与 BMP 存储序一致
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 24
        bi.bmiHeader.biCompression = 0                # BI_RGB
        bi.bmiHeader.biSizeImage = row * h
        buf = ctypes.create_string_buffer(row * h)
        if not gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0):
            return False
        import struct as _struct
        payload = buf.raw
        header = b"BM" + _struct.pack("<IHHI", 14 + 40 + len(payload), 0, 0, 14 + 40)
        dib = _struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(payload), 0, 0, 0, 0)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(header + dib + payload)
        return True
    except Exception:  # noqa: BLE001 — 截图是尽力而为，异常由调用方吞掉
        return False
    finally:
        if mem:
            gdi32.DeleteDC(mem)
        if bmp:
            gdi32.DeleteObject(bmp)
        if hdc:
            user32.ReleaseDC(None, hdc)


@dataclass
class _GuiCall:
    """一次投递给 GUI worker 的调用（结果用 Event 回传给等待中的调用方）。"""

    fn: Callable[[], Any]
    done: threading.Event = field(default_factory=threading.Event)
    value: Any = None
    error: BaseException | None = None


class _GuiSerialWorker:
    """单线程执行器：全通道只有一个线程碰 easytrader/pywinauto。

    超时后调用方拿到异常继续走 fail-closed，但 worker 可能仍卡在那次 GUI 调用里——
    这是有意为之：GUI 操作不能强杀（点了半截的鼠标事件比超时更危险），后续调用排队等待。
    """

    def __init__(self, timeout_s: float = 30.0, name: str = "easytrader-gui") -> None:
        self._queue: queue.Queue[_GuiCall | None] = queue.Queue()
        self._name = name
        self._timeout_s = float(timeout_s)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def thread_ident(self) -> int | None:
        return self._thread.ident if self._thread is not None else None

    def _loop(self) -> None:
        while True:
            call = self._queue.get()
            if call is None:                          # 停机哨兵
                break
            try:
                call.value = call.fn()
            except BaseException as e:  # noqa: BLE001 — 原异常交回调用线程再归类抛出
                call.error = e
            finally:
                call.done.set()

    def _ensure_started(self) -> None:
        with self._lock:
            t = self._thread
            if t is not None and t.is_alive():
                return                    # 唯一消费者还活着：绝不另起线程（串行是硬约束）
            self._thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
            self._thread.start()

    def call(self, fn: Callable[[], Any], timeout: float | None = None) -> Any:
        self._ensure_started()
        wait_s = float(timeout) if timeout else self._timeout_s
        call = _GuiCall(fn=fn)
        self._queue.put(call)
        if not call.done.wait(wait_s):
            raise ConnectionLostError(
                f"easytrader GUI 调用超时（>{wait_s:.1f}s）：同花顺下单窗口可能被弹窗或"
                f"前台程序卡住。请人工检查客户端窗口，期间不要操作鼠标键盘。")
        if call.error is not None:
            raise call.error
        return call.value

    def stop(self, join_s: float = 2.0) -> None:
        """停机：投哨兵并等线程退出。

        卡在一次界面调用里时线程不会立刻退出——不强行杀（点了半截的鼠标事件比留着线程更危险），
        重新 connect() 会沿用同一个还活着的线程，串行约束不被破坏。
        """
        with self._lock:
            t = self._thread
        if t is None or not t.is_alive():
            return
        self._queue.put(None)
        t.join(join_s)
        if t.is_alive():
            log.warning("easytrader GUI 线程未在 %.1fs 内退出（正卡在一次界面调用里）", join_s)


@dataclass
class _Binding:
    """本地 order_id ↔ 客户端合同编号 的绑定（只在实例内存里，见类文档）。"""

    entrust_no: str
    strategy_id: str
    symbol: str
    side: str
    volume: int


@dataclass
class _Snapshot:
    """一次轮询取回的四样东西（归一化之后）。"""

    account: Account | None = None
    positions: list[Position] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)


class EasytraderBroker(BrokerAdapter):
    """easytrader（同花顺独立下单程序）通道：GUI 自动化 + 轮询回报。"""

    name = "easytrader"
    label = "easytrader（同花顺下单程序自动操作）"
    prereq = ("本机安装并登录同花顺「独立下单程序」、锁定下单程序版本（升级后可能失效），"
              "启用需在设置页输入确认文本；非官方软件，稳定性低，操作期间不要使用鼠标键盘")
    official = False
    default_enabled = False
    enable_flag = "enable_easytrader"
    requires = ("easytrader",)
    is_paper = False

    def __init__(self, event_bus: EventBus | None = None, ths_path: str = "",
                 client: str = "ths", call_timeout_s: float = 30.0) -> None:
        self.bus = event_bus
        self._ths_path = str(ths_path or "")
        self._client = str(client or "ths")
        self._user: Any = None
        self._connected = False
        self._worker = _GuiSerialWorker(timeout_s=call_timeout_s)
        # 本地 order_id ↔ 合同编号：只活在实例内存（进程重启即丢）。
        # 为什么不做持久化：easytrader 没有推送能力，回报全靠轮询 today_entrusts 比对，
        # 映射丢失后同一笔委托只能以「ths-<合同编号>」形态归一——connect() 时会打一条
        # 对账告警说明有多少笔当日委托失去本地归属，需人工核对（不做静默合并）。
        self._bindings: dict[str, _Binding] = {}
        self._no_to_local: dict[str, tuple[str, str]] = {}
        self._raw_entrust_by_no: dict[str, dict] = {}   # 撤单要把整行原始委托传回去的版本用
        self._unconfirmed: set[str] = set()          # 提交超时、结果未知的 order_id
        self._last_orders: dict[str, Order] = {}
        self._last_trade_keys: set[str] = set()
        self._last_positions: dict[str, tuple] = {}
        self._last_account: tuple | None = None
        self._baselined = False
        self._lock = threading.RLock()

    # ---------- 生命周期 ----------
    def connect(self, **credentials: str) -> None:
        """启动 GUI 会话：easytrader.use(client).connect(xiadan.exe)。

        credentials: `ths_path`（xiadan.exe 全路径）、`client`（默认 "ths"）。
        凭据解析顺序：入参 → 本地保险库 `easytrader.ths_path` → 同花顺安装目录下探测。
        """
        client = str(credentials.get("client") or self._client)
        ths_path = (str(credentials.get("ths_path") or "") or self._ths_path
                    or get_secret("easytrader.ths_path") or self._guess_xiadan_path())
        mod = import_optional(
            "easytrader", channel="easytrader（同花顺 GUI 自动化）",
            install_hint="pip install easytrader（会带上 pywinauto；非官方通道，"
                         "需先在设置页确认风险并置 enable_easytrader=True）")
        self._client = client
        self._ths_path = ths_path
        try:
            user = mod.use(client)
        except Exception as e:  # noqa: BLE001
            raise ConnectionLostError(
                f"easytrader 无法初始化客户端类型 {client!r}：{e.__class__.__name__}: {e}。"
                f"请确认客户端版本与 easytrader 支持范围（ths=同花顺独立下单程序）。") from e

        def _do_connect() -> None:
            if not ths_path:
                raise ConnectionLostError(
                    "easytrader 未提供 xiadan.exe 路径：connect(ths_path=r'C:\\...\\xiadan.exe')，"
                    "或写入本地凭据 easytrader.ths_path（DPAPI 加密存储）。")
            user.connect(ths_path)

        self._user = user
        self._connected = False
        self._baselined = False
        balance_row: Any = None
        try:
            # 走 _gui：控件类异常在那里统一截图 + 归一为 AdapterError
            self._gui(_do_connect, what="连接下单窗口")
            balance_row = self._gui(self._fetch_balance_only, what="连接探活")  # 取到余额才算连上
        except ConnectionLostError as e:
            self._connected = False
            raise ConnectionLostError(
                f"easytrader 连接同花顺下单程序失败：{self._redact(e)}。排查：①客户端是否已登录"
                f"并开着「独立下单程序」；②路径是否正确（{mask(ths_path) or '未配置'}）；"
                f"③同花顺版本是否匹配（升级即失效，需锁版本）。") from e
        except AdapterError as e:
            self._connected = False
            raise ConnectionLostError(
                f"easytrader 连接同花顺下单程序失败：{self._redact(e)}。排查：①客户端是否已登录；"
                f"②路径是否正确（{mask(ths_path) or '未配置'}）；③版本是否匹配（需锁版本）。"
            ) from e
        except Exception as e:  # noqa: BLE001
            self._connected = False
            raise ConnectionLostError(
                f"easytrader 连接异常（{e.__class__.__name__}）：{self._redact(e)}。"
                f"排查：①客户端未登录 / ②xiadan.exe 路径不对（{mask(ths_path) or '未配置'}）/ "
                f"③同花顺版本升级导致控件不匹配（需锁版本）。") from e

        if not isinstance(balance_row, dict) or not balance_row:
            self._connected = False
            raise ConnectionLostError(
                "easytrader 连上了窗口但取不到资金数据（stock_balance 返回空）："
                "通常是客户端未登录成功或下单窗口处于非交易账户状态。")

        self._connected = True
        # 宪法合同：重连成功后必须刷新对账——这里表现为「重建基线快照」，
        # 于是第一次 poll() 不会把当日历史委托当成新变化刷屏。
        self._reset_baseline()
        log.info("easytrader 已连接：client=%s 程序=%s", self._client, mask(self._ths_path) or "未配置")

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            self._user = None
        try:
            self._worker.stop()
        except Exception as e:  # noqa: BLE001 — 断开路径不再抛（探口语义交给 health_check）
            log.warning("easytrader GUI 线程停止异常: %s", e.__class__.__name__)
        log.info("easytrader 已断开")

    def health_check(self) -> bool:
        """能否定位窗口并取到余额；任何异常一律 False（可探测语义，不外抛）。"""
        if not self._connected or self._user is None:
            return False
        try:
            return bool(self._gui(self._fetch_balance_only, what="探活"))
        except Exception as e:  # noqa: BLE001
            log.warning("easytrader 探活失败（health_check=False）: %s: %s",
                        e.__class__.__name__, self._redact(e)[:120])
            return False

    # ---------- 交易 ----------
    def place_order(self, order: Order) -> Order:
        """限价单 → order_buy/order_sell；返回补齐 broker_order_id 的快照。

        未受理（返回空委托号）与超时（结果未知）都抛异常，禁止静默失败。
        """
        self._require_connected("下单")
        if order.order_type in (OrderType.MARKET, "market"):
            raise BrokerError(
                "easytrader（同花顺 GUI 自动化）只支持限价单：市价单需先在线核对客户端"
                "下单面板的市价控件形态（见 docs/integration/easytrader.md），当前版本 fail-closed 拒单")
        side = order.side.value if isinstance(order.side, Side) else str(order.side)
        symbol6 = self._to_client_symbol(order.symbol)
        price = float(order.price)
        amount = int(order.volume)

        def _submit() -> Any:
            u = self._session()
            fn = u.order_buy if side == Side.BUY.value else u.order_sell
            return fn(symbol=symbol6, price=price, amount=amount)

        try:
            raw = self._gui(_submit, what=f"{side} 下单 {order.symbol}")
        except ConnectionLostError:
            # 点了半截的界面：委托可能已经进客户端，状态未知 → 交对账，不谎报失败
            self._unconfirmed.add(order.order_id)
            log.warning("easytrader 下单超时，委托结果未知，需人工对账: order_id=%s", order.order_id)
            raise
        entrust_no = str(raw or "").strip()
        if not entrust_no:
            raise BrokerError(
                f"easytrader 下单未被客户端受理（order_buy/order_sell 返回空委托号）："
                f"symbol={order.symbol} side={side} volume={amount} price={price}。"
                f"常见原因：可用资金/可卖不足、非交易时间、客户端弹窗吃掉了输入。")
        with self._lock:
            self._bindings[order.order_id] = _Binding(
                entrust_no=entrust_no, strategy_id=order.strategy_id,
                symbol=order.symbol, side=side, volume=amount)
            self._no_to_local[entrust_no] = (order.order_id, order.strategy_id)
            self._unconfirmed.discard(order.order_id)
        return replace(order, broker_order_id=entrust_no, status=OrderStatus.SUBMITTED,
                       message=(order.message or "") + f"easytrader 已受理（合同编号 {entrust_no}）；",
                       updated_at=now_ms())

    def cancel_order(self, order_id: str) -> bool:
        """发起撤单（返回是否受理；最终状态以轮询回报为准）。"""
        self._require_connected("撤单")
        with self._lock:
            binding = self._bindings.get(order_id)
            no = binding.entrust_no if binding else self._entrust_no_of_foreign(order_id)
        if not no:
            raise BrokerError(
                f"easytrader 撤单失败：本地无 order_id={order_id!r} 到合同编号的映射"
                f"（进程重启会丢映射，或该单不是本通道下的）。请刷新当日委托后重试。")

        def _cancel() -> Any:
            # 不同 easytrader 版本签名不同：generic 版读 entrust_no，ths 版读「合同编号」，
            # 少数版本要整行原始委托数据——三样都塞进同一个 dict 里传出去。
            payload: dict[str, Any] = dict(self._raw_entrust_by_no.get(no) or {})
            payload["entrust_no"] = no
            payload["合同编号"] = no
            return self._session().cancel_order(payload)

        res = self._gui(_cancel, what=f"撤单 {order_id}")
        if res is None:                 # easytrader 多数实现不返回值：没抛异常即视为受理
            return True
        return bool(res)

    # ---------- 查询 ----------
    def get_position(self, symbol: str | None = None) -> list[Position]:
        self._require_connected("查持仓")
        rows = self._gui(lambda: _rows(self._session().stock_positions()), what="持仓")
        out = [self._to_position(r) for r in rows]
        if symbol is None:
            return out
        return [p for p in out if p.symbol == symbol]

    def get_account(self) -> Account:
        self._require_connected("查资金")
        row = self._gui(lambda: self._session().stock_balance(), what="资金")
        acct = self._to_account(row)
        if acct is None:
            raise BrokerError("easytrader 资金查询返回空数据（stock_balance 无内容）")
        return acct

    def get_orders(self, status: OrderStatus | None = None) -> list[Order]:
        self._require_connected("查委托")
        orders = self._orders_now()
        if status is None:
            return orders
        want = status.value if isinstance(status, OrderStatus) else str(status)
        return [o for o in orders if (o.status.value if isinstance(o.status, OrderStatus)
                                      else str(o.status)) == want]

    def get_trades(self, start: datetime | None = None) -> list[Trade]:
        self._require_connected("查成交")
        trades = self._trades_now()
        if start is None:
            return trades
        start_ms = int(start.timestamp() * 1000)
        return [t for t in trades if t.traded_at >= start_ms]

    # ---------- 轮询（本通道的核心义务：无回调能力） ----------
    def poll(self) -> None:
        """拉四样 → 与上次快照 diff → 只把变化经 EventBus 发布。

        取数在 GUI worker 里做（一次任务，保证一次快照内四样数据同属一刻）；
        diff 与发布在调用线程做（不占用碰界面的时间）。
        适配器不落库：先落库后广播是 services 层的职责。
        """
        self._require_connected("轮询")
        snap = self._gui(self._fetch_snapshot, what="轮询快照")
        if not self._baselined:
            # 连接时基线没建起来（那一刻界面取数失败）：本次只建基线，
            # 不把当日历史委托/成交当成「刚发生的变化」刷屏广播。
            self._absorb(snap, publish=False)
            self._baselined = True
            log.warning("easytrader 首次轮询用于重建基线（连接时未取到快照），本次不发布事件")
            return
        for etype, data in self._diff(snap):
            self._publish(etype, data)

    def snapshot_for_reconcile(self) -> tuple[list[Order], list[Trade], list[Position],
                                              Account | None]:
        """对账成套快照（services/reconcile.py 优先走这个接口）。

        为什么单独实现：GUI 通道每次查询都要抢一次界面，四次独立调用之间客户端状态还可能变；
        放进一个 worker 任务里取全四样，既少占焦点又保证同一时刻口径一致。
        """
        self._require_connected("对账快照")
        snap = self._gui(self._fetch_snapshot, what="对账快照")
        return snap.orders, snap.trades, snap.positions, snap.account

    # ---------- 内部：SDK 调用统一入口 ----------
    def _gui(self, fn: Callable[[], Any], *, what: str,
             timeout: float | None = None) -> Any:
        try:
            return self._worker.call(fn, timeout=timeout)
        except AdapterError:
            raise                                    # 已是结构化异常，保持类型不降级
        except Exception as e:  # noqa: BLE001
            if _is_control_error(e):
                shot = self._screenshot(what)
                raise AdapterError(
                    f"easytrader 界面控件定位失败（{what}）：{e.__class__.__name__}: "
                    f"{self._redact(e)}。"
                    f"同花顺客户端版本可能已升级，需锁版本（或改 use('generic') 口径），"
                    f"详见 docs/integration/easytrader.md"
                    f"{'；取证截图 ' + shot if shot else '；截图失败'}") from e
            raise BrokerError(
                f"easytrader 调用失败（{what}）：{e.__class__.__name__}: "
                f"{self._redact(e)}") from e

    def _redact(self, value: Any) -> str:
        """异常/日志文案统一脱敏：资金路径（含 SDK 自己回显的路径）只留前 4 位。"""
        out = str(value or "")
        p = self._ths_path
        if p and len(p) > 4 and p in out:
            out = out.replace(p, mask(p))
        return out

    def _require_connected(self, what: str) -> None:
        if not self._connected or self._user is None:
            raise ConnectionLostError(
                f"easytrader 未连接，无法{what}：先 connect(ths_path=...) "
                f"（非官方通道，需在设置页启用 enable_easytrader）")

    def _fetch_balance_only(self) -> Any:
        return self._session().stock_balance()

    def _session(self) -> Any:
        """取当前 SDK 会话对象。

        为什么要显式取：调用闭包在 worker 线程里执行，而 `disconnect()` 会把 `self._user`
        置 None——闭包里现取现用会撞上空对象，这里统一成结构化断线异常。
        """
        u = self._user
        if u is None:
            raise ConnectionLostError("easytrader 会话已释放（已断开），需重新 connect()")
        return u

    def _fetch_snapshot(self) -> _Snapshot:
        u = self._session()
        return _Snapshot(
            account=self._to_account(u.stock_balance()),
            positions=[self._to_position(r) for r in _rows(u.stock_positions())],
            orders=self._orders_from(_rows(u.today_entrusts())),
            trades=self._trades_from(_rows(u.today_trades())),
        )

    def _orders_now(self) -> list[Order]:
        return self._orders_from(_rows(self._gui(lambda: self._session().today_entrusts(),
                                                 what="当日委托")))

    def _trades_now(self) -> list[Trade]:
        return self._trades_from(_rows(self._gui(lambda: self._session().today_trades(), what="当日成交")))

    # ---------- 内部：中文口径 → 核心模型 ----------
    def _to_account(self, row: Any) -> Account | None:
        if not isinstance(row, dict) or not row:
            return None
        got = {k: _to_float(_pick(row, keys)) for k, keys in BALANCE_KEY_MAP.items()}
        known = {k for keys in BALANCE_KEY_MAP.values() for k in keys}
        if not known & set(row):
            # 一个已知中文键都没命中：多半是客户端版本换了措辞，必须说出来而不是报 0
            log.warning("easytrader 资金返回未命中任何已知中文键: %s", sorted(row)[:12])
        return Account(total_asset=round(got["total_asset"], 2), cash=round(got["cash"], 2),
                       market_value=round(got["market_value"], 2),
                       frozen=round(got["frozen"], 2), updated_at=now_ms())

    def _to_position(self, row: dict) -> Position:
        code = _pick(row, POSITION_KEY_MAP["code"])
        symbol = _symbol_from_row(code, _pick(row, POSITION_KEY_MAP["exchange"]))
        volume = _to_int(_pick(row, POSITION_KEY_MAP["volume"]))
        avail_raw = _pick(row, POSITION_KEY_MAP["available"])
        if avail_raw is None:
            # 缺「可用股份」列时按 0：宁可显示不可卖，也不虚增 T+1 可用量
            log.warning("easytrader 持仓行缺少可用列（%s），available 按 0 处理", symbol)
        return Position(symbol=symbol, volume=volume, available=_to_int(avail_raw),
                        avg_cost=_to_float(_pick(row, POSITION_KEY_MAP["avg_cost"])),
                        market_value=_to_float(_pick(row, POSITION_KEY_MAP["market_value"])),
                        strategy_id="manual")

    def _orders_from(self, rows: list[dict]) -> list[Order]:
        out = []
        raw_cache: dict[str, dict] = {}
        for idx, row in enumerate(rows):
            no = str(_pick(row, ENTRUST_KEY_MAP["entrust_no"]) or "").strip()
            if no:
                raw_cache[no] = row
            status_text = str(_pick(row, ENTRUST_KEY_MAP["status"]) or "").strip()
            status = ENTRUST_STATUS_MAP.get(status_text)
            if status is None:
                log.warning("easytrader 未识别的委托状态 %r（按 SUBMITTED 处理，不误判终态）",
                            status_text)
                status = OrderStatus.SUBMITTED
            with self._lock:
                bound = self._no_to_local.get(no) if no else None
            if bound:
                order_id, strategy_id = bound
            else:
                order_id = f"ths-{no}" if no else f"ths-unknown-{idx}"
                strategy_id = "manual"
                if not no:
                    log.warning("easytrader 委托行缺少合同编号，按 %s 归一（无法与本地订单关联）",
                                order_id)
            sym = _symbol_from_row(_pick(row, ENTRUST_KEY_MAP["code"]))
            price = _to_float(_pick(row, ENTRUST_KEY_MAP["price"]))
            filled = _to_int(_pick(row, ENTRUST_KEY_MAP["filled_volume"]))
            ts = _parse_ts(_pick(row, ENTRUST_KEY_MAP["date"]), _pick(row, ENTRUST_KEY_MAP["time"]))
            remark = str(_pick(row, ENTRUST_KEY_MAP["remark"]) or "")
            out.append(Order(
                order_id=order_id, symbol=sym, side=_side_from_biz(_pick(row, ENTRUST_KEY_MAP["biz"])),
                price=price, volume=_to_int(_pick(row, ENTRUST_KEY_MAP["volume"])),
                order_type=OrderType.LIMIT, strategy_id=strategy_id, status=status,
                filled_volume=filled,
                avg_filled_price=price if status in (OrderStatus.PART_FILLED, OrderStatus.FILLED) else 0.0,
                message=(f"easytrader 状态={status_text}" + (f"；{remark}" if remark else "")),
                broker_order_id=no or None,
                created_at=ts or now_ms(), updated_at=now_ms(),
            ))
        with self._lock:
            self._raw_entrust_by_no.update(raw_cache)   # 撤单时整行回传（部分版本要求）
        return out

    def _trades_from(self, rows: list[dict]) -> list[Trade]:
        out = []
        for idx, row in enumerate(rows):
            tno = str(_pick(row, TRADE_KEY_MAP["trade_no"]) or "").strip()
            no = str(_pick(row, TRADE_KEY_MAP["entrust_no"]) or "").strip()
            ts = _parse_ts(_pick(row, TRADE_KEY_MAP["date"]), _pick(row, TRADE_KEY_MAP["time"]))
            price = _to_float(_pick(row, TRADE_KEY_MAP["price"]))
            vol = _to_int(_pick(row, TRADE_KEY_MAP["volume"]))
            amount = _to_float(_pick(row, TRADE_KEY_MAP["amount"])) or round(price * vol, 2)
            with self._lock:
                bound = self._no_to_local.get(no) if no else None
            order_id = bound[0] if bound else (f"ths-{no}" if no else f"ths-unknown-{idx}")
            out.append(Trade(
                trade_id=tno or f"{order_id}-{ts or now_ms()}-{idx}",
                order_id=order_id, symbol=_symbol_from_row(_pick(row, TRADE_KEY_MAP["code"])),
                side=_side_from_biz(_pick(row, TRADE_KEY_MAP["biz"])), price=price, volume=vol,
                amount=amount, traded_at=ts or now_ms(), broker_trade_id=tno or None))
        return out

    def _to_client_symbol(self, symbol: str) -> str:
        try:
            return from_internal(symbol, "akshare")     # 同花顺下单界面收 6 位裸代码
        except SymbolError as e:
            raise BrokerError(f"easytrader 无法解析证券代码 {symbol!r}：{e}") from e

    @staticmethod
    def _guess_xiadan_path() -> str:
        """从设置里的同花顺安装目录探测独立下单程序（用户只填了目录时的兜底）。

        找到多个或找不到都返回空串——路径是资金相关配置，宁可不猜，让 connect() 明确报缺。
        """
        from quantdesk.config import get_settings

        base = str(getattr(get_settings(), "ths_install_dir", "") or "").strip()
        if not base:
            return ""
        root = Path(base)
        hits = list(root.rglob("xiadan.exe"))[:2]
        if len(hits) == 1:
            return str(hits[0])
        log.warning("同花顺安装目录里 xiadan.exe 命中 %d 个（%s），需在凭据里显式指定完整路径",
                    len(hits), mask(base))
        return ""

    # ---------- 内部：diff / 广播 / 取证 ----------
    def _reset_baseline(self) -> None:
        try:
            snap = self._gui(self._fetch_snapshot, what="基线快照")
        except AdapterError as e:
            log.warning("easytrader 基线快照取数失败（首次 poll 将重新建基线）: %s",
                        self._redact(e)[:120])
            return
        foreign = [o for o in snap.orders
                   if o.broker_order_id and o.order_id.startswith("ths-")]
        if foreign:
            log.warning("easytrader 本地 order_id↔合同编号 映射为空/已随重启丢失："
                        "%d 笔当日委托按 ths-<合同编号> 归一，需人工对账（本通道无推送，"
                        "映射只存内存）", len(foreign))
        self._absorb(snap, publish=False)
        self._baselined = True

    def _diff(self, snap: _Snapshot) -> list[tuple[EventType, Any]]:
        events = self._absorb(snap, publish=True)
        for o in snap.orders:                     # 超时的单子回来后要留痕
            if o.order_id in self._unconfirmed and o.broker_order_id:
                log.warning("easytrader 提交超时的委托已在客户端找回: order_id=%s 合同=%s 状态=%s",
                            o.order_id, o.broker_order_id, o.status)
                self._unconfirmed.discard(o.order_id)
        return events

    def _absorb(self, snap: _Snapshot, *, publish: bool) -> list[tuple[EventType, Any]]:
        """与上次快照比对；有变化才（在 publish=True 时）产出事件。返回待发布事件。"""
        events: list[tuple[EventType, Any]] = []
        with self._lock:
            new_orders = {o.order_id: o for o in snap.orders}
            for oid, o in new_orders.items():
                prev = self._last_orders.get(oid)
                changed = (prev is None or prev.status != o.status
                           or prev.filled_volume != o.filled_volume
                           or prev.avg_filled_price != o.avg_filled_price)
                if changed and publish:
                    events.append((EventType.ORDER, o))
            self._last_orders = new_orders

            trade_keys = {f"{t.broker_trade_id or ''}|{t.order_id}|{t.traded_at}|{t.volume}"
                          for t in snap.trades}
            for t in snap.trades:
                k = f"{t.broker_trade_id or ''}|{t.order_id}|{t.traded_at}|{t.volume}"
                if k not in self._last_trade_keys and publish:
                    events.append((EventType.TRADE, t))
            self._last_trade_keys = trade_keys

            pos_map = {p.symbol: (p.volume, p.available, p.avg_cost, p.market_value)
                       for p in snap.positions}
            if pos_map != self._last_positions:
                if publish:
                    events.append((EventType.POSITION, snap.positions))
                self._last_positions = pos_map

            acct_key = None if snap.account is None else (
                snap.account.total_asset, snap.account.cash,
                snap.account.market_value, snap.account.frozen)
            if acct_key is not None and acct_key != self._last_account:
                if publish:
                    events.append((EventType.ACCOUNT, snap.account))
                self._last_account = acct_key
        return events

    def _entrust_no_of_foreign(self, order_id: str) -> str | None:
        """本地映射表没命中时，从最近一次委托快照里反查（客户端手工单/重启后的单子）。"""
        with self._lock:
            for oid, o in self._last_orders.items():
                if oid == order_id and o.broker_order_id:
                    return o.broker_order_id
        return None

    def unconfirmed_orders(self) -> list[str]:
        """提交超时、结果未知的本地 order_id（运行时/UI 据此强制对账）。"""
        with self._lock:
            return sorted(self._unconfirmed)

    def _screenshot(self, what: str) -> str:
        """控件异常取证截图，尽力而为：失败只记日志，绝不掩盖原异常（返回描述串）。"""
        try:
            from quantdesk.config import data_dir

            d = Path(data_dir()) / "logs" / "easytrader"
            d.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            tag = re.sub(r"\W+", "", what)[:16] or "gui"
            path = d / f"{stamp}-{tag}-{uuid.uuid4().hex[:6]}"
            if self._capture_window(path.with_suffix(".png")):
                return str(path.with_suffix(".png"))
            if _capture_fullscreen_bmp(path.with_suffix(".bmp")):
                return str(path.with_suffix(".bmp"))
            log.warning("easytrader 截图不可用（无窗口句柄，且本机不支持 GDI 抓屏）")
            return ""
        except Exception as e:  # noqa: BLE001 — 截图失败不得掩盖原异常
            log.warning("easytrader 取证截图失败（原异常照常上抛）: %s: %s",
                        e.__class__.__name__, str(e)[:120])
            return ""

    def _capture_window(self, path: Path) -> bool:
        """鸭子类型走 SDK 已连上的窗口对象（pywinauto 窗口有 capture_as_image）。"""
        user = self._user
        if user is None:
            return False
        for attr in _WINDOW_ATTRS:
            obj = getattr(user, attr, None)
            if obj is None:
                continue
            try:
                img = obj.capture_as_image()
                if img is None:
                    continue
                img.save(str(path))
                return True
            except Exception:  # noqa: BLE001 — 该属性不是窗口对象/抓图失败：换下一个
                continue
        return False

    def _publish(self, etype: EventType, data: Any) -> None:
        if self.bus is not None:
            self.bus.publish(Event(etype, data=data, source=self.name))


ADAPTER = EasytraderBroker
