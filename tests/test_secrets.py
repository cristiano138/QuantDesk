"""凭据保险库单测：DPAPI 加解密、明文不外泄、脱敏形态、解析优先级。

真机 DPAPI 才能验证「密文离开这台机器/这个账户就解不开」这条边界，
所以这些用例在非 Windows 上整组跳过，而不是用假加密凑绿。
"""

from __future__ import annotations

import base64
import ctypes

import pytest

from quantdesk.core import secrets
from quantdesk.core.errors import ConfigError

requires_dpapi = pytest.mark.skipif(
    not (hasattr(ctypes, "windll") and hasattr(ctypes.windll, "crypt32")),
    reason="凭据保险库依赖 Windows DPAPI")


@pytest.fixture(autouse=True)
def vault_in_tmp(tmp_path, monkeypatch):
    """把保险库文件指到 tmp，绝不动开发者真实 data/credentials.vault。"""
    from quantdesk import config as cfg

    monkeypatch.setattr(cfg, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(secrets, "vault_path", lambda: tmp_path / "credentials.vault")
    return tmp_path


@requires_dpapi
def test_roundtrip_and_file_contains_no_plaintext():
    secrets.set_secret("gm.token", "abc123def456-SECRET")
    assert secrets.get_secret("gm.token") == "abc123def456-SECRET"
    raw = secrets.vault_path().read_bytes()
    assert b"abc123def456" not in raw                       # 文件里绝无明文
    assert b"abc123def456" not in base64.b64decode(raw)     # 解码后也不是明文


@requires_dpapi
def test_other_key_survives_and_empty_value_deletes_key():
    secrets.set_secret("qmt.account_id", "888800012345")
    secrets.set_secret("ifind.username", "trader@example.com")
    assert secrets.list_keys() == ["ifind.username", "qmt.account_id"]
    secrets.set_secret("qmt.account_id", "")                # 空串=删除（UI 清空即撤销）
    assert secrets.list_keys() == ["ifind.username"]
    assert secrets.delete_secret("ifind.username") is True
    assert secrets.delete_secret("ifind.username") is False
    assert secrets.list_keys() == []


@requires_dpapi
def test_describe_never_returns_plaintext():
    secrets.set_secret("gm.token", "abcdefghijklmnop")
    rows = secrets.describe()
    assert rows == [{"key": "gm.token", "value_masked": "abcd***", "set": True}]
    dumped = repr(rows)
    assert "abcdefghijklmnop" not in dumped                 # 接口层不可能带出明文


@requires_dpapi
def test_mask_short_and_empty_values():
    assert secrets.mask("") == ""
    assert secrets.mask("abc") == "***"                     # 不足 4 位全遮，不泄漏长度信息
    assert secrets.mask("abcd") == "***"
    assert secrets.mask("abcdefg") == "abcd***"


@requires_dpapi
def test_corrupted_vault_gives_actionable_error(tmp_path):
    secrets.set_secret("gm.token", "x" * 20)
    secrets.vault_path().write_bytes(b"not-base64-at-all")
    with pytest.raises(ConfigError) as ei:
        secrets.get_secret("gm.token")
    msg = str(ei.value)
    assert "账户" in msg and "重新录入" in msg               # 必须说清「为什么会这样、怎么办」


@requires_dpapi
def test_wrong_entropy_cannot_decrypt(monkeypatch):
    """换熵（=别的程序/别的版本）解不开——验证熵真的参与保护而非摆设。"""
    secrets.set_secret("gm.token", "top-secret-value")
    monkeypatch.setattr(secrets, "_ENTROPY", b"other.app.entropy")
    with pytest.raises(ConfigError):
        secrets.get_secret("gm.token")


def test_dpapi_unavailable_raises_guidance(monkeypatch):
    monkeypatch.setattr(secrets, "_dpapi_available", lambda: False)
    with pytest.raises(ConfigError) as ei:
        secrets.set_secret("gm.token", "x")
    assert "模拟盘" in str(ei.value)                         # 引导：非 Windows 请只用模拟盘


@requires_dpapi
def test_resolve_prefers_vault_then_settings(monkeypatch):
    from quantdesk import config as cfg

    monkeypatch.setattr(cfg, "get_settings",
                        lambda: cfg.Settings(tushare_token="from-env"))
    assert secrets.resolve("tushare.token", "tushare_token") == "from-env"
    secrets.set_secret("tushare.token", "from-vault")
    assert secrets.resolve("tushare.token", "tushare_token") == "from-vault"
    assert secrets.resolve("qmt.account_id") == ""           # 无 settings 映射时缺省空串
