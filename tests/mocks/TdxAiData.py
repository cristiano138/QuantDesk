"""假通达信官方 AI 数据 SDK（模块名 ``TdxAiData``，最少够用）。

真 SDK 随通达信官方客户端分发、需申请权限，本机/CI 都装不出来；本 mock 只提供
``TdxOfficialTransport`` 按候选名探测到的那一组类与方法，用于验证「官方 SDK 优先」
的传输层选择逻辑，以及官方返回字段走同一套归一化的路径。

**方法名/形参是本项目按公开资料的假设，不是官方 SDK 的事实描述**——真实名称需在线核对，
见 docs/integration/tdx_aidata.md 的在线核对清单。

剧本状态与 pytdx mock 共用（``_script()`` 取同一个 ``SCRIPT`` 单例），这样两个传输层
可以用同一份数据交叉验证归一化结果。
"""

from __future__ import annotations

CONNECT_ERROR: Exception | None = None      # 注入官方 SDK 握手失败（pytdx 不受影响）
INSTANTIATED = 0


def _script():
    """取 pytdx mock 的剧本单例（同一模块对象，避免两份 SCRIPT）。"""
    import pytdx.hq as hq_mod
    return hq_mod.SCRIPT


class TdxAiDataAPI:
    """假官方 API。"""

    def __init__(self) -> None:
        global INSTANTIATED
        INSTANTIATED += 1
        self._connected = False

    def connect(self, install_dir: str = "") -> bool:
        _script().record("official.connect", (install_dir,))
        if CONNECT_ERROR is not None:
            raise CONNECT_ERROR
        self._connected = True
        return True

    def disconnect(self) -> None:
        _script().record("official.disconnect", ())
        self._connected = False

    def get_security_count(self) -> int:
        script = _script()
        script.record("official.get_security_count", ())
        script.enter()
        try:
            if "official.get_security_count" in script.fail_methods:
                raise ConnectionError("mock TdxAiData: 探活失败")
            return int(script.security_count)
        finally:
            script.exit()

    def get_kline(self, market: int, code: str, period: str, start: int, count: int) -> list[dict]:
        script = _script()
        script.record("official.get_kline", (market, code, period, start, count))
        script.enter()
        try:
            # 与 pytdx 的 category 取值对应，方便两个传输层共用同一份 bar 剧本
            category = {"1d": 4, "5m": 0, "1m": 7}.get(period, 4)
            all_bars = script.bars_by_key.get((category, int(market), str(code)), script.bars)
            right = len(all_bars) - int(start)
            return list(all_bars[max(0, right - int(count)):max(0, right)])
        finally:
            script.exit()

    def get_snapshot(self, stock_list: list[tuple[int, str]]) -> list[dict]:
        script = _script()
        script.record("official.get_snapshot", tuple(stock_list))
        script.enter()
        try:
            wanted = {(int(m), str(c)) for m, c in stock_list}
            return [dict(q) for q in (script.quotes or [])
                    if (int(q.get("market", 0)), str(q.get("code", ""))) in wanted]
        finally:
            script.exit()

    def get_transaction(self, market: int, code: str, start: int, count: int) -> list[dict]:
        script = _script()
        script.record("official.get_transaction", (market, code, start, count))
        script.enter()
        try:
            return list(script.transactions)
        finally:
            script.exit()
