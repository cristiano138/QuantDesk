"""QuantDesk 桌面壳（D29）：pywebview 加载本地后端（同源 127.0.0.1:8787）。

宪法约束：
- 只加载 http://127.0.0.1:8787（后端写死本机回环，不做服务器部署）；
- 壳不承载业务逻辑，仅为窗口容器；
- 启动方式：先起后端（uvicorn，见 启动.bat）再运行本壳；后端不可达时显示引导文案。
"""

from __future__ import annotations

import sys
import time

import webview

BACKEND = "http://127.0.0.1:8787"
BOOT_TIMEOUT_S = 20


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
            "QuantDesk — 后端未就绪",
            html=(
                "<body style='font-family:sans-serif;background:#111827;color:#e5e7eb;"
                "display:flex;align-items:center;justify-content:center;height:100vh'>"
                "<div><h2>后端未启动</h2>"
                "<p>请先运行 启动.bat，或：<code>uv run python -m quantdesk.app</code></p>"
                f"<p>预期地址：{BACKEND}</p></div></body>"
            ),
            width=900, height=560,
        )
        webview.start()
        return 1

    webview.create_window(
        f"QuantDesk v{sys.argv[1] if len(sys.argv) > 1 else ''}".strip(),
        BACKEND,
        width=1440, height=900,
        min_size=(1100, 700),
    )
    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
