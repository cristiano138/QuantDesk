"""SQLite 引擎管理（SQLModel + WAL + 外键）。

- WAL 允许单写多读：全部写操作走 store/repo 的模块级单写锁；
- engine 按路径缓存；测试可用 reset_engine() 切换环境。
"""

import logging
import threading
from pathlib import Path

from sqlalchemy import event as sa_event
from sqlmodel import Session, SQLModel, create_engine

from quantdesk.core.timeutil import now_ms

log = logging.getLogger(__name__)

_lock = threading.Lock()
_engine = None
_engine_path: Path | None = None


def _tune(dbapi_conn, _record) -> None:  # pragma: no cover - sqlite 驱动钩子
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def _make_engine(path: Path):
    engine = create_engine(
        f"sqlite+pysqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    sa_event.listen(engine, "connect", _tune)
    return engine


def default_db_path() -> Path:
    """默认库路径。

    惰性取 data_dir()：模块导入期绑死函数引用会让「运行时改目录」（测试隔离、
    便携模式）失效——实测踩过：测试夹具改了 data_dir，写入仍落到开发库。
    """
    from quantdesk.config import data_dir

    return data_dir() / "quantdesk.db"


def get_engine(db_path: str | Path | None = None):
    global _engine, _engine_path
    with _lock:
        path = Path(db_path) if db_path else default_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if _engine is None or _engine_path != path.resolve():
            _engine = _make_engine(path)
            _engine_path = path.resolve()
        return _engine


def reset_engine() -> None:
    """测试辅助：释放并清空引擎缓存（切换 data_dir / 临时目录清理前调用）。"""
    global _engine, _engine_path
    with _lock:
        if _engine is not None:
            try:
                _engine.dispose()
            except Exception:  # noqa: BLE001 清理尽力而为
                pass
        _engine = None
        _engine_path = None


def get_session(db_path: str | Path | None = None) -> Session:
    return Session(get_engine(db_path))


def init_db(db_path: str | Path | None = None) -> Path:
    """建表（幂等）：重复执行不报错、不重复建。"""
    import quantdesk.store.models_db as models  # noqa: F401 确保表定义注册

    engine = get_engine(db_path)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        row = s.get(models.SchemaVersion, 1)
        if row is None:
            s.add(models.SchemaVersion(id=1, version=1, applied_at=now_ms()))
        else:
            row.version = max(row.version, 1)
            row.applied_at = now_ms()
        s.commit()
    path = Path(db_path) if db_path else default_db_path()
    log.info("数据库就绪: %s", path)
    return path
