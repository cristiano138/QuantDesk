"""插件注册表（宪法第 1 条：适配器模式 + 第 9 条：非官方通道默认禁用）。

扫描 adapters/data/ 与 adapters/brokers/ 下的模块，收集模块级 ADAPTER 类导出。
新增数据源/券商 = 新增一个文件 + 模块级 ADAPTER 导出，核心引擎零改动。

治理规则：
- 模块 import 失败（可选依赖在**顶层**被 import 的旧式适配器，或文件本身有语法错误）
  → 不阻塞启动，但**登记成一条状态为 unready 的通道并带 load_error 原文**。
  静默跳过等于让整条通道从界面消失：本轮真实发生过一次（自己写的文件里一个语法错误
  → AKShare 整条通道不见 → 界面只剩一句"无可用财务源: None"，谁也查不出来）；
- 类声明 `requires` 的 SDK 缺失 → 仍然登记，状态如实上报「未就绪 + 缺哪个模块」，
  实例化时由适配器自己抛带安装引导的 AdapterError（可观测性优于静默消失）；
- 类声明 `default_enabled=False` → **注册表源头即不可见**（get_adapter_class 抛 KeyError、
  list_adapters 不返回），只有设置页把对应 enable_flag 打开后才进入交易 API 视野。
  前端藏按钮不算禁用——通道选择、下单路由都从这里过。
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from quantdesk.adapters.base.optional import AdapterMeta, meta_of

log = logging.getLogger(__name__)

_SCAN_PACKAGES = {
    "quantdesk.adapters.data": "data",
    "quantdesk.adapters.brokers": "broker",
}

_ALL: dict[str, type] = {}            # name → 适配器类（含默认禁用者）
_META: dict[str, AdapterMeta] = {}   # name → 元数据快照
_BROKEN: dict[str, dict] = {}        # 模块名 → 装载失败详情（静默跳过会让通道凭空消失）
_SCAN_DONE = False


def _scan() -> None:
    global _SCAN_DONE
    if _SCAN_DONE:
        return
    for pkg_name, kind in _SCAN_PACKAGES.items():
        pkg = importlib.import_module(pkg_name)
        for mod_info in pkgutil.iter_modules(pkg.__path__):
            mod_name = f"{pkg_name}.{mod_info.name}"
            try:
                mod = importlib.import_module(mod_name)
            except Exception as e:  # noqa: BLE001 可选依赖在顶层被 import 的旧式适配器：跳过并告警
                log.warning("适配器模块加载失败（跳过）: %s: %s", mod_name, e)
                # 但必须登记成"看得见的坏通道"：本轮真实踩过——自己写的文件里一个语法错误
                # 让整条 AKShare 通道从注册表消失，界面只剩一句"无可用财务源: None"
                _BROKEN[mod_name] = {"kind": kind, "error": f"{type(e).__name__}: {e}"}
                continue
            adapter = getattr(mod, "ADAPTER", None)
            if adapter is None:
                continue
            try:
                meta = meta_of(adapter, kind)
            except Exception as e:  # noqa: BLE001 元数据读取失败只跳过本适配器
                log.error("适配器元数据解析失败（跳过）: %s: %s", mod_name, e)
                continue
            name = meta.name
            if not name:
                log.warning("适配器缺少 name 属性（跳过）: %s", mod_name)
                continue
            if name in _ALL:
                raise RuntimeError(f"适配器注册名冲突: {name}（{mod_name}）")
            _ALL[name] = adapter
            _META[name] = meta
    _SCAN_DONE = True
    log.info("适配器注册完成: %s", sorted(_ALL))


def _flag_on(meta: AdapterMeta) -> bool:
    """default_enabled=False 的通道是否被用户在设置里显式启用。"""
    if meta.default_enabled:
        return True
    if not meta.enable_flag:
        return False
    from quantdesk.config import get_settings

    return bool(getattr(get_settings(), meta.enable_flag, False))


def is_visible(name: str) -> bool:
    """是否进入交易/路由 API 视野（默认禁用且未启用 → False）。"""
    _scan()
    meta = _META.get(name)
    return meta is not None and _flag_on(meta)


def get_adapter_class(name: str) -> type:
    """按注册名取适配器类。未注册或被禁用抛 KeyError（对调用方同构：都不存在）。"""
    _scan()
    if name not in _ALL or not _flag_on(_META[name]):
        raise KeyError(f"未启用的适配器: {name!r}（可用: {sorted(list_adapters())}）")
    return _ALL[name]


def list_adapters() -> list[str]:
    _scan()
    return sorted(n for n, m in _META.items() if _flag_on(m))


def describe_adapters(include_disabled: bool = True) -> list[dict]:
    """通道清单（含未就绪/禁用状态）——供系统面板与接入向导渲染。

    include_disabled=False 时只返回交易 API 可见的通道（下单路径的通道选择以此为准）。
    """
    _scan()
    out = []
    for name in sorted(_META):
        meta = _META[name]
        enabled = _flag_on(meta)
        if not enabled and not include_disabled:
            continue
        row = meta.to_dict()
        if not enabled:
            row["status"] = "disabled"
        elif not meta.deps_ready():
            row["status"] = "unready"
        else:
            row["status"] = "ready"
        out.append(row)
    # 装载失败的模块也进清单（键与其它行完全同构，前端不需要为它加新分支）
    for mod_name, info in sorted(_BROKEN.items()):
        short = mod_name.rsplit(".", 1)[-1]
        out.append({"name": f"{short}(装载失败)", "kind": info["kind"],
                    "label": f"{short}（模块装载失败）",
                    "prereq": "这个适配器文件当前导入不了，修好它才会出现在这里",
                    "official": False, "requires": [], "requires_any": False,
                    "missing": [], "deps_ready": False,
                    "default_enabled": True, "enable_flag": None,
                    "is_paper": False, "capabilities": [],
                    "status": "unready", "load_error": info["error"]})
    out.sort(key=lambda r: r["name"])
    return out


def get_meta(name: str) -> AdapterMeta:
    _scan()
    return _META[name]


def reset_registry_for_test() -> None:
    global _SCAN_DONE
    _SCAN_DONE = False
    _ALL.clear()
    _META.clear()
    _BROKEN.clear()
