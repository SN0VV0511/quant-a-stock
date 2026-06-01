"""大盘择时 is_risk_on 测试。"""

import numpy as np
import pandas as pd

from strategies.market_regime import is_risk_on


def test_above_ma_is_risk_on():
    # 持续上行,收盘价在 MA20 之上
    closes = list(np.linspace(3000, 3600, 40))
    assert is_risk_on(closes, ma_period=20) is True


def test_below_ma_is_risk_off():
    # 持续下行,收盘价跌破 MA20
    closes = list(np.linspace(3600, 3000, 40))
    assert is_risk_on(closes, ma_period=20) is False


def test_insufficient_data_defaults_on():
    assert is_risk_on([3000, 3100, 3200], ma_period=20) is True


def test_none_defaults_on():
    assert is_risk_on(None) is True


def test_accepts_dataframe():
    df = pd.DataFrame({"close": np.linspace(3600, 3000, 40)})
    assert is_risk_on(df, ma_period=20) is False


def test_dataframe_without_close_defaults_on():
    df = pd.DataFrame({"price": [1, 2, 3]})
    assert is_risk_on(df, ma_period=2) is True
