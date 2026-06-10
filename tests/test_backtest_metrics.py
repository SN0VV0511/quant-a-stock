"""回测绩效指标 compute_performance_metrics 测试:已知序列验证口径正确。"""

import numpy as np

from backtest.metrics import compute_performance_metrics


def _daily(values):
    days = [f"2024{m:02d}{d:02d}" for m in range(1, 2) for d in range(1, len(values) + 1)]
    return [
        {"date": day, "total_value": v, "cash": v, "position_count": 0}
        for day, v in zip(days, values)
    ]


def test_empty_returns_empty():
    assert compute_performance_metrics([], [], [], 100_000) == {}


def test_total_return_and_final_value():
    dv = _daily([100_000, 110_000, 121_000])
    m = compute_performance_metrics(dv, [], [], 100_000)
    assert m["final_value"] == 121_000
    assert abs(m["total_return"] - 0.21) < 1e-6


def test_max_drawdown():
    # 峰值 120k 后回落到 90k,最大回撤 = (120-90)/120 = 25%
    dv = _daily([100_000, 120_000, 90_000, 95_000])
    m = compute_performance_metrics(dv, [], [], 100_000)
    assert abs(m["max_drawdown"] - 0.25) < 1e-6


def test_win_rate_and_profit_factor():
    trades = [
        {"action": "sell", "profit": 100},
        {"action": "sell", "profit": 300},
        {"action": "sell", "profit": -200},
    ]
    m = compute_performance_metrics(_daily([100_000, 100_200]), trades, [], 100_000)
    assert abs(m["win_rate"] - 2 / 3) < 1e-3  # 指标四舍五入到 4 位
    # 平均盈利 200,平均亏损 200,盈亏比 = 1.0
    assert abs(m["profit_factor"] - 1.0) < 1e-6


def test_zero_volatility_sharpe_is_zero():
    dv = _daily([100_000, 100_000, 100_000])
    m = compute_performance_metrics(dv, [], [], 100_000)
    assert m["sharpe_ratio"] == 0.0


def test_trade_counts():
    trades = [
        {"action": "buy", "cost": 5},
        {"action": "buy", "cost": 5},
        {"action": "sell", "profit": 10, "cost": 5},
    ]
    m = compute_performance_metrics(_daily([100_000, 100_010]), trades, [], 100_000)
    assert m["buy_count"] == 2
    assert m["sell_count"] == 1
    assert m["total_trades"] == 3
    assert m["total_cost"] == 15


def test_review_metrics_and_reason_contribution():
    """复盘指标应覆盖卖飞、止损有效率、持仓天数、换手和原因贡献。"""
    trades = [
        {"action": "buy", "amount": 10_000, "cost": 5},
        {
            "action": "sell",
            "price": 10,
            "amount": 11_000,
            "profit": 1_000,
            "cost": 5,
            "holding_days": 5,
            "sell_reason": "TRAILING_TAKE_PROFIT",
            "post_sell_3d_high": 10.6,
            "post_sell_3d_close": 10.4,
        },
        {
            "action": "sell",
            "price": 9,
            "amount": 9_000,
            "profit": -500,
            "cost": 5,
            "holding_days": 3,
            "sell_reason": "ATR_STOP_LOSS",
            "post_sell_3d_high": 9.1,
            "post_sell_3d_close": 8.5,
        },
    ]
    metrics = compute_performance_metrics(_daily([100_000, 100_500]), trades, [], 100_000)
    assert metrics["fly_away_rate"] == 0.5
    assert metrics["stop_effectiveness"] == 1.0
    assert metrics["average_holding_days"] == 4.0
    assert metrics["turnover_rate"] > 0
    assert metrics["fee_to_gross_profit"] == 0.015
    assert metrics["sell_reason_contribution"]["ATR_STOP_LOSS"] == -500
