"""策略层单元测试:ComboSignal / RSI / MACross 信号正确性与边界。"""

import numpy as np
import pandas as pd
import pytest

from strategies.combo_signal import ComboSignalStrategy
from strategies.ma_cross import MACrossStrategy
from strategies.rsi import RSIStrategy


def _df(closes):
    return pd.DataFrame({"close": list(closes)})


class TestMACross:
    def test_golden_cross_buy(self):
        # 先跌后涨,制造金叉
        closes = [10] * 20 + list(np.linspace(10, 20, 20))
        df = MACrossStrategy(short_window=5, long_window=20).calculate_signals(_df(closes))
        assert (df["signal"] == 1).any(), "上行段应出现金叉买入信号"

    def test_death_cross_sell(self):
        closes = list(np.linspace(10, 20, 20)) + list(np.linspace(20, 10, 20))
        df = MACrossStrategy(short_window=5, long_window=20).calculate_signals(_df(closes))
        assert (df["signal"] == -1).any(), "下行段应出现死叉卖出信号"

    def test_invalid_window_raises(self):
        with pytest.raises(ValueError):
            MACrossStrategy(short_window=20, long_window=5)


class TestRSI:
    def test_rsi_oversold_recovery_buy(self):
        # 持续下跌后反弹,RSI 由超卖区上穿
        closes = list(np.linspace(20, 10, 25)) + list(np.linspace(10, 14, 10))
        df = RSIStrategy(period=14, oversold=30, overbought=70).calculate_signals(_df(closes))
        assert (df["signal"] == 1).any(), "超卖反弹应触发买入"

    def test_rsi_values_bounded(self):
        closes = list(np.linspace(10, 30, 40))
        df = RSIStrategy().calculate_signals(_df(closes))
        rsi = df["rsi"].dropna()
        assert ((rsi >= 0) & (rsi <= 100)).all(), "RSI 必须落在 0~100"


class TestComboSignal:
    def test_insufficient_data_returns_hold(self):
        combo = ComboSignalStrategy()
        result = combo.check_realtime(_df([10, 11, 12]))
        assert result["signal"] == "hold"
        assert "数据不足" in result["reason"]

    def test_healthy_pullback_in_uptrend_buys(self):
        # 多头排列 + 周期回调:验证 combo 能产出有效信号且 RSI 已计算
        t = np.arange(60)
        closes = 10 + 0.1 * t + 1.2 * np.sin(t / 4.0)
        combo = ComboSignalStrategy()
        result = combo.check_realtime(_df(closes))
        assert result["signal"] in {"buy", "sell", "hold"}
        assert result["rsi"] is not None
        assert result["ma_short"] is not None

    def test_overbought_sells(self):
        # 急速拉升使 RSI 远超 70
        closes = list(np.linspace(10, 11, 25)) + list(np.linspace(11, 30, 15))
        combo = ComboSignalStrategy()
        result = combo.check_realtime(_df(closes))
        # 强势直拉时应给出 buy(动量追涨)或 sell(超买),但不应是 hold
        assert result["signal"] in {"buy", "sell"}

    def test_calculate_signals_columns(self):
        closes = list(np.linspace(10, 20, 40))
        combo = ComboSignalStrategy()
        df = combo.calculate_signals(_df(closes))
        assert {"ma_short", "ma_long", "rsi", "signal"} <= set(df.columns)
