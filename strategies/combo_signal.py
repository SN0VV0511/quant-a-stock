"""MA + RSI + 成交量组合确认策略。

买入侧在原有均线和 RSI 基础上增加成交量确认,用于过滤缩量假突破;
卖出侧仍保持均线/RSI 优先,避免成交量不足导致该卖不卖。
"""

from __future__ import annotations

import pandas as pd

from config.settings import (
    MA_SHORT,
    MA_LONG,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    RSI_MOMENTUM_MAX,
    MOMENTUM_CHASE_GAP,
    ENABLE_VOLUME_FILTER,
    VOLUME_FILTER_LOOKBACK,
    VOLUME_FILTER_MIN_RATIO,
)
from strategies.indicators import calculate_volume_filter, calculate_volume_ratio


class ComboSignalStrategy:
    """MA + RSI + 成交量组合策略。"""

    def __init__(
        self,
        ma_short: int = MA_SHORT,
        ma_long: int = MA_LONG,
        rsi_period: int = 14,
        rsi_oversold: int = RSI_OVERSOLD,
        rsi_overbought: int = RSI_OVERBOUGHT,
        volume_filter_enabled: bool = ENABLE_VOLUME_FILTER,
        volume_lookback: int = VOLUME_FILTER_LOOKBACK,
        volume_min_ratio: float = VOLUME_FILTER_MIN_RATIO,
    ) -> None:
        """初始化组合策略参数。

        Args:
            ma_short: 短期均线周期。
            ma_long: 长期均线周期。
            rsi_period: RSI 周期。
            rsi_oversold: RSI 健康区下限。
            rsi_overbought: RSI 超买线。
            volume_filter_enabled: 是否启用成交量确认。
            volume_lookback: 成交量均量回看周期。
            volume_min_ratio: 最新成交量相对均量的最低倍数。
        """
        self.ma_short = ma_short
        self.ma_long = ma_long
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.volume_filter_enabled = volume_filter_enabled
        self.volume_lookback = volume_lookback
        self.volume_min_ratio = volume_min_ratio
        # 动量追涨模式:RSI 上限放宽、价差阈值(均从 config 读取,便于回测调参)
        self.rsi_momentum_max = RSI_MOMENTUM_MAX
        self.momentum_chase_gap = MOMENTUM_CHASE_GAP
        suffix = "+量能" if volume_filter_enabled else ""
        self.name = f"MA{ma_short}/{ma_long}+RSI{rsi_period}{suffix}"

    def calc_rsi(self, series: pd.Series, period: int = 14) -> pd.Series:
        """计算 RSI。"""
        delta = series.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.rolling(window=period, min_periods=period).mean()
        avg_loss = loss.rolling(window=period, min_periods=period).mean()
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def calculate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算组合信号。

        买入条件（同时满足）：
          1. MA5 > MA20（均线多头）
          2. RSI < 70 且 RSI > 35（未超买，且脱离超卖区）
          3. 收盘价站上 MA5
          4. 成交量不低于 20 日均量（启用时）

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

        # 成交量确认:缺少 volume 的旧数据源默认放行,有 volume 时严格过滤
        if self.volume_filter_enabled:
            df["volume_ratio"] = calculate_volume_ratio(df, self.volume_lookback)
            df["volume_passed"] = calculate_volume_filter(
                df,
                lookback=self.volume_lookback,
                min_ratio=self.volume_min_ratio,
            )
        else:
            df["volume_ratio"] = pd.NA
            df["volume_passed"] = True

        # 信号
        df["signal"] = 0

        # 买入：多头排列 + RSI 确认 + 站上短期均线
        buy_mask = (
            (df["ma_short"] > df["ma_long"]) &
            (df["rsi"] < self.rsi_overbought) &
            (df["rsi"] > self.rsi_oversold) &
            (df["close"] > df["ma_short"]) &
            (df["volume_passed"])
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

    def check_realtime(self, df: pd.DataFrame) -> dict[str, object]:
        """盘中实时检查（最新一根 K 线可能未收盘）。

        返回:
            dict: {signal: "buy"/"sell"/"hold", reason: str, rsi: float, ma_short: float, ma_long: float}
        """
        if df is None or len(df) < self.ma_long + 5:
            return {"signal": "hold", "reason": "数据不足", "rsi": None}

        df_calc = self.calculate_signals(df)
        last = df_calc.iloc[-1]

        rsi_val = last["rsi"] if pd.notna(last["rsi"]) else 50
        ma_s = last["ma_short"] if pd.notna(last["ma_short"]) else 0
        ma_l = last["ma_long"] if pd.notna(last["ma_long"]) else 0
        close = last["close"]
        volume_ratio = last.get("volume_ratio", pd.NA)
        volume_passed = bool(last.get("volume_passed", True))
        volume_suffix = ""
        if self.volume_filter_enabled and pd.notna(volume_ratio):
            volume_suffix = f" + 量能{float(volume_ratio):.1f}倍"

        # 当前信号
        if last["signal"] == 1:
            return {
                "signal": "buy",
                "reason": (
                    f"MA{self.ma_short}/{self.ma_long}多头排列 + RSI{rsi_val:.0f}健康"
                    f" + 站上MA{self.ma_short}{volume_suffix}"
                ),
                "rsi": round(rsi_val, 1),
                "ma_short": round(ma_s, 2),
                "ma_long": round(ma_l, 2),
                "volume_ratio": round(float(volume_ratio), 2) if pd.notna(volume_ratio) else None,
                "volume_passed": volume_passed,
            }

        # 动量追涨模式：MA5>MA20 且价差>阈值 + RSI 未极端超买
        if (last["signal"] != -1 and
            ma_s > ma_l and
            (ma_s - ma_l) / ma_l > self.momentum_chase_gap and
            self.rsi_oversold < rsi_val < self.rsi_momentum_max and
            close > ma_l and  # 至少站上长均线
            volume_passed):
            return {
                "signal": "buy",
                "reason": (
                    f"动量追涨 MA{self.ma_short}/{self.ma_long}多头"
                    f"+价差{(ma_s-ma_l)/ma_l:.0%} RSI{rsi_val:.0f}{volume_suffix}"
                ),
                "rsi": round(rsi_val, 1),
                "ma_short": round(ma_s, 2),
                "ma_long": round(ma_l, 2),
                "volume_ratio": round(float(volume_ratio), 2) if pd.notna(volume_ratio) else None,
                "volume_passed": volume_passed,
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
                "volume_ratio": round(float(volume_ratio), 2) if pd.notna(volume_ratio) else None,
                "volume_passed": volume_passed,
            }
        else:
            # 持有，但给状态
            status = "多头" if ma_s > ma_l else "空头"
            if self.volume_filter_enabled and not volume_passed:
                volume_suffix = "，量能不足"
            return {
                "signal": "hold",
                "reason": f"均线{status}，RSI{rsi_val:.0f}{volume_suffix}",
                "rsi": round(rsi_val, 1),
                "ma_short": round(ma_s, 2),
                "ma_long": round(ma_l, 2),
                "volume_ratio": round(float(volume_ratio), 2) if pd.notna(volume_ratio) else None,
                "volume_passed": volume_passed,
            }

    def describe(self) -> str:
        """返回策略描述。"""
        volume_desc = (
            f" + 成交量≥{self.volume_lookback}日均量{self.volume_min_ratio:.1f}倍"
            if self.volume_filter_enabled else ""
        )
        return (
            f"MA+RSI 组合策略 ({self.name})\n"
            f"  买入: MA{self.ma_short} 上穿 MA{self.ma_long} + RSI<{self.rsi_overbought} + 站上MA{self.ma_short}{volume_desc}\n"
            f"  卖出: MA{self.ma_short} 下穿 MA{self.ma_long} 或 RSI>{self.rsi_overbought} 或 跌破MA{self.ma_long}"
        )
