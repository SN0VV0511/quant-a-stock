"""robust_v2 收盘信号与次日执行链路测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
import signal
import threading
import time
from types import SimpleNamespace

import pandas as pd
import pytest

import robust_runner as runner_module
from robust_runner import (
    DataCompletenessError,
    LeaseHeartbeat,
    RobustDataService,
    RobustV2Runner,
    SignalData,
    _latest_completed_trade_date,
    _parse_observation_end_date,
    load_runtime_config,
    run_daemon,
)
from strategies.robust_v2 import RobustV2Config, build_market_data_hash
from trading.brokers import SQLitePaperBrokerAdapter
from trading.ledger import PaperLedger
from trading.models import (
    ExecutionReport,
    MarketSnapshot,
    OrderIntent,
    TargetPortfolio,
    TargetPosition,
)


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
            "sh600001": {
                "name": "高流动",
                "price": 10.0,
                "volume": 30_000_000,
                "source": "tencent",
            },
            "sh600002": {
                "name": "次高流动",
                "price": 8.0,
                "volume": 20_000_000,
                "source": "tencent",
            },
            "sh600003": {
                "name": "ST测试",
                "price": 10.0,
                "volume": 30_000_000,
                "source": "tencent",
            },
            "sh600004": {
                "name": "低价",
                "price": 2.0,
                "volume": 30_000_000,
                "source": "tencent",
            },
            "sh600005": {
                "name": "低流动",
                "price": 10.0,
                "volume": 1_000,
                "source": "tencent",
            },
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


def test_structural_validation_error_fails_signal_after_three_attempts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """结构性校验错误最多执行 3 次，之后标记 failed 并移出待重试队列。"""
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
    monkeypatch.setattr("robust_runner.previous_trading_day", lambda _date: "20260709")

    def _structural_failure(*_args: object, **_kwargs: object) -> None:
        raise ValueError("目标组合最多允许 2 只股票")

    monkeypatch.setattr(runner.allocator, "allocate", _structural_failure)
    target = runner.generate_close_target("20260709", force=True)
    assert target is not None

    # 每次重试间隔超过 ROBUST_V2_SIGNAL_RETRY_SECONDS(60s)，保证可以重新领取。
    first = runner.execute_pending_target(
        "20260710", current_time=datetime(2026, 7, 10, 9, 40, 0)
    )
    signal_id = first.signal_id
    assert signal_id is not None
    state = runner.ledger.query_signal_state(signal_id)
    assert state["status"] == "retryable"
    assert "最多允许 2 只股票" in state["last_error"]

    second = runner.execute_pending_target(
        "20260710", current_time=datetime(2026, 7, 10, 9, 42, 0)
    )
    assert "已安排重试" in second.message
    assert runner.ledger.query_signal_state(signal_id)["status"] == "retryable"

    third = runner.execute_pending_target(
        "20260710", current_time=datetime(2026, 7, 10, 9, 44, 0)
    )
    assert "已停止重试" in third.message
    state = runner.ledger.query_signal_state(signal_id)
    assert state["status"] == "failed"
    assert state["attempt_count"] == 3
    assert "结构性错误" in state["last_error"]
    assert "最多允许 2 只股票" in state["last_error"]
    assert state["next_retry_at"] is None

    # failed 信号不再进入待执行队列，后续轮次直接空转。
    idle = runner.execute_pending_target(
        "20260710", current_time=datetime(2026, 7, 10, 9, 46, 0)
    )
    assert idle.signal_id is None
    assert idle.message == "没有待执行目标"
    broker.close()


def test_transient_runtime_error_keeps_retrying_beyond_three_attempts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """瞬时错误(网络/行情缺失)不得触发 failed，重试逻辑保持不变。"""
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
    monkeypatch.setattr("robust_runner.previous_trading_day", lambda _date: "20260709")

    def _transient_failure(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("行情暂缺: 信号日行情 0/5")

    monkeypatch.setattr(runner.allocator, "allocate", _transient_failure)
    target = runner.generate_close_target("20260709", force=True)
    assert target is not None

    for attempt in range(1, 5):
        outcome = runner.execute_pending_target(
            "20260710",
            current_time=datetime(2026, 7, 10, 9, 40 + attempt * 2, 0),
        )
        assert "已安排重试" in outcome.message
        state = runner.ledger.query_signal_state(outcome.signal_id)
        assert state["status"] == "retryable"
        assert state["attempt_count"] == attempt
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
        "tencent_quote_count": 5,
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
                "tencent_quote_count": 5,
                "rough_candidate_count": 1,
                "history_loaded_count": 1,
                "universe_authoritative": 1,
            },
            etf_history_count=5,
        )


def test_formal_signal_rejects_when_stock_history_is_one_day_late(
    tmp_path: Path,
) -> None:
    """ETF 已到 T 日时不能掩盖全部个股仍停留在 T-1。"""
    service = RobustDataService(
        FakeUniverseLoader(),  # type: ignore[arg-type]
        tmp_path,
        stock_candidate_limit=100,
        min_mainboard_universe=100,
        min_realtime_quote_coverage=0.9,
        min_history_coverage=0.9,
        min_etf_history_coverage=0.8,
    )

    with pytest.raises(DataCompletenessError, match="候选信号日行情覆盖率"):
        service._require_complete_signal_data(
            universe={
                "mainboard_count": 100,
                "realtime_quote_count": 100,
                "tencent_quote_count": 100,
                "rough_candidate_count": 100,
                "history_loaded_count": 100,
                "history_signal_date_count": 0,
                "etf_signal_date_count": 5,
                "universe_authoritative": 1,
            },
            etf_history_count=5,
        )


def test_formal_signal_rejects_zero_tencent_coverage_and_zero_candidates(
    tmp_path: Path,
) -> None:
    """正式信号必须证明腾讯行情参与，且不能把零候选解释为 100% 覆盖。"""
    service = RobustDataService(
        FakeUniverseLoader(),  # type: ignore[arg-type]
        tmp_path,
        min_mainboard_universe=100,
        min_realtime_quote_coverage=0.9,
        min_history_coverage=0.9,
        min_etf_history_coverage=0.8,
    )
    base = {
        "mainboard_count": 100,
        "realtime_quote_count": 100,
        "rough_candidate_count": 1,
        "history_loaded_count": 1,
        "history_signal_date_count": 1,
        "etf_signal_date_count": 5,
        "universe_authoritative": 1,
    }

    with pytest.raises(DataCompletenessError, match="腾讯实时行情覆盖率"):
        service._require_complete_signal_data(
            universe={**base, "tencent_quote_count": 0},
            etf_history_count=5,
        )

    with pytest.raises(DataCompletenessError, match="未产生任何个股候选"):
        service._require_complete_signal_data(
            universe={
                **base,
                "tencent_quote_count": 100,
                "rough_candidate_count": 0,
                "history_loaded_count": 0,
                "history_signal_date_count": 0,
            },
            etf_history_count=5,
        )


def test_formal_signal_uses_five_broad_etfs_for_completeness(
    tmp_path: Path,
) -> None:
    """行业 ETF 不能替代 robust_v2 的五只宽基完成数据验收。"""
    service = RobustDataService(
        FakeUniverseLoader(),  # type: ignore[arg-type]
        tmp_path,
        min_mainboard_universe=100,
        min_realtime_quote_coverage=0.9,
        min_history_coverage=0.9,
        min_etf_history_coverage=0.8,
    )

    with pytest.raises(DataCompletenessError, match=r"宽基 ETF 历史行情仅 3/5"):
        service._require_complete_signal_data(
            universe={
                "mainboard_count": 100,
                "realtime_quote_count": 100,
                "tencent_quote_count": 100,
                "rough_candidate_count": 1,
                "history_loaded_count": 1,
                "history_signal_date_count": 1,
                "etf_signal_date_count": 3,
                "universe_authoritative": 1,
            },
            etf_history_count=3,
        )


def test_formal_signal_error_lists_kline_source_failures(
    tmp_path: Path,
) -> None:
    """日K源全部失败时，DataCompletenessError 消息应列出尝试过的源和失败原因。"""

    class AllSourcesDownLoader(FakeUniverseLoader):
        """模拟全部日K数据源失败且无历史缓存。"""

        def get_batch_etf_history(self, codes, days=420, adjust="qfq"):
            """ETF 历史全部获取失败。"""
            return {}

        def get_batch_history_ext(self, codes, days=420, max_batch=1):
            """个股扩展历史全部获取失败。"""
            return {}

        def kline_source_failure_summary(self) -> str:
            """返回各源失败摘要。"""
            return (
                "sina: 456 Client Error; tencent: ReadTimeout; eastmoney: 返回空数据"
            )

    service = RobustDataService(
        AllSourcesDownLoader(),  # type: ignore[arg-type]
        tmp_path,
    )

    with pytest.raises(
        DataCompletenessError,
        match=r"主板股票池仅 5 只.*日K数据源尝试明细: sina: 456 Client Error.*eastmoney: 返回空数据",
    ):
        service.load_signal_data("20260709", 50_000)


def test_missing_position_quote_freezes_all_new_buys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """任一持仓缺少实时估值时，只能等待或减仓，不能先买入其他标的。"""
    ledger = PaperLedger(
        tmp_path / "paper_v2.db",
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
            reason="TEST_POSITION",
            date="20260708",
        )
    )
    runner = RobustV2Runner(
        broker,
        FakeLoader(),  # type: ignore[arg-type]
        RobustV2Config(enable_stock_enhancement=False, etf_min_avg_amount=0),
        tmp_path,
    )

    class MissingPositionQuoteData(FakeDataService):
        """只返回目标 ETF 行情，故意遗漏既有股票持仓。"""

        def load_execution_data(self, codes: set[str], signal_date: str):
            assert codes == {"510300", "600000"}
            history = {"510300": self.history["510300"]}
            quotes = {
                "510300": {
                    "price": float(self.history["510300"]["close"].iloc[-1]),
                    "prev_close": float(self.history["510300"]["close"].iloc[-1]),
                    "quote_time": "20260710094100",
                    "captured_at": "2026-07-10 09:41:01",
                    "source": "test",
                    "is_suspended": False,
                    "trade_status": 1,
                }
            }
            return self.snapshot, history, quotes

    runner.data = MissingPositionQuoteData()  # type: ignore[assignment]
    target = TargetPortfolio(
        account_id="paper_v2",
        strategy_version="robust_v2",
        signal_date="20260709",
        positions=(
            TargetPosition(
                code="510300",
                name="沪深300ETF",
                target_weight=0.24,
                reason="ETF_CORE_TREND",
                asset_type="etf",
            ),
        ),
        source_snapshot_hash="test-hash",
        cash_weight=0.76,
    )
    signal_id = ledger.record_signal(target)
    monkeypatch.setattr("robust_runner.previous_trading_day", lambda _date: "20260709")

    outcome = runner.execute_pending_target(
        "20260710",
        current_time=datetime(2026, 7, 10, 9, 41, 30),
    )

    assert outcome.reports == ()
    assert outcome.allocation is not None
    assert "持仓实时估值不完整" in outcome.allocation.skipped["510300"]
    assert ledger.query_signal_state(signal_id)["status"] == "retryable"
    assert [order.code for order in ledger.query_orders()] == ["600000"]
    broker.close()


@pytest.mark.parametrize("sell_failure", ["limit_down", "rejected", "partial"])
def test_unfilled_sales_do_not_fund_new_buys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sell_failure: str,
) -> None:
    """卖出受限、拒单或部分成交时，不能把预计回款用于买入。"""
    ledger = PaperLedger(tmp_path / "paper_v2.db", strict_order_source=False)
    broker = SQLitePaperBrokerAdapter(ledger=ledger)
    broker.connect()
    broker.place_order(
        OrderIntent(
            account_id="paper_v2",
            strategy_version="robust_v2",
            signal_date="20260708",
            code="600000",
            action="buy",
            price=10.0,
            shares=2000,
            date="20260708",
        )
    )
    runner = RobustV2Runner(broker, FakeLoader(), root_dir=tmp_path)  # type: ignore[arg-type]

    class RotationData(FakeDataService):
        def load_execution_data(self, codes: set[str], signal_date: str):
            self.history["600000"] = _etf_history("600000", 0)
            self.history["600000"]["close"] = 10.0
            snapshot, history, quotes = super().load_execution_data(codes, signal_date)
            if sell_failure == "limit_down":
                quotes["600000"]["price"] = 9.0
            return snapshot, history, quotes

    data = RotationData()
    runner.data = data  # type: ignore[assignment]
    target = TargetPortfolio(
        account_id="paper_v2",
        strategy_version="robust_v2",
        signal_date="20260709",
        positions=(TargetPosition("510300", 0.24, "ETF_ROTATION", "etf"),),
        source_snapshot_hash="rotation",
        cash_weight=0.76,
    )
    signal_id = ledger.record_signal(target)
    original_place_order = broker.place_order

    def place_order(order: OrderIntent) -> ExecutionReport:
        if order.action == "sell" and sell_failure == "rejected":
            return ExecutionReport(
                order_id="rejected-sell",
                status="rejected",
                code=order.code,
                action="sell",
                price=order.price,
                actual_price=0,
                shares=0,
                amount=0,
                message="卖出暂时失败",
            )
        if order.action == "sell" and sell_failure == "partial":
            return original_place_order(replace(order, shares=100))
        return original_place_order(order)

    monkeypatch.setattr(broker, "place_order", place_order)
    monkeypatch.setattr("robust_runner.previous_trading_day", lambda _date: "20260709")
    first = runner.execute_pending_target(
        "20260710",
        current_time=datetime(2026, 7, 10, 9, 36, 30),
    )

    assert not any(report.action == "buy" for report in first.reports)
    assert "510300" not in broker.query_positions()
    assert ledger.query_signal_state(signal_id)["status"] == "retryable"

    sell_failure = "recovered"
    data.execution_quote_time = "20260710093800"
    second = runner.execute_pending_target(
        "20260710",
        current_time=datetime(2026, 7, 10, 9, 38, 30),
    )
    assert [report.action for report in second.reports] == ["sell", "buy"]
    assert all(report.is_success for report in second.reports)
    assert "600000" not in broker.query_positions()
    assert ledger.query_signal_state(signal_id)["status"] == "completed"
    broker.close()


def test_preview_scan_uses_latest_completed_close(monkeypatch) -> None:
    """盘中预览必须回退到上一交易日，收盘后才允许使用当天。"""
    monkeypatch.setattr(
        "robust_runner._is_trading_day",
        lambda date, ignore_calendar: datetime.strptime(date, "%Y%m%d").weekday() < 5,
    )

    assert _latest_completed_trade_date(datetime(2026, 7, 13, 10, 0)) == "20260710"
    assert _latest_completed_trade_date(datetime(2026, 7, 13, 15, 6)) == "20260713"


def test_observation_end_date_is_strict_and_keeps_end_day_active() -> None:
    """观察期结束日必须可验证，且停止边界应落在结束日次日。"""
    end_date = _parse_observation_end_date("20260823")

    assert end_date is not None
    assert datetime(2026, 8, 23, 23, 59).date() <= end_date
    assert datetime(2026, 8, 24, 0, 0).date() > end_date
    assert _parse_observation_end_date(None) is None
    with pytest.raises(ValueError, match="YYYYMMDD"):
        _parse_observation_end_date("2026-08-23")


def test_runtime_loads_walk_forward_selection_and_etf_fallback(tmp_path: Path) -> None:
    """守护入口应加载有效维度、忽略旧维度并支持 ETF 回退。"""
    path = tmp_path / "selected.json"
    path.write_text(
        """{
  "selected_params": {
    "enable_etf_trend_filter": false,
    "stock_reversal_days": 999,
    "etf_min_20d_return": 0.0,
    "stock_min_earnings_yield": 0.04,
    "rebalance_days": 20,
    "stock_stop_pct": 0.09,
    "max_total_position": 0.60
  },
  "fallback_to_etf": true
}
""",
        encoding="utf-8",
    )

    config = load_runtime_config(path)

    assert config.etf_min_20d_return == 0.0
    assert config.stock_min_earnings_yield == 0.04
    assert config.max_total_position == 0.60
    assert config.rebalance_days == 20
    assert config.stock_stop_pct == 0.09
    assert config.enable_etf_trend_filter is True
    assert config.stock_reversal_days == 20
    assert config.enable_stock_enhancement is False


def test_runtime_keeps_stable_fields_from_legacy_selection(tmp_path: Path) -> None:
    """旧选择文件没有新维度时，仍应保留仓位、周期和止损参数。"""
    path = tmp_path / "legacy-selected.json"
    path.write_text(
        """{
  "selected_params": {
    "enable_etf_trend_filter": false,
    "stock_reversal_days": 10,
    "rebalance_days": 10,
    "stock_stop_pct": 0.07,
    "max_total_position": 0.70
  },
  "fallback_to_etf": false
}
""",
        encoding="utf-8",
    )

    config = load_runtime_config(path)

    assert config.etf_min_20d_return == -0.05
    assert config.stock_min_earnings_yield == 0.02
    assert config.max_total_position == 0.70
    assert config.rebalance_days == 10
    assert config.stock_stop_pct == 0.07
    assert config.enable_etf_trend_filter is True
    assert config.stock_reversal_days == 20
    assert config.enable_stock_enhancement is True


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


def test_daemon_sigterm_interrupts_poll_wait_and_releases_lease(
    monkeypatch, tmp_path: Path
) -> None:
    """SIGTERM 必须立即唤醒轮询，并在 launchd 超时前完成账本清理。"""

    class FakeLedger:
        """记录守护进程生命周期调用的最小账本替身。"""

        path = tmp_path / "paper.db"

        def __init__(self) -> None:
            self.finished_status = ""
            self.released = False

        def acquire_lease(self, _holder_id: str, _ttl_seconds: int) -> None:
            """模拟成功取得唯一写租约。"""

        def start_run(self, **_kwargs: object) -> SimpleNamespace:
            """返回可供 finish_run 使用的运行标识。"""
            return SimpleNamespace(run_id="run-1")

        def finish_run(self, _run_id: str, *, status: str) -> None:
            """记录最终运行状态。"""
            self.finished_status = status

        def release_lease(self, _holder_id: str) -> None:
            """记录租约已释放。"""
            self.released = True

    class FakeHeartbeat:
        """避免测试启动真实后台线程。"""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            """模拟续租线程启动。"""

        def stop(self) -> None:
            """模拟续租线程停止。"""

        def raise_if_failed(self) -> None:
            """模拟续租健康。"""

    handlers: dict[int, object] = {}
    handler_ready = threading.Event()

    def fake_signal(signum: int, handler: object) -> object:
        previous = handlers.get(signum, signal.SIG_DFL)
        handlers[signum] = handler
        if signum == signal.SIGTERM and callable(handler):
            handler_ready.set()
        return previous

    monkeypatch.setattr(runner_module, "LeaseHeartbeat", FakeHeartbeat)
    monkeypatch.setattr(runner_module, "validate_startup", lambda _config: None)
    monkeypatch.setattr(
        runner_module,
        "_startup_log",
        lambda _broker, _ledger, _config: ("commit", "config"),
    )
    monkeypatch.setattr(runner_module, "_is_trading_day", lambda *_args: False)
    monkeypatch.setattr(runner_module.signal, "signal", fake_signal)

    trigger_errors: list[str] = []

    def trigger_sigterm() -> None:
        if not handler_ready.wait(timeout=1):
            trigger_errors.append("SIGTERM handler was not installed")
            return
        handler = handlers[signal.SIGTERM]
        if not callable(handler):
            trigger_errors.append("SIGTERM handler is not callable")
            return
        handler(signal.SIGTERM, None)

    trigger = threading.Thread(target=trigger_sigterm)
    trigger.start()
    ledger = FakeLedger()
    runner = SimpleNamespace(
        config=SimpleNamespace(strategy_version="robust_v2"),
        ledger=ledger,
        broker=object(),
    )

    started = time.monotonic()
    result = run_daemon(runner, poll_seconds=60)  # type: ignore[arg-type]
    elapsed = time.monotonic() - started
    trigger.join(timeout=1)

    assert trigger_errors == []
    assert elapsed < 2
    assert result == 0
    assert ledger.finished_status == "completed"
    assert ledger.released is True


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
