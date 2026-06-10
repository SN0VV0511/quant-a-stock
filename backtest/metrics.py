"""回测绩效指标计算(纯函数,无 IO)。

从 ``backtest/engine.py`` 中沉淀出的公共指标计算,供事件回测与组合策略回测器
共用,避免重复实现夏普/回撤/胜率等口径。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# 无风险年化利率,用于夏普比率
RISK_FREE_RATE = 0.02
# A 股每年约 252 个交易日
TRADING_DAYS_PER_YEAR = 252


def compute_performance_metrics(
    daily_values: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    risk_events: list[dict[str, Any]],
    initial_capital: float,
) -> dict[str, Any]:
    """根据每日净值与成交记录计算回测绩效指标。

    Args:
        daily_values: 每日 ``{date, total_value, cash, position_count}`` 列表(按日期升序)。
        trades: 成交记录,卖出含 ``profit`` 字段用于胜率/盈亏比。
        risk_events: 风控拒绝事件列表。
        initial_capital: 初始资金。

    Returns:
        dict: 含总/年化收益、最大回撤、夏普、卡尔玛、胜率、盈亏比、交易次数等。
    """
    if not daily_values:
        return {}

    df = pd.DataFrame(daily_values)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")

    final_value = float(df["total_value"].iloc[-1])
    total_return = (final_value - initial_capital) / initial_capital

    trading_days = len(df)
    if trading_days > 1:
        annual_return = (1 + total_return) ** (TRADING_DAYS_PER_YEAR / trading_days) - 1
    else:
        annual_return = 0.0

    df["daily_return"] = df["total_value"].pct_change()
    df.loc[df.index[0], "daily_return"] = 0.0

    df["cummax"] = df["total_value"].cummax()
    df["drawdown"] = (df["cummax"] - df["total_value"]) / df["cummax"]
    max_drawdown = float(df["drawdown"].max())

    daily_std = df["daily_return"].std()
    if daily_std and daily_std > 0:
        sharpe = (df["daily_return"].mean() - RISK_FREE_RATE / TRADING_DAYS_PER_YEAR) / daily_std
        sharpe *= np.sqrt(TRADING_DAYS_PER_YEAR)
    else:
        sharpe = 0.0

    calmar = annual_return / max_drawdown if max_drawdown > 0 else 0.0

    winning = [t for t in trades if t.get("profit", 0) > 0]
    losing = [t for t in trades if t.get("profit", 0) < 0]
    closed = len(winning) + len(losing)
    win_rate = len(winning) / closed if closed > 0 else 0.0

    avg_win = float(np.mean([t["profit"] for t in winning])) if winning else 0.0
    avg_loss = abs(float(np.mean([t["profit"] for t in losing]))) if losing else 1.0
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 0.0

    buy_count = len([t for t in trades if t.get("action") == "buy"])
    sell_count = len([t for t in trades if t.get("action") == "sell"])
    total_cost = sum(t.get("cost", 0) for t in trades)
    sell_trades = [t for t in trades if t.get("action") == "sell"]
    gross_profit = sum(max(float(t.get("profit", 0) or 0), 0.0) for t in sell_trades)
    fee_to_gross_profit = total_cost / gross_profit if gross_profit > 0 else 0.0
    average_holding_days = (
        float(np.mean([float(t.get("holding_days", 0) or 0) for t in sell_trades]))
        if sell_trades
        else 0.0
    )
    average_nav = float(df["total_value"].mean()) if not df.empty else 0.0
    traded_amount = sum(float(t.get("amount", 0) or 0) for t in trades)
    turnover_rate = traded_amount / average_nav if average_nav > 0 else 0.0

    fly_away_trades = [
        t for t in sell_trades
        if float(t.get("price", 0) or 0) > 0
        and float(t.get("post_sell_3d_high", 0) or 0) / float(t["price"]) - 1 > 0.05
    ]
    fly_away_rate = len(fly_away_trades) / len(sell_trades) if sell_trades else 0.0

    stop_trades = [
        t for t in sell_trades
        if t.get("sell_reason") in {"CATASTROPHIC_STOP_LOSS", "ATR_STOP_LOSS"}
    ]
    effective_stops = [
        t for t in stop_trades
        if float(t.get("post_sell_3d_close", t.get("price", 0)) or 0)
        < float(t.get("price", 0) or 0)
    ]
    stop_effectiveness = len(effective_stops) / len(stop_trades) if stop_trades else 0.0

    reason_contribution: dict[str, float] = {}
    for trade in sell_trades:
        reason = str(trade.get("sell_reason") or "UNKNOWN")
        reason_contribution[reason] = round(
            reason_contribution.get(reason, 0.0) + float(trade.get("profit", 0) or 0),
            2,
        )

    return {
        "initial_capital": round(initial_capital, 2),
        "final_value": round(final_value, 2),
        "total_return": round(total_return, 4),
        "annual_return": round(annual_return, 4),
        "max_drawdown": round(max_drawdown, 4),
        "sharpe_ratio": round(float(sharpe), 2),
        "calmar_ratio": round(float(calmar), 2),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 2),
        "total_trades": len(trades),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "total_cost": round(total_cost, 2),
        "gross_profit": round(gross_profit, 2),
        "fee_to_gross_profit": round(fee_to_gross_profit, 4),
        "average_holding_days": round(average_holding_days, 2),
        "turnover_rate": round(turnover_rate, 4),
        "fly_away_count": len(fly_away_trades),
        "fly_away_rate": round(fly_away_rate, 4),
        "stop_count": len(stop_trades),
        "stop_effective_count": len(effective_stops),
        "stop_effectiveness": round(stop_effectiveness, 4),
        "sell_reason_contribution": reason_contribution,
        "trading_days": trading_days,
        "risk_events": len(risk_events),
    }


def format_summary(metrics: dict[str, Any]) -> str:
    """将指标字典格式化为可读摘要文本。"""
    if not metrics:
        return "(无回测结果)"
    lines = [
        "=" * 46,
        "          组合策略回测结果摘要",
        "=" * 46,
        f"初始资金:     {metrics['initial_capital']:>12,.2f} 元",
        f"最终市值:     {metrics['final_value']:>12,.2f} 元",
        f"总收益率:     {metrics['total_return']:>12.2%}",
        f"年化收益率:   {metrics['annual_return']:>12.2%}",
        f"最大回撤:     {metrics['max_drawdown']:>12.2%}",
        f"夏普比率:     {metrics['sharpe_ratio']:>12.2f}",
        f"卡尔玛比率:   {metrics['calmar_ratio']:>12.2f}",
        f"胜率:         {metrics['win_rate']:>12.2%}",
        f"盈亏比:       {metrics['profit_factor']:>12.2f}",
        f"交易次数:     {metrics['total_trades']:>12d}",
        f"  买入:       {metrics['buy_count']:>12d}",
        f"  卖出:       {metrics['sell_count']:>12d}",
        f"总交易成本:   {metrics['total_cost']:>12,.2f} 元",
        f"手续费/毛利润: {metrics.get('fee_to_gross_profit', 0):>12.2%}",
        f"平均持仓天数: {metrics.get('average_holding_days', 0):>12.2f} 天",
        f"换手率:       {metrics.get('turnover_rate', 0):>12.2%}",
        f"卖飞率:       {metrics.get('fly_away_rate', 0):>12.2%}",
        f"止损有效率:   {metrics.get('stop_effectiveness', 0):>12.2%}",
        f"交易日数:     {metrics['trading_days']:>12d}",
        f"风控触发:     {metrics['risk_events']:>12d} 次",
        "=" * 46,
    ]
    return "\n".join(lines)
