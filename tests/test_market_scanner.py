"""选股打分纯函数 score_candidates 测试:排序、量纲标准化与边界。"""

import numpy as np
import pandas as pd

from strategies import market_scanner
from strategies.market_scanner import MarketScanner, score_candidates, _zscore


def _hist(trend_pct, n=70, volume=4_000_000, base=10.0):
    """构造收盘价从 base 线性变动 trend_pct 的历史数据。"""
    closes = np.linspace(base, base * (1 + trend_pct), n)
    return pd.DataFrame({"close": closes, "volume": [volume] * n})


def test_higher_momentum_ranks_first():
    history = {
        "600000": _hist(0.5),    # 强动量
        "600001": _hist(0.05),   # 弱动量
        "600002": _hist(-0.2),   # 负动量
    }
    ranked = score_candidates(history, top_n=3, momentum_period=60)
    assert ranked[0]["code"] == "600000"
    assert ranked[-1]["code"] == "600002"


def test_low_volume_filtered_out():
    history = {
        "600000": _hist(0.5, volume=4_000_000),
        "600001": _hist(0.5, volume=100),   # 均量过低,应被剔除
    }
    ranked = score_candidates(history, top_n=5, momentum_period=60, min_avg_volume=500_000)
    codes = {r["code"] for r in ranked}
    assert "600000" in codes
    assert "600001" not in codes


def test_restricted_board_filtered_out():
    history = {
        "600000": _hist(0.1),
        "688981": _hist(0.8),
        "300750": _hist(0.9),
    }
    ranked = score_candidates(history, top_n=5, momentum_period=60)
    codes = {r["code"] for r in ranked}
    assert codes == {"600000"}


def test_insufficient_history_skipped():
    history = {"600000": _hist(0.5, n=30)}  # 不足 momentum_period+1
    ranked = score_candidates(history, top_n=5, momentum_period=60)
    assert ranked == []


def test_empty_returns_empty():
    assert score_candidates({}, top_n=5) == []


def test_top_n_limit_and_rank_assigned():
    history = {f"60000{i}": _hist(0.5 - i * 0.05) for i in range(5)}
    ranked = score_candidates(history, top_n=3, momentum_period=60)
    assert len(ranked) == 3
    assert [r["rank"] for r in ranked] == [1, 2, 3]


def test_ma_passed_priority():
    """均线多头的标的应优先于非多头,即使后者得分项不低。"""
    history = {
        "600000": _hist(0.5),    # 上行,close>ma20>ma60,ma_passed=True
        "600002": _hist(-0.2),   # 下行,ma_passed=False
    }
    ranked = score_candidates(history, top_n=2, momentum_period=60)
    assert ranked[0]["ma_passed"] is True


def test_zscore_constant_returns_zeros():
    out = _zscore(np.array([5.0, 5.0, 5.0]))
    assert np.allclose(out, 0.0), "全相同输入的 z-score 应为 0,不得除零"


def test_zscore_standardizes():
    out = _zscore(np.array([1.0, 2.0, 3.0]))
    assert abs(out.mean()) < 1e-9
    assert out[0] < out[1] < out[2]


def test_full_market_scan_does_not_truncate_history_targets(monkeypatch):
    """全盘模式下实时粗筛通过的股票必须全部进入历史加载。"""

    class FakeLoader:
        """记录扫描器传入历史加载阶段的完整代码列表。"""

        def __init__(self):
            self.history_codes = []

        def get_realtime_quotes(self, codes):
            return {
                code: {
                    "price": 10.0,
                    "pct_change": 1.0,
                    "volume": 2_000_000,
                }
                for code in codes
            }

        def get_batch_history(self, codes, days):
            self.history_codes = list(codes)
            return {}

    loader = FakeLoader()
    stocks = [
        {"code": f"60{index:04d}", "name": f"股票{index}"}
        for index in range(25)
    ]
    monkeypatch.setattr(market_scanner, "SCAN_MAX_HIST_FETCH", 0)
    monkeypatch.setattr("config.settings.SCAN_ENABLE_FUNDAMENTAL_FILTER", False)

    result = MarketScanner(loader=loader).scan(stocks, top_n=5)

    assert result == []
    assert loader.history_codes == [stock["code"] for stock in stocks]
