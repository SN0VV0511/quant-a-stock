"""策略通用技术指标。

将 ATR、成交量确认等基础指标集中在本模块,避免实盘、回测和单策略之间
各自复制公式后出现口径漂移。
"""

from __future__ import annotations

import pandas as pd


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """计算平均真实波幅 ATR。

    Args:
        df: 至少包含 ``high``、``low``、``close`` 列的行情数据。
        period: ATR 回看周期。

    Returns:
        与输入索引对齐的 ATR 序列。

    Raises:
        ValueError: 当周期非法或行情列缺失时抛出。
    """
    if period <= 0:
        raise ValueError(f"ATR 周期必须为正整数: {period}")

    required = {"high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"计算 ATR 缺少必要列: {sorted(missing)}")

    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    prev_close = close.shift(1)

    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(window=period, min_periods=period).mean()


def calculate_volume_ratio(df: pd.DataFrame, lookback: int = 20) -> pd.Series:
    """计算当前成交量相对均量的倍数。

    Args:
        df: 行情数据。缺少 ``volume`` 列时返回全空序列,由调用方决定是否放行。
        lookback: 均量回看周期。

    Returns:
        ``volume / volume_ma`` 序列。

    Raises:
        ValueError: 当回看周期非法时抛出。
    """
    if lookback <= 0:
        raise ValueError(f"成交量回看周期必须为正整数: {lookback}")

    if "volume" not in df.columns:
        return pd.Series(pd.NA, index=df.index, dtype="Float64")

    volume = pd.to_numeric(df["volume"], errors="coerce")
    # 用前 N 日均量作为基准,避免当日低量把分母一起拉低而放松过滤。
    volume_ma = volume.shift(1).rolling(window=lookback, min_periods=lookback).mean()
    return volume / volume_ma.mask(volume_ma == 0)


def calculate_volume_filter(
    df: pd.DataFrame,
    lookback: int = 20,
    min_ratio: float = 1.0,
) -> pd.Series:
    """判断成交量是否通过确认过滤。

    Args:
        df: 行情数据。缺少 ``volume`` 列时返回全 True,兼容旧测试和无量数据源。
        lookback: 均量回看周期。
        min_ratio: 当前成交量相对均量的最低倍数。

    Returns:
        布尔序列,True 表示成交量确认通过。

    Raises:
        ValueError: 当 ``min_ratio`` 非正时抛出。
    """
    if min_ratio <= 0:
        raise ValueError(f"成交量倍数必须为正数: {min_ratio}")

    if "volume" not in df.columns:
        return pd.Series(True, index=df.index, dtype=bool)

    ratio = calculate_volume_ratio(df, lookback=lookback)
    return (ratio >= min_ratio).fillna(False).astype(bool)


def calculate_obv(df: pd.DataFrame) -> pd.Series:
    """计算 OBV 能量潮。

    Args:
        df: 至少包含 ``close``、``volume`` 列的行情数据。

    Returns:
        与输入索引对齐的 OBV 序列。

    Raises:
        ValueError: 当行情列缺失时抛出。
    """
    required = {"close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"计算 OBV 缺少必要列: {sorted(missing)}")

    close = pd.to_numeric(df["close"], errors="coerce")
    volume = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    direction = close.diff().apply(lambda x: 1 if x > 0 else -1 if x < 0 else 0)
    return (direction * volume).fillna(0).cumsum()


def calculate_obv_trend(df: pd.DataFrame, lookback: int = 20) -> float:
    """计算 OBV 趋势强度。

    Args:
        df: 至少包含 ``close``、``volume`` 列的行情数据。
        lookback: OBV 对比周期。

    Returns:
        归一化后的 OBV 趋势值,大于 0 表示资金累积偏强。

    Raises:
        ValueError: 当回看周期非法时抛出。
    """
    if lookback <= 0:
        raise ValueError(f"OBV 回看周期必须为正整数: {lookback}")
    if len(df) < lookback + 1:
        return 0.0

    obv = calculate_obv(df)
    volume = pd.to_numeric(df["volume"], errors="coerce").dropna()
    base_volume = float(volume.tail(lookback).mean()) if len(volume) >= lookback else 0.0
    if base_volume <= 0:
        return 0.0
    return float((obv.iloc[-1] - obv.iloc[-(lookback + 1)]) / base_volume)
