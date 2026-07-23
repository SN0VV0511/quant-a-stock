"""robust_v2 组合回测和滚动窗口测试。"""

from __future__ import annotations

import pandas as pd
import pytest

from backtest.robust_v2 import (
    BacktestResult,
    RobustParameterSet,
    RobustPortfolioBacktester,
    RobustWalkForwardSelector,
    ValidationWindow,
    build_validation_windows,
    parameter_grid,
)
from trading.models import MarketSnapshot, TargetPortfolio, TargetPosition


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
            "is_suspended": [False] * periods,
            "is_st": [False] * periods,
            "name": [code] * periods,
        }
    )


def _force_single_stock_target(
    monkeypatch: pytest.MonkeyPatch,
    code: str = "600001",
) -> None:
    """固定生成单只股票目标，隔离撮合和估值测试。"""

    def generate_target(
        _strategy: object,
        snapshot: MarketSnapshot,
        *_args: object,
        **_kwargs: object,
    ) -> TargetPortfolio:
        return TargetPortfolio(
            account_id="paper_v2",
            strategy_version="test",
            signal_date=snapshot.trade_date,
            positions=(
                TargetPosition(
                    code=code,
                    name=code,
                    asset_type="stock",
                    target_weight=0.16,
                    reason="TEST_TARGET",
                ),
            ),
            source_snapshot_hash=snapshot.data_hash,
            cash_weight=0.84,
        )

    monkeypatch.setattr(
        "backtest.robust_v2.RobustV2Strategy.generate_target",
        generate_target,
    )


def test_parameter_grid_is_bounded_to_32_groups() -> None:
    """参数搜索不得扩张为无限网格。"""
    grid = parameter_grid()

    assert len(grid) == 32
    assert len(set(grid)) == 32
    assert {params.etf_min_20d_return for params in grid} == {-0.05, 0.0}
    assert {params.stock_min_earnings_yield for params in grid} == {0.02, 0.04}
    assert {params.rebalance_days for params in grid} == {10, 20}
    assert {params.stock_stop_pct for params in grid} == {0.07, 0.09}
    assert {params.max_total_position for params in grid} == {0.70, 0.80}


def test_parameter_set_applies_only_effective_strategy_dimensions() -> None:
    """样本外参数必须写入当前实际参与筛选和调仓的配置。"""
    config = RobustParameterSet(
        etf_min_20d_return=0.0,
        stock_min_earnings_yield=0.04,
        rebalance_days=20,
        stock_stop_pct=0.09,
        max_total_position=0.70,
    ).to_config()

    assert config.etf_min_20d_return == 0.0
    assert config.stock_min_earnings_yield == 0.04
    assert config.rebalance_days == 20
    assert config.stock_stop_pct == 0.09
    assert config.max_total_position == 0.70


def test_walk_forward_fallback_and_pure_etf_use_effective_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无候选过门槛时应回退到保守月度参数，纯 ETF 基线沿用同组维度。"""

    class AlwaysFailBacktester:
        """无需真实行情即可隔离参数选择分支的回测替身。"""

        benchmark_history = pd.DataFrame({"date": ["20200101"], "close": [1.0]})
        trading_dates = ["20200101", "20211231"]

        def __init__(self) -> None:
            self.stock_modes: list[bool] = []

        def run(
            self,
            params: RobustParameterSet,
            start_date: str,
            end_date: str,
            *,
            enable_stock_enhancement: bool = True,
            slippage_multiplier: float = 1.0,
        ) -> BacktestResult:
            self.stock_modes.append(enable_stock_enhancement)
            return BacktestResult(
                params=params,
                start_date=start_date,
                end_date=end_date,
                metrics={
                    "calmar_ratio": 0.0,
                    "sharpe_ratio": 0.0,
                    "max_drawdown": 0.20,
                    "annual_turnover": 1.0,
                    "fee_to_gross_profit": 0.10,
                    "total_return": 0.0,
                },
                daily_values=(),
                trades=(),
                benchmark_return=0.0,
            )

    monkeypatch.setattr(
        "backtest.robust_v2.build_validation_windows",
        lambda _dates: (
            (
                ValidationWindow(
                    train_start="20200101",
                    train_end="20201231",
                    validation_start="20210101",
                    validation_end="20210630",
                ),
            ),
            "20210701",
            "20211231",
        ),
    )
    backtester = AlwaysFailBacktester()

    selection = RobustWalkForwardSelector(backtester).select()  # type: ignore[arg-type]

    assert selection.fallback_to_etf is True
    assert selection.selected_params == RobustParameterSet(
        etf_min_20d_return=0.0,
        stock_min_earnings_yield=0.04,
        rebalance_days=20,
        stock_stop_pct=0.09,
        max_total_position=0.70,
    )
    assert selection.pure_etf_test.params == selection.selected_params
    assert backtester.stock_modes[-1] is False


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
    params = RobustParameterSet(-0.05, 0.02, 10, 0.07, 0.80)

    result = backtester.run(params, dates[260], dates[-1])

    assert result.metrics["total_trades"] > 0
    assert any(trade["code"] == "600001" for trade in result.trades)
    same_day_actions: dict[tuple[str, str], set[str]] = {}
    for trade in result.trades:
        same_day_actions.setdefault((trade["date"], trade["code"]), set()).add(
            trade["action"]
        )
    assert all(actions != {"buy", "sell"} for actions in same_day_actions.values())
    assert result.trades[0]["date"] > dates[260]


@pytest.mark.parametrize(
    ("status_column", "status_value"),
    (("tradestatus", 0), ("is_suspended", True)),
)
def test_t1_buy_does_not_fill_when_execution_day_is_suspended(
    monkeypatch: pytest.MonkeyPatch,
    status_column: str,
    status_value: object,
) -> None:
    """T 日信号后的 T+1 若停牌，不得凭有效开盘价假设成交。"""
    _force_single_stock_target(monkeypatch)
    etfs = {"510300": _history("510300", 0.0, periods=5)}
    stock = _history("600001", 0.0, periods=5)
    dates = etfs["510300"]["date"].tolist()
    stock.loc[stock["date"] == dates[-1], status_column] = status_value
    universe = {date: {"600001"} for date in dates}
    backtester = RobustPortfolioBacktester(
        etfs,
        {"600001": stock},
        universe,
        initial_capital=60_000,
    )

    result = backtester.run(
        RobustParameterSet(-0.05, 0.02, 10, 0.07, 0.80),
        dates[0],
        dates[-1],
    )

    assert result.trades == ()
    assert result.daily_values[-1]["position_count"] == 0


@pytest.mark.parametrize("blocked_reason", ("suspended", "limit_down_open"))
def test_catastrophic_stop_is_deferred_until_stock_can_trade(
    monkeypatch: pytest.MonkeyPatch,
    blocked_reason: str,
) -> None:
    """停牌或开盘跌停时止损延期，恢复可卖后才按当日开盘成交。"""
    _force_single_stock_target(monkeypatch)
    etfs = {"510300": _history("510300", 0.0, periods=7)}
    stock = _history("600001", 0.0, periods=7)
    dates = etfs["510300"]["date"].tolist()
    buy_date, blocked_date, sell_date = dates[4], dates[5], dates[6]

    stock.loc[stock["date"] == blocked_date, ["open", "high", "low", "close"]] = (
        9.0,
        9.0,
        8.9,
        9.0,
    )
    if blocked_reason == "suspended":
        stock.loc[stock["date"] == blocked_date, "is_suspended"] = True
    stock.loc[stock["date"] == sell_date, ["open", "high", "low", "close"]] = (
        8.5,
        8.6,
        8.4,
        8.5,
    )
    universe = {date: {"600001"} for date in dates}
    backtester = RobustPortfolioBacktester(
        etfs,
        {"600001": stock},
        universe,
        initial_capital=60_000,
    )

    result = backtester.run(
        RobustParameterSet(-0.05, 0.02, 10, 0.07, 0.80),
        dates[0],
        dates[-1],
    )

    buys = [trade for trade in result.trades if trade["action"] == "buy"]
    sells = [trade for trade in result.trades if trade["action"] == "sell"]
    assert len(buys) == 1
    assert buys[0]["date"] == buy_date
    assert buys[0]["date"] > dates[3]
    assert buys[0]["price"] == 10.0
    assert buys[0]["cost"] > 0
    assert all(trade["date"] != blocked_date for trade in sells)
    assert len(sells) == 1
    assert sells[0]["date"] == sell_date
    assert sells[0]["price"] == 8.5
    assert sells[0]["sell_reason"] == "CATASTROPHIC_STOP_LOSS"


def test_missing_position_bar_uses_last_point_in_time_close_for_valuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """持仓缺当日 K 线时沿用历史收盘价，并显式记录估值数据缺口。"""
    _force_single_stock_target(monkeypatch)
    etfs = {"510300": _history("510300", 0.0, periods=7)}
    stock = _history("600001", 0.0, periods=7)
    dates = etfs["510300"]["date"].tolist()
    buy_date, missing_date, resumed_date = dates[4], dates[5], dates[6]
    stock = stock.loc[stock["date"] != missing_date].copy()
    universe = {date: {"600001"} for date in dates}
    backtester = RobustPortfolioBacktester(
        etfs,
        {"600001": stock},
        universe,
        initial_capital=60_000,
    )

    result = backtester.run(
        RobustParameterSet(-0.05, 0.02, 10, 0.07, 0.80),
        dates[0],
        dates[-1],
    )

    daily_by_date = {row["date"]: row for row in result.daily_values}
    assert (
        daily_by_date[buy_date]["total_value"]
        == daily_by_date[missing_date]["total_value"]
    )
    assert (
        daily_by_date[missing_date]["total_value"]
        == daily_by_date[resumed_date]["total_value"]
    )
    assert daily_by_date[missing_date]["stale_valuation_codes"] == ["600001"]
    assert daily_by_date[missing_date]["has_valuation_data_gap"] is True
    assert daily_by_date[resumed_date]["stale_valuation_codes"] == []
    assert daily_by_date[resumed_date]["has_valuation_data_gap"] is False


def test_build_validation_windows_reserves_last_twelve_months() -> None:
    """滚动选择必须保留最后 12 个月为完全锁定测试集。"""
    dates = pd.bdate_range("2020-01-02", "2026-06-30").strftime("%Y%m%d").tolist()

    windows, test_start, test_end = build_validation_windows(dates)

    assert windows
    assert all(window.validation_end < test_start for window in windows)
    assert test_end == "20260630"
    months = (pd.Timestamp(test_end) - pd.Timestamp(test_start)).days
    assert 360 <= months <= 370
