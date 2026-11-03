"""D04：插件注册表测试（宪法第 1 条：注册表自动扫描发现）。"""

import pytest

from quantdesk.adapters import get_adapter_class, list_adapters


def test_registry_discovers_builtin_adapters():
    names = list_adapters()
    assert "akshare" in names  # tushare 断言在 D05 落地后加入（test_registry_d05）


def test_registered_adapters_have_unique_names():
    names = list_adapters()
    assert len(names) == len(set(names))
    for name in names:
        assert getattr(get_adapter_class(name), "name", None) == name


def test_get_adapter_class_has_unique_name():
    cls = get_adapter_class("akshare")
    assert cls.name == "akshare"
    assert hasattr(cls, "get_history")


def test_unknown_adapter_raises_keyerror():
    with pytest.raises(KeyError):
        get_adapter_class("no_such_feed")
