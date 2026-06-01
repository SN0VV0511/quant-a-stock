"""
MA + RSI 组合确认策略
双重过滤减少假信号：
- 均线多头 + RSI 未超买 → 买入
- 均线空头 或 RSI 超买 → 卖出
"""

import pandas as pd
import numpy as np

from config.settings import (
    MA_SHORT,
    MA_LONG,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    RSI_MOMENTUM_MAX,
    MOMENTUM_CHASE_GAP,
)


class ComboSignalStrategy:
    """MA + RSI 组合策略"""

    def __init__(self, ma_short=MA_SHORT, ma_long=MA_LONG, rsi_period=14,
                 rsi_oversold=RSI_OVERSOLD, rsi_overbought=RSI_OVERBOUGHT):
        self.ma_short = ma_short
        self.ma_long = ma_long
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        # 动量追涨模式:RSI 上限放宽、价差阈值(均从 config 读取,便于回测调参)
        self.rsi_momentum_max = RSI_MOMENTUM_MAX
        self.momentum_chase_gap = MOMENTUM_CHASE_GAP
        self.name = f"MA{ma_short}/{ma_long}+RSI{rsi_period}"

    def calc_rsi(self, series, period=14):
        """计算 RSI"""
        delta = series.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.rolling(window=period, min_periods=period).mean()
        avg_loss = loss.rolling(window=period, min_periods=period).mean()
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def calculate_signals(self, df):
        """
        计算组合信号

        买入条件（同时满足）：
          1. MA5 > MA20（均线多头）
          2. RSI < 70 且 RSI > 35（未超买，且脱离超卖区）
          3. 收盘价站上 MA5

        卖出条件（任一满足）：
          1. MA5 < MA20（均线空头，死叉）
          2. RSI > 70（超买）
          3. 收盘价跌破 MA20
        """
        df = df.copy()

        # 均线
        df["ma_short"] = df["close"].rolling(window=self.ma_short).mean()
        df["ma_long"] = df["close"].rolling(window=self.ma_long).mean()

        # RSI
        df["rsi"] = self.calc_rsi(df["close"], self.rsi_period)

        # 信号
        df["signal"] = 0

        # 买入：多头排列 + RSI 确认 + 站上短期均线
        buy_mask = (
            (df["ma_short"] > df["ma_long"]) &
            (df["rsi"] < self.rsi_overbought) &
            (df["rsi"] > self.rsi_oversold) &
            (df["close"] > df["ma_short"])
        )
        df.loc[buy_mask, "signal"] = 1

        # 卖出：死叉 或 超买 或 跌破长均线
        sell_mask = (
            ((df["ma_short"] < df["ma_long"]) & (df["ma_short"].shift(1) >= df["ma_long"].shift(1))) |
            (df["rsi"] > self.rsi_overbought) |
            ((df["close"] < df["ma_long"]) & (df["close"].shift(1) >= df["ma_long"].shift(1)))
        )
        df.loc[sell_mask, "signal"] = -1

        return df

    def check_realtime(self, df):
        """
        盘中实时检查（最新一根 K 线可能未收盘）

        返回:
            dict: {signal: "buy"/"sell"/"hold", reason: str, rsi: float, ma_short: float, ma_long: float}
        """
        if df is None or len(df) < self.ma_long + 5:
            return {"signal": "hold", "reason": "数据不足", "rsi": None}

        df_calc = self.calculate_signals(df)
        last = df_calc.iloc[-1]
        prev = df_calc.iloc[-2]

        rsi_val = last["rsi"] if pd.notna(last["rsi"]) else 50
        ma_s = last["ma_short"] if pd.notna(last["ma_short"]) else 0
        ma_l = last["ma_long"] if pd.notna(last["ma_long"]) else 0
        close = last["close"]

        # 当前信号
        if last["signal"] == 1:
            return {
                "signal": "buy",
                "reason": f"MA{self.ma_short}/{self.ma_long}多头排列 + RSI{rsi_val:.0f}健康 + 站上MA{self.ma_short}",
                "rsi": round(rsi_val, 1),
                "ma_short": round(ma_s, 2),
                "ma_long": round(ma_l, 2),
            }

        # 动量追涨模式：MA5>MA20 且价差>阈值 + RSI 未极端超买
        if (last["signal"] != -1 and
            ma_s > ma_l and
            (ma_s - ma_l) / ma_l > self.momentum_chase_gap and
            self.rsi_oversold < rsi_val < self.rsi_momentum_max and
            close > ma_l):  # 至少站上长均线
            return {
                "signal": "buy",
                "reason": f"动量追涨 MA{self.ma_short}/{self.ma_long}多头+价差{(ma_s-ma_l)/ma_l:.0%} RSI{rsi_val:.0f}",
                "rsi": round(rsi_val, 1),
                "ma_short": round(ma_s, 2),
                "ma_long": round(ma_l, 2),
            }
        elif last["signal"] == -1:
            reasons = []
            if ma_s < ma_l:
                reasons.append(f"MA{self.ma_short}/{self.ma_long}死叉")
            if rsi_val > self.rsi_overbought:
                reasons.append(f"RSI超买{rsi_val:.0f}")
            if close < ma_l:
                reasons.append(f"跌破MA{self.ma_long}")
            return {
                "signal": "sell",
                "reason": " + ".join(reasons),
                "rsi": round(rsi_val, 1),
                "ma_short": round(ma_s, 2),
                "ma_long": round(ma_l, 2),
            }
        else:
            # 持有，但给状态
            status = "多头" if ma_s > ma_l else "空头"
            return {
                "signal": "hold",
                "reason": f"均线{status}，RSI{rsi_val:.0f}",
                "rsi": round(rsi_val, 1),
                "ma_short": round(ma_s, 2),
                "ma_long": round(ma_l, 2),
            }

    def describe(self):
        return (
            f"MA+RSI 组合策略 ({self.name})\n"
            f"  买入: MA{self.ma_short} 上穿 MA{self.ma_long} + RSI<{self.rsi_overbought} + 站上MA{self.ma_short}\n"
            f"  卖出: MA{self.ma_short} 下穿 MA{self.ma_long} 或 RSI>{self.rsi_overbought} 或 跌破MA{self.ma_long}"
        )
