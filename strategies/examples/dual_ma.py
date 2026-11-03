"""双均线策略（D08 示例）：fast 上穿 slow 金叉买入、死叉清仓。

示范要点（对照总设计第 7 节）：
- 只依赖 ctx API（subscribe/history/buy/sell/get_position），零适配器依赖；
- T+1 感知：卖出只卖 available（当日买入不可卖）；
- 市价单（price=None）→ 次根 bar 开盘价 ± 滑点成交，无未来函数。
"""

from quantdesk.engine.strategy_base import BaseStrategy


class DualMAStrategy(BaseStrategy):
    name = "dual_ma"
    params = {
        "symbol": "600000.SH",   # 目标标的（回测单标的示例）
        "fast": 5,               # 快线周期
        "slow": 20,              # 慢线周期
        "volume": 500,           # 每次买入股数（整手）
    }

    def on_start(self) -> None:
        self.ctx.subscribe([str(self.params["symbol"])])

    def on_bar(self, bar) -> None:
        symbol = str(self.params["symbol"])
        if bar.symbol != symbol:
            return
        fast_n, slow_n = int(self.params["fast"]), int(self.params["slow"])
        if fast_n >= slow_n:
            self.ctx.log("参数非法：fast 应小于 slow")
            return
        h = self.ctx.history(symbol, bars=slow_n + 2)
        if len(h) < slow_n + 1:
            return                                    # 预热不足
        close = h["close"]
        fast = close.rolling(fast_n).mean()
        slow = close.rolling(slow_n).mean()
        f_now, f_prev = fast.iloc[-1], fast.iloc[-2]
        s_now, s_prev = slow.iloc[-1], slow.iloc[-2]

        pos = self.ctx.get_position(symbol)
        held = pos.volume if pos else 0

        if f_prev <= s_prev and f_now > s_now and held == 0:      # 金叉空仓 → 买入
            o = self.ctx.buy(symbol, int(self.params["volume"]))
            self.ctx.log(f"金叉买入 {o.order_id} fast={f_now:.3f} slow={s_now:.3f}")
        elif f_prev >= s_prev and f_now < s_now and held > 0:     # 死叉持仓 → 清仓
            avail = pos.available                                  # T+1 感知
            if avail > 0:
                o = self.ctx.sell(symbol, avail)
                self.ctx.log(f"死叉卖出 {o.order_id} {avail} 股 fast={f_now:.3f} slow={s_now:.3f}")
