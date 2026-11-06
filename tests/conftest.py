"""全局测试隔离：任何用例都不许碰仓库里的真实 `data/` 目录。

为什么必须放在 conftest 而不是各测试文件自带：夹具是"各文件自己写"的时候，
写全了的文件安全，漏掉的那个把整轮污染带进开发库——本轮实测跑一次全量，
真实 `data/quantdesk.db` 多出 300+ 行 run_log（hello-ws、confirm_request、
合成回测完成提示这些一眼就是假数据），另有 paper 账户与 backtest_runs 落进真库。
漏一个等于漏全体，所以隔离要默认生效，而不是默认没有、靠每个人记得写。

三道：
① 会话级把 `QUANTDESK_DATA_DIR` 钉到临时目录并建好默认库——内部用默认路径的组件
   （paper_broker、LogSink、ParquetStore）从此只会写到临时目录；
② 用例级在前后各清一次 settings 缓存与引擎——某个用例自己换了目录，
   不许把换过的目录留给下一个用例（否则下一条就开始写真实库）；
③ 结束时再清一次，避免引擎句柄把临时目录外的路径带进后续会话。

第四条（offline_quote_hook）：默认掐断「为下单临时补一次行情」这类自动外呼——
理由不是防污染，而是防结果随当天行情漂移。
"""

from __future__ import annotations

import os

import pytest


def _pin_and_reset(path: str | None) -> None:
    from quantdesk.config import get_settings
    from quantdesk.store.db import reset_engine

    if path is None:
        os.environ.pop("QUANTDESK_DATA_DIR", None)
    else:
        os.environ["QUANTDESK_DATA_DIR"] = path
    # 用例可以把 get_settings 换成普通可调用（test_settings 就这么测热参），
    # 那时没有 cache_clear——清理夹具不许因为被清的对象换了形状就把自己弄崩。
    clear = getattr(get_settings, "cache_clear", None)
    if clear is not None:
        clear()
    reset_engine()


@pytest.fixture(scope="session", autouse=True)
def isolated_data_dir(tmp_path_factory):
    """整个测试会话共用一个临时数据目录（默认库在这里，不在工作区）。"""
    from quantdesk.store.db import init_db

    root = tmp_path_factory.mktemp("quantdesk_data")
    _pin_and_reset(str(root))
    init_db()                          # 默认库先建好：组件用默认路径时不该报"表不存在"
    yield root
    _pin_and_reset(None)


@pytest.fixture(autouse=True)
def no_path_leak(isolated_data_dir):
    """每个用例前后都重置缓存与引擎：用例内部换目录不许外溢。"""
    _pin_and_reset(str(isolated_data_dir))
    yield
    _pin_and_reset(str(isolated_data_dir))


@pytest.fixture(autouse=True)
def offline_quote_hook(request):
    """下单前的「补一次行情」钩子默认掐空——单测必须离线可复现。

    TradingService 会在委托进场前问一次行情快照（昨收/交易所涨跌停价，并在模拟盘
    缺行情时当场补一份喂给撮合）。真实实现打的是公开网页接口，测试里打出去就是两件事：
    跑一次全量给对方刷几百次请求（宪法第 11 条），以及结果随当天行情漂移、今天绿明天红。
    需要真接口的用例自己打 `@pytest.mark.network`；要测这段逻辑的用例在实例上覆写
    `_quote_row`（见 tests/test_paper_trading.py 的按需行情用例）。

    这里故意不拿 monkeypatch 当参数：把它拉进 autouse 链会把 monkeypatch 的建立时刻提前到
    各模块级 autouse 夹具之前，清理顺序一变，别的夹具的 teardown 就会看到还没还原的替身函数
    （本轮全量跑实测撞到 test_settings 的清理夹具）。
    """
    if request.node.get_closest_marker("network"):
        yield
        return
    from quantdesk.services.trading import TradingService

    original = TradingService._quote_row
    TradingService._quote_row = lambda self, symbol: {}
    try:
        yield
    finally:
        TradingService._quote_row = original
