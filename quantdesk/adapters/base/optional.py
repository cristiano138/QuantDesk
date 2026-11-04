"""可选依赖与通道元数据公共设施（主流股票软件 SDK 统一治理）。

市面主流股票软件的行情/交易 SDK 全部是**可选依赖**：xtquant 随券商 QMT 客户端分发、
iFinDPy 是同花顺私有 whl、TdxAiData 随通达信官方 SDK 安装、gmjsdk 需掘金终端在跑、
easytrader 依赖本机同花顺/通达信客户端。它们不能进 pyproject 主依赖（宪法第 8 条），
也不能在模块顶层 import——否则注册表扫描直接崩，整个应用起不来。

约定（新增通道必须遵守）：
1. 适配器文件顶层不 import SDK；在 connect()（或首次调用）里 import_optional()；
2. 类属性声明元数据，注册表据此如实上报状态、UI 据此渲染接入向导：
   requires        : 需要的 Python 模块名（缺失 → 状态「未就绪」，给出安装引导）
   label           : 中文通道名（UI 展示）
   prereq          : 环境/行政前置（一句话说清用户要做完哪一步本通道才能用）
   official        : 是否官方接口（False → UI 红字「非官方接口，稳定性低」）
   default_enabled : False → 注册表源头跳过（交易 API 不可见），需设置页显式启用
   enable_flag     : default_enabled=False 时对应的 Settings 布尔字段名
3. 缺 SDK 时 import_optional 抛 AdapterError（带安装引导），不是 ImportError、
   也不是静默返回 None——用户看到的必须是「怎么办」而不是堆栈。
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from types import ModuleType

from quantdesk.core.errors import AdapterError

__all__ = ["dependency_available", "import_optional", "meta_of", "AdapterMeta", "DYNAMIC"]

DYNAMIC = ("dynamic",)   # 能力由实例动态聚合（如 FederatedFeed 的 property）


class AdapterMeta:
    """注册表侧读取到的适配器元数据（类属性的归一化快照）。"""

    __slots__ = ("name", "kind", "label", "prereq", "official", "requires",
                 "requires_any", "default_enabled", "enable_flag", "is_paper",
                 "capabilities")

    def __init__(self, name: str, kind: str, label: str, prereq: str, official: bool,
                 requires: tuple[str, ...], default_enabled: bool, enable_flag: str,
                 is_paper: bool, capabilities: tuple[str, ...],
                 requires_any: bool = False) -> None:
        self.name = name
        self.kind = kind                    # "data" | "broker"
        self.label = label
        self.prereq = prereq
        self.official = official
        self.requires = requires
        self.requires_any = requires_any
        self.default_enabled = default_enabled
        self.enable_flag = enable_flag
        self.is_paper = is_paper
        self.capabilities = capabilities

    def deps_ready(self) -> bool:
        """requires_any=True → 任一依赖可用即就绪（通达信：官方 SDK 或 pytdx 择一）。"""
        if not self.requires:
            return True
        flags = [dependency_available(m) for m in self.requires]
        return any(flags) if self.requires_any else all(flags)

    def missing_deps(self) -> list[str]:
        absent = [m for m in self.requires if not dependency_available(m)]
        # 「任一可用」且确实有一条路通着，就不算缺——否则面板永远列着另一条用不上的路
        return [] if (self.requires_any and self.deps_ready()) else absent

    def to_dict(self) -> dict:
        return {
            "name": self.name, "kind": self.kind, "label": self.label,
            "prereq": self.prereq, "official": self.official,
            "requires": list(self.requires), "requires_any": self.requires_any,
            "missing": self.missing_deps(),
            "deps_ready": self.deps_ready(),
            "default_enabled": self.default_enabled, "enable_flag": self.enable_flag,
            "is_paper": self.is_paper, "capabilities": list(self.capabilities),
        }


def dependency_available(module: str) -> bool:
    """SDK 是否可用（已导入或能在 sys.path 上定位到）。"""
    if module in sys.modules:
        return True
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):       # 父包缺失/命名空间异常都按不可用处理
        return False


def import_optional(module: str, *, channel: str, install_hint: str) -> ModuleType:
    """惰性导入可选 SDK；缺失即抛带引导的 AdapterError（禁止静默降级）。"""
    try:
        return importlib.import_module(module)
    except Exception as e:  # noqa: BLE001 — 导入期任何异常都归一为结构化错误
        raise AdapterError(
            f"{channel} 依赖模块 {module!r} 未就绪：{e.__class__.__name__}: {e}。{install_hint}"
        ) from e


def _static_capabilities(cls: type) -> tuple[str, ...]:
    """类级静态能力声明。

    FederatedFeed 这类组合适配器的 capabilities 是 property（聚合子源动态得出），
    类对象上取到的是 property 描述符本身——此时按「动态能力」登记，不做迭代。
    """
    from inspect import getattr_static

    caps = getattr_static(cls, "capabilities", None)
    if isinstance(caps, property) or caps is None or isinstance(caps, str):
        return () if caps is None else DYNAMIC
    try:
        return tuple(sorted(getattr(c, "value", str(c)) for c in caps))
    except TypeError:
        return DYNAMIC


def meta_of(cls: type, kind: str) -> AdapterMeta:
    """从适配器类读取元数据（未声明的取安全默认值）。"""
    return AdapterMeta(
        name=getattr(cls, "name", cls.__name__),
        kind=kind,
        label=getattr(cls, "label", getattr(cls, "name", cls.__name__)),
        prereq=getattr(cls, "prereq", ""),
        official=bool(getattr(cls, "official", True)),
        requires=tuple(getattr(cls, "requires", ()) or ()),
        requires_any=bool(getattr(cls, "requires_any", False)),
        default_enabled=bool(getattr(cls, "default_enabled", True)),
        enable_flag=str(getattr(cls, "enable_flag", "")),
        is_paper=bool(getattr(cls, "is_paper", False)),
        capabilities=_static_capabilities(cls),
    )
