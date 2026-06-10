"""按策略标签分层的持仓退出规则。

本模块只负责决策，不执行 IO。所有股票策略共享灾难止损和 T+1 约束，
但趋势、涨停延续、小盘价值和 ETF 使用不同的退出策略，避免短线指标误杀。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from config.settings import (
    ATR_STOP_MAX_PCT,
    ATR_STOP_MULTIPLIER,
    COMBO_DEFENSIVE_PROFIT_THRESHOLD,
    ENABLE_ATR_STOP,
    ENABLE_TRAILING_STOP,
    ETF_EXTREME_STOP_PCT,
    LIMITUP_INTRADAY_DRAWDOWN,
    STOP_LOSS_PCT,
    TIME_STOP_DAYS,
    TIME_STOP_MIN_PROFIT,
    TRAILING_GIVEBACK_NORMAL,
    TRAILING_GIVEBACK_RSI75,
    TRAILING_GIVEBACK_RSI80,
    TRAILING_PROFIT_ACTIVATE,
)

ExitAction = Literal["hold", "sell"]


@dataclass(frozen=True)
class ExitDecision:
    """结构化退出决策。"""

    action: ExitAction
    sell_reason: str
    detail: str
    indicators: dict[str, Any] = field(default_factory=dict)

    @property
    def should_sell(self) -> bool:
        """返回是否应提交卖单。"""
        return self.action == "sell"


def get_exit_policy(strategy_tag: str) -> str:
    """根据策略标签返回退出策略名称。"""
    if strategy_tag in {"etf_rotation", "rps_rotation"}:
        return "etf_rotation"
    if strategy_tag == "smallcap_value":
        return "smallcap_value"
    if strategy_tag == "limitup_follow":
        return "limitup_follow"
    if strategy_tag == "momentum_breakout":
        return "momentum_breakout"
    return "combo_trend"


def _valid_positive(value: float | None) -> bool:
    """判断数值是否可用于风控计算。"""
    return value is not None and value > 0


def _trailing_giveback(rsi: float | None) -> float:
    """RSI 只用于收紧移动止盈，不直接触发卖出。"""
    if rsi is not None and rsi > 80:
        return TRAILING_GIVEBACK_RSI80
    if rsi is not None and rsi > 75:
        return TRAILING_GIVEBACK_RSI75
    return TRAILING_GIVEBACK_NORMAL


def _combo_has_defensive_signal(combo_sell: bool, combo_reason: str) -> bool:
    """过滤仅由 RSI 超买产生的 Combo 卖出信号。"""
    if not combo_sell:
        return False
    reasons = [part.strip() for part in combo_reason.split("+") if part.strip()]
    return any("RSI" not in reason for reason in reasons) if reasons else True


def evaluate_position_exit(
    *,
    avg_cost: float,
    price: float,
    strategy_tag: str = "combo_trend",
    sellable_qty: int = 1,
    highest_price: float | None = None,
    intraday_high_price: float | None = None,
    ma20: float | None = None,
    ma60: float | None = None,
    atr: float | None = None,
    rsi: float | None = None,
    holding_days: int = 0,
    combo_sell: bool = False,
    combo_reason: str = "",
    previous_close: float | None = None,
    vwap: float | None = None,
    below_vwap_minutes: int = 0,
    rebalance_exit: bool = False,
) -> ExitDecision:
    """按策略标签和统一优先级判断是否退出。

    优先级为 T+1、灾难止损、ATR 硬止损、移动止盈、趋势破坏、
    Combo 防守、时间止损、策略调仓。
    """
    indicators = {
        "price": price,
        "avg_cost": avg_cost,
        "highest_price": highest_price,
        "intraday_high_price": intraday_high_price,
        "ma20": ma20,
        "ma60": ma60,
        "atr": atr,
        "rsi": rsi,
        "vwap": vwap,
        "below_vwap_minutes": below_vwap_minutes,
    }
    if sellable_qty <= 0:
        return ExitDecision(
            "hold",
            "T1_LOCKED",
            "T+1 locked, signal recorded but cannot sell",
            indicators,
        )
    if avg_cost <= 0 or price <= 0:
        return ExitDecision("hold", "INVALID_PRICE", "成本价或当前价无效", indicators)

    policy = get_exit_policy(strategy_tag)
    pnl_pct = (price - avg_cost) / avg_cost
    indicators["profit_pct"] = pnl_pct
    indicators["exit_policy"] = policy

    # 1. 灾难止损。ETF 仅保留更宽的极端风险线。
    disaster_pct = ETF_EXTREME_STOP_PCT if policy == "etf_rotation" else STOP_LOSS_PCT
    if pnl_pct <= -disaster_pct:
        return ExitDecision(
            "sell",
            "CATASTROPHIC_STOP_LOSS",
            f"亏损 {pnl_pct:.2%} 达灾难止损线 {-disaster_pct:.2%}",
            indicators,
        )

    # 2. ATR 硬止损。ETF 不使用股票 ATR 日内止损。
    if policy != "etf_rotation" and ENABLE_ATR_STOP and _valid_positive(atr):
        atr_distance_pct = min(ATR_STOP_MULTIPLIER * float(atr) / avg_cost, ATR_STOP_MAX_PCT)
        stop_price = avg_cost * (1 - atr_distance_pct)
        indicators["atr_stop_price"] = stop_price
        if price <= stop_price:
            return ExitDecision(
                "sell",
                "ATR_STOP_LOSS",
                f"价格 {price:.3f} 跌破 ATR 止损线 {stop_price:.3f}",
                indicators,
            )

    # 3. 移动止盈。价值和 ETF 不使用股票式移动止盈。
    if policy == "limitup_follow":
        intraday_high = max(float(intraday_high_price or price), price)
        peak_profit_pct = (intraday_high - avg_cost) / avg_cost
        drawdown = (intraday_high - price) / intraday_high if intraday_high > 0 else 0.0
        indicators["intraday_peak_profit_pct"] = peak_profit_pct
        indicators["intraday_drawdown_pct"] = drawdown
        limit_price = previous_close * 1.10 if _valid_positive(previous_close) else None
        near_limit_up = limit_price is not None and price >= limit_price * 0.995
        if (
            peak_profit_pct >= TRAILING_PROFIT_ACTIVATE
            and drawdown >= LIMITUP_INTRADAY_DRAWDOWN
            and not near_limit_up
        ):
            return ExitDecision(
                "sell",
                "LIMITUP_FOLLOW_TRAILING_STOP",
                f"日内高点回撤 {drawdown:.2%} 达阈值 {LIMITUP_INTRADAY_DRAWDOWN:.2%}",
                indicators,
            )
    elif policy in {"combo_trend", "momentum_breakout"} and ENABLE_TRAILING_STOP:
        peak = max(float(highest_price or price), price)
        if peak >= avg_cost * (1 + TRAILING_PROFIT_ACTIVATE):
            giveback_ratio = _trailing_giveback(rsi)
            stop_line = max(peak - (peak - avg_cost) * giveback_ratio, avg_cost)
            indicators["trailing_giveback"] = giveback_ratio
            indicators["trailing_stop_price"] = stop_line
            if price <= stop_line:
                return ExitDecision(
                    "sell",
                    "TRAILING_TAKE_PROFIT",
                    f"峰值 {peak:.3f} 后回吐 {giveback_ratio:.0%}，跌破 {stop_line:.3f}",
                    indicators,
                )

    # 4. 趋势破坏。价值策略不使用短线均线，ETF 使用专属 MA20/MA60 条件。
    if policy == "etf_rotation":
        if (_valid_positive(ma20) and price < float(ma20)) or (
            _valid_positive(ma20) and _valid_positive(ma60) and float(ma20) < float(ma60)
        ):
            return ExitDecision(
                "sell",
                "ETF_ROTATION_EXIT",
                "ETF 跌破 MA20 或 MA20 低于 MA60",
                indicators,
            )
    elif policy == "limitup_follow":
        if _valid_positive(vwap) and price < float(vwap) and below_vwap_minutes >= 3:
            return ExitDecision(
                "sell",
                "VWAP_BREAK",
                f"跌破 VWAP {vwap:.3f} 持续 {below_vwap_minutes} 分钟",
                indicators,
            )
    elif policy in {"combo_trend", "momentum_breakout"}:
        if pnl_pct >= COMBO_DEFENSIVE_PROFIT_THRESHOLD and _valid_positive(ma20) and price < float(ma20):
            return ExitDecision(
                "sell",
                "TREND_BREAK_EXIT",
                f"盈利持仓跌破 MA20 {ma20:.3f}",
                indicators,
            )

    # 5. Combo 仅在浮亏或微利时防守，且 RSI 超买本身不能清仓。
    if (
        policy in {"combo_trend", "momentum_breakout"}
        and pnl_pct < COMBO_DEFENSIVE_PROFIT_THRESHOLD
        and _combo_has_defensive_signal(combo_sell, combo_reason)
    ):
        return ExitDecision(
            "sell",
            "COMBO_DEFENSIVE_EXIT",
            combo_reason or "Combo 弱势防守退出",
            indicators,
        )

    # 6. 时间止损。
    if (
        policy != "etf_rotation"
        and TIME_STOP_DAYS > 0
        and holding_days >= TIME_STOP_DAYS
        and pnl_pct < TIME_STOP_MIN_PROFIT
    ):
        return ExitDecision(
            "sell",
            "TIME_STOP",
            f"持有 {holding_days} 日未达预期，盈亏 {pnl_pct:.2%}",
            indicators,
        )

    # 7. 策略调仓或排名退出。
    if rebalance_exit:
        reason = "ETF_ROTATION_EXIT" if policy == "etf_rotation" else "STRATEGY_REBALANCE_EXIT"
        return ExitDecision("sell", reason, "策略调仓移出目标池", indicators)

    return ExitDecision("hold", "HOLD", "未触发退出条件", indicators)


def evaluate_exit(
    avg_cost: float,
    price: float,
    peak_price: float | None = None,
    ma20: float | None = None,
    atr: float | None = None,
    holding_days: int = 0,
    combo_sell: bool = False,
    combo_reason: str = "",
    **kwargs: Any,
) -> tuple[str, str] | None:
    """兼容旧调用方的元组返回接口。"""
    decision = evaluate_position_exit(
        avg_cost=avg_cost,
        price=price,
        highest_price=peak_price,
        ma20=ma20,
        atr=atr,
        holding_days=holding_days,
        combo_sell=combo_sell,
        combo_reason=combo_reason,
        **kwargs,
    )
    if not decision.should_sell:
        return None
    return decision.sell_reason, decision.detail
