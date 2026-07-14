"""实盘与回测共享调仓日规则测试。"""

from __future__ import annotations

from trading.schedule import backtest_rebalance_due, rebalance_interval_elapsed


def test_backtest_only_signals_on_last_trading_day_of_week() -> None:
    """回测不得从样本起点机械每五天调仓，应与实盘周末收盘口径一致。"""
    dates = [
        "20260706",
        "20260707",
        "20260708",
        "20260709",
        "20260710",
        "20260713",
    ]

    assert backtest_rebalance_due(dates, 0, None, 5) is False
    assert backtest_rebalance_due(dates, 4, None, 5) is True


def test_double_week_interval_matches_live_calendar_rule() -> None:
    """双周策略必须跨满两个自然周才允许再次产生目标。"""
    assert rebalance_interval_elapsed("20260717", "20260710", 10) is False
    assert rebalance_interval_elapsed("20260724", "20260710", 10) is True
