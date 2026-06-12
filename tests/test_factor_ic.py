"""因子 IC 分析测试:用指数增长合成数据构造确定性相关。"""

import numpy as np
import pandas as pd

from backtest.factor_ic import compute_factor_ic


def _exp_stock(rate: float, n: int = 140) -> pd.DataFrame:
    """固定日增长率的股票:动量与下期收益都由 rate 单调决定。"""
    dates = [d.strftime("%Y%m%d") for d in pd.date_range("2026-01-01", periods=n, freq="B")]
    close = 10.0 * (1.0 + rate) ** np.arange(n)
    return pd.DataFrame({"date": dates, "close": close})


def test_momentum_ic_positive_reversal_negative_when_trend_persists() -> None:
    """8 只股票各有固定增长率:动量与下期收益完美同序 → 动量 IC≈+1、反转 IC≈-1。"""
    history = {f"S{i}": _exp_stock(rate=0.001 * (i + 1)) for i in range(8)}
    days = [d.strftime("%Y%m%d") for d in pd.date_range("2026-01-01", periods=140, freq="B")]

    out = compute_factor_ic(history, days, period=20)

    assert out["momentum_60"]["n_periods"] >= 1
    assert out["momentum_60"]["ic_mean"] > 0.9       # 趋势持续 → 动量强正相关
    assert out["reversal_20"]["ic_mean"] < -0.9      # 反转方向恰好相反
    assert out["size_small"]["ic_mean"] is None       # 无 mktcap 列 → 无数据
    assert out["low_pb"]["ic_mean"] is None           # 无 pb 列 → 无数据

    # 动量分层收益应单调:最低动量层下期收益 < 最高动量层
    valid = [layer for layer in out["momentum_60"]["layer_returns"] if layer is not None]
    assert len(valid) >= 2 and valid[0] < valid[-1]
