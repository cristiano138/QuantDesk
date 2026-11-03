"""统一异常体系。

宪法适配器合同：外部调用失败必须抛结构化异常，禁止静默失败；
断线必须抛 ConnectionLostError 且可被 health_check 探测。
"""


class QuantDeskError(Exception):
    """项目统一异常基类。"""


class ConfigError(QuantDeskError):
    """配置缺失/非法（如 token 未配置）。message 必须包含修复引导。"""


class SymbolError(QuantDeskError):
    """证券代码解析/转换失败。"""


class AdapterError(QuantDeskError):
    """适配器（数据源/券商）异常基类。"""


class ConnectionLostError(AdapterError):
    """通道断线。调用方据此走 fail-closed / 重连重放路径。"""


class BrokerError(AdapterError):
    """券商通道执行失败（提交失败必须抛出，禁止静默）。"""


class FeedCapabilityError(AdapterError):
    """请求了适配器不支持的能力（如 AKShare 请求实时行情）。"""
