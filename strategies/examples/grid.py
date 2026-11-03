"""网格策略（D08 示例）：以基准价画等比网格，跌买涨卖、每格一手。

示范要点：
- 只依赖 ctx API，零适配器依赖；T+1 感知（涨格卖出受 available 限制）；
- 每根 bar 最多一次买入/一次卖出一格（避免同一 bar 连续吃多格）。
- 参数与含义：base 基准价；step_pct 网格间距（基准价的百分比）；
  grid_volume 每格股数；max_grids 最多向下接的格数（仓位上限）。
  适用：震荡市；不适用：单边趋势（单边下跌会满仓套牢，单边上涨会提前清仓）。
"""

from quantdesk.engine.strategy_base import BaseStrategy


class GridStrategy(BaseStrategy):
    name = "grid"
    params = {
        "symbol": "600000.SH",
        "base": 10.0,        # 基准价（网格中枢）
        "step_pct": 0.03,    # 网格间距 3%
        "grid_volume": 500,  # 每格股数
        "max_grids": 5,      # 最多向下接 5 格
    }

    def on_start(self) -> None:
        self.ctx.subscribe([str(self.params["symbol"])])

    def on_bar(self, bar) -> None:
        symbol = str(self.params["symbol"])
        if bar.symbol != symbol:
            return
        base = float(self.params["base"])
        step = base * float(self.params["step_pct"])
        vol_per_grid = int(self.params["grid_volume"])
        max_grids = int(self.params["max_grids"])

        price = bar.close
        pos = self.ctx.get_position(symbol)
        vol = pos.volume if pos else 0

        if price < base:
            # 目标持仓 = 已跌破格数 × 每格量（封顶 max_grids）
            levels = min(int((base - price) / step) + 1, max_grids)
            target = levels * vol_per_grid
            if vol < target:
                self.ctx.buy(symbol, vol_per_grid)            # 一次补一格
        elif price > base and vol > 0:
            levels_up = int((price - base) / step) + 1
            profit_grids = min(levels_up, vol // vol_per_grid)
            if profit_grids >= 1 and pos.available >= vol_per_grid:
                self.ctx.sell(symbol, vol_per_grid)           # 涨一格卖一格（T+1 感知）
