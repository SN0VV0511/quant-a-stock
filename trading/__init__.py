"""交易执行层公共接口。

Broker 依赖交易规则，规则又会读取证券元数据。这里使用延迟导入，避免仅导入
``trading.instruments`` 时提前初始化整个 Broker 图并形成循环依赖。
"""

from __future__ import annotations

from typing import Any

from trading.models import ExecutionReport, OrderIntent, PortfolioSnapshot, RiskDecision

__all__ = [
    "BrokerAdapter",
    "ExecutionReport",
    "EventRecorder",
    "OrderIntent",
    "PaperBrokerAdapter",
    "PortfolioSnapshot",
    "QmtBrokerAdapter",
    "RiskDecision",
]


def __getattr__(name: str) -> Any:
    """按需加载较重的 Broker 和观测模块。"""
    if name in {"BrokerAdapter", "PaperBrokerAdapter", "QmtBrokerAdapter"}:
        from trading.brokers import BrokerAdapter, PaperBrokerAdapter, QmtBrokerAdapter

        return {
            "BrokerAdapter": BrokerAdapter,
            "PaperBrokerAdapter": PaperBrokerAdapter,
            "QmtBrokerAdapter": QmtBrokerAdapter,
        }[name]
    if name == "EventRecorder":
        from trading.observability import EventRecorder

        return EventRecorder
    raise AttributeError(name)
