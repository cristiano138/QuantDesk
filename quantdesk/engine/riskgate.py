"""RiskGate——订单唯一咽喉（D16：第②道闸 A 股规则校验全量落地）。

三道闸（宪法第 2 条，顺序强制）：
① 全局模拟盘开关：simulation_only ON → 路由内置模拟撮合（实盘通道缺位不影响）；
② 交易规则校验：T+1 可用 / 整手 / 委托价 ∈ [跌停, 涨停]（含 ST ±5%）/
   单票市值上限 / 日委托数上限 / 资金充足；拦截 → RISK 事件 + message 落库；
③ 实盘二次确认（PENDING_CONFIRM → WS 推送 → 10s 超时作废，D17 完整版）。

fail-closed 口径：
- 实盘路径缺通道/断线 → 直接拒单（不排队不重试）；
- 实盘限价单缺昨收数据 → 拒（无法校验涨跌停区间）；模拟盘缺昨收 → 放行并 LOG 告警
  （模拟盘用于策略迭代，实盘绝不绕过校验）。

两处市场差别（2026-11-06 起的产品口径，见 AGENTS.md 第 10 条）：
- **港股**：内置模拟盘（本机虚拟资金）可以买卖；实盘通道一律拒单——不开港股真实下单。
- **涨跌停区间**：优先用行情给的交易所官方值（腾讯 p47/p48、东财 f51/f52、同花顺 69/70，
  三源已实测逐位一致），官方值取不到才按昨收×幅度自己算。自己算会把「上市首日/次日起
  不设涨跌幅」的正常价判成越界（实测 N森峰 920085 现价 44.88、昨收 17.79 被自家
  ±30% 区间拒掉），那是买不了的假故障。
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import threading
from datetime import datetime, timezone
from uuid import uuid4

import pandas as pd

from quantdesk.core import constants
from quantdesk.core.enums import AdjustType, Board, Frequency, OrderStatus, OrderType, Side
from quantdesk.core.errors import BrokerError, ConnectionLostError
from quantdesk.core.events import Event, EventType, EventBus
from quantdesk.core.models import Order
from quantdesk.core.symbols import board_of, currency_of, is_hk, is_st, normalize
from quantdesk.core.timeutil import now_ms
from quantdesk.engine.matching import FeeCalculator
from quantdesk.store import repo
from quantdesk.store.parquet_store import ParquetStore

log = logging.getLogger(__name__)

__all__ = ["RiskGate"]


def _side_str(v) -> str:
    return v.value if isinstance(v, Side) else str(v)


def _otype_str(v) -> str:
    return v.value if isinstance(v, OrderType) else str(v)


class _SimSettings:
    simulation_only = True


class RiskGate:
    """submit() 是一切下单意图的唯一入口；confirm/reject 为第③道闸回调。

    live_broker 与 live_provider 二选一：
    - live_broker：测试/单通道场景直接注入实例；
    - live_provider：运行时装配（services/channels.ChannelHub），每次 submit 按
      settings.broker_channel 解析当前实盘通道——热切通道不需重启，也不缓存连接。
    风控参数缺省即动态读设置（D19 热参纪律：每单取最新值，不缓存）；
    显式传参则固定（用例与回测需要确定性）。
    """

    def __init__(self, paper_broker=None, live_broker=None,
                 settings=None, db_path=None, event_bus: EventBus | None = None,
                 store: ParquetStore | None = None,
                 max_position_value: float | None = None,
                 max_daily_orders: int | None = None,
                 fee: FeeCalculator | None = None,
                 confirm_timeout_s: float = 10.0,
                 live_provider=None, quote_provider=None) -> None:
        """quote_provider：callable(symbol) -> 行情快照 dict。

        涨跌停与昨收一律先在调用点问行情（宪法第 11 条⑤）；没注入或取不到才回落
        到本地日线。用过期日线算出来的区间会拒掉今天的正常委托——那是数据口径错，
        不是风控严格。
        """
        self.paper = paper_broker
        self.live = live_broker
        self.live_provider = live_provider
        self.quote_provider = quote_provider
        self.settings = settings          # None → 运行时读 config（支持 D19 热切）
        self.db_path = db_path
        self.bus = event_bus
        self.store = store
        self._max_position_value = (None if max_position_value is None
                                    else float(max_position_value))
        self._max_daily_orders = None if max_daily_orders is None else int(max_daily_orders)
        self.fee = fee or FeeCalculator()
        self.confirm_timeout_s = float(confirm_timeout_s)
        self._pending_confirm: dict[str, Order] = {}
        # 本进程内记「这笔是哪个连接实例武装的」：热切换/换连接时旧意图不能投到新实例上。
        # 跨进程的判据不在这儿——那份跟着委托落库（见 store/repo.arm_confirm）。
        self._armed_broker: dict[str, object] = {}
        self._lock = threading.RLock()
        self._prev_close_cache: dict[str, tuple[str, float]] = {}
        self._quote_cache: dict[str, tuple[str, dict]] = {}
        self._expiry_stop = threading.Event()
        if confirm_timeout_s > 0:
            self._expiry_thread = threading.Thread(target=self._expiry_loop,
                                                   name="riskgate-expiry", daemon=True)
            self._expiry_thread.start()

    # ---------- 超时作废（闸③：判据是落库的绝对截止时间，不是本进程计时） ----------
    def _expiry_loop(self) -> None:
        while not self._expiry_stop.wait(0.2):
            try:
                self._expire_overdue()
            except Exception:                      # noqa: BLE001 — 守护循环不中断
                continue

    def stop(self) -> None:
        """停掉超时线程（进程退出/测试）。待确认记录不动——判定本来就不依赖这个线程。"""
        self._expiry_stop.set()
        thread = getattr(self, "_expiry_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def _expire_overdue(self) -> None:
        """按库里的截止时间作废过期确认。

        清理线程只决定「什么时候被用户看到」，不决定「算不算过期」：
        线程没跑到，确认时也照样当场判过期（`confirm()` 走同一条判据）。
        """
        expired = repo.expire_overdue_confirm(now_ms(), db_path=self.db_path)
        if not expired:
            return
        with self._lock:
            for oid in expired:
                o = self._pending_confirm.pop(oid, None)
                self._armed_broker.pop(oid, None)
                if o is None:
                    continue
                o = _replace(o, status=OrderStatus.EXPIRED_CONFIRM,
                             message=f"二次确认超时（{self.confirm_timeout_s:.0f}s）自动作废")
                self._publish(EventType.RISK, {"action": "expired", "order": _to_dict(o)})

    def restore_pending(self) -> int:
        """重启后把待确认单读回来。

        确认窗口是落库的绝对截止时间，所以恢复既不重新计时、也不因为「换了个连接实例」
        就判不了——身份能不能核对，看库里那份判据和当前通道/账户指纹对不对得上。
        结果不明（`result_uncertain`）的单不属于待确认队列，单独广播一次提醒核对。
        """
        rows = repo.list_orders(status=OrderStatus.PENDING_CONFIRM, db_path=self.db_path)
        with self._lock:
            for o in rows:
                self._pending_confirm.setdefault(o.order_id, o)
        uncertain = repo.list_uncertain(db_path=self.db_path)
        if uncertain:
            self._publish(EventType.RISK, {
                "action": "uncertain_review",
                "count": len(uncertain),
                "order_ids": [o.order_id for o in uncertain],
                "hint": "这些委托结果不明：先与券商核对，不自动重发",
            })
        self._expire_overdue()
        return len(rows)

    # ---------- 咽喉 ----------
    def submit(self, intent: Order) -> Order:
        o = intent
        if not getattr(o, "order_id", ""):
            o = _replace(o, order_id=self.new_order_id())

        sim = self._sim_only()
        # 港股：模拟盘（本机虚拟资金）放开；实盘通道仍然不开——闸②的市场规则按港股口径走
        if is_hk(o.symbol) and not sim:
            return self._reject(o, f"{o.symbol} 为港股——港股只做模拟盘，真实下单通道未开通")

        broker = self.paper if sim else self._resolve_live()
        if broker is None:
            return self._reject(o, "模拟盘开关 OFF 且实盘通道未接入——fail-closed 拒单"
                                if not sim else "模拟通道未装配（paper_broker 缺失）")

        # 闸②：A 股规则校验（两条路径都必经）
        violations = self.validate(o, broker=broker, live=not sim)
        if violations:
            return self._reject(o, "；".join(violations))

        if sim:
            # 闸① 路由：模拟撮合（无确认流）
            return broker.place_order(_replace(o, status=OrderStatus.SUBMITTED))

        # 实盘路径：闸③ 二次确认（先落库 → WS 推送 → 超时作废）
        if not broker.health_check():
            return self._reject(o, "实盘通道断线——fail-closed 拒单（不排队不重试）")
        deadline = (now_ms() + int(self.confirm_timeout_s * 1000)
                    if self.confirm_timeout_s > 0 else 0)
        o = _replace(o, status=OrderStatus.PENDING_CONFIRM)
        repo.upsert_order(o, db_path=self.db_path)     # 先落库
        repo.arm_confirm(o.order_id, mode="live", channel=self._channel_id(broker),
                         account=self._account_fingerprint(broker),
                         rules=self._ruleset_version(), deadline_ms=deadline,
                         db_path=self.db_path)         # 判据跟着委托走，重启后仍可核对
        with self._lock:
            self._pending_confirm[o.order_id] = o
            self._armed_broker[o.order_id] = broker
        self._publish(EventType.RISK, {"action": "confirm_request", "order": _to_dict(o)})
        return o

    # ---------- 闸②：规则校验 ----------
    def validate(self, order: Order, broker=None, live: bool | None = None) -> list[str]:
        """返回违规原因列表（空 = 通过）。逐项独立检查，一次报全。

        broker 缺省时按闸①路由自动选取（sim→paper / 实盘→live）；
        live 缺省时由开关推断（实盘路径 fail-closed 口径）。
        """
        if broker is None:
            sim = self._sim_only()
            broker = self.paper if sim else self._resolve_live()
            if live is None:
                live = not sim
        elif live is None:
            live = broker is not self.paper
        v: list[str] = []
        side = _side_str(order.side)
        otype = _otype_str(order.order_type)

        # 板块与代码合法性
        try:
            board = board_of(order.symbol)
        except Exception as e:                         # noqa: BLE001
            return [f"非法证券代码: {order.symbol}（{e}）"]
        if is_hk(order.symbol) and live:                # 绕过 submit 直连 validate 也要拦
            return [f"{order.symbol} 为港股——港股只做模拟盘，真实下单通道未开通"]
        if order.volume <= 0:
            v.append(f"委托数量必须 > 0（当前 {order.volume}）")
            return v                                   # 后续检查无意义

        # 整手（仅买入；卖出允许零股——A 股卖出可非整手）
        if side == Side.BUY.value:
            from quantdesk.core.constants import is_valid_lot

            if not is_valid_lot(order.volume, board):
                v.append(f"{constants.lot_rule_text(board)}（当前 {order.volume} 股）")

        # T+1：卖出量 ≤ available（查最新持仓快照）
        if side == Side.SELL.value and broker is not None:
            pos = None
            for p in broker.get_position(order.symbol):
                pos = p
                break
            avail = pos.available if pos else 0
            if order.volume > avail:
                v.append(f"T+1 可用不足：委托卖出 {order.volume} > 可用 {avail}")

        # 委托价 ∈ [跌停, 涨停]（仅限价单；市价单在撮合端控制）
        if otype == OrderType.LIMIT.value and order.price > 0:
            band = self._limit_band(order.symbol, board)
            if band is None:
                if live:
                    v.append("取不到昨收/涨跌停，无法校验价格区间——fail-closed 拒单"
                             "（请确认行情源可用或先下载日线）")
                else:
                    self._publish(EventType.LOG, {
                        "message": f"{order.symbol} 无昨收数据，跳过涨跌停校验（模拟盘）"})
            else:
                lo, hi, basis = band
                if lo is not None and hi is not None and not (lo - 1e-9 <= order.price <= hi + 1e-9):
                    v.append(f"委托价 {order.price:.2f} 超出涨跌停区间 [{lo:.2f}, {hi:.2f}]"
                             f"（{basis}）——疑似复权价误用")
                elif lo is None and hi is None and not is_hk(order.symbol):
                    # 交易所明写「本日无价格限制」（新股首日/次日起、退市整理期等）
                    self._publish(EventType.LOG, {
                        "message": f"{order.symbol} {basis}，本单不做涨跌停区间校验"})

        # 资金充足（买入：金额 + 费用 ≤ 可用现金）
        if side == Side.BUY.value and broker is not None and order.price > 0:
            acct = broker.get_account()
            notional = order.price * order.volume
            need = notional + self.fee.fee(Side.BUY, notional, order.symbol)
            if need > acct.cash + 1e-6:
                v.append(f"资金不足：需 {need:.2f} > 可用 {acct.cash:.2f}")

        # 单票市值上限（买入：现有持仓市值 + 本次金额 ≤ 上限）
        if side == Side.BUY.value and broker is not None and order.price > 0:
            pos = next(iter(broker.get_position(order.symbol)), None)
            held_val = pos.market_value if pos else 0.0
            new_val = held_val + order.price * order.volume
            cap = self._pos_cap()
            if new_val > cap + 1e-6:
                v.append(f"单票市值超限：{new_val:.2f} > 上限 {cap:.2f}")

        # 日委托数上限
        if broker is not None:
            today_ms = _today_start_ms()
            n = sum(1 for o in broker.get_orders() if o.created_at >= today_ms)
            cap = self._orders_cap()
            if n >= cap:
                v.append(f"日委托数超限：今日已 {n} ≥ 上限 {cap}")

        return v

    def rule_card(self, symbol: str) -> dict:
        """下单前该知道的都在这：板块/币种/最小委托量/价格区间/这单能不能下、为什么。

        和 validate() 共用同一批判据（_limit_band / constants），界面显示的规则和真正
        执行的规则必须是同一份事实——否则用户按界面填的单，会被一条看不见的规则退回。
        """
        sim = self._sim_only()
        try:
            s = normalize(symbol)
            board = board_of(s)
        except Exception as e:                         # noqa: BLE001
            return {"symbol": symbol, "ok": False, "error": f"代码认不出来：{e}",
                    "board": "", "board_cn": "未知", "currency": "", "min_volume": 0}
        row = self._quote_row(s)
        band = self._limit_band(s, board)
        lo, hi, basis = (band if band else (None, None, "取不到昨收，暂无法给出价格区间"))
        last = row.get("last") or 0.0
        board_cn = {Board.MAIN: "主板", Board.GEM: "创业板", Board.STAR: "科创板",
                    Board.BSE: "北交所", Board.HK: "港股"}[board]
        ok, reason = True, ""
        if board is Board.HK and not sim:
            ok, reason = False, "港股只做模拟盘，真实下单通道未开通"
        if not row:
            reason = reason or ("行情取不到，价格区间无法核对；模拟盘仍可下单，"
                                "但委托价请自己确认")
        est_fee = 0.0
        if last:
            notional = last * constants.min_volume(board)
            est_fee = round(self.fee.fee(Side.BUY, notional, s), 2)
        return {
            "symbol": s, "name": row.get("name") or "", "ok": ok, "reason": reason,
            "board": board.value, "board_cn": board_cn,
            "currency": currency_of(s), "min_volume": constants.min_volume(board),
            "lot_step": constants.LOT_SIZE if board in (Board.MAIN, Board.GEM) else 1,
            "lot_rule": constants.lot_rule_text(board),
            "t_plus_1": constants.is_t_plus_1(board),
            "last": last, "prev_close": row.get("prev_close"),
            "limit_up": hi, "limit_down": lo, "band_basis": basis,
            "simulation_only": sim,
            "need_cash": round(last * constants.min_volume(board) + est_fee, 2) if last else 0.0,
            "fee_estimate": est_fee,
        }

    def _quote_row(self, symbol: str) -> dict:
        """在调用点取一次行情快照（名称/昨收/涨跌停都来自这里），当日缓存。

        缓存只在取到时写：取不到就缓存空，等于今天这一天这只票再也没机会走官方口径
        （宪法第 11 条⑥——缺失不许填成看起来像的值，也不许把「还没取到」当成「没有」。）
        """
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        cached = self._quote_cache.get(symbol)
        if cached and cached[0] == today:
            return cached[1]
        if self.quote_provider is None:
            return {}
        try:
            row = self.quote_provider(symbol) or {}
        except Exception as e:                         # noqa: BLE001 取行情失败回落日线口径
            log.info("涨跌停校验取实时快照失败 %s: %s", symbol, e)
            return {}
        if row:
            self._quote_cache[symbol] = (today, dict(row))
        return dict(row)

    def _limit_band(self, symbol: str, board) -> tuple[float | None, float | None, str] | None:
        """(跌停, 涨停, 口径说明)；None = 无从判定；(None, None, …) = 本日不设涨跌幅限制。

        优先级：① 行情给的交易所官方涨跌停价 → ② 昨收 × 板块幅度（昨收同样先取行情、
        再回落本地日线）。ST 判定用简称兜底（注册表只在有名单时才有值）。
        """
        row = self._quote_row(symbol)
        lu = _pos_or_none(row.get("limit_up"))
        ld = _pos_or_none(row.get("limit_down"))
        if lu and ld and lu > ld:
            return ld, lu, "交易所官方涨跌停价"
        if board is not None and not constants.has_price_limit(board):
            return None, None, "该市场不设涨跌幅限制"
        live_prev = _pos_or_none(row.get("prev_close"))
        prev = live_prev or self._prev_close(symbol)
        if not prev:
            return None
        st = is_st(symbol) or _name_says_st(row.get("name"))
        pct = constants.limit_pct(board, st)
        if pct is None:
            return None, None, "该市场不设涨跌幅限制"
        tag = "（ST ±5%）" if st else ""
        basis = f"按昨收 {prev:.2f} × ±{pct:.0%} 推算{tag}"
        lo = constants.limit_down(prev, board, st)
        hi = constants.limit_up(prev, board, st)
        name = str(row.get("name") or "").strip()
        if _name_says_new(name):
            return None, None, "简称带新上市标记（N/C），上市初期不设涨跌幅限制"
        # 数据自证：当日真实成交**大幅**越过推算区间，说明今天交易所就是没设这个限制
        # （新股上市初期常见）。阈值取「幅度的 2 倍」而不是贴着区间沿：除权日昨收口径差一点
        # 就会造成小幅越界，那种时候更可能是数据问题，不能顺手把校验整条放开。
        # 只在昨收来自实时快照时才这样判——用过期日线判「越界」是自己骗自己。
        if live_prev:
            far_hi = prev * (1 + 2 * pct)
            far_lo = prev * (1 - 2 * pct)
            day_hi = _pos_or_none(row.get("high")) or _pos_or_none(row.get("last"))
            day_lo = _pos_or_none(row.get("low")) or _pos_or_none(row.get("last"))
            if (day_hi and day_hi > far_hi + constants.PRICE_TICK / 2) or \
               (day_lo and day_lo < far_lo - constants.PRICE_TICK / 2):
                return None, None, (f"当日最高/最低大幅越过推算区间 [{lo:.2f}, {hi:.2f}]，"
                                    "判定本日不设涨跌幅限制")
            return lo, hi, basis
        # 昨收只从本地日线补到：区间可靠性下降，越界太多就当成不可用（不硬拒当天的正常价）
        last = _pos_or_none(row.get("last"))
        if last and not (lo * 0.8 - 0.01 <= last <= hi * 1.2 + 0.01):
            return None
        return lo, hi, basis + "（昨收取自本地日线，行情快照未给）"

    def _prev_close(self, symbol: str) -> float | None:
        """昨收（原始价口径，不复权）；日线表取 < 今日的最后一根。带当日缓存。

        这里只是**回落**：实时快照给不到（源未就绪/断网）才会走到这。
        """
        if self.store is None:
            return None
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        cached = self._prev_close_cache.get(symbol)
        if cached and cached[0] == today:
            return cached[1]
        try:
            df = self.store.read(symbol, Frequency.D1, adjust=AdjustType.NONE)
        except Exception:                              # noqa: BLE001
            return None
        if df.empty:
            return None
        df = df[df["dt"] < pd.Timestamp(today)]
        if df.empty:
            return None
        px = float(df.iloc[-1]["close"])
        self._prev_close_cache[symbol] = (today, px)
        return px

    # ---------- 闸③：确认回调 ----------
    def confirm(self, order_id: str) -> Order:
        """确认 = 按落库那份判据重新走一遍闸，通过才投真钱。

        顺序有讲究：先判「这张确认票还作不作数」（截止时间、模式、通道、账户、规则版本），
        再按最新资金/可卖量/价格/仓位过闸②，最后用一次原子状态转换（pending_confirm →
        submitted）占住这笔，才调券商。所以：清理线程停不停不影响过期判定；重复确认与
        并发确认只可能有一次投递；进程在券商返回前死掉，重启看到的是 submitted 而不是待确认。
        """
        with self._lock:
            o = self._pending_confirm.pop(order_id, None)
            armed_broker = self._armed_broker.pop(order_id, None)
        if o is None:
            raise ValueError(f"订单 {order_id} 不在待确认队列")

        ctx = repo.confirm_context(order_id, db_path=self.db_path)
        if ctx is None:
            return self._expire(o, "这条待确认记录没有确认判据（升级前的老记录）——"
                                   "身份无法核对，已作废，请重新提交")
        if ctx["deadline"] and now_ms() > ctx["deadline"]:
            return self._expire(o, f"二次确认超时（窗口 {self.confirm_timeout_s:.0f}s）自动作废")
        if self._sim_only():
            return self._reject(o, "确认前已切回模拟盘，原实盘意图作废；请重新提交")

        live = self._resolve_live()
        if live is None:
            return self._fail(o, "确认时实盘通道不可用——fail-closed")
        if not live.health_check():
            return self._fail(o, "确认时通道断线——fail-closed")
        if ctx["mode"] != "live":
            return self._fail(o, f"确认判据的模式不是实盘（{ctx['mode']}）——请重新提交")
        channel = str(getattr(self._current_settings(), "broker_channel", ""))
        if armed_broker is not None and armed_broker is not live:
            return self._fail(o, "原实盘连接实例已被更换，身份无法核对——请重新提交并确认")
        if ctx["channel"] != self._channel_id(live) or (channel and ctx["channel"] != channel):
            return self._fail(o, f"实盘通道已变化（确认时为 {ctx['channel'] or '未知'}，"
                                 f"现在 {self._channel_id(live)}）——原确认作废，请重新提交")
        if ctx["account"] != self._account_fingerprint(live):
            return self._fail(o, "资金账户与确认时不是同一个——原确认作废，请重新提交")
        if ctx["rules"] != self._ruleset_version():
            return self._fail(o, "交易规则口径在确认期间变了——原确认的校验结论不再沿用，"
                                 "请重新提交并按新规则复核")

        # 原子占位：抢不到说明这一笔已被确认过/已作废，绝不会投第二遍
        if not repo.claim_pending(order_id, db_path=self.db_path):
            raise ValueError(f"订单 {order_id} 已不在待确认状态（已提交或已作废）")
        try:
            violations = self.validate(o, broker=live, live=True)
            if violations:
                return self._reject(o, "确认时校验未通过：" + "；".join(violations))
            placed = live.place_order(_replace(o, status=OrderStatus.SUBMITTED))
        except ConnectionLostError as e:
            # 断线时这一笔可能已经被券商收下：落「结果不明」，重启先核对不盲重发
            return self._fail(o, f"通道断线，委托结果不确定：{e}（请勿重复提交，先与券商核对）",
                              uncertain=True)
        except BrokerError as e:
            return self._fail(o, f"实盘委托失败：{e}")
        except Exception as e:                              # noqa: BLE001 — 结果一律按不确定处理
            return self._fail(o, f"通道返回异常，委托结果不确定：{e}"
                                 "（请勿重复提交，先与券商核对）", uncertain=True)
        repo.upsert_order(placed, db_path=self.db_path)
        self._publish(EventType.ORDER, placed)
        return placed

    def reject(self, order_id: str) -> Order:
        with self._lock:
            o = self._pending_confirm.pop(order_id, None)
            self._armed_broker.pop(order_id, None)
        if o is None:
            raise ValueError(f"订单 {order_id} 不在待确认队列")
        o = _replace(o, status=OrderStatus.REJECTED, message="用户拒绝（二次确认）")
        repo.upsert_order(o, db_path=self.db_path)
        self._publish(EventType.RISK, {"action": "rejected", "order": _to_dict(o)})
        return o

    # ---------- 辅助 ----------
    def new_order_id(self) -> str:
        return f"OD-{uuid4().hex[:12].upper()}"

    def _current_settings(self):
        s = self.settings
        if s is None:
            from quantdesk.config import get_settings

            s = get_settings()
        return s

    def _sim_only(self) -> bool:
        return bool(getattr(self._current_settings(), "simulation_only", True))

    def _resolve_live(self):
        """实盘通道解析：注入实例优先，其次运行时装配（按 broker_channel 热切）。"""
        if self.live is not None:
            return self.live
        if self.live_provider is None:
            return None
        try:
            return self.live_provider()
        except Exception as e:  # noqa: BLE001 装配失败按「无实盘通道」处理 → 上层 fail-closed 拒单
            log.warning("实盘通道解析失败（fail-closed 拒单）: %s", e)
            return None

    def _pos_cap(self) -> float:
        if self._max_position_value is not None:
            return self._max_position_value
        return float(getattr(self._current_settings(), "max_position_value", 1_000_000.0))

    def _orders_cap(self) -> int:
        if self._max_daily_orders is not None:
            return self._max_daily_orders
        return int(getattr(self._current_settings(), "max_daily_orders", 200))

    def _reject(self, o: Order, message: str) -> Order:
        o = _replace(o, status=OrderStatus.REJECTED, message=message)
        repo.upsert_order(o, db_path=self.db_path)     # 拦截落库，可查原因
        self._publish(EventType.RISK, {"action": "rejected", "order": _to_dict(o)})
        return o

    def _fail(self, o: Order, message: str, uncertain: bool = False) -> Order:
        """确认作废/断线/委托失败：状态与原因都要落库，不能只把单子从队列里摘掉。"""
        o = _replace(o, status=OrderStatus.FAILED, message=message)
        repo.upsert_order(o, db_path=self.db_path)
        if uncertain:
            repo.mark_uncertain(o.order_id, db_path=self.db_path)   # 结果不明：先核对再决定
        self._publish(EventType.RISK, {"action": "rejected", "order": _to_dict(o)})
        return o

    def _expire(self, o: Order, message: str) -> Order:
        o = _replace(o, status=OrderStatus.EXPIRED_CONFIRM, message=message)
        repo.upsert_order(o, db_path=self.db_path)
        self._publish(EventType.RISK, {"action": "expired", "order": _to_dict(o)})
        return o

    @staticmethod
    def _channel_id(broker) -> str:
        return str(getattr(broker, "name", "") or "")

    @staticmethod
    def _account_fingerprint(broker) -> str:
        """资金账户指纹（sha256 截断）：明文账号不入库、不进日志（宪法第 7 条）。

        通道给不出账户标识时是指纹(空串)——同一个值，跨重启仍然相等；它只表示
        「这个通道不上报账户」，不代表已经核对过账户。
        """
        fn = getattr(broker, "account_identity", None)
        raw = ""
        if callable(fn):
            try:
                raw = str(fn() or "")
            except Exception:                          # noqa: BLE001 — 取不到就按未知
                raw = ""
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _ruleset_version(self) -> str:
        """闸② 这一笔真正用到的规则口径：常量表 + 本闸实际生效的费用参数。

        只报常量表会把「设置页改了费率」当成没改——那正是确认票该发现的事。
        """
        fee = "|".join(f"{k}={v}" for k, v in sorted(vars(self.fee).items()))
        return f"{constants.ruleset_version()}+{hashlib.sha256(fee.encode()).hexdigest()[:8]}"

    def _publish(self, etype: EventType, data) -> None:
        if self.bus is not None:
            self.bus.publish(Event(etype, data=data))


def _today_start_ms() -> int:
    d = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp() * 1000)


def _pos_or_none(v) -> float | None:
    """正数才当数：接口对「无涨跌停」给 0 / -1 / 空串，这些都是没有，不是价格。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _name_says_st(name) -> bool:
    """证券简称以 ST / *ST 开头 → ±5%。

    涨跌停校验只看注册表时，没同步名单的日子会把 ST 股按 ±10% 放行（判得过松）；
    简称是行情快照本来就带的字段，用它兜底不需要额外权限。
    """
    s = str(name or "").strip().upper()
    return s.startswith("ST") or s.startswith("*ST")


def _name_says_new(name) -> bool:
    """简称以 N（上市首日）/ C（上市后 2~5 日）开头 = 交易所标明的新上市标记。

    全面注册制后新股上市前 5 个交易日不设涨跌幅，界面上一律按板块幅度算区间会把
    这些票当天的正常价判成越界（实测 N森峰 920085：现价 44.88、昨收 17.79、
    按 ±30% 区间 [12.45, 23.13] 直接被拒——用户看到的就是「这只买不了」）。
    只认「N/C + 汉字」，避免把 TCL科技 这类以字母开头的简称误伤。
    """
    s = str(name or "").strip()
    return len(s) > 1 and s[0] in ("N", "C") and "\u4e00" <= s[1] <= "\u9fff"


def _replace(o: Order, **kw) -> Order:
    return dataclasses.replace(o, **kw)


def _to_dict(o: Order) -> dict:
    return {
        "order_id": o.order_id, "symbol": o.symbol,
        "side": o.side.value if hasattr(o.side, "value") else str(o.side),
        "price": o.price, "volume": o.volume,
        "order_type": o.order_type.value if hasattr(o.order_type, "value") else str(o.order_type),
        "status": o.status.value if hasattr(o.status, "value") else str(o.status),
        "strategy_id": o.strategy_id, "message": o.message,
    }
