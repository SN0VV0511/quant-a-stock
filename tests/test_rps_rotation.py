"""ETF / 行业代理 RPS 轮动策略测试。"""

import numpy as np
import pandas as pd

from strategies.rps_rotation import RPSRotationStrategy, calculate_rps_scores


def _hist(code: str, trend: float, n: int = 40, volume: int = 1_000_000) -> pd.DataFrame:
    """构造带日期的历史行情。"""
    dates = [d.strftime("%Y%m%d") for d in pd.date_range("2026-01-01", periods=n, freq="B")]
    closes = np.linspace(10, 10 * (1 + trend), n)
    return pd.DataFrame({
        "date": dates,
        "close": closes,
        "volume": [volume] * n,
        "name": [code] * n,
    })


def test_rps_scores_rank_relative_strength() -> None:
    history = {
        "A": _hist("A", 0.30),
        "B": _hist("B", 0.10),
        "C": _hist("C", -0.05),
    }
    ranked = calculate_rps_scores(history, lookback=20, top_n=2, min_rps=50)
    assert [r["code"] for r in ranked] == ["A", "B"]
    assert ranked[0]["rps"] == 100.0


def test_rps_filters_low_volume() -> None:
    history = {
        "A": _hist("A", 0.30, volume=1_000_000),
        "B": _hist("B", 0.40, volume=100),
    }
    ranked = calculate_rps_scores(history, lookback=20, top_n=2, min_rps=0, min_avg_volume=500_000)
    assert [r["code"] for r in ranked] == ["A"]


def test_generate_orders_daily_rotates_selected_pool() -> None:
    pool = {"A": {"name": "强势A"}, "B": {"name": "弱势B"}}
    strategy = RPSRotationStrategy(target_pool=pool, lookback=20, top_n=1, min_rps=50)
    history = {
        "A": _hist("A", 0.30),
        "B": _hist("B", -0.10),
    }
    portfolio = {"B": {"name": "弱势B", "shares": 100, "current_price": 9.0}}
    orders = strategy.generate_orders(history, portfolio, "20260228")
    assert any(o["action"] == "sell" and o["code"] == "B" for o in orders)
    assert any(o["action"] == "buy" and o["code"] == "A" for o in orders)


def test_industry_index_rps_signals_do_not_generate_orders() -> None:
    """行业指数可用于强弱观察,但不能被当成可交易标的下单。"""
    pool = {
        "证券": {"name": "证券", "asset_type": "industry_index"},
        "半导体": {"name": "半导体", "asset_type": "industry_index"},
    }
    strategy = RPSRotationStrategy(target_pool=pool, lookback=20, top_n=1, min_rps=50)
    history = {
        "证券": _hist("证券", 0.30),
        "半导体": _hist("半导体", -0.10),
    }

    signals = strategy.calculate_signals(history, "20260228")
    orders = strategy.generate_orders(history, {}, "20260228")

    assert [s["code"] for s in signals] == ["证券"]
    assert orders == []
