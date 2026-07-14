"""交易领域模型。

这些模型用于隔离策略、风控、虚拟成交和未来 QMT 回报，避免各模块传递不稳定的裸字典。
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from config.time_utils import format_local, today_yyyymmdd

Action = Literal["buy", "sell"]
ExecutionStatus = Literal[
    "filled",
    "partially_filled",
    "rejected",
    "cancelled",
    "submitted",
    "failed",
]
StrategyTag = Literal[
    "combo_trend",
    "momentum_breakout",
    "limitup_follow",
    "smallcap_value",
    "etf_rotation",
    "rps_rotation",
    "robust_v2",
]
VALID_STRATEGY_TAGS: frozenset[str] = frozenset(
    {
        "combo_trend",
        "momentum_breakout",
        "limitup_follow",
        "smallcap_value",
        "etf_rotation",
        "rps_rotation",
        "robust_v2",
    }
)

Adjustment = Literal["qfq", "hfq", "none"]
AssetType = Literal["stock", "etf"]


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
        account_id: 账户标识，新账本固定使用 ``paper_v2``。
        strategy_version: 产生订单的策略版本。
        signal_date: 产生目标仓位的信号日期。
        idempotency_key: 幂等键；未传入时按订单关键字段稳定生成。
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
    account_id: str = "legacy"
    strategy_version: str = "legacy"
    signal_date: str | None = None
    idempotency_key: str = ""

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
        if not self.account_id.strip():
            raise ValueError("账户标识不能为空")
        if not self.strategy_version.strip():
            raise ValueError("策略版本不能为空")
        if not self.idempotency_key:
            signal_date = self.signal_date or self.date or "undated"
            raw = "|".join(
                (
                    self.account_id,
                    self.strategy_version,
                    signal_date,
                    self.code,
                    self.action,
                    str(self.shares),
                )
            )
            object.__setattr__(
                self, "idempotency_key", hashlib.sha256(raw.encode("utf-8")).hexdigest()
            )

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
            account_id=str(order.get("account_id", "legacy")),
            strategy_version=str(order.get("strategy_version", "legacy")),
            signal_date=order.get("signal_date") or order.get("date"),
            idempotency_key=str(order.get("idempotency_key", "")),
            metadata={
                k: v
                for k, v in order.items()
                if k
                not in {
                    "code",
                    "action",
                    "price",
                    "shares",
                    "name",
                    "strategy",
                    "reason",
                    "date",
                    "source",
                    "strategy_tag",
                    "account_id",
                    "strategy_version",
                    "signal_date",
                    "idempotency_key",
                }
            },
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
            "account_id": self.account_id,
            "strategy_version": self.strategy_version,
            "signal_date": self.signal_date,
            "idempotency_key": self.idempotency_key,
            **self.metadata,
        }

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化字典。"""
        return asdict(self)


@dataclass(frozen=True)
class MarketSnapshot:
    """策略输入使用的可审计市场快照。

    ``freshness_seconds`` 由数据装载层在生成快照时计算。策略只接受未超过
    ``max_freshness_seconds`` 的快照，避免缓存陈旧时继续下单。
    """

    trade_date: str
    source: str
    adjustment: Adjustment
    data_hash: str
    freshness_seconds: int
    max_freshness_seconds: int = 86_400
    previous_trade_date: str | None = None
    captured_at: str = field(default_factory=_now_str)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.trade_date) != 8 or not self.trade_date.isdigit():
            raise ValueError(f"交易日必须为 YYYYMMDD: {self.trade_date}")
        if not self.source.strip():
            raise ValueError("行情数据源不能为空")
        if not self.data_hash.strip():
            raise ValueError("行情数据哈希不能为空")
        if self.freshness_seconds < 0:
            raise ValueError("行情新鲜度不能为负数")
        if self.max_freshness_seconds <= 0:
            raise ValueError("行情最大允许陈旧时间必须大于 0")

    @property
    def is_fresh(self) -> bool:
        """返回快照是否仍在允许的新鲜度范围内。"""
        return self.freshness_seconds <= self.max_freshness_seconds

    def require_fresh(self) -> None:
        """陈旧快照直接失败，禁止策略静默降级下单。"""
        if not self.is_fresh:
            raise ValueError(
                f"行情快照已陈旧: freshness={self.freshness_seconds}s, "
                f"limit={self.max_freshness_seconds}s"
            )

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化字典。"""
        return asdict(self)


@dataclass(frozen=True)
class TargetPosition:
    """目标组合中的单个证券权重。"""

    code: str
    target_weight: float
    reason: str
    asset_type: AssetType
    name: str = ""

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("目标标的代码不能为空")
        if not 0 < self.target_weight <= 1:
            raise ValueError(f"目标权重必须位于 (0, 1]: {self.target_weight}")
        if not self.reason.strip():
            raise ValueError("目标仓位原因不能为空")

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化字典。"""
        return asdict(self)


@dataclass(frozen=True)
class TargetPortfolio:
    """策略输出的目标组合，不包含任何券商下单行为。"""

    account_id: str
    strategy_version: str
    signal_date: str
    positions: tuple[TargetPosition, ...]
    source_snapshot_hash: str
    cash_weight: float
    generated_at: str = field(default_factory=_now_str)
    fallback_reason: str = ""

    def __post_init__(self) -> None:
        if not self.account_id.strip():
            raise ValueError("目标组合账户标识不能为空")
        if not self.strategy_version.strip():
            raise ValueError("目标组合策略版本不能为空")
        if len(self.signal_date) != 8 or not self.signal_date.isdigit():
            raise ValueError(f"信号日期必须为 YYYYMMDD: {self.signal_date}")
        codes = [position.code for position in self.positions]
        if len(codes) != len(set(codes)):
            raise ValueError("目标组合不能包含重复标的")
        exposure = sum(position.target_weight for position in self.positions)
        if exposure > 1 + 1e-9:
            raise ValueError(f"目标组合总权重不能超过 100%: {exposure:.4f}")
        if not 0 <= self.cash_weight <= 1:
            raise ValueError(f"现金权重必须位于 [0, 1]: {self.cash_weight}")
        if abs(exposure + self.cash_weight - 1) > 1e-6:
            raise ValueError(
                f"目标持仓与现金权重之和必须为 100%: exposure={exposure:.4f}, "
                f"cash={self.cash_weight:.4f}"
            )

    @property
    def exposure(self) -> float:
        """返回目标总仓位。"""
        return sum(position.target_weight for position in self.positions)

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
