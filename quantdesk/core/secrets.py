"""本地凭据保险库（宪法第 7 条：账号/token/资金路径只进本地 config，**加密存储**）。

为什么不是 .env：env 文件是明文，会被备份软件、截图、`git add .`、日志采集顺带带走；
券商资金账号与 iFinD/GM token 一旦泄漏就是资金风险。桌面单机场景下 Windows DPAPI
（当前用户熵）是零依赖、免密钥管理的正确解：密文只能在这台机器 + 这个 Windows 账户
下解开，拷走文件没用。

设计：
- 密文落 `data/credentials.vault`（base64 of DPAPI blob），文件本身无明文价值；
- 额外熵 `ENTROPY` 参与加解密——即使同用户下其他程序枚举到文件，也解不出内容；
- **任何读接口都不回显明文**：`list_keys()` 只给键名，`describe()` 给掩码（前 4 位 + ***），
  明文只经 `get_secret()` 流向真正需要它的适配器进程内使用；
- DPAPI 不可用（非 Windows / 罕见策略禁用）→ 抛 ConfigError 带引导，
  绝不静默退化成明文落盘（宁可拒绝存储，也不降级安全边界）。

键名约定（通道凭据统一 `通道.字段`）：
  qmt.userdata_path / qmt.account_id / gm.token / gm.account_id /
  ifind.username / ifind.password / ifind.refresh_token /
  easytrader.ths_path / tushare.token
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wintypes
import json
import threading
from pathlib import Path

from quantdesk.core.errors import ConfigError

__all__ = ["set_secret", "get_secret", "has_secret", "delete_secret",
           "list_keys", "describe", "mask", "vault_path"]

_ENTROPY = b"quantdesk.credential.vault.v1"
_LOCK = threading.RLock()


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]


def vault_path() -> Path:
    from quantdesk.config import data_dir

    return data_dir() / "credentials.vault"


def _dpapi_available() -> bool:
    return hasattr(ctypes, "windll") and hasattr(ctypes.windll, "crypt32")


_SIGNATURE_SET = False


def _bind_signatures() -> None:
    """必须显式声明 argtypes/restype：64 位下默认 int 会把指针截断（LocalFree 直接炸）。

    windll 的函数对象是进程级共享的，这里只在首次调用时绑定一次。
    """
    global _SIGNATURE_SET
    if _SIGNATURE_SET or not _dpapi_available():
        return
    blob_ptr = ctypes.POINTER(_DataBlob)
    for name in ("CryptProtectData", "CryptUnprotectData"):
        fn = getattr(ctypes.windll.crypt32, name)
        fn.argtypes = [blob_ptr, wintypes.LPCWSTR, blob_ptr, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, blob_ptr]
        fn.restype = wintypes.BOOL
    lf = ctypes.windll.kernel32.LocalFree
    lf.argtypes = [ctypes.c_void_p]
    lf.restype = ctypes.c_void_p
    _SIGNATURE_SET = True


def _to_blob(data: bytes):
    """构造 DATA_BLOB；必须一并返回缓冲区对象，否则指针指向的内存会被回收。"""
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buf, ctypes.c_void_p))
    return blob, buf


def _from_blob(blob: _DataBlob) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def _dpapi_call(fn, data: bytes, what: str) -> bytes:
    if not _dpapi_available():
        raise ConfigError(
            "账号加密保存暂不可用：当前电脑缺少系统加密能力。"
            "实盘账号信息必须加密保存，不能明文存储；如需继续使用，请只在模拟盘模式下运行。")
    _bind_signatures()
    in_blob, keep = _to_blob(data)              # keep 防止 GC 回收缓冲区
    entropy, keep_e = _to_blob(_ENTROPY)
    out_blob = _DataBlob()
    ok = fn(ctypes.byref(in_blob), None, ctypes.byref(entropy), None, None,
            0x00000001,                          # CRYPTPROTECT_UI_FORBIDDEN：不弹任何 UI
            ctypes.byref(out_blob))
    del keep, keep_e                             # 此刻指针不再需要
    if not ok:
        raise ConfigError(
            f"账号信息{what}失败。"
            + ("保存文件损坏，或换了电脑/系统账户（加密内容与账户绑定）——"
               "请在设置页重新录入账号信息。" if what == "解密" else "本机系统策略可能禁用了加密保存，请检查系统安全设置。")
        )
    return _from_blob(out_blob)


def _encrypt(plain: bytes) -> bytes:
    return _dpapi_call(ctypes.windll.crypt32.CryptProtectData, plain, "加密")


def _decrypt(cipher: bytes) -> bytes:
    return _dpapi_call(ctypes.windll.crypt32.CryptUnprotectData, cipher, "解密")


# ---------- 明文读写 ----------
def _load() -> dict:
    p = vault_path()
    if not p.exists():
        return {}
    try:
        raw = p.read_bytes()
        return json.loads(_decrypt(base64.b64decode(raw)).decode("utf-8"))
    except ConfigError:
        raise
    except Exception as e:  # noqa: BLE001 文件损坏/换机器/换 Windows 账户
        raise ConfigError(
            "账号信息无法解密：加密内容与当前电脑账户绑定，"
            f"重装系统或换账户后需在设置页重新录入。文件：{p}") from e


def _save(store: dict) -> None:
    p = vault_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    blob = base64.b64encode(_encrypt(json.dumps(store, ensure_ascii=False).encode("utf-8")))
    tmp = p.with_suffix(".vault.tmp")
    tmp.write_bytes(blob)                       # 原子写：半成品不能毁掉可用凭据
    tmp.replace(p)


# ---------- 对外 API ----------
def set_secret(key: str, value: str) -> None:
    key = key.strip()
    if not key:
        raise ConfigError("凭据键名不能为空（约定形如 qmt.userdata_path）")
    with _LOCK:
        store = _load()
        if value == "":
            store.pop(key, None)
        else:
            store[key] = str(value)
        _save(store)


def get_secret(key: str, default: str = "") -> str:
    with _LOCK:
        return str(_load().get(key, default))


def has_secret(key: str) -> bool:
    with _LOCK:
        return key in _load()


def delete_secret(key: str) -> bool:
    with _LOCK:
        store = _load()
        if key not in store:
            return False
        del store[key]
        _save(store)
        return True


def list_keys() -> list[str]:
    """只给键名——值永不离开解密边界。"""
    with _LOCK:
        return sorted(_load())


def mask(value: str) -> str:
    """日志/界面用的脱敏形态：前 4 位 + ***（宪法日志脱敏条款）。"""
    v = str(value or "")
    if not v:
        return ""
    if len(v) <= 4:
        return "***"
    return v[:4] + "***"


def describe() -> list[dict]:
    """已存凭据的掩码清单（供设置页展示「配了什么、没配什么」）。"""
    with _LOCK:
        store = _load()
    return [{"key": k, "value_masked": mask(v), "set": bool(v)}
            for k, v in sorted(store.items())]


def resolve(key: str, settings_attr: str = "") -> str:
    """通道凭据解析顺序：保险库 → Settings（env/.env/override 文件）。

    env 优先用于 CI/开发（QUANTDESK_TUSHARE_TOKEN），生产用户走 UI 写入的保险库；
    两者都空返回空串，由适配器抛带引导的 ConfigError。
    """
    v = get_secret(key)
    if v:
        return v
    if not settings_attr:
        return ""
    from quantdesk.config import get_settings

    return str(getattr(get_settings(), settings_attr, "") or "")
