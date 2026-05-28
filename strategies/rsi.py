"""
RSI 超买超卖策略 - 反转策略，专捡便宜
- RSI < 30（超卖）→ 买入
- RSI > 70（超买）→ 卖出
"""
import pandas as pd
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class RSIStrategy:
    """RSI 超买超卖策略"""

    def __init__(self, period=14, oversold=30, overbought=70):
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def calculate_signals(self, df):
        df = df.copy()
        delta = df["close"].diff()

        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)

        avg_gain = gain.rolling(window=self.period, min_periods=self.period).mean()
        avg_loss = loss.rolling(window=self.period, min_periods=self.period).mean()

        rs = avg_gain / avg_loss
        df["rsi"] = 100 - (100 / (1 + rs))

        df["signal"] = 0
        # RSI 从下方穿过超卖线 → 买入
        df.loc[
            (df["rsi"] > self.oversold) &
            (df["rsi"].shift(1) <= self.oversold),
            "signal"
        ] = 1
        # RSI 从上方穿过超买线 → 卖出
        df.loc[
            (df["rsi"] < self.overbought) &
            (df["rsi"].shift(1) >= self.overbought),
            "signal"
        ] = -1

        return df

    def describe(self):
        return (
            f"RSI 超买超卖策略 (RSI{self.period})\n"
            f"  买入信号: RSI < {self.oversold}（超卖）\n"
            f"  卖出信号: RSI > {self.overbought}（超买）"
        )
