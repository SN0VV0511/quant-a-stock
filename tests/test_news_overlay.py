"""新闻事件研判覆盖层(加载校验与 robust_runner 集成)测试。"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from news_overlay import (
    MIN_ACTION_CONFIDENCE,
    NewsVerdict,
    load_news_overlay,
    news_overlay_path,
)
from robust_runner import NewsOverlayPlan, RobustV2Runner, SignalData
from strategies.robust_v2 import RobustV2Config, StockScanResult
from trading.brokers import SQLitePaperBrokerAdapter
from trading.ledger import PaperLedger
from trading.models import (
    MarketSnapshot,
    OrderIntent,
    TargetPortfolio,
    TargetPosition,
)

TRADE_DATE = "20260709"
EXEC_DATE = "20260710"


def _write_overlay(
    root: Path, date: str, payload: dict, *, mtime: float | None = None
) -> Path:
    """写入当日研判文件,可显式指定 mtime 以模拟盘中覆盖更新。"""
    directory = Path(root) / "data" / "news_overlay"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{date}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _overlay_payload(
    verdicts: list[dict], generated_at: str = "2026-07-09T18:00:00"
) -> dict:
    """构造合法研判文件负载。"""
    return {"generated_at": generated_at, "verdicts": verdicts}


def _verdict(
    code: str,
    action: str = "risk_sell",
    confidence: float = 0.85,
    reason: str = "负面舆情发酵",
    sources: tuple[str, ...] = ("新闻标题1",),
    name: str = "测试股票",
) -> dict:
    """构造单条研判。"""
    return {
        "code": code,
        "name": name,
        "action": action,
        "confidence": confidence,
        "reason": reason,
        "sources": list(sources),
    }


class FakeLoader:
    """仅满足 runner 测试所需接口。"""

    def close(self) -> None:
        """兼容真实加载器。"""


def _stock_history(code: str, base_price: float = 10.0) -> pd.DataFrame:
    """生成包含信号日与上一交易日的历史行情。"""
    closes = [base_price - 0.2, base_price - 0.1, base_price]
    return pd.DataFrame(
        {
            "date": ["20260707", "20260708", TRADE_DATE],
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [5_000_000] * 3,
            "amount": [60_000_000] * 3,
        }
    )


class FakeDataService:
    """返回固定的收盘和 T+1 执行行情。"""

    def __init__(self) -> None:
        self.snapshot = MarketSnapshot(
            trade_date=TRADE_DATE,
            previous_trade_date="20260708",
            source="test",
            adjustment="qfq",
            data_hash="test-hash",
            freshness_seconds=0,
        )

    def load_signal_data(self, trade_date: str, account_value: float) -> SignalData:
        """返回固定收盘数据。"""
        return SignalData(self.snapshot, {}, {}, {}, {})

    def load_execution_data(self, codes: set[str], signal_date: str):
        """返回与信号日前收盘一致的次日实时行情。"""
        history = {code: _stock_history(code) for code in codes}
        quotes = {
            code: {
                "name": code,
                "price": 10.0,
                "prev_close": 10.0,
                "quote_time": f"{EXEC_DATE}100100",
                "captured_at": "2026-07-10 10:01:01",
                "source": "test",
                "is_suspended": False,
                "trade_status": 1,
            }
            for code in codes
        }
        return self.snapshot, history, quotes


class FakeStrategy:
    """按给定候选顺序选择前 N 只的策略替身,隔离新闻覆盖层逻辑。"""

    name = "A股稳健策略V2"

    def __init__(
        self,
        candidates: tuple[str, ...] = (),
        weights: dict[str, float] | None = None,
        select_count: int = 2,
    ) -> None:
        self.candidates = tuple(candidates)
        self.weights = weights or {}
        self.select_count = select_count

    def scan_stocks(
        self,
        history_map,
        snapshot,
        account_value: float,
        name_map=None,
    ) -> StockScanResult:
        """返回固定候选列表。"""
        return StockScanResult(
            input_count=len(self.candidates),
            candidates=tuple(
                {"code": code, "name": code, "price": 10.0}
                for code in self.candidates
            ),
            filter_counts={},
        )

    def generate_target(
        self,
        snapshot,
        etf_history,
        stock_history,
        account_value: float,
        name_map=None,
        *,
        stock_scan_result: StockScanResult | None = None,
    ) -> TargetPortfolio:
        """按候选顺序选择前 N 只并使用固定权重。"""
        scan = stock_scan_result or self.scan_stocks(
            stock_history, snapshot, account_value, name_map
        )
        positions = tuple(
            TargetPosition(
                code=str(row["code"]),
                name=str(row["code"]),
                asset_type="stock",
                target_weight=self.weights.get(str(row["code"]), 0.08),
                reason="FAKE_TEST",
            )
            for row in scan.candidates[: self.select_count]
        )
        exposure = sum(position.target_weight for position in positions)
        return TargetPortfolio(
            account_id="paper_v2",
            strategy_version="robust_v2",
            signal_date=snapshot.trade_date,
            positions=positions,
            source_snapshot_hash=snapshot.data_hash,
            cash_weight=round(1 - exposure, 6),
        )


def _build_runner(
    tmp_path: Path,
    *,
    holdings: dict[str, int] | None = None,
    candidates: tuple[str, ...] = (),
    weights: dict[str, float] | None = None,
    select_count: int = 2,
) -> tuple[RobustV2Runner, SQLitePaperBrokerAdapter]:
    """构建带既有持仓和替身策略/数据的 runner。"""
    ledger = PaperLedger(
        tmp_path / "paper_v2.db",
        initial_cash=50_000,
        strict_order_source=False,
    )
    broker = SQLitePaperBrokerAdapter(ledger=ledger)
    broker.connect()
    for code, shares in (holdings or {}).items():
        ledger.place_order(
            OrderIntent(
                account_id="paper_v2",
                strategy_version="robust_v2",
                signal_date="20260708",
                code=code,
                action="buy",
                price=10.0,
                shares=shares,
                name=f"股票{code}",
                strategy="测试",
                strategy_tag="robust_v2",
                reason="TEST_SEED",
                date="20260708",
            )
        )
    runner = RobustV2Runner(
        broker,
        FakeLoader(),  # type: ignore[arg-type]
        RobustV2Config(enable_stock_enhancement=False, etf_min_avg_amount=0),
        tmp_path,
    )
    runner.data = FakeDataService()  # type: ignore[assignment]
    runner.strategy = FakeStrategy(  # type: ignore[assignment]
        candidates, weights=weights, select_count=select_count
    )
    return runner, broker


# ==================== 模块加载与 schema 校验 ====================


def test_load_overlay_returns_none_for_missing_or_corrupt_files(
    tmp_path: Path,
) -> None:
    """文件缺失、JSON 损坏或结构不符时必须返回 None 且不抛异常。"""
    assert load_news_overlay(TRADE_DATE, tmp_path) is None

    path = _write_overlay(tmp_path, TRADE_DATE, _overlay_payload([]))
    assert load_news_overlay(TRADE_DATE, tmp_path) is not None  # 空列表合法

    path.write_text("{不是合法JSON", encoding="utf-8")
    assert load_news_overlay(TRADE_DATE, tmp_path) is None

    path.write_text('["不是对象"]', encoding="utf-8")
    assert load_news_overlay(TRADE_DATE, tmp_path) is None

    path.write_text(json.dumps({"verdicts": []}), encoding="utf-8")
    assert load_news_overlay(TRADE_DATE, tmp_path) is None  # 缺 generated_at

    path.write_text(
        json.dumps({"generated_at": "不是ISO时间", "verdicts": []}), encoding="utf-8"
    )
    assert load_news_overlay(TRADE_DATE, tmp_path) is None

    path.write_text(json.dumps({"generated_at": "2026-07-09T18:00:00"}), encoding="utf-8")
    assert load_news_overlay(TRADE_DATE, tmp_path) is None  # 缺 verdicts

    path.write_text(
        json.dumps({"generated_at": "2026-07-09T18:00:00", "verdicts": {}}),
        encoding="utf-8",
    )
    assert load_news_overlay(TRADE_DATE, tmp_path) is None  # verdicts 不是列表

    assert load_news_overlay("2026-07-09", tmp_path) is None  # 日期参数非法


def test_load_overlay_drops_invalid_entries_and_keeps_highest_confidence(
    tmp_path: Path,
) -> None:
    """非法条目丢弃;重复 code 保留 confidence 最高的一条。"""
    _write_overlay(
        tmp_path,
        TRADE_DATE,
        _overlay_payload(
            [
                _verdict("600267"),
                _verdict("60026", "risk_sell"),  # code 非 6 位
                _verdict("sh600267", "risk_sell"),  # code 含前缀
                _verdict("600268", "hold"),  # action 非法
                _verdict("600269", "risk_sell", 1.5),  # confidence 超界
                _verdict("600270", "risk_sell", "0.8"),  # confidence 非数值
                {
                    "code": "600271",
                    "name": "缺动作",
                    "confidence": 0.8,
                    "reason": "缺字段",
                    "sources": [],
                },  # 缺 action
                _verdict("600267", "risk_sell", 0.60, reason="低置信重复"),
                _verdict("600272", "boost", 0.75, reason="利好", sources=("标题",)),
            ],
            generated_at="2026-07-09T18:00:00Z",  # Z 后缀应可解析
        ),
    )

    overlay = load_news_overlay(TRADE_DATE, tmp_path)

    assert overlay is not None
    by_code = overlay.by_code
    assert set(by_code) == {"600267", "600272"}
    assert by_code["600267"].confidence == pytest.approx(0.85)
    assert by_code["600267"].reason == "负面舆情发酵"
    assert by_code["600272"].action == "boost"
    assert by_code["600272"].sources == ("标题",)


def test_verdict_actionable_requires_non_watch_and_min_confidence() -> None:
    """watch 或 confidence<0.7 的研判不得视为可执行。"""
    assert MIN_ACTION_CONFIDENCE == 0.7
    assert (
        NewsVerdict(
            code="600267",
            name="海正药业",
            action="risk_sell",
            confidence=0.69,
            reason="低置信",
            sources=(),
        ).is_actionable
        is False
    )
    assert (
        NewsVerdict(
            code="600267",
            name="海正药业",
            action="watch",
            confidence=0.99,
            reason="观察",
            sources=(),
        ).is_actionable
        is False
    )
    assert (
        NewsVerdict(
            code="600267",
            name="海正药业",
            action="boost",
            confidence=0.7,
            reason="利好",
            sources=(),
        ).is_actionable
        is True
    )


# ==================== 收盘目标路径集成 ====================


def test_plan_news_overlay_builds_forced_exit_order(
    tmp_path: Path,
) -> None:
    """已持仓的 risk_sell 应生成 NEWS_RISK_EXIT 订单并携带研判审计字段。"""
    runner, broker = _build_runner(
        tmp_path, holdings={"600267": 1000}, candidates=("600001",)
    )
    _write_overlay(
        tmp_path,
        TRADE_DATE,
        _overlay_payload(
            [_verdict("600267", "risk_sell", 0.85, reason="风险事件", sources=("标题A",))]
        ),
    )

    plan = runner._plan_news_overlay(
        load_news_overlay(TRADE_DATE, tmp_path),
        positions=broker.query_positions(),
        candidate_codes=("600001",),
        trade_date=TRADE_DATE,
    )

    assert plan.risk_sell_codes == frozenset({"600267"})
    assert plan.boost_codes == frozenset()
    assert len(plan.exit_orders) == 1
    order = plan.exit_orders[0]
    assert order.code == "600267"
    assert order.action == "sell"
    assert order.reason == "NEWS_RISK_EXIT"
    assert order.shares == 1000
    assert order.metadata["news_confidence"] == pytest.approx(0.85)
    assert order.metadata["news_reason"] == "风险事件"
    assert order.metadata["news_sources"] == ["标题A"]
    broker.close()


def test_out_of_whitelist_verdicts_are_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """非持仓、非当日候选的 code 一律忽略,只打 DEBUG 日志。"""
    runner, broker = _build_runner(
        tmp_path, holdings={"600267": 1000}, candidates=("600001",)
    )
    _write_overlay(
        tmp_path,
        TRADE_DATE,
        _overlay_payload(
            [
                _verdict("600999", "risk_sell", 0.9),
                _verdict("600888", "boost", 0.9),
            ]
        ),
    )

    with caplog.at_level(logging.DEBUG, logger="robust_runner"):
        plan = runner._plan_news_overlay(
            load_news_overlay(TRADE_DATE, tmp_path),
            positions=broker.query_positions(),
            candidate_codes=("600001",),
            trade_date=TRADE_DATE,
        )

    assert plan.risk_sell_codes == frozenset()
    assert plan.boost_codes == frozenset()
    assert plan.exit_orders == ()
    assert any("白名单外" in message for message in caplog.messages)
    assert all("白名单外" in message for message in caplog.messages)
    broker.close()


def test_risk_sell_excludes_candidate_and_forces_exit_on_next_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """收盘剔除风险标的并生成强卖计划;T+1 执行时卖单改标 NEWS_RISK_EXIT。"""
    runner, broker = _build_runner(
        tmp_path,
        holdings={"600267": 1000},
        candidates=("600267", "600001", "600519", "600002"),
    )
    _write_overlay(
        tmp_path,
        TRADE_DATE,
        _overlay_payload(
            [
                _verdict(
                    "600267",
                    "risk_sell",
                    0.85,
                    reason="监管立案调查",
                    sources=("公司公告：立案调查",),
                ),
                _verdict("600519", "risk_sell", 0.5),  # 置信度不足:候选保留
                _verdict("600002", "watch", 0.95),  # watch:不动作
            ]
        ),
    )
    monkeypatch.setattr(
        "robust_runner.previous_trading_day", lambda _date: TRADE_DATE
    )
    placed: list[OrderIntent] = []
    original_place = broker.place_order

    def _capture(order: OrderIntent):
        placed.append(order)
        return original_place(order)

    monkeypatch.setattr(broker, "place_order", _capture)

    with caplog.at_level(logging.INFO, logger="robust_runner"):
        target = runner.generate_close_target(TRADE_DATE, force=True)

    assert target is not None
    target_codes = [position.code for position in target.positions]
    assert "600267" not in target_codes
    assert "600001" in target_codes
    assert "600519" in target_codes
    assert any(
        "新闻强制卖出订单已生成" in message and "600267" in message
        for message in caplog.messages
    )

    outcome = runner.execute_pending_target(
        EXEC_DATE, current_time=datetime(2026, 7, 10, 10, 1, 30)
    )

    exits = [order for order in placed if order.code == "600267"]
    assert len(exits) == 1
    assert exits[0].action == "sell"
    assert exits[0].reason == "NEWS_RISK_EXIT"
    assert exits[0].metadata["news_confidence"] == pytest.approx(0.85)
    assert exits[0].metadata["news_reason"] == "监管立案调查"
    assert exits[0].metadata["news_sources"] == ["公司公告：立案调查"]
    assert all(report.status == "filled" for report in outcome.reports)
    assert "600267" not in broker.query_positions()
    broker.close()


def test_boost_ranks_candidate_top_and_multiplies_weight(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """boost 候选置顶入选,入选后目标权重 ×1.25。"""
    runner, broker = _build_runner(
        tmp_path,
        candidates=("600001", "600002", "600100"),
        weights={"600100": 0.08},
    )
    _write_overlay(
        tmp_path,
        TRADE_DATE,
        _overlay_payload(
            [_verdict("600100", "boost", 0.8, reason="业绩超预期", sources=("半年报点评",))]
        ),
    )

    with caplog.at_level(logging.INFO, logger="robust_runner"):
        target = runner.generate_close_target(TRADE_DATE, force=True)

    assert target is not None
    codes = [position.code for position in target.positions]
    assert codes == ["600100", "600001"]
    boosted = target.positions[0]
    assert boosted.target_weight == pytest.approx(0.10)  # 0.08 × 1.25
    assert "NEWS_BOOST" in boosted.reason
    assert target.positions[1].target_weight == pytest.approx(0.08)
    assert any(
        "新闻利好加权" in message and "600100" in message
        for message in caplog.messages
    )
    broker.close()


def _target_with_positions(positions: tuple[TargetPosition, ...]) -> TargetPortfolio:
    """构造用于权重封顶单测的目标组合。"""
    exposure = sum(position.target_weight for position in positions)
    return TargetPortfolio(
        account_id="paper_v2",
        strategy_version="robust_v2",
        signal_date=TRADE_DATE,
        positions=positions,
        source_snapshot_hash="test-hash",
        cash_weight=round(1 - exposure, 6),
    )


def test_boost_weight_capped_by_single_stock_and_total_position(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """boost 加权必须同时受单票上限和总仓位硬约束截断。"""
    runner, broker = _build_runner(tmp_path)
    verdict = NewsVerdict(
        code="600100",
        name="测试股票",
        action="boost",
        confidence=0.9,
        reason="利好",
        sources=("标题",),
    )
    capped_plan = NewsOverlayPlan(
        frozenset(), frozenset({"600100"}), {"600100": verdict}, ()
    )

    with caplog.at_level(logging.INFO, logger="robust_runner"):
        # 单票上限:0.14 × 1.25 = 0.175 截断到 max_single_stock=0.16。
        single_capped = runner._apply_news_overlay_to_target(
            _target_with_positions(
                (
                    TargetPosition("600100", 0.14, "FAKE", "stock"),
                    TargetPosition("600002", 0.14, "FAKE", "stock"),
                    TargetPosition("600003", 0.10, "FAKE", "stock"),
                )
            ),
            capped_plan,
        )
        # 总仓位上限:其余持仓 0.66 后仅剩 0.14 空间,加权不突破 80%。
        total_capped = runner._apply_news_overlay_to_target(
            _target_with_positions(
                (
                    TargetPosition("600100", 0.14, "FAKE", "stock"),
                    TargetPosition("600002", 0.16, "FAKE", "stock"),
                    TargetPosition("600003", 0.14, "FAKE", "stock"),
                    TargetPosition("600004", 0.12, "FAKE", "stock"),
                    TargetPosition("600005", 0.12, "FAKE", "stock"),
                    TargetPosition("600006", 0.12, "FAKE", "stock"),
                )
            ),
            capped_plan,
        )

    boosted_single = single_capped.positions[0]
    assert boosted_single.target_weight == pytest.approx(0.16)
    assert single_capped.exposure <= runner.config.max_total_position + 1e-9
    boosted_total = total_capped.positions[0]
    assert boosted_total.target_weight == pytest.approx(0.14)  # 无空间,保持原权重
    assert total_capped.exposure <= runner.config.max_total_position + 1e-9
    assert any("触及单票或总仓位上限" in message for message in caplog.messages)
    broker.close()


# ==================== 盘中监控路径集成 ====================


def _sell_orders(broker: SQLitePaperBrokerAdapter) -> list:
    """返回账本中的卖单(忽略测试建仓买单)。"""
    return [order for order in broker.query_orders() if order.action == "sell"]


def _capture_orders(
    monkeypatch: pytest.MonkeyPatch, broker: SQLitePaperBrokerAdapter
) -> list[OrderIntent]:
    """记录提交到 broker 的订单意图。"""
    placed: list[OrderIntent] = []
    original_place = broker.place_order

    def _capture(order: OrderIntent):
        placed.append(order)
        return original_place(order)

    monkeypatch.setattr(broker, "place_order", _capture)
    return placed


def test_monitor_sells_on_mtime_update_and_not_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """盘中文件更新触发增量强卖;mtime 不变不重复加载或下单。"""
    runner, broker = _build_runner(
        tmp_path, holdings={"600267": 1000, "600519": 500}
    )
    placed = _capture_orders(monkeypatch, broker)

    # 文件尚未生成:不动作、不抛异常。
    assert runner.monitor_news_overlay(
        EXEC_DATE, current_time=datetime(2026, 7, 10, 10, 0, 0)
    ) == ()
    assert _sell_orders(broker) == []

    _write_overlay(
        tmp_path,
        EXEC_DATE,
        _overlay_payload(
            [
                _verdict(
                    "600267", "risk_sell", 0.85, reason="负面报道", sources=("新闻标题1",)
                )
            ]
        ),
        mtime=1000.0,
    )
    reports = runner.monitor_news_overlay(
        EXEC_DATE, current_time=datetime(2026, 7, 10, 10, 1, 0)
    )
    assert [report.code for report in reports] == ["600267"]
    assert all(report.status == "filled" for report in reports)
    first_order = placed[0]
    assert first_order.reason == "NEWS_RISK_EXIT"
    assert first_order.source == "robust_v2_monitor"
    assert first_order.metadata["news_confidence"] == pytest.approx(0.85)
    assert first_order.metadata["news_reason"] == "负面报道"

    # mtime 未变化:本轮直接跳过。
    assert runner.monitor_news_overlay(
        EXEC_DATE, current_time=datetime(2026, 7, 10, 10, 2, 0)
    ) == ()
    assert len(_sell_orders(broker)) == 1

    # 同路径覆盖(mtime 变化):只处理新增研判,已卖标的不重复下单。
    _write_overlay(
        tmp_path,
        EXEC_DATE,
        _overlay_payload(
            [
                _verdict("600267", "risk_sell", 0.85),
                _verdict(
                    "600519", "risk_sell", 0.9, reason="减持公告", sources=("减持公告",)
                ),
            ]
        ),
        mtime=2000.0,
    )
    second_reports = runner.monitor_news_overlay(
        EXEC_DATE, current_time=datetime(2026, 7, 10, 10, 3, 0)
    )
    assert [report.code for report in second_reports] == ["600519"]
    assert sorted(order.code for order in _sell_orders(broker)) == [
        "600267",
        "600519",
    ]
    broker.close()


def test_monitor_skips_low_confidence_watch_boost_and_unheld(
    tmp_path: Path,
) -> None:
    """confidence<0.7、watch、boost 以及未持仓标的盘中一律不交易。"""
    runner, broker = _build_runner(tmp_path, holdings={"600267": 1000})
    _write_overlay(
        tmp_path,
        EXEC_DATE,
        _overlay_payload(
            [
                _verdict("600267", "risk_sell", 0.69),  # 置信度不足
                _verdict("600519", "risk_sell", 0.9),  # 未持仓
                _verdict("600001", "watch", 0.95),
                _verdict("600002", "boost", 0.9),
            ]
        ),
        mtime=1000.0,
    )

    assert (
        runner.monitor_news_overlay(
            EXEC_DATE, current_time=datetime(2026, 7, 10, 10, 1, 0)
        )
        == ()
    )
    assert _sell_orders(broker) == []
    # 已终结的研判不会在下一轮重复触发。
    assert runner.monitor_news_overlay(
        EXEC_DATE, current_time=datetime(2026, 7, 10, 10, 2, 0)
    ) == ()
    broker.close()


def test_monitor_defers_when_position_t1_locked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T+1 锁定导致无可卖数量时不下单,交给后续执行路径。"""
    runner, broker = _build_runner(tmp_path, holdings={"600267": 1000})
    locked_positions = {
        "600267": {
            "code": "600267",
            "name": "海正药业",
            "shares": 1000,
            "sellable_qty": 0,
            "avg_cost": 10.0,
            "current_price": 9.5,
        }
    }
    monkeypatch.setattr(broker, "query_positions", lambda: locked_positions)
    _write_overlay(
        tmp_path,
        EXEC_DATE,
        _overlay_payload([_verdict("600267", "risk_sell", 0.9)]),
        mtime=1000.0,
    )

    with caplog.at_level(logging.INFO, logger="robust_runner"):
        reports = runner.monitor_news_overlay(
            EXEC_DATE, current_time=datetime(2026, 7, 10, 10, 1, 0)
        )

    assert reports == ()
    assert _sell_orders(broker) == []
    assert any("T+1" in message for message in caplog.messages)
    broker.close()


def test_monitor_survives_corrupt_rewrite_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """覆盖成坏文件时保留旧状态不出单;再次覆盖合法文件后恢复正常。"""
    runner, broker = _build_runner(tmp_path, holdings={"600267": 1000, "600519": 500})
    _capture_orders(monkeypatch, broker)
    path = news_overlay_path(EXEC_DATE, tmp_path)
    _write_overlay(
        tmp_path,
        EXEC_DATE,
        _overlay_payload([_verdict("600267", "risk_sell", 0.85)]),
        mtime=1000.0,
    )
    assert (
        runner.monitor_news_overlay(
            EXEC_DATE, current_time=datetime(2026, 7, 10, 10, 1, 0)
        )
        != ()
    )

    path.write_text("{坏文件", encoding="utf-8")
    os.utime(path, (2000.0, 2000.0))
    assert (
        runner.monitor_news_overlay(
            EXEC_DATE, current_time=datetime(2026, 7, 10, 10, 2, 0)
        )
        == ()
    )
    assert len(_sell_orders(broker)) == 1

    _write_overlay(
        tmp_path,
        EXEC_DATE,
        _overlay_payload(
            [_verdict("600519", "risk_sell", 0.9, reason="新的风险", sources=("标题",))]
        ),
        mtime=3000.0,
    )
    reports = runner.monitor_news_overlay(
        EXEC_DATE, current_time=datetime(2026, 7, 10, 10, 3, 0)
    )
    assert [report.code for report in reports] == ["600519"]
    broker.close()
