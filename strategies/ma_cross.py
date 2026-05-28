"""
双均线交叉策略 - 经典入门策略
- 短期均线上穿长期均线 → 买入（金叉）
- 短期均线下穿长期均线 → 卖出（死叉）
"""
import pandas as pd
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import MA_SHORT, MA_LONG


class MACrossStrategy:
    """双均线交叉策略"""

    def __init__(self, short_window=None, long_window=None):
        self.short_window = short_window or MA_SHORT
        self.long_window = long_window or MA_LONG

        if self.short_window >= self.long_window:
            raise ValueError("短期均线周期必须小于长期均线周期")

    def calculate_signals(self, df):
        """
        计算交易信号

        参数:
            df: 包含 close 列的 DataFrame

        返回:
            添加了 ma_short, ma_long, signal 列的 DataFrame
        """
        df = df.copy()

        # 计算均线
        df["ma_short"] = df["close"].rolling(window=self.short_window).mean()
        df["ma_long"] = df["close"].rolling(window=self.long_window).mean()

        # 生成信号：1=买入，-1=卖出，0=持有
        df["signal"] = 0

        # 金叉：短期从下往上穿过长期
        df.loc[
            (df["ma_short"] > df["ma_long"]) &
            (df["ma_short"].shift(1) <= df["ma_long"].shift(1)),
            "signal"
        ] = 1

        # 死叉：短期从上往下穿过长期
        df.loc[
            (df["ma_short"] < df["ma_long"]) &
            (df["ma_short"].shift(1) >= df["ma_long"].shift(1)),
            "signal"
        ] = -1

        return df

    def describe(self):
        """策略描述"""
        return (
            f"双均线交叉策略 (MA{self.short_window}/{self.long_window})\n"
            f"  买入信号: MA{self.short_window} 上穿 MA{self.long_window}（金叉）\n"
            f"  卖出信号: MA{self.short_window} 下穿 MA{self.long_window}（死叉）"
        )


if __name__ == "__main__":
    from data.data_loader import get_stock_data

    df = get_stock_data("000001", "20240101", "20250101")
    strategy = MACrossStrategy()
    print(strategy.describe())
    df = strategy.calculate_signals(df)

    # 打印有信号的日期
    signals = df[df["signal"] != 0][["date", "close", "ma_short", "ma_long", "signal"]]
    print(f"\n📊 信号统计:")
    print(f"  买入信号: {(df['signal'] == 1).sum()} 次")
    print(f"  卖出信号: {(df['signal'] == -1).sum()} 次")
    print(f"\n信号明细:")
    print(signals.to_string(index=False))
