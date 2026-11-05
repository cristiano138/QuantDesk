"""QuantDesk 桌面壳（D29）：pywebview 加载本地后端（同源 127.0.0.1:8787）。

宪法约束：
- 只加载 http://127.0.0.1:8787（后端写死本机回环，不做服务器部署）；
- 壳不承载业务逻辑，仅为窗口容器；
- 日常入口是 QuantDesk-App 里的 QuantDesk.exe（或开始菜单/桌面图标），它会把行情
  服务和这个窗口一起带起来；单独运行本壳时后端不可达会显示引导文案。
"""

from __future__ import annotations

import time

import webview

BACKEND = "http://127.0.0.1:8787"
BOOT_TIMEOUT_S = 20
WINDOW_TITLE = "QuantDesk 看盘台"


def _wait_backend(timeout: float = BOOT_TIMEOUT_S) -> bool:
    """轮询健康端点（标准库实现，避免壳依赖 requests）。"""
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{BACKEND}/api/system/health", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:                        # noqa: BLE001 — 未就绪则继续等
            time.sleep(0.5)
    return False


def main() -> int:
    if not _wait_backend():
        webview.create_window(
            f"{WINDOW_TITLE} — 暂时打不开",
            html=(
                "<body style='font-family:\'Microsoft YaHei\',sans-serif;background:#111827;"
                "color:#e5e7eb;display:flex;align-items:center;justify-content:center;"
                "height:100vh;text-align:center'>"
                "<div><h2>软件暂时打不开</h2>"
                "<p>关掉这个窗口，重新双击桌面的「QuantDesk 看盘台」图标即可。</p>"
                "<p>反复打不开时，把 data\\logs 目录发给开发者。</p></div></body>"
            ),
            width=900, height=560,
        )
        webview.start()
        return 1

    webview.create_window(
        WINDOW_TITLE,
        BACKEND,
        width=1440, height=900,
        min_size=(1100, 700),
    )
    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
