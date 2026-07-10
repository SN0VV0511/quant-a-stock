"""robust_v2 组合回测和滚动窗口测试。"""

from __future__ import annotations

import pandas as pd

from backtest.robust_v2 import (
    RobustParameterSet,
    RobustPortfolioBacktester,
    build_validation_windows,
    parameter_grid,
)


def _history(code: str, slope: float, periods: int = 340) -> pd.DataFrame:
    """生成可交易的确定性历史数据。"""
    dates = pd.bdate_range("2024-01-02", periods=periods)
    close = [10 + slope * index for index in range(periods)]
    return pd.DataFrame(
        {
            "date": dates.strftime("%Y%m%d"),
            "open": close,
            "high": [value * 1.01 for value in close],
            "low": [value * 0.99 for value in close],
            "close": close,
            "volume": [20_000_000] * periods,
            "amount": [200_000_000] * periods,
            "pb": [1.0] * periods,
            "mktcap": [8_000_000_000.0] * periods,
            "peTTM": [12.0] * periods,
            "roe": [10.0] * periods,
            "tradestatus": [1] * periods,
            "is_st": [False] * periods,
            "name": [code] * periods,
        }
    )


def test_parameter_grid_is_bounded_to_32_groups() -> None:
    """参数搜索不得扩张为无限网格。"""
    grid = parameter_grid()
    assert len(grid) == 32
    assert len(set(grid)) == 32


def test_backtest_uses_next_day_open_and_never_round_trips_same_day() -> None:
    """回测成交必须晚于信号日，且同一标的不出现当日买卖。"""
    etfs = {
        "510300": _history("510300", 0.03),
        "510500": _history("510500", 0.025),
    }
    stocks = {"600001": _history("600001", 0.015)}
    dates = etfs["510300"]["date"].tolist()
    universe = {date: {"600001"} for date in dates}
    backtester = RobustPortfolioBacktester(etfs, stocks, universe)
    params = RobustParameterSet(True, 20, 5, 0.07, 0.80)

    result = backtester.run(params, dates[260], dates[-1])

    assert result.metrics["total_trades"] > 0
    same_day_actions: dict[tuple[str, str], set[str]] = {}
    for trade in result.trades:
        same_day_actions.setdefault((trade["date"], trade["code"]), set()).add(
            trade["action"]
        )
    assert all(actions != {"buy", "sell"} for actions in same_day_actions.values())
    assert result.trades[0]["date"] > dates[260]


def test_build_validation_windows_reserves_last_twelve_months() -> None:
    """滚动选择必须保留最后 12 个月为完全锁定测试集。"""
    dates = pd.bdate_range("2020-01-02", "2026-06-30").strftime("%Y%m%d").tolist()

    windows, test_start, test_end = build_validation_windows(dates)

    assert windows
    assert all(window.validation_end < test_start for window in windows)
    assert test_end == "20260630"
    months = (pd.Timestamp(test_end) - pd.Timestamp(test_start)).days
    assert 360 <= months <= 370
