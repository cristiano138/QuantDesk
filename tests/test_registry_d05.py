"""D05：注册表补充断言（tushare 落地后）。"""

from quantdesk.adapters import list_adapters


def test_registry_contains_tushare():
    assert "tushare" in list_adapters()
