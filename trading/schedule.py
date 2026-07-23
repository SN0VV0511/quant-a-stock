"""robust_v2 实盘与回测共用的调仓日规则。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence


def rebalance_interval_elapsed(
    current_date: str,
    previous_signal_date: str | None,
    rebalance_days: int,
) -> bool:
    """判断周度、双周或月度调仓周期是否已跨过。

    ``rebalance_days`` 是低频周期档位，不用自然日整除冒充交易日计数。周度档按
    ISO 周边界判断，避免春节等短交易周永远少于七个自然日；月度档按月份边界判断。
    """
    if rebalance_days not in {5, 10, 20}:
        raise ValueError("调仓周期只允许周度、双周或月度")
    if previous_signal_date is None:
        return True
    current = datetime.strptime(current_date, "%Y%m%d").date()
    previous = datetime.strptime(previous_signal_date, "%Y%m%d").date()
    if current <= previous:
        return False
    if rebalance_days == 20:
        current_month = current.year * 12 + current.month
        previous_month = previous.year * 12 + previous.month
        return current_month > previous_month
    current_week = current - timedelta(days=current.weekday())
    previous_week = previous - timedelta(days=previous.weekday())
    required_weeks = 1 if rebalance_days == 5 else 2
    return (current_week - previous_week).days // 7 >= required_weeks


def is_series_week_end(trading_dates: Sequence[str], index: int) -> bool:
    """用点时交易日序列判断某日是否为当周最后一个交易日。"""
    if index < 0 or index >= len(trading_dates):
        raise IndexError("交易日索引越界")
    current = datetime.strptime(trading_dates[index], "%Y%m%d").date()
    if index == len(trading_dates) - 1:
        return True
    following = datetime.strptime(trading_dates[index + 1], "%Y%m%d").date()
    return current.isocalendar()[:2] != following.isocalendar()[:2]


def is_series_month_end(trading_dates: Sequence[str], index: int) -> bool:
    """用点时交易日序列判断某日是否为当月最后一个交易日。"""
    if index < 0 or index >= len(trading_dates):
        raise IndexError("交易日索引越界")
    current = datetime.strptime(trading_dates[index], "%Y%m%d").date()
    if index == len(trading_dates) - 1:
        return True
    following = datetime.strptime(trading_dates[index + 1], "%Y%m%d").date()
    return (current.year, current.month) != (following.year, following.month)


def backtest_rebalance_due(
    trading_dates: Sequence[str],
    index: int,
    previous_signal_date: str | None,
    rebalance_days: int,
) -> bool:
    """按与实盘一致的周期末收盘规则判断回测信号日。"""
    period_end = (
        is_series_month_end(trading_dates, index)
        if rebalance_days == 20
        else is_series_week_end(trading_dates, index)
    )
    return period_end and rebalance_interval_elapsed(
        trading_dates[index], previous_signal_date, rebalance_days
    )
