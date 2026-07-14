"""robust_v2 收盘信号与次日执行链路测试。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import time

import pandas as pd
import pytest

from robust_runner import (
    DataCompletenessError,
    LeaseHeartbeat,
    RobustDataService,
    RobustV2Runner,
    SignalData,
    _latest_completed_trade_date,
    load_runtime_config,
)
from strategies.robust_v2 import RobustV2Config, build_market_data_hash
from trading.brokers import SQLitePaperBrokerAdapter
from trading.ledger import PaperLedger
from trading.models import MarketSnapshot, OrderIntent


def _etf_history(code: str, slope: float) -> pd.DataFrame:
    """生成截至 2026-07-09 的 ETF 历史。"""
    dates = pd.bdate_range(end="2026-07-09", periods=280)
    close = [4 + slope * index for index in range(len(dates))]
    return pd.DataFrame(
        {
            "date": dates.strftime("%Y%m%d"),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": [30_000_000] * len(dates),
            "amount": [200_000_000] * len(dates),
            "name": [code] * len(dates),
        }
    )


class FakeLoader:
    """仅满足 runner 测试所需接口。"""

    def close(self) -> None:
        """兼容真实加载器。"""


class FakeUniverseLoader:
    """为实时粗筛统计提供确定性股票和行情。"""

    def get_all_stocks(self) -> list[dict[str, str]]:
        """返回五只沪深主板股票。"""
        return [
            {"code": "sh600001", "name": "高流动"},
            {"code": "sh600002", "name": "次高流动"},
            {"code": "sh600003", "name": "ST测试"},
            {"code": "sh600004", "name": "低价"},
            {"code": "sh600005", "name": "低流动"},
        ]

    def get_realtime_quotes(self, codes: list[str]) -> dict[str, dict[str, object]]:
        """返回覆盖 ST、价格、流动性和数量上限的行情。"""
        assert len(codes) == 5
        return {
            "sh600001": {"name": "高流动", "price": 10.0, "volume": 30_000_000},
            "sh600002": {"name": "次高流动", "price": 8.0, "volume": 20_000_000},
            "sh600003": {"name": "ST测试", "price": 10.0, "volume": 30_000_000},
            "sh600004": {"name": "低价", "price": 2.0, "volume": 30_000_000},
            "sh600005": {"name": "低流动", "price": 10.0, "volume": 1_000},
        }


class FakeDataService:
    """返回固定的收盘和执行行情。"""

    def __init__(self) -> None:
        self.history = {
            "510300": _etf_history("510300", 0.01),
            "510500": _etf_history("510500", 0.008),
        }
        self.snapshot = MarketSnapshot(
            trade_date="20260709",
            previous_trade_date="20260708",
            source="test",
            adjustment="qfq",
            data_hash=build_market_data_hash(self.history, "20260709"),
            freshness_seconds=0,
        )
        self.execution_quote_time = "20260710093600"

    def load_signal_data(self, trade_date: str, account_value: float) -> SignalData:
        """返回固定收盘数据。"""
        assert trade_date == "20260709"
        assert account_value == 50_000
        return SignalData(self.snapshot, self.history, {}, {}, {})

    def load_execution_data(self, codes: set[str], signal_date: str):
        """返回与信号日前收盘一致的次日实时行情。"""
        assert signal_date == "20260709"
        quotes = {
            code: {
                "price": float(self.history[code]["close"].iloc[-1]),
                "prev_close": float(self.history[code]["close"].iloc[-1]),
                "quote_time": self.execution_quote_time,
                "captured_at": "2026-07-10 09:36:01",
                "source": "test",
                "is_suspended": False,
                "trade_status": 1,
            }
            for code in codes
        }
        return self.snapshot, {code: self.history[code] for code in codes}, quotes


def test_close_signal_does_not_trade_until_next_day(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """T 日只写目标，T+1 才产生订单和持仓。"""
    broker = SQLitePaperBrokerAdapter(
        ledger_path=str(tmp_path / "paper_v2.db"),
        initial_cash=50_000,
    )
    broker.connect()
    runner = RobustV2Runner(
        broker,
        FakeLoader(),  # type: ignore[arg-type]
        RobustV2Config(enable_stock_enhancement=False, etf_min_avg_amount=0),
        tmp_path,
    )
    runner.data = FakeDataService()  # type: ignore[assignment]
    monkeypatch.setattr(
        "robust_runner.previous_trading_day",
        lambda date: "20260709" if date == "20260710" else "20260708",
    )

    target = runner.generate_close_target("20260709", force=True)
    same_day = runner.execute_pending_target("20260709")
    next_day = runner.execute_pending_target(
        "20260710",
        current_time=datetime(2026, 7, 10, 9, 36, 30),
    )

    assert target is not None
    assert broker.query_orders() == list(next_day.reports)
    assert same_day.reports == ()
    assert next_day.reports
    assert all(report.status == "filled" for report in next_day.reports)
    assert broker.query_positions()
    assert all(
        position["sellable_qty"] == 0
        for position in broker.ledger.query_positions("20260710").values()
    )
    broker.close()


def test_transient_quote_failure_keeps_signal_and_retries_same_day(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """陈旧行情不得吃掉目标；行情恢复后应重新分配并完成。"""
    broker = SQLitePaperBrokerAdapter(
        ledger_path=str(tmp_path / "paper_v2.db"),
        initial_cash=50_000,
    )
    broker.connect()
    runner = RobustV2Runner(
        broker,
        FakeLoader(),  # type: ignore[arg-type]
        RobustV2Config(enable_stock_enhancement=False, etf_min_avg_amount=0),
        tmp_path,
    )
    data = FakeDataService()
    runner.data = data  # type: ignore[assignment]
    monkeypatch.setattr("robust_runner.previous_trading_day", lambda _date: "20260709")
    target = runner.generate_close_target("20260709", force=True)
    assert target is not None

    first = runner.execute_pending_target(
        "20260710",
        current_time=datetime(2026, 7, 10, 9, 40),
    )
    signal_id = first.signal_id
    assert signal_id is not None
    assert first.reports == ()
    assert "已安排重试" in first.message
    assert runner.ledger.query_signal_state(signal_id)["status"] == "retryable"

    data.execution_quote_time = "20260710094100"
    second = runner.execute_pending_target(
        "20260710",
        current_time=datetime(2026, 7, 10, 9, 41, 30),
    )

    assert second.reports
    assert all(report.status == "filled" for report in second.reports)
    state = runner.ledger.query_signal_state(signal_id)
    assert state["status"] == "completed"
    assert state["attempt_count"] == 2
    broker.close()


def test_preview_scan_writes_snapshot_without_creating_actionable_signal(
    tmp_path: Path,
) -> None:
    """安全预览只允许写扫描快照，不得产生待执行信号或订单。"""
    broker = SQLitePaperBrokerAdapter(
        ledger_path=str(tmp_path / "paper_v2.db"),
        initial_cash=50_000,
    )
    broker.connect()
    runner = RobustV2Runner(
        broker,
        FakeLoader(),  # type: ignore[arg-type]
        RobustV2Config(enable_stock_enhancement=False, etf_min_avg_amount=0),
        tmp_path,
    )
    runner.data = FakeDataService()  # type: ignore[assignment]

    snapshot = runner.preview_stock_scan("20260709")

    assert snapshot.status == "completed"
    assert snapshot.mode == "manual_preview"
    assert runner.ledger.latest_signal_date("robust_v2") is None
    assert broker.query_orders() == []
    assert runner.scan_store.latest_path.exists()


def test_daily_candidate_scan_updates_once_without_creating_signal(
    tmp_path: Path,
) -> None:
    """非调仓日也应更新候选观察，同一天不得重复执行全市场扫描。"""
    broker = SQLitePaperBrokerAdapter(
        ledger_path=str(tmp_path / "paper_v2.db"),
        initial_cash=50_000,
    )
    broker.connect()
    runner = RobustV2Runner(
        broker,
        FakeLoader(),  # type: ignore[arg-type]
        RobustV2Config(enable_stock_enhancement=False, etf_min_avg_amount=0),
        tmp_path,
    )
    runner.data = FakeDataService()  # type: ignore[assignment]

    first = runner.ensure_daily_candidate_scan("20260709")
    second = runner.ensure_daily_candidate_scan("20260709")

    assert first is not None
    assert first.mode == "daily_observation"
    assert second is None
    assert runner.ledger.latest_signal_date("robust_v2") is None
    assert broker.query_orders() == []
    broker.close()
    broker.close()


def test_realtime_prefilter_reports_rejections_and_candidate_limit(
    tmp_path: Path,
) -> None:
    """实时粗筛应解释主板股票为何未进入完整历史因子扫描。"""
    service = RobustDataService(
        FakeUniverseLoader(),  # type: ignore[arg-type]
        tmp_path,
        stock_candidate_limit=1,
    )

    selected, _, _, universe, counts = service._stock_candidates(50_000)

    assert selected == ["sh600001"]
    assert universe == {
        "mainboard_count": 5,
        "realtime_quote_count": 5,
        "rough_candidate_count": 1,
    }
    assert counts == {
        "st_or_delisting": 1,
        "price_out_of_range": 1,
        "insufficient_liquidity": 1,
        "candidate_limit": 1,
    }


def test_formal_signal_rejects_incomplete_market_data(tmp_path: Path) -> None:
    """局部股票池或低行情覆盖率不得生成看似正常的正式信号。"""
    service = RobustDataService(
        FakeUniverseLoader(),  # type: ignore[arg-type]
        tmp_path,
        stock_candidate_limit=1,
        min_mainboard_universe=100,
        min_realtime_quote_coverage=0.9,
        min_history_coverage=0.9,
        min_etf_history_coverage=0.8,
    )

    with pytest.raises(DataCompletenessError, match="主板股票池仅 5 只"):
        service._require_complete_signal_data(
            universe={
                "mainboard_count": 5,
                "realtime_quote_count": 5,
                "rough_candidate_count": 1,
                "history_loaded_count": 1,
                "universe_authoritative": 1,
            },
            etf_history_count=10,
        )


def test_preview_scan_uses_latest_completed_close(monkeypatch) -> None:
    """盘中预览必须回退到上一交易日，收盘后才允许使用当天。"""
    monkeypatch.setattr(
        "robust_runner._is_trading_day",
        lambda date, ignore_calendar: datetime.strptime(date, "%Y%m%d").weekday() < 5,
    )

    assert _latest_completed_trade_date(datetime(2026, 7, 13, 10, 0)) == "20260710"
    assert _latest_completed_trade_date(datetime(2026, 7, 13, 15, 6)) == "20260713"


def test_runtime_loads_walk_forward_selection_and_etf_fallback(tmp_path: Path) -> None:
    """守护入口应使用样本外结果，并在失败标记下关闭个股增强。"""
    path = tmp_path / "selected.json"
    path.write_text(
        """{
  "selected_params": {
    "enable_etf_trend_filter": true,
    "stock_reversal_days": 20,
    "rebalance_days": 10,
    "stock_stop_pct": 0.09,
    "max_total_position": 0.60
  },
  "fallback_to_etf": true
}
""",
        encoding="utf-8",
    )

    config = load_runtime_config(path)

    assert config.max_total_position == 0.60
    assert config.rebalance_days == 10
    assert config.enable_stock_enhancement is False


def test_lease_heartbeat_runs_during_long_market_data_task() -> None:
    """主线程等待行情时，后台续租不能停止。"""

    class FakeLedger:
        """记录续租调用的最小账本替身。"""

        def __init__(self) -> None:
            self.calls = 0

        def heartbeat_lease(self, holder_id: str, ttl_seconds: int) -> None:
            assert holder_id == "holder"
            assert ttl_seconds == 1
            self.calls += 1

    ledger = FakeLedger()
    heartbeat = LeaseHeartbeat(
        ledger,
        "holder",
        1,
        interval_seconds=0.02,
    )
    heartbeat.start()
    time.sleep(0.07)
    heartbeat.stop()

    assert ledger.calls >= 2


def test_robust_runner_blocks_new_buys_after_drawdown_trigger(tmp_path: Path) -> None:
    """全局最大回撤配置必须实际接入 robust_v2，而不是只停留在配置文件。"""
    ledger = PaperLedger(
        tmp_path / "risk.db",
        initial_cash=50_000,
        strict_order_source=False,
    )
    broker = SQLitePaperBrokerAdapter(ledger=ledger)
    broker.connect()
    ledger.place_order(
        OrderIntent(
            account_id="paper_v2",
            strategy_version="robust_v2",
            signal_date="20260708",
            code="600000",
            action="buy",
            price=10.0,
            shares=1000,
            name="浦发银行",
            strategy="测试",
            strategy_tag="robust_v2",
            reason="TEST",
            date="20260708",
        )
    )
    ledger.record_snapshot(
        {"600000": 10.0},
        snapshot_date="20260709",
        data_version="test",
        strategy_version="robust_v2",
    )
    runner = RobustV2Runner(
        broker,
        FakeLoader(),  # type: ignore[arg-type]
        RobustV2Config(enable_stock_enhancement=False),
        tmp_path,
    )

    reason = runner._buy_risk_block_reason(
        {"600000": 6.0},
        execution_date="20260710",
    )

    assert "回撤" in reason
    assert "暂停新买入" in reason
    broker.close()
