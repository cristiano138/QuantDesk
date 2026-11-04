"""xtconstant 假实现：委托方向 / 报价类型 / 委托状态码常量。

数值取自社区常见 xtquant 版本，**属需在线核对项**（docs/integration/qmt.md 清单第 3 条）：
适配器一律 `getattr(xtconstant, "名字", 兜底整数)` 读取，因此真机取值与此处不同时，
改的是核对清单与兜底表，不是适配器逻辑。

这里刻意保留真实 SDK 的两种类拼写（ORDER_PART_FILLD / ORDER_PART_FILLED），
用来验证适配器的「同义常量名」容错。
"""

from __future__ import annotations

# ---------- 委托方向 ----------
STOCK_BUY = 23
STOCK_SELL = 24
CREDIT_BUY = 23
CREDIT_SELL = 24

# ---------- 报价类型 ----------
LATEST_PRICE = 5            # 最新价
FIX_PRICE = 11              # 限价（指定价）
MARKET_PEER_PRICE_FIRST = 42    # 对手方最优价格申报（深市常用市价口径）
MARKET_MINE_PRICE_FIRST = 43    # 本方最优价格申报
MARKET_BEST_5 = 54              # 最优五档即时成交剩余撤销（沪市常用市价口径）
MARKET_BEST_5_TRANSFER = 55     # 最优五档即时成交剩余转限价
MARKET_FAK = 42                 # 立即成交剩余撤销（与对手方最优同值，真机需核对）
MARKET_FOK = 43                 # 全部成交或撤销

# ---------- 委托状态码（xtquant 常见 48~57） ----------
ORDER_UNREPORTED = 48           # 未报
ORDER_WAIT_REPORTING = 49       # 待报
ORDER_REPORTED = 50             # 已报
ORDER_REPORTED_CANCEL = 51      # 已报待撤
ORDER_PARTSUCC_CANCEL = 52      # 部成待撤
ORDER_PART_CANCEL = 53          # 部撤
ORDER_CANCELED = 54             # 已撤
ORDER_PART_FILLD = 55           # 部成（真实 SDK 的历史拼写）
ORDER_PART_FILLED = 55          # 部成（同义拼写）
ORDER_ALL_FILLDED = 56          # 已成（真实 SDK 的历史拼写）
ORDER_ALL_FILLED = 56           # 已成（同义拼写）
ORDER_JUNK = 57                 # 废单
ORDER_CANT_USE = 255            # 不可用

# ---------- 成交类型（部分券商回报带此字段） ----------
TRADE_UNREPORTED = 48
