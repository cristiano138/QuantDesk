"""xtdata 假实现（行情侧接口面）。

只实现适配器会调用的那几个函数，行为由 xtquant.SCRIPT / xtquant.DATA 脚本化：

    xtdata.get_stock_list_in_sector("沪深A股")   # → DATA["sector_list"]
    xtdata.download_history_data(...)            # 记录进 DATA["downloaded"]，可注入 TypeError/异常
    xtdata.get_market_data_ex(...)               # → {code: DATA["history"][code]}
    xtdata.subscribe_quote(...)                  # 建订阅句柄（fire_quote 反向触发）
    xtdata.unsubscribe_quote(seq)
    xtdata.get_full_tick([...])

测试触发推送：`xtdata.fire_quote(code, period, payload)`（模拟 SDK 自己的推送线程）。
"""

from __future__ import annotations

import pandas as pd

from xtquant import DATA, SCRIPT, SUBSCRIPTIONS, fail, next_seq, record

__all__ = ["get_stock_list_in_sector", "download_history_data", "get_market_data_ex",
           "subscribe_quote", "unsubscribe_quote", "get_full_tick", "fire_quote"]


def get_stock_list_in_sector(sector_name, *args, **kwargs):
    record("get_stock_list_in_sector", (sector_name,), kwargs)
    fail("get_stock_list_in_sector")
    if SCRIPT.get("sector_raises"):
        raise RuntimeError("miniQMT 未登录，取不到板块列表")
    return list(DATA.get("sector_list") or [])


def download_history_data(stock_code, period="1d", start_time="", end_time="", **kwargs):
    record("download_history_data", (stock_code, period, start_time, end_time), kwargs)
    # 老版本 xtdata 没有 incrementally 参数 → 注入 TypeError 验适配器的容错分支
    if SCRIPT.get("download_rejects_incrementally") and "incrementally" in kwargs:
        raise TypeError("download_history_data() 不支持关键字 incrementally")
    fail("download_history_data")
    if SCRIPT.get("download_raises"):
        raise RuntimeError("本地数据仓库不可用（客户端未就绪）")
    DATA.setdefault("downloaded", []).append(
        {"code": stock_code, "period": period, "start": start_time, "end": end_time,
         "kwargs": dict(kwargs)})
    return None


def get_market_data_ex(fields=None, stock_list=None, period="1d", start_time="",
                       end_time="", dividend_type="none", fill_data=True, **kwargs):
    record("get_market_data_ex", (), {
        "fields": fields, "stock_list": list(stock_list or []), "period": period,
        "start_time": start_time, "end_time": end_time, "dividend_type": dividend_type,
        "fill_data": fill_data, **kwargs})
    fail("get_market_data_ex")
    rejected = set(SCRIPT.get("reject_dividend") or ())
    if dividend_type in rejected:
        raise RuntimeError(f"dividend_type={dividend_type} 不受支持（需本地前复权数据仓库）")
    store = DATA.get("history") or {}
    out = {}
    for code in (stock_list or []):
        df = store.get(code)
        out[code] = df if df is not None else pd.DataFrame()
    return out


def subscribe_quote(stock_code, period="1d", start_time="", end_time="", count=0,
                    callback=None, **kwargs):
    record("subscribe_quote", (), {"stock_code": stock_code, "period": period,
                                   "start_time": start_time, "end_time": end_time,
                                   "count": count, **kwargs})
    fail("subscribe_quote")
    if SCRIPT.get("subscribe_raises"):
        raise RuntimeError("订阅失败：客户端未就绪")
    seq = next_seq()
    SUBSCRIPTIONS[seq] = {"seq": seq, "code": stock_code, "period": period,
                          "callback": callback}
    return seq


def unsubscribe_quote(sub_id, *args, **kwargs):
    ids = sub_id if isinstance(sub_id, (list, tuple)) else [sub_id]
    record("unsubscribe_quote", tuple(ids), kwargs)
    for i in ids:
        SUBSCRIPTIONS.pop(i, None)
    return None


def get_full_tick(code_list, **kwargs):
    record("get_full_tick", (tuple(code_list),), kwargs)
    fail("get_full_tick")
    store = DATA.get("full_tick") or {}
    if isinstance(code_list, str):
        code_list = [code_list]
    return {c: store[c] for c in code_list if c in store}


def fire_quote(code: str, period: str, payload) -> int:
    """触发 SDK 行情推送线程：对所有匹配 (code, period) 的订阅回调 payload。

    返回被触发的回调数（用例据此确认「确实走进了 SDK 线程」）。
    """
    fired = 0
    for entry in list(SUBSCRIPTIONS.values()):
        if entry["code"] == code and entry["period"] == period and entry["callback"]:
            entry["callback"](payload)
            fired += 1
    return fired


def sub_count() -> int:
    return len(SUBSCRIPTIONS)
