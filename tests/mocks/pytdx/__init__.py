"""假 pytdx（离线单测用）。

真 pytdx 直连通达信行情服务器，离线环境连不上；适配器要验证的是我们自己的代码，
所以这里给一份可脚本化的 ``TdxHq_API`` 替身（详见 ``pytdx.hq``）。

配合 ``tests.mocks.fake_sdk("pytdx")`` 使用；退出上下文即从 sys.modules 清掉，
不给其他用例泄漏「本机装了 pytdx」的假象。
"""

from __future__ import annotations

from .hq import TdxHq_API

__all__ = ["TdxHq_API"]
