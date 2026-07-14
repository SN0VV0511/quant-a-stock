"""A 股交易时段与实时行情执行校验。

策略、虚拟盘和未来 QMT 通道共享这里的市场边界，避免每个 Broker 各自解释
停牌、涨跌停和行情时间。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time as dt_time
from typing import Any, Literal, Mapping

from rules.engine import TradingRules

TradingSession = Literal[
    "pre_open",
    "morning",
    "lunch_break",
    "afternoon",
    "closed",
]


@dataclass(frozen=True)
class ExecutionQuote:
    """已通过新鲜度、停牌和价格制度校验的执行行情。"""

    code: str
    name: str
    current_price: float
    prev_close: float
    quote_time: str
    trade_date: str
    source: str
    captured_at: str
    validated_at: str
    max_age_seconds: int
    is_suspended: bool
    can_buy: bool
    can_sell: bool
    limit_type: str

    def to_dict(self) -> dict[str, Any]:
        """转换为订单元数据可持久化字典。"""
        return asdict(self)


@dataclass(frozen=True)
class QuoteValidationResult:
    """单只实时行情的校验结果。"""

    context: ExecutionQuote | None
    reason: str = ""
    retryable: bool = True

    @property
    def accepted(self) -> bool:
        """行情是否可进入分配和委托阶段。"""
        return self.context is not None


def classify_trading_session(current: datetime) -> TradingSession:
    """按上交所/深交所连续竞价时段分类本地时间。"""
    value = current.time()
    if value < dt_time(9, 15):
        return "closed"
    if value < dt_time(9, 30):
        return "pre_open"
    if value < dt_time(11, 30):
        return "morning"
    if value < dt_time(13, 0):
        return "lunch_break"
    if value < dt_time(15, 0):
        return "afternoon"
    return "closed"


def is_continuous_trading_session(current: datetime) -> bool:
    """判断是否处于交易所连续竞价时段。"""
    return classify_trading_session(current) in {"morning", "afternoon"}


def is_strategy_execution_session(current: datetime) -> bool:
    """判断是否处于策略允许下单的 09:35 后连续竞价时段。"""
    value = current.time()
    return dt_time(9, 35) <= value < dt_time(11, 30) or dt_time(
        13, 0
    ) <= value < dt_time(15, 0)


def _parse_quote_time(value: object) -> datetime | None:
    """解析腾讯时间戳及常见日志时间格式。"""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y%m%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def validate_execution_quote(
    code: str,
    quote: Mapping[str, Any],
    *,
    execution_date: str,
    now: datetime,
    max_age_seconds: int,
    rules: TradingRules | None = None,
) -> QuoteValidationResult:
    """校验一条实时行情并计算买卖方向可用性。

    缺失、陈旧、停牌和涨跌停都属于盘中可能恢复的市场状态，因此返回
    ``retryable=True``，由订单状态机在当前交易日继续重试。
    """
    if max_age_seconds <= 0:
        raise ValueError("实时行情最大年龄必须大于 0")
    price = float(quote.get("price", 0) or 0)
    prev_close = float(quote.get("prev_close", 0) or 0)
    quote_at = _parse_quote_time(quote.get("quote_time"))
    if quote_at is None:
        return QuoteValidationResult(None, "行情缺少可验证的交易所时间戳")
    if quote_at.strftime("%Y%m%d") != execution_date:
        return QuoteValidationResult(
            None,
            f"行情日期 {quote_at:%Y%m%d} 与执行日 {execution_date} 不一致",
        )
    age_seconds = (now - quote_at).total_seconds()
    if age_seconds < -5:
        return QuoteValidationResult(
            None, f"行情时间晚于系统时间 {-age_seconds:.0f} 秒"
        )
    if age_seconds > max_age_seconds:
        return QuoteValidationResult(
            None,
            f"行情已陈旧 {age_seconds:.0f} 秒，超过 {max_age_seconds} 秒",
        )
    suspended = (
        bool(quote.get("is_suspended", False))
        or int(quote.get("trade_status", 1) or 0) == 0
    )
    if suspended:
        return QuoteValidationResult(None, "标的当前停牌或无有效成交")
    if price <= 0 or prev_close <= 0:
        return QuoteValidationResult(None, "实时价格或前收盘无效")

    engine = rules or TradingRules()
    name = str(quote.get("name") or code)
    can_buy, can_sell, limit_type = engine.check_price_limit(
        code,
        price,
        prev_close,
        name=name,
    )
    captured_at = str(quote.get("captured_at") or now.strftime("%Y-%m-%d %H:%M:%S"))
    context = ExecutionQuote(
        code=code,
        name=name,
        current_price=price,
        prev_close=prev_close,
        quote_time=quote_at.strftime("%Y-%m-%d %H:%M:%S"),
        trade_date=execution_date,
        source=str(quote.get("source") or "unknown"),
        captured_at=captured_at,
        validated_at=now.strftime("%Y-%m-%d %H:%M:%S"),
        max_age_seconds=max_age_seconds,
        is_suspended=False,
        can_buy=can_buy,
        can_sell=can_sell,
        limit_type=limit_type,
    )
    return QuoteValidationResult(context)


def order_is_tradable(
    action: Literal["buy", "sell"],
    context: ExecutionQuote,
) -> tuple[bool, str]:
    """按订单方向应用涨跌停限制。"""
    if action == "buy" and not context.can_buy:
        return False, f"标的 {context.code} {context.limit_type}，不可买入"
    if action == "sell" and not context.can_sell:
        return False, f"标的 {context.code} {context.limit_type}，不可卖出"
    return True, ""
