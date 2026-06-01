"""持仓退出规则(纯函数,无 IO)。

将"何时离场"的判定从 live_runner 与回测器中抽出集中管理,保证实盘与回测使用
**完全一致**的退出优先级与阈值,避免两边逻辑漂移导致回测失真。

退出优先级(自上而下,命中即返回):
    1. 策略卖出:Combo 给出 sell(RSI 超买 / 死叉 / 跌破长均线)
    2. 硬止损:亏损达 ``STOP_LOSS_PCT``
    3. 移动止损:峰值相对成本盈利达 ``TRAILING_ACTIVATE_PCT`` 后,自峰值回撤达
       ``TRAILING_STOP_PCT``(锁住趋势利润,避免大幅回吐)
    4. 时间止损:持有达 ``TIME_STOP_DAYS`` 自然日且盈亏低于 ``TIME_STOP_MIN_PROFIT``
       (默认禁用)
    5. 止盈:盈利达 ``TAKE_PROFIT_PCT`` 且跌破 MA20
"""

from __future__ import annotations

from config.settings import (
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    TRAILING_STOP_PCT,
    TRAILING_ACTIVATE_PCT,
    ENABLE_TRAILING_STOP,
    TIME_STOP_DAYS,
    TIME_STOP_MIN_PROFIT,
)


def evaluate_exit(
    avg_cost: float,
    price: float,
    peak_price: float | None = None,
    ma20: float | None = None,
    holding_days: int = 0,
    combo_sell: bool = False,
    combo_reason: str = "",
) -> tuple[str, str] | None:
    """判断持仓是否应离场。

    Args:
        avg_cost: 持仓均价。
        price: 当前价格。
        peak_price: 持仓期间最高价(用于移动止损);None 视为不启用移动止损。
        ma20: 20 日均线(用于止盈确认);None 时止盈条件不满足。
        holding_days: 已持有自然日数(用于时间止损)。
        combo_sell: Combo 策略是否给出卖出信号。
        combo_reason: Combo 卖出原因(用于日志)。

    Returns:
        (策略名, 原因) 或 None(不离场)。
    """
    if avg_cost <= 0 or price <= 0:
        return None

    pnl_pct = (price - avg_cost) / avg_cost

    # 1. 策略卖出
    if combo_sell:
        return "策略卖出", combo_reason or "策略信号"

    # 2. 硬止损
    if pnl_pct <= -STOP_LOSS_PCT:
        return "止损", f"止损,亏损 {pnl_pct:.2%}"

    # 3. 移动止损:仅在峰值盈利越过激活线后启用
    if (
        ENABLE_TRAILING_STOP
        and peak_price
        and peak_price >= avg_cost * (1 + TRAILING_ACTIVATE_PCT)
        and price <= peak_price * (1 - TRAILING_STOP_PCT)
    ):
        drop = (peak_price - price) / peak_price
        return "移动止损", f"自峰值回撤 {drop:.2%}(峰值 {peak_price:.3f})"

    # 4. 时间止损(默认禁用)
    if (
        TIME_STOP_DAYS > 0
        and holding_days >= TIME_STOP_DAYS
        and pnl_pct < TIME_STOP_MIN_PROFIT
    ):
        return "时间止损", f"持有 {holding_days} 日未达预期(盈亏 {pnl_pct:.2%})"

    # 5. 止盈:盈利达标且跌破 MA20
    if pnl_pct >= TAKE_PROFIT_PCT and ma20 and price < ma20:
        return "止盈", f"止盈 {pnl_pct:.2%} 且跌破 MA20"

    return None
