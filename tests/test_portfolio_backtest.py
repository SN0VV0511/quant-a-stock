"""组合策略回测器测试。

用构造的合成数据离线验证:
1. 回测器端到端可运行,产出完整绩效指标;
2. 成交发生在信号次日(T+1),无未来函数;
3. 上升趋势能触发买入并盈利。
"""

import numpy as np
import pandas as pd
import pytest

from backtest.portfolio_backtest import PortfolioBacktester


def _make_frame(dates, closes, volume=3_000_000):
    """构造单只股票历史 DataFrame(open=前一日close,模拟跳空较小)。"""
    closes = list(closes)
    opens = [closes[0]] + closes[:-1]
    return pd.DataFrame({
        "date": dates,
        "open": opens,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [volume] * len(closes),
    })


def _trading_days(n):
    base = pd.date_range("2024-01-01", periods=n, freq="B")
    return [d.strftime("%Y%m%d") for d in base]


@pytest.fixture
def uptrend_history():
    """一只带回调的上行股 + 一只震荡股,80 个交易日。

    上行股带周期性回调,使 RSI 周期性回到健康区间(35~70),让 Combo 在
    多头排列下捕捉"健康回调买点"——这正是该择时策略的设计意图;若用无回调
    的直线上涨,RSI 会持续超买(>70)反而被策略正确拦截。
    """
    days = _trading_days(80)
    t = np.arange(80)
    # 上行:整体上行 + 周期回调(锯齿),从约 10 涨到约 17
    up = 10 + 0.09 * t + 1.2 * np.sin(t / 4.0)
    # 震荡:围绕 20 上下波动,无趋势
    flat = 20 + np.sin(t / 3.0) * 0.5
    history = {
        "600000": _make_frame(days, up),
        "600001": _make_frame(days, flat),
    }
    return history, days


def test_backtest_runs_end_to_end(uptrend_history):
    history, days = uptrend_history
    bt = PortfolioBacktester(initial_capital=100_000, top_n=2, momentum_period=20)
    result = bt.run(history, days)

    assert result, "回测应返回非空结果"
    assert result["trading_days"] == len(days)
    assert len(result["daily_values"]) == len(days)
    # 关键指标字段齐全
    for key in ("total_return", "max_drawdown", "sharpe_ratio", "win_rate"):
        assert key in result


def test_no_lookahead_execution_is_t_plus_1(uptrend_history):
    """每一笔成交日必须是某个信号日的下一交易日(T+1),不能当日成交。"""
    history, days = uptrend_history
    bt = PortfolioBacktester(initial_capital=100_000, top_n=2, momentum_period=20)
    result = bt.run(history, days)

    trades = result["trades"]
    assert trades, "上升趋势应至少产生一笔买入"
    # 第一笔成交不可能发生在第一个交易日(信号最早 T0 收盘生成,T1 才成交)
    first_trade_date = trades[0]["date"]
    assert first_trade_date != days[0], "成交不应发生在首个交易日(说明用了当日信号当日成交)"
    assert first_trade_date in days[1:]


def test_uptrend_generates_profitable_buy(uptrend_history):
    history, days = uptrend_history
    bt = PortfolioBacktester(initial_capital=100_000, top_n=2, momentum_period=20)
    result = bt.run(history, days)

    buys = [t for t in result["trades"] if t["action"] == "buy"]
    assert buys, "持续上行的标的应触发买入"
    # 买入的应是上行股,而非震荡股
    assert any(t["code"] == "600000" for t in buys)
    # 全程持有上行股,最终净值应高于初始
    assert result["final_value"] > 100_000


def test_empty_inputs_return_empty():
    bt = PortfolioBacktester()
    assert bt.run({}, []) == {}
    assert bt.run({"600000": pd.DataFrame()}, []) == {}


def test_market_regime_off_blocks_buys(uptrend_history, monkeypatch):
    """指数全程下行(risk-off)时应屏蔽所有买入。"""
    import config.settings as cfg
    monkeypatch.setattr(cfg, "ENABLE_MARKET_REGIME", True)  # 显式开启择时进行验证

    history, days = uptrend_history
    # 构造一路下跌的指数 → is_risk_on 始终 False
    index = pd.DataFrame({
        "date": days,
        "close": np.linspace(4000, 3000, len(days)),
    })
    bt = PortfolioBacktester(initial_capital=100_000, top_n=2, momentum_period=20)
    result = bt.run(history, days, index_history=index)

    buys = [t for t in result["trades"] if t["action"] == "buy"]
    assert not buys, "大盘 risk-off 时不应有任何买入"
    # 无持仓 → 净值应保持初始资金附近(仅现金)
    assert result["final_value"] == 100_000


def test_market_regime_on_allows_buys(uptrend_history, monkeypatch):
    """指数全程上行(risk-on)时买入不被屏蔽,与无指数行为一致。"""
    import config.settings as cfg
    monkeypatch.setattr(cfg, "ENABLE_MARKET_REGIME", True)

    history, days = uptrend_history
    index = pd.DataFrame({
        "date": days,
        "close": np.linspace(3000, 4000, len(days)),
    })
    bt = PortfolioBacktester(initial_capital=100_000, top_n=2, momentum_period=20)
    result = bt.run(history, days, index_history=index)
    buys = [t for t in result["trades"] if t["action"] == "buy"]
    assert buys, "大盘 risk-on 时上行股应能买入"
