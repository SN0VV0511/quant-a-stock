"""大盘择时(系统性风险过滤,纯函数)。

A 股动量策略最大的回撤来源,是在系统性下跌中继续买入"相对强势股"。本模块用一个
基准指数(默认沪深300)是否站上其均线来判断市场状态:指数跌破均线视为风险关闭
(risk-off),此时暂停开新仓,只允许卖出/止损。

设计为纯函数,实盘与回测共用;数据不足时默认 risk-on,避免因取数失败而停摆。
"""

from __future__ import annotations

import pandas as pd

from config.settings import MARKET_REGIME_MA


def is_risk_on(index_close, ma_period: int = MARKET_REGIME_MA) -> bool:
    """判断市场是否处于 risk-on(允许开新仓)。

    Args:
        index_close: 基准指数收盘价序列(list / Series / 含 close 列的 DataFrame)。
        ma_period: 均线周期。

    Returns:
        True 表示指数收盘价 >= 其 ``ma_period`` 日均线(允许买入);
        数据缺失或不足时返回 True(默认放行,不因取数失败而停摆)。
    """
    if index_close is None:
        return True
    if isinstance(index_close, pd.DataFrame):
        if "close" not in index_close.columns:
            return True
        series = index_close["close"]
    else:
        series = pd.Series(index_close)

    series = pd.to_numeric(series, errors="coerce").dropna()
    if len(series) < ma_period:
        return True

    last = float(series.iloc[-1])
    ma = float(series.tail(ma_period).mean())
    return last >= ma
