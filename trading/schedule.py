"""robust_v2 实盘与回测共用的调仓日规则。"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence


def rebalance_interval_elapsed(
    current_date: str,
    previous_signal_date: str | None,
    rebalance_days: int,
) -> bool:
    """判断周度或双周调仓间隔是否已满足。"""
    if rebalance_days not in {5, 10}:
        raise ValueError("调仓周期只允许 5 或 10 个交易日")
    if previous_signal_date is None:
        return True
    current = datetime.strptime(current_date, "%Y%m%d").date()
    previous = datetime.strptime(previous_signal_date, "%Y%m%d").date()
    required_weeks = 1 if rebalance_days == 5 else 2
    return (current - previous).days // 7 >= required_weeks


def is_series_week_end(trading_dates: Sequence[str], index: int) -> bool:
    """用点时交易日序列判断某日是否为当周最后一个交易日。"""
    if index < 0 or index >= len(trading_dates):
        raise IndexError("交易日索引越界")
    current = datetime.strptime(trading_dates[index], "%Y%m%d").date()
    if index == len(trading_dates) - 1:
        return True
    following = datetime.strptime(trading_dates[index + 1], "%Y%m%d").date()
    return current.isocalendar()[:2] != following.isocalendar()[:2]


def backtest_rebalance_due(
    trading_dates: Sequence[str],
    index: int,
    previous_signal_date: str | None,
    rebalance_days: int,
) -> bool:
    """按与实盘一致的周末收盘规则判断回测信号日。"""
    return is_series_week_end(trading_dates, index) and rebalance_interval_elapsed(
        trading_dates[index],
        previous_signal_date,
        rebalance_days,
    )
