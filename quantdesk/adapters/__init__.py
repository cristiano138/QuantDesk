"""插件注册表（宪法第 1 条：适配器模式）。

扫描 adapters/data/ 与 adapters/brokers/ 下的模块，收集模块级 ADAPTER 类导出。
新增数据源/券商 = 新增一个文件 + 模块级 ADAPTER 导出，核心引擎零改动。
可选依赖缺失的模块（xtquant/gm/easytrader 等）跳过并告警，不阻塞启动。
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

log = logging.getLogger(__name__)

_REGISTRY: dict[str, type] = {}
_SCAN_DONE = False

_SCAN_PACKAGES = ("quantdesk.adapters.data", "quantdesk.adapters.brokers")


def _scan() -> None:
    global _SCAN_DONE
    if _SCAN_DONE:
        return
    for pkg_name in _SCAN_PACKAGES:
        pkg = importlib.import_module(pkg_name)
        for mod_info in pkgutil.iter_modules(pkg.__path__):
            mod_name = f"{pkg_name}.{mod_info.name}"
            try:
                mod = importlib.import_module(mod_name)
            except Exception as e:  # noqa: BLE001 可选依赖缺失 → 跳过该适配器
                log.warning("适配器模块加载失败（跳过）: %s: %s", mod_name, e)
                continue
            adapter = getattr(mod, "ADAPTER", None)
            if adapter is None:
                continue
            name = getattr(adapter, "name", None)
            if not name:
                log.warning("适配器缺少 name 属性（跳过）: %s", mod_name)
                continue
            if name in _REGISTRY:
                raise RuntimeError(f"适配器注册名冲突: {name}（{mod_name}）")
            _REGISTRY[name] = adapter
    _SCAN_DONE = True
    log.info("适配器注册完成: %s", sorted(_REGISTRY))


def get_adapter_class(name: str) -> type:
    """按注册名取适配器类。未注册抛 KeyError。"""
    _scan()
    if name not in _REGISTRY:
        raise KeyError(f"未注册的适配器: {name!r}（可用: {sorted(_REGISTRY)}）")
    return _REGISTRY[name]


def list_adapters() -> list[str]:
    _scan()
    return sorted(_REGISTRY)


def reset_registry_for_test() -> None:
    global _SCAN_DONE
    _SCAN_DONE = False
    _REGISTRY.clear()
