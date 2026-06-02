"""小市值价值策略测试:打分排序、国九条风控过滤、周度调仓回测。"""

import numpy as np
import pandas as pd

from strategies.small_cap_value import build_factor_rows, score_small_cap_value, passes_risk_filter
from backtest.factor_backtest import FactorBacktester


def _row(code, mktcap, pb, reversal=0.0, price=10.0, is_st=False, is_suspended=False):
    return {
        "code": code, "name": code, "price": price, "mktcap": mktcap,
        "pb": pb, "reversal": reversal, "is_st": is_st, "is_suspended": is_suspended,
    }


class TestRiskFilter:
    def test_st_excluded(self):
        assert not passes_risk_filter(_row("600000", 30e8, 1.0, is_st=True))

    def test_suspended_excluded(self):
        assert not passes_risk_filter(_row("600000", 30e8, 1.0, is_suspended=True))

    def test_low_price_excluded(self):
        assert not passes_risk_filter(_row("600000", 30e8, 1.0, price=1.5))

    def test_high_price_excluded_for_small_capital(self):
        assert not passes_risk_filter(_row("600000", 30e8, 1.0, price=35.0))

    def test_negative_pb_excluded(self):
        assert not passes_risk_filter(_row("600000", 30e8, -1.0))

    def test_mktcap_out_of_band_excluded(self):
        assert not passes_risk_filter(_row("600000", 5e8, 1.0))    # 太小
        assert not passes_risk_filter(_row("600000", 500e8, 1.0))  # 太大

    def test_valid_passes(self):
        assert passes_risk_filter(_row("600000", 50e8, 1.2))


class TestScoring:
    def test_small_cheap_reversal_ranks_first(self):
        rows = [
            _row("600001", 30e8, 1.0, reversal=-0.15),   # 小+便宜+超跌 → 最优
            _row("600002", 150e8, 3.0, reversal=+0.15),  # 大+贵+超涨 → 最差
            _row("600003", 80e8, 2.0, reversal=0.0),
        ]
        ranked = score_small_cap_value(rows, top_n=3)
        assert ranked[0]["code"] == "600001"
        assert ranked[-1]["code"] == "600002"

    def test_top_n_and_rank(self):
        rows = [_row(f"60000{i}", (20 + i * 10) * 1e8, 1.0 + i * 0.2) for i in range(6)]
        ranked = score_small_cap_value(rows, top_n=3)
        assert len(ranked) == 3
        assert [r["rank"] for r in ranked] == [1, 2, 3]

    def test_all_filtered_returns_empty(self):
        rows = [_row("600000", 30e8, 1.0, is_st=True)]
        assert score_small_cap_value(rows) == []

    def test_empty_returns_empty(self):
        assert score_small_cap_value([]) == []

    def test_obv_bonus_improves_rank_without_hard_filter(self):
        rows = [
            _row("600001", 50e8, 1.5, reversal=-0.05) | {"obv_trend": -5.0},
            _row("600002", 50e8, 1.5, reversal=-0.05) | {"obv_trend": 5.0},
        ]
        ranked = score_small_cap_value(rows, top_n=2, weights=(0.0, 0.0, 0.0, 1.0))
        assert ranked[0]["code"] == "600002"
        assert {r["code"] for r in ranked} == {"600001", "600002"}

    def test_build_factor_rows_contains_obv_trend(self):
        days = _bdays(25)
        df = _factor_frame(days, np.linspace(10, 12, 25), 30e8, 1.0)
        rows = build_factor_rows({"600001": df}, reversal_days=20)
        assert rows and "obv_trend" in rows[0]
        assert rows[0]["obv_trend"] > 0


def _factor_frame(dates, close, mktcap, pb):
    n = len(dates)
    closes = list(close)
    return pd.DataFrame({
        "date": dates,
        "open": [closes[0]] + closes[:-1],
        "close": closes,
        "volume": [3_000_000] * n,
        "pb": [pb] * n,
        "mktcap": [mktcap] * n,
        "is_st": [0] * n,
        "tradestatus": [1] * n,
    })


def _bdays(n):
    return [d.strftime("%Y%m%d") for d in pd.date_range("2024-01-01", periods=n, freq="B")]


class TestFactorBacktester:
    """注意:当前小资金风控会按单票 15%、总仓 60% 控制调仓目标。"""

    @staticmethod
    def _universe(days):
        n = len(days)
        # 6 只:前 5 只小/中盘且便宜,600006 大盘且贵(应被 top5 排除)
        specs = {
            "600001": (25e8, 1.0), "600002": (30e8, 1.2), "600003": (45e8, 1.5),
            "600004": (60e8, 1.8), "600005": (90e8, 2.2), "600006": (190e8, 6.0),
        }
        hist = {}
        for code, (mktcap, pb) in specs.items():
            hist[code] = _factor_frame(days, 10 + np.linspace(0, 2, n), mktcap, pb)
        return hist

    def test_weekly_rebalance_trades(self):
        days = _bdays(40)
        bt = FactorBacktester(initial_capital=100_000, top_n=5,
                              rebalance_days=5, reversal_days=20)
        result = bt.run(self._universe(days), days)
        assert result["trading_days"] == len(days)
        assert result["total_trades"] > 0, "周度调仓应产生成交"

    def test_trades_only_on_rebalance_days(self):
        days = _bdays(40)
        bt = FactorBacktester(initial_capital=100_000, top_n=5,
                              rebalance_days=5, reversal_days=20)
        result = bt.run(self._universe(days), days)
        trade_dates = {t["date"] for t in result["trades"]}
        assert trade_dates, "应有成交"
        idx = {days.index(d) for d in trade_dates}
        # 成交在调仓日(index%5==0)的次日 → index-1 应为 5 的倍数
        assert all((i - 1) % 5 == 0 for i in idx), f"成交不应发生在非调仓次日: {sorted(idx)}"

    def test_excludes_large_expensive(self):
        days = _bdays(40)
        bt = FactorBacktester(initial_capital=100_000, top_n=5,
                              rebalance_days=5, reversal_days=20)
        result = bt.run(self._universe(days), days)
        buys = [t for t in result["trades"] if t["action"] == "buy"]
        assert buys, "应有买入"
        # 大盘+高估值的 600006 应被 top5 排除,从不被买入
        assert all(t["code"] != "600006" for t in buys)
        # 应建仓多只(等权分散)
        assert len({t["code"] for t in buys}) >= 3
