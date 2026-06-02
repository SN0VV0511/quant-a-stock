"""小市值虚拟盘 runner 测试:调仓日逻辑 + 完整调仓执行(对临时状态文件,不碰真实盘)。"""

import tempfile

import numpy as np
import pandas as pd
import pytest

import smallcap_runner as sr
from rules.position import PositionManager
from risk.control import RiskController
from trading.brokers import PaperBrokerAdapter
from trading.observability import EventRecorder


# ---------- 调仓日逻辑 ----------

class TestRebalanceDay:
    def test_first_trading_day_of_week(self, monkeypatch):
        # 设定一组交易日:周一~周五(20240603 周一 起),周末非交易
        trading = {"20240603", "20240604", "20240605", "20240606", "20240607",
                   "20240611", "20240612"}  # 0610 端午休
        monkeypatch.setattr(sr, "is_trading_day", lambda d: d in trading)
        assert sr.is_rebalance_day("20240603") is True    # 本周第一个交易日
        assert sr.is_rebalance_day("20240604") is False   # 同周第二天
        assert sr.is_rebalance_day("20240607") is False
        # 0610 休市,本周首个交易日是 0611
        assert sr.is_rebalance_day("20240611") is True
        assert sr.is_rebalance_day("20240612") is False


# ---------- 完整调仓执行(临时状态) ----------

class FakeLoader:
    """合成数据加载器:8 只小盘股,市值/PB 递增,便于验证选股方向。"""

    def __init__(self):
        days = [d.strftime("%Y%m%d") for d in pd.date_range("2024-01-01", periods=30, freq="B")]
        self._days = days
        self.stocks = [{"code": f"60000{i}", "name": f"票{i}"} for i in range(8)]
        self._hist = {}
        for i, s in enumerate(self.stocks):
            close = 10 + np.linspace(0, 1, 30) + i * 0.1
            self._hist[s["code"]] = pd.DataFrame({
                "close": close,
                "pb": [1.0 + i * 0.3] * 30,             # PB 递增
                "mktcap": [(25 + i * 15) * 1e8] * 30,    # 市值递增,均在 [20e8,200e8]
                "is_st": [0] * 30,
                "is_suspended": [False] * 30,
            })

    def get_all_stocks(self):
        return self.stocks

    def get_realtime_quotes(self, codes):
        out = {}
        for c in codes:
            df = self._hist.get(c)
            if df is None:
                continue
            px = float(df["close"].iloc[-1])
            out[c] = {"price": px, "prev_close": px, "volume": 5_000_000, "name": "票"}
        return out

    def get_batch_history_ext(self, codes, days=40):
        return {c: self._hist[c] for c in codes if c in self._hist}

    def close(self):
        pass


def _temp_broker():
    bk = PaperBrokerAdapter(
        state_file=tempfile.mktemp(suffix=".json"),
        trade_log_file=tempfile.mktemp(suffix=".jsonl"),
        snapshot_log_file=tempfile.mktemp(suffix=".jsonl"),
    )
    bk.portfolio.state["cash"] = 100_000
    bk.portfolio.state["peak_value"] = 100_000
    bk.portfolio.save()
    bk.connect()
    return bk


def test_rebalance_builds_equal_weight_smallcap_portfolio():
    broker = _temp_broker()
    risk = RiskController()
    recorder = EventRecorder(path=tempfile.mktemp(suffix=".jsonl"))
    loader = FakeLoader()

    sr.rebalance(broker, loader, risk, recorder, "20240115",
                 top_n=5, max_universe=100, dry_run=False)

    pos = broker.query_positions()
    assert pos, "调仓应建立持仓"
    # top_n=5 时按 min(单票15%,总仓60%/5)≈12% 建仓,应能建多只
    assert len(pos) >= 4
    # 选中的应是市值最小/最便宜的(600000~600004),不含最大最贵的 600007
    assert "600007" not in pos
    assert "600000" in pos


def test_dry_run_does_not_trade():
    broker = _temp_broker()
    risk = RiskController()
    recorder = EventRecorder(path=tempfile.mktemp(suffix=".jsonl"))
    loader = FakeLoader()

    sr.rebalance(broker, loader, risk, recorder, "20240115",
                 top_n=5, max_universe=100, dry_run=True)

    assert broker.query_positions() == {}, "dry-run 不应产生任何持仓"


def test_rebalance_sells_dropped_holdings():
    broker = _temp_broker()
    risk = RiskController()
    recorder = EventRecorder(path=tempfile.mktemp(suffix=".jsonl"))
    loader = FakeLoader()
    # 先持有一只不在目标池的票(用昨日日期买入以绕过 T+1)
    broker.portfolio.buy("600007", "票7", 17.0, 100, "20240110", "manual")

    sr.rebalance(broker, loader, risk, recorder, "20240115",
                 top_n=5, max_universe=100, dry_run=False)

    # 600007 市值最大最贵,应被调出
    assert "600007" not in broker.query_positions()
