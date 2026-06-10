"""交易领域模型。

这些模型用于隔离策略、风控、虚拟成交和未来 QMT 回报，避免各模块传递不稳定的裸字典。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from config.time_utils import format_local, today_yyyymmdd

Action = Literal["buy", "sell"]
ExecutionStatus = Literal["filled", "rejected", "cancelled", "submitted", "failed"]
StrategyTag = Literal[
    "combo_trend",
    "momentum_breakout",
    "limitup_follow",
    "smallcap_value",
    "etf_rotation",
    "rps_rotation",
]
VALID_STRATEGY_TAGS: frozenset[str] = frozenset({
    "combo_trend",
    "momentum_breakout",
    "limitup_follow",
    "smallcap_value",
    "etf_rotation",
    "rps_rotation",
})


def _now_str() -> str:
    """返回统一格式的本地时间字符串。"""
    return format_local()


@dataclass(frozen=True)
class OrderIntent:
    """标准化订单意图。

    Args:
        code: 标的代码。
        action: 买卖方向，取值为 buy 或 sell。
        price: 委托参考价。
        shares: 委托数量，A 股股票必须为 100 股整数倍，生成阶段允许为 0。
        name: 标的名称。
        strategy: 策略名称。
        reason: 触发订单的原因。
        date: 交易日期，格式 YYYYMMDD。
        source: 订单来源，用于观测和审计。
        metadata: 扩展字段。
    """

    code: str
    action: Action
    price: float
    shares: int
    name: str = ""
    strategy: str = ""
    reason: str = ""
    date: str | None = None
    source: str = "strategy"
    created_at: str = field(default_factory=_now_str)
    metadata: dict[str, Any] = field(default_factory=dict)
    strategy_tag: StrategyTag = "combo_trend"

    def __post_init__(self) -> None:
        """校验订单意图的基础合法性。"""
        if self.action not in {"buy", "sell"}:
            raise ValueError(f"不支持的交易方向: {self.action}")
        if not self.code:
            raise ValueError("标的代码不能为空")
        if self.price < 0:
            raise ValueError(f"价格不能为负数: {self.price}")
        if self.shares < 0:
            raise ValueError(f"股数不能为负数: {self.shares}")
        if self.strategy_tag not in VALID_STRATEGY_TAGS:
            raise ValueError(f"不支持的策略标签: {self.strategy_tag}")

    @classmethod
    def from_order_dict(cls, order: dict[str, Any]) -> "OrderIntent":
        """从旧版订单字典创建标准订单意图。"""
        return cls(
            code=str(order.get("code", "")),
            action=order.get("action", "buy"),
            price=float(order.get("price", 0) or 0),
            shares=int(order.get("shares", 0) or 0),
            name=str(order.get("name", "")),
            strategy=str(order.get("strategy", "")),
            reason=str(order.get("reason", "")),
            date=order.get("date"),
            source=str(order.get("source", "legacy")),
            strategy_tag=order.get("strategy_tag", "combo_trend"),
            metadata={k: v for k, v in order.items() if k not in {
                "code", "action", "price", "shares", "name", "strategy", "reason", "date", "source",
                "strategy_tag",
            }},
        )

    def to_order_dict(self) -> dict[str, Any]:
        """转换为兼容旧版风控模块的订单字典。"""
        return {
            "code": self.code,
            "name": self.name,
            "action": self.action,
            "shares": self.shares,
            "price": self.price,
            "reason": self.reason,
            "strategy": self.strategy,
            "date": self.date,
            "source": self.source,
            "strategy_tag": self.strategy_tag,
            **self.metadata,
        }

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化字典。"""
        return asdict(self)


@dataclass(frozen=True)
class RiskDecision:
    """标准化风控审批结果。"""

    order: OrderIntent
    approved: bool
    reason: str
    checked_at: str = field(default_factory=_now_str)

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化字典。"""
        data = asdict(self)
        data["order"] = self.order.to_dict()
        return data


@dataclass(frozen=True)
class ExecutionReport:
    """标准化成交回报。

    Args:
        order_id: 内部订单编号或券商委托编号。
        status: 成交状态。
        code: 标的代码。
        action: 买卖方向。
        price: 委托参考价。
        actual_price: 实际成交价。
        shares: 成交数量。
        amount: 成交名义金额。
        cost: 交易成本。
        strategy: 策略名称。
        message: 成交或拒单说明。
        profit: 卖出时的已实现盈亏。
        raw: 原始券商回报或内部结果。
    """

    order_id: str
    status: ExecutionStatus
    code: str
    action: Action
    price: float
    actual_price: float
    shares: int
    amount: float
    cost: float = 0.0
    strategy: str = ""
    strategy_tag: StrategyTag = "combo_trend"
    sell_reason: str = ""
    message: str = ""
    profit: float | None = None
    date: str | None = None
    timestamp: str = field(default_factory=_now_str)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        """是否已成交。"""
        return self.status == "filled"

    def to_trade_dict(self) -> dict[str, Any]:
        """转换为日报和 Web 可读的交易记录。"""
        trade = {
            "date": self.date or today_yyyymmdd(),
            "time": self.timestamp[-8:],
            "code": self.code,
            "action": self.action,
            "direction": self.action,
            "price": round(self.actual_price, 4),
            "shares": self.shares,
            "amount": round(self.amount, 2),
            "cost": round(self.cost, 2),
            "strategy": self.strategy,
            "strategy_tag": self.strategy_tag,
            "sell_reason": self.sell_reason,
            "status": self.status,
            "message": self.message,
        }
        if self.profit is not None:
            trade["profit"] = round(self.profit, 2)
        return trade

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化字典。"""
        return asdict(self)


@dataclass(frozen=True)
class PortfolioSnapshot:
    """标准化账户快照。"""

    cash: float
    total_value: float
    position_ratio: float
    position_count: int
    positions: list[dict[str, Any]]
    drawdown: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    source: str = "paper"
    timestamp: str = field(default_factory=_now_str)

    @classmethod
    def from_portfolio(
        cls,
        portfolio: Any,
        current_prices: dict[str, float] | None = None,
        source: str = "paper",
    ) -> "PortfolioSnapshot":
        """从持仓管理器生成账户快照。"""
        summary = portfolio.summary(current_prices)
        return cls(
            cash=float(summary["cash"]),
            total_value=float(summary["total_value"]),
            position_ratio=float(summary["position_ratio"]),
            position_count=int(summary["position_count"]),
            positions=list(summary["positions"]),
            drawdown=float(summary["drawdown"]),
            pnl=float(summary["pnl"]),
            pnl_pct=float(summary["pnl_pct"]),
            source=source,
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化字典。"""
        return asdict(self)
