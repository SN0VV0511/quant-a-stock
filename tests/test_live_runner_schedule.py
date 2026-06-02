"""实时盯盘启动节奏测试。"""

from datetime import datetime
import json

import numpy as np
import pandas as pd

from live_runner import _initial_scan_time, _run_daily_rps_rotation
from risk.control import RiskController
from trading.brokers import PaperBrokerAdapter
from trading.observability import EventRecorder


def _hist(trend: float, n: int = 45) -> pd.DataFrame:
    """构造 ETF/行业 RPS 测试行情。"""
    dates = [d.strftime("%Y%m%d") for d in pd.date_range("2026-01-01", periods=n, freq="B")]
    closes = np.linspace(4.0, 4.0 * (1 + trend), n)
    return pd.DataFrame({
        "date": dates,
        "open": closes,
        "high": closes * 1.01,
        "low": closes * 0.99,
        "close": closes,
        "volume": [1_000_000] * n,
        "amount": [4_000_000] * n,
    })


class FakeRpsLoader:
    """模拟 RPS 所需行情接口。"""

    def get_batch_etf_history(self, codes, days, max_batch):
        """返回三只 ETF 历史行情。"""
        return {
            "510300": _hist(0.30),
            "510500": _hist(0.10),
            "159915": _hist(-0.05),
        }

    def get_batch_industry_index_history(self, industry_names, days, max_batch):
        """返回行业观察历史行情。"""
        return {
            "证券": _hist(0.20),
            "半导体": _hist(0.05),
        }

    def get_realtime_quotes(self, codes):
        """返回 ETF 实时行情。"""
        return {
            "510300": {"price": 5.2, "prev_close": 5.1, "open": 5.1, "high": 5.25, "low": 5.05, "volume": 1_000_000},
            "510500": {"price": 4.4, "prev_close": 4.35, "open": 4.35, "high": 4.45, "low": 4.3, "volume": 1_000_000},
            "159915": {"price": 3.8, "prev_close": 3.85, "open": 3.85, "high": 3.9, "low": 3.75, "volume": 1_000_000},
        }


def test_initial_scan_after_continuous_auction_open() -> None:
    """首次全市场扫描默认应避开 9:00 未开盘行情。"""
    now = datetime(2026, 6, 2, 8, 50, 0)
    assert _initial_scan_time(now, delay_minutes=5) == datetime(2026, 6, 2, 9, 35, 0)


def test_initial_scan_delay_clamped_to_market_open() -> None:
    """负数延迟不应把扫描提前到开盘前。"""
    now = datetime(2026, 6, 2, 10, 0, 0)
    assert _initial_scan_time(now, delay_minutes=-3) == datetime(2026, 6, 2, 9, 30, 0)


def test_daily_rps_rotation_places_paper_orders(monkeypatch, tmp_path) -> None:
    """ETF/RPS 应进入模拟盘订单、成交和状态文件链路。"""
    state_path = tmp_path / "portfolio_state.json"
    trade_log_path = tmp_path / "trade_log.json"
    snapshot_path = tmp_path / "snapshots.jsonl"
    rps_state_path = tmp_path / "rps_state.json"
    event_path = tmp_path / "events.jsonl"
    monkeypatch.setattr("live_runner.RPS_STATE_FILE", str(rps_state_path))

    broker = PaperBrokerAdapter(
        state_file=str(state_path),
        trade_log_file=str(trade_log_path),
        snapshot_log_file=str(snapshot_path),
    )
    broker.connect()
    recorder = EventRecorder(path=str(event_path))

    result = _run_daily_rps_rotation(
        broker=broker,
        loader=FakeRpsLoader(),
        risk_ctrl=RiskController(),
        recorder=recorder,
        today="20260602",
        force=True,
    )

    saved = json.loads(rps_state_path.read_text(encoding="utf-8"))
    positions = broker.query_positions()
    events = event_path.read_text(encoding="utf-8")

    assert result["status"] == "ok"
    assert saved["completed"] is True
    assert saved["etf_loaded"] == 3
    assert saved["industry_loaded"] == 2
    assert saved["orders"]
    assert positions
    assert "rps_rotation_completed" in events
    assert "execution" in events
