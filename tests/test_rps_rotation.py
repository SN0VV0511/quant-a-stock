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
    """RPS 只生成买入信号，卖出由趋势退出统一管理。"""
    pool = {"A": {"name": "强势A"}, "B": {"name": "弱势B"}}
    strategy = RPSRotationStrategy(target_pool=pool, lookback=20, top_n=1, min_rps=50)
    history = {
        "A": _hist("A", 0.30),
        "B": _hist("B", -0.10),
    }
    portfolio = {"B": {"name": "弱势B", "shares": 100, "current_price": 9.0}}
    orders = strategy.generate_orders(history, portfolio, "20260228")
    # RPS 不再生成卖单，只生成买入信号
    assert all(o["action"] == "buy" for o in orders)
    assert any(o["code"] == "A" for o in orders)
    # B 虽然不在 top N 但不会被 RPS 卖出
    assert not any(o["code"] == "B" for o in orders)


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


def test_small_pool_selects_top_n_not_blocked_by_percentile() -> None:
    """回归 B2:小池子不应被高分位阈值卡死。

    10 只均上涨且站上 MA20 的标的、top_n=3:旧逻辑按横截面分位 + min_rps=85
    只能选出分位 >=85 的 2 只(分位最高也仅 ~89);新逻辑用"趋势+绝对正动量"
    过滤,应按动量降序选满 3 只。
    """
    history = {f"S{i}": _hist(f"S{i}", 0.05 + i * 0.03) for i in range(10)}
    ranked = calculate_rps_scores(history, lookback=20, top_n=3)
    assert len(ranked) == 3
    assert [r["code"] for r in ranked] == ["S9", "S8", "S7"]


def test_downtrend_pool_selects_nothing() -> None:
    """普跌池:全部下跌时绝对动量过滤应使其空仓避险(不被相对最强者诱导买入)。"""
    history = {f"D{i}": _hist(f"D{i}", -0.02 - i * 0.02) for i in range(5)}
    ranked = calculate_rps_scores(history, lookback=20, top_n=3)
    assert ranked == []
