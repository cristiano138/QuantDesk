"""策略文件 API：两种标识都要认、报错不许带出本机绝对路径。

这两条都是本轮审计翻出来的：
① 回测页手里的是点分模块名（examples.dual_ma），文件接口只认相对路径——同一个文件两套
   标识，深链过去必然 404；② read_file 的 FileNotFoundError 原文进 detail，把
   「F:\\…\\strategies\\x.py」整条路径回显给界面（本地软件也不该把目录结构随便吐出去）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quantdesk.api import routes_strategy
from quantdesk.config import get_settings
from quantdesk.services.strategy_mgr import StrategyManager
from quantdesk.store.db import init_db, reset_engine

CODE = ("from quantdesk.core.strategy import BaseStrategy\n\n\n"
        "class MyDemo(BaseStrategy):\n"
        "    name = 'my_demo'\n")


@pytest.fixture()
def mgr(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTDESK_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    reset_engine()
    root = tmp_path / "strategies"
    (root / "examples").mkdir(parents=True)
    init_db(tmp_path / "s.db")
    manager = StrategyManager(db_path=tmp_path / "s.db", strategies_root=root)
    monkeypatch.setattr(routes_strategy, "get_strategy_manager", lambda: manager)
    yield manager
    reset_engine()
    get_settings.cache_clear()


@pytest.fixture()
def client():
    from quantdesk.app import app

    return TestClient(app)


def test_file_readable_by_path_and_by_module(client, mgr):
    mgr.write_file("examples/my_demo.py", CODE)
    by_path = client.get("/api/strategies/file", params={"path": "examples/my_demo.py"})
    by_module = client.get("/api/strategies/file", params={"module": "examples.my_demo"})
    assert by_path.status_code == by_module.status_code == 200
    assert by_path.json()["code"] == by_module.json()["code"] == CODE
    assert by_module.json()["path"] == "examples/my_demo.py"


def test_missing_params_and_unknown_name_are_clear(client, mgr):
    r = client.get("/api/strategies/file")
    assert r.status_code == 400 and "path 或 module" in r.json()["detail"]

    r = client.get("/api/strategies/file", params={"path": "no_such.py"})
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "策略文件不存在" in detail and "no_such.py" in detail
    assert str(mgr.root) not in detail              # 不带本机绝对路径


def test_path_traversal_still_rejected_without_leaking(client, mgr):
    r = client.get("/api/strategies/file", params={"path": "../quantdesk/app.py"})
    assert r.status_code == 404
    assert "越界" in r.json()["detail"]
    assert str(mgr.root.parent) not in r.json()["detail"]


def test_save_then_read_roundtrip(client, mgr):
    r = client.post("/api/strategies/file", json={"path": "my_demo.py", "code": CODE})
    assert r.status_code == 200 and r.json()["saved"] == "my_demo.py"
    assert client.get("/api/strategies/file", params={"module": "my_demo"}).json()["code"] == CODE
    assert mgr.list_files() == ["my_demo.py"]


def test_bad_filename_is_400(client, mgr):
    r = client.post("/api/strategies/file", json={"path": "_hidden.py", "code": CODE})
    assert r.status_code == 400 and ".py" in r.json()["detail"]
