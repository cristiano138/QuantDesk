"""gm（掘金量化）假 SDK 包——仅供离线单测经 fake_sdk("gm") 装载。

真实 gm.api 是掘金终端的交易接口，CI 装不出来。本包复刻适配器实际用到的接口面，
并额外记录「调用发生在哪个线程 id」（关键：断言下单确实跑在终端线程而非调用方线程）。
"""

from . import api  # noqa: F401  让 `import gm.api` 命中
