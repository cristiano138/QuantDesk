"""离线 mock SDK 装载（主流股票软件通道的可测性方案）。

miniQMT / 掘金终端 / 通达信 / iFinD / 同花顺客户端都不是 CI 环境里能装出来的东西，
但适配器里真正需要验证的是**我们自己的代码**：列名归一化、参数映射、状态机、
回调只入队、断线 fail-closed、重连重放。所以每个通道在 tests/mocks/ 下放一份同名假 SDK，
用 fake_sdk() 临时把 tests/mocks 插到 sys.path 首位——适配器惰性 import 时拿到的就是它。

用法（各通道单测统一走这个入口，避免 sys.path / sys.modules 污染其他用例）：

    from tests.mocks import fake_sdk

    def test_qmt_history_normalizes():
        with fake_sdk("xtquant"):
            feed = QmtXtdataFeed()
            ...

退出时按包名前缀清理 sys.modules 并还原 sys.path：xtquant 这类假包绝不能泄漏给后续用例
（否则注册表会把「未就绪」的通道报成就绪）。
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MOCKS_DIR = Path(__file__).resolve().parent


def _purge(names: tuple[str, ...]) -> None:
    for key in list(sys.modules):
        if any(key == n or key.startswith(n + ".") for n in names):
            del sys.modules[key]


@contextmanager
def fake_sdk(*names: str) -> Iterator[None]:
    """临时挂载 tests/mocks 下的假 SDK 包（可多个），退出即还原。"""
    saved_path = list(sys.path)
    targets = tuple(names)
    _purge(targets)                       # 丢掉之前可能残留的同名模块
    sys.path.insert(0, str(MOCKS_DIR))
    importlib.invalidate_caches()
    try:
        yield
    finally:
        _purge(targets)
        sys.path[:] = saved_path
        importlib.invalidate_caches()
