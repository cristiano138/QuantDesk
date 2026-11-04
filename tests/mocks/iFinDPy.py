"""iFinDPy 假 SDK（同花顺私有 whl 的离线替身，tests/mocks 装载）。

只覆盖 ths_ifind 适配器实际用到的调用面，接口名与参数顺序按公开文档口径写；
真实签名/字段名以在线核对为准（见 docs/integration/ths_ifind.md 的核对清单）——
适配器侧全部走映射常量，核对后改适配器常量 + 这里的数据形状即可，逻辑不动。

脚本化手段（模块级，测试直接改）：
    STATE["logged_in"]            当前登录态（THS_iFinDLogin/Logout 会改它）
    STATE["require_login"]        True 时未登录的数据调用返回「会话失效」errorcode
    STATE["fail_times"]           接下来 N 次数据调用强制返回会话失效（重登路径注入）
    STATE["fail_errorcode"]       上述失效用的 errorcode（默认 -1010）
    STATE["login_errorcode"]      非 0 时登录直接失败
    STATE["access_token_errorcode"]  非 0 时 refresh token 换 access_token 失败
    STATE["history_response"] / "basic_response" / "feed_batch_response"
                                  脚本化响应（None → 用下面的内置默认数据）
    STATE["rt_callback"]          THS_RealTimeQuotes 登记下来的推送回调
    STATE["raise_on_call"]        非 None 时数据调用抛该异常（SDK 侧炸裂场景）
    CALLS / LOGIN_CALLS / TOKEN_CALLS   调用流水（断言参数与次数）

push_realtime(raw) 是测试辅助函数（真 SDK 没有），用于模拟服务端主动推送。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "THS_iFinDLogin", "THS_iFinDLogout", "iFinDGetAccessToken", "THS_HistoryQuotes",
    "THS_BasicData", "THS_FeedBatch", "THS_RealTimeQuotes", "THS_UnsubscribeRealTime",
    "STATE", "CALLS", "LOGIN_CALLS", "TOKEN_CALLS", "reset", "push_realtime",
    "default_history_response",
]

_OK = {"errorcode": 0, "errmsg": ""}

STATE: dict[str, Any] = {}
CALLS: list[tuple[str, tuple]] = []
LOGIN_CALLS: list[tuple[str, str]] = []
TOKEN_CALLS: list[str] = []

_DEFAULTS: dict[str, Any] = {
    "logged_in": False,
    "require_login": True,
    "fail_times": 0,
    "fail_errorcode": -1010,
    "login_errorcode": 0,
    "access_token_errorcode": 0,
    "history_response": None,
    "basic_response": None,
    "feed_batch_response": None,
    "rt_callback": None,
    "raise_on_call": None,
}


def reset(**overrides: Any) -> None:
    """恢复默认脚本并清空调用流水（overrides 可临时改写脚本）。"""
    STATE.update(_DEFAULTS)
    STATE.update(overrides)
    CALLS.clear()
    LOGIN_CALLS.clear()
    TOKEN_CALLS.clear()


reset()


def _record(func: str, *args: Any) -> None:
    CALLS.append((func, args))


def _session_invalid() -> dict:
    return {"errorcode": int(STATE["fail_errorcode"]), "errmsg": "未登录或会话已失效"}


def _data_call(func: str, *args: Any) -> dict | None:
    """数据类调用的公共前置：异常注入 / 脚本失效 / 未登录。返回 None 表示继续走数据。"""
    _record(func, *args)
    if STATE["raise_on_call"] is not None:
        raise STATE["raise_on_call"]
    if int(STATE["fail_times"]) > 0:
        STATE["fail_times"] = int(STATE["fail_times"]) - 1
        return _session_invalid()
    if STATE["require_login"] and not STATE["logged_in"]:
        return _session_invalid()
    return None


# ---------- 登录 ----------
def THS_iFinDLogin(user: str = "", pwd: str = "") -> dict:
    LOGIN_CALLS.append((str(user), str(pwd)))
    _record("THS_iFinDLogin", user, pwd)
    code = int(STATE["login_errorcode"])
    if code != 0:
        return {"errorcode": code, "errmsg": "登录失败（脚本注入）"}
    STATE["logged_in"] = True
    return dict(_OK)


def THS_iFinDLogout() -> dict:
    _record("THS_iFinDLogout")
    STATE["logged_in"] = False
    return dict(_OK)


def iFinDGetAccessToken(refresh_token: str = "") -> dict:
    TOKEN_CALLS.append(str(refresh_token))
    _record("iFinDGetAccessToken", refresh_token)
    code = int(STATE["access_token_errorcode"])
    if code != 0:
        return {"errorcode": code, "errmsg": "refresh token 无效或已过期"}
    return {"errorcode": 0, "errmsg": "",
            "data": {"access_token": "MOCKACCESSTOKEN-0123456789abcdef"}}


# ---------- 历史行情 ----------
def default_history_response(symbol: str = "600000.SH") -> dict:
    """内置默认响应：乱序 3 根日线（故意不升序，逼适配器排序）。"""
    return {
        "errorcode": 0, "errmsg": "",
        "data": [{
            "thscode": symbol,
            "time": ["2024-01-03", "2024-01-02", "2024-01-04"],
            "table": {
                "thstime": ["2024-01-03", "2024-01-02", "2024-01-04"],
                "open": [10.1, 10.0, 10.3],
                "high": [10.4, 10.2, 10.5],
                "low": [9.9, 9.8, 10.1],
                "close": [10.2, 10.1, 10.4],
                "volume": [120000, 100000, 130000],
                "amount": [1210000.0, 1010000.0, 1350000.0],
            },
        }],
    }


def THS_HistoryQuotes(codes: Any, indicators: Any, func_params: Any = None,
                      start_time: Any = None, end_time: Any = None) -> dict:
    resp = _data_call("THS_HistoryQuotes", codes, indicators, func_params,
                      start_time, end_time)
    if resp is not None:
        return resp
    scripted = STATE["history_response"]
    return scripted if scripted is not None else default_history_response(str(codes))


# ---------- 基本数据 / 财务批量 ----------
def THS_BasicData(codes: Any, indicators: Any, func_params: Any = None) -> dict:
    resp = _data_call("THS_BasicData", codes, indicators, func_params)
    if resp is not None:
        return resp
    scripted = STATE["basic_response"]
    if scripted is not None:
        return scripted
    return {"errorcode": 0, "errmsg": "",
            "data": [{"thscode": str(codes), "time": "2024-01-03",
                      "updatetime": "2024-04-30 08:00:00",
                      "table": {"ths_stock_short_name_stock": ["浦发银行"]}}]}


def THS_FeedBatch(codes: Any, indicators: Any, func_params: Any = None) -> dict:
    resp = _data_call("THS_FeedBatch", codes, indicators, func_params)
    if resp is not None:
        return resp
    scripted = STATE["feed_batch_response"]
    if scripted is not None:
        return scripted
    return {"errorcode": 0, "errmsg": "",
            "data": [{"thscode": str(codes), "time": ["2022-12-31", "2023-12-31"],
                      "updatetime": "2024-04-30 08:00:00",
                      "table": {"ths_revenue_stock": [9.0, 10.0],
                                "ths_net_profit_stock": [1.0, None]}}]}


# ---------- 实时（快照式订阅） ----------
def THS_RealTimeQuotes(codes: Any, indicators: Any, callback: Any = None) -> dict:
    _record("THS_RealTimeQuotes", codes, indicators, "callback")
    if callback is not None:
        STATE["rt_callback"] = callback
    if STATE["require_login"] and not STATE["logged_in"]:
        return _session_invalid()
    return dict(_OK)


def THS_UnsubscribeRealTime(codes: Any) -> dict:
    _record("THS_UnsubscribeRealTime", codes)
    if int(STATE["fail_times"]) > 0:
        STATE["fail_times"] = int(STATE["fail_times"]) - 1
        return _session_invalid()
    return dict(_OK)


def push_realtime(raw: dict) -> int:
    """测试辅助：模拟服务端推送一条快照，返回收到该快照的订阅者数量。"""
    cb = STATE["rt_callback"]
    if cb is None:
        return 0
    cb(raw)
    return 1
