"""建库脚本（幂等）。宪法存储层：启动自动建表；本脚本供 CLI/交付使用。

用法：uv run python scripts/init_db.py
"""

import sys

from quantdesk.store.db import init_db

if __name__ == "__main__":
    path = init_db()
    print(f"数据库就绪: {path}")
    sys.exit(0)
