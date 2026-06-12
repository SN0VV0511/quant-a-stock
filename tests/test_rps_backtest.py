"""RPSBacktester(统一 RPS 回测器)测试:复用实盘选股 + 分层退出。"""

import numpy as np
import pandas as pd

from backtest.rps_backtest import RPSBacktester


def _etf_hist(code: str, trend: float, n: int = 60, volume: int = 2_000_000) -> pd.DataFrame:
    """构造带 OHLCV 的合成 ETF 历史(单调趋势)。"""
    dates = [d.strftime("%Y%m%d") for d in pd.date_range("2026-01-05", periods=n, freq="B")]
    close = np.linspace(10.0, 10.0 * (1 + trend), n)
    return pd.DataFrame({
        "date": dates,
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": [volume] * n,
        "name": [code] * n,
    })


def test_rps_backtester_buys_strongest_avoids_downtrend() -> None:
    """回测应买入强势 ETF、回避下跌 ETF,且复用实盘内核能正常产出净值。"""
    history = {
        "510300": _etf_hist("510300", 0.30),    # 强势上涨
        "510500": _etf_hist("510500", 0.18),    # 次强
        "159915": _etf_hist("159915", -0.15),   # 下跌,绝对动量<0 应被回避
        "512100": _etf_hist("512100", 0.08),    # 温和
    }
    days = [d.strftime("%Y%m%d") for d in pd.date_range("2026-01-05", periods=60, freq="B")]

    bt = RPSBacktester(top_n=2, lookback=20, rebalance_weekday=0)
    result = bt.run(history, days)

    assert result, "回测结果不应为空"
    assert result["final_value"] > 0
    buys = [t for t in result.get("trades", []) if t["action"] == "buy"]
    assert len(buys) >= 1, "应至少发生一次买入"
    bought = {t["code"] for t in buys}
    assert "510300" in bought          # 最强者被买入
    assert "159915" not in bought      # 下跌者被绝对动量过滤,不买


def test_rps_backtester_empty_when_all_downtrend() -> None:
    """全池下跌时绝对动量过滤应使其空仓(不被相对最强者诱导买入)。"""
    history = {
        "510300": _etf_hist("510300", -0.10),
        "510500": _etf_hist("510500", -0.20),
        "159915": _etf_hist("159915", -0.30),
    }
    days = [d.strftime("%Y%m%d") for d in pd.date_range("2026-01-05", periods=60, freq="B")]

    bt = RPSBacktester(top_n=2, lookback=20, rebalance_weekday=0)
    result = bt.run(history, days)

    buys = [t for t in result.get("trades", []) if t["action"] == "buy"]
    assert buys == [], "普跌池不应买入任何 ETF"
