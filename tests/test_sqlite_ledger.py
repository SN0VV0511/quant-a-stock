"""paper_v2 SQLite 单账本回归测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from trading.ledger import LeaseUnavailableError, PaperLedger
from trading.models import OrderIntent, TargetPortfolio


def _order(
    code: str,
    action: str,
    date: str,
    shares: int = 100,
    price: float = 10.0,
) -> OrderIntent:
    """生成 robust_v2 测试订单。"""
    return OrderIntent(
        account_id="paper_v2",
        strategy_version="robust_v2",
        signal_date=date,
        code=code,
        action=action,  # type: ignore[arg-type]
        price=price,
        shares=shares,
        name=code,
        strategy="测试",
        strategy_tag="robust_v2",
        reason="TEST",
        date=date,
    )


def _ledger(tmp_path) -> PaperLedger:
    """创建并连接测试账本。"""
    ledger = PaperLedger(
        tmp_path / "paper_v2.db",
        initial_cash=50_000,
        strict_order_source=False,
    )
    ledger.connect()
    return ledger


def test_ledger_persists_cash_across_restart_and_is_idempotent(tmp_path) -> None:
    """重启不得重置 5 万元，重复幂等订单不得二次扣款。"""
    ledger = _ledger(tmp_path)
    order = _order("sh600000", "buy", "20260709")
    first = ledger.place_order(order)
    cash_after_first = ledger.query_cash()
    replay = ledger.place_order(order)
    ledger.close()

    reopened = _ledger(tmp_path)
    assert first.status == "filled"
    assert replay.order_id == first.order_id
    assert replay.raw["idempotent_replay"] is True
    assert reopened.query_cash() == cash_after_first
    assert reopened.query_cash() < 50_000


def test_ledger_enforces_t1_with_partial_sellable_lots(tmp_path) -> None:
    """仅前一交易日批次可卖，当日新增批次保持锁定。"""
    ledger = _ledger(tmp_path)
    ledger.place_order(_order("sh600000", "buy", "20260708"))
    ledger.place_order(_order("sh600000", "buy", "20260709"))

    sell = _order("sh600000", "sell", "20260709", shares=200, price=10.5)
    report = ledger.place_order(sell)
    positions = ledger.query_positions("20260709")

    assert report.status == "filled"
    assert report.shares == 100
    assert report.raw["partial_t1_fill"] is True
    assert positions["sh600000"]["shares"] == 100
    assert positions["sh600000"]["sellable_qty"] == 0


def test_ledger_rejects_same_day_round_trip(tmp_path) -> None:
    """7 月 9 日当日买入的普通股票不得当日卖出。"""
    ledger = _ledger(tmp_path)
    ledger.place_order(_order("sh600000", "buy", "20260709"))

    report = ledger.place_order(_order("sh600000", "sell", "20260709", price=9.5))

    assert report.status == "rejected"
    assert "T+1 locked" in report.message
    assert ledger.query_positions("20260709")["sh600000"]["shares"] == 100


def test_etf_trade_has_no_stamp_tax(tmp_path) -> None:
    """境内股票 ETF 卖出不得计提股票印花税。"""
    ledger = _ledger(tmp_path)
    ledger.place_order(_order("510300", "buy", "20260708", price=4.0))
    ledger.place_order(_order("510300", "sell", "20260709", price=4.1))

    sell_trade = [
        trade for trade in ledger.query_trades() if trade["action"] == "sell"
    ][0]
    assert sell_trade["stamp_tax"] == 0
    assert sell_trade["transfer_fee"] == 0


def test_single_writer_lease_expires_and_can_be_reacquired(tmp_path) -> None:
    """活动租约阻止第二实例，过期后允许接管。"""
    first = _ledger(tmp_path)
    second = PaperLedger(tmp_path / "paper_v2.db", initial_cash=50_000)
    second.connect()
    now = datetime(2026, 7, 10, 9, 0)
    first.acquire_lease("instance-a", 30, now=now)

    with pytest.raises(LeaseUnavailableError):
        second.acquire_lease("instance-b", 30, now=now + timedelta(seconds=5))

    second.acquire_lease("instance-b", 30, now=now + timedelta(seconds=31))
    assert second.release_lease("instance-b") is True


def test_production_ledger_rejects_orders_bypassing_allocator(tmp_path) -> None:
    """默认账本不允许旧策略或手工意图绕过唯一组合分配器。"""
    ledger = PaperLedger(tmp_path / "strict.db", initial_cash=50_000)
    ledger.connect()

    report = ledger.place_order(_order("sh600000", "buy", "20260709"))

    assert report.status == "rejected"
    assert "统一目标分配器" in report.message


def test_production_ledger_rechecks_limit_state_and_quote_evidence(tmp_path) -> None:
    """即使上层遗漏，账本也不能成交缺证据或涨停买单。"""
    ledger = PaperLedger(tmp_path / "strict.db", initial_cash=50_000)
    ledger.connect()
    base = replace(
        _order("600000", "buy", "20260709"),
        source="target_allocator",
    )
    missing = ledger.place_order(base)
    limit_up = ledger.place_order(
        replace(
            base,
            idempotency_key="limit-up-attempt",
            price=11.0,
            metadata={
                "data_health_checked": True,
                "execution_quote": {
                    "current_price": 11.0,
                    "trade_date": "20260709",
                    "quote_time": "2026-07-09 09:36:00",
                    "is_suspended": False,
                    "can_buy": False,
                    "can_sell": True,
                    "limit_type": "涨停",
                },
            },
        )
    )

    assert missing.status == "rejected"
    assert "缺少行情健康校验证据" in missing.message
    assert limit_up.status == "rejected"
    assert "不可买入" in limit_up.message


def test_executing_latest_signal_supersedes_older_pending_targets(tmp_path) -> None:
    """服务停机期间积压的旧目标不得在新目标之后继续追单。"""
    ledger = _ledger(tmp_path)

    def target(date: str) -> TargetPortfolio:
        return TargetPortfolio(
            account_id="paper_v2",
            strategy_version="robust_v2",
            signal_date=date,
            positions=(),
            source_snapshot_hash=f"hash-{date}",
            cash_weight=1.0,
        )

    old_id = ledger.record_signal(target("20260703"))
    latest_id = ledger.record_signal(target("20260709"))
    pending = ledger.pending_target("20260710")

    assert pending is not None
    assert pending[0] == latest_id
    assert pending[0] != old_id
    ledger.mark_signal_executed(latest_id)
    assert ledger.pending_target("20260710") is None


def test_signal_claim_is_atomic_and_retryable(tmp_path) -> None:
    """同一目标只能被一个执行者领取，暂时失败后到点才能再次领取。"""
    ledger = _ledger(tmp_path)
    target = TargetPortfolio(
        account_id="paper_v2",
        strategy_version="robust_v2",
        signal_date="20260709",
        positions=(),
        source_snapshot_hash="hash",
        cash_weight=1.0,
    )
    signal_id = ledger.record_signal(target)
    now = datetime(2026, 7, 10, 9, 36)

    first = ledger.claim_pending_target(
        "20260710",
        signal_date="20260709",
        now=now,
    )

    assert first is not None
    assert first.attempt_count == 1
    assert (
        ledger.claim_pending_target(
            "20260710",
            signal_date="20260709",
            now=now,
        )
        is None
    )
    ledger.mark_signal_retryable(signal_id, "模拟行情超时", now=now)
    assert (
        ledger.claim_pending_target(
            "20260710",
            signal_date="20260709",
            now=now + timedelta(seconds=59),
        )
        is None
    )
    second = ledger.claim_pending_target(
        "20260710",
        signal_date="20260709",
        now=now + timedelta(seconds=60),
    )
    assert second is not None
    assert second.attempt_count == 2


def test_daily_job_and_nav_are_idempotent_across_restarts(tmp_path) -> None:
    """收盘任务和每日净值按账户日期唯一，run_id 重启不能制造重复点。"""
    ledger = _ledger(tmp_path)
    first = ledger.start_run(
        strategy_version="robust_v2",
        code_commit="a",
        config_hash="a",
        data_version="a",
    )
    assert ledger.claim_daily_job("close_cycle", "20260710") is True
    ledger.record_snapshot(
        {},
        snapshot_date="20260710",
        data_version="a",
        strategy_version="robust_v2",
    )
    ledger.finish_daily_job("close_cycle", "20260710")
    assert ledger.claim_daily_job("close_cycle", "20260710") is False
    ledger.finish_run(first.run_id)

    second = ledger.start_run(
        strategy_version="robust_v2",
        code_commit="b",
        config_hash="b",
        data_version="b",
    )
    ledger.record_snapshot(
        {},
        snapshot_date="20260710",
        data_version="b",
        strategy_version="robust_v2",
    )
    review = ledger.build_review("20260710", "20260710")

    assert len(review.snapshots) == 1
    assert review.snapshots[0]["run_id"] == second.run_id


def test_online_backup_preserves_account_state(tmp_path) -> None:
    """WAL 模式运行中生成的备份必须可以独立打开并恢复余额。"""
    ledger = _ledger(tmp_path)
    ledger.place_order(_order("600000", "buy", "20260709"))
    expected_cash = ledger.query_cash()

    backup_path = ledger.backup_to(tmp_path / "backups" / "paper_v2.db")
    restored = PaperLedger(backup_path, initial_cash=50_000, strict_order_source=False)
    restored.connect()

    assert restored.query_cash() == expected_cash
    restored.require_integrity()
    restored.close()


def test_execution_price_excludes_commission_but_position_cost_includes_it(
    tmp_path,
) -> None:
    """成交价只反映滑点，佣金进入持仓成本，保持与券商回报字段一致。"""
    ledger = _ledger(tmp_path)
    report = ledger.place_order(_order("600000", "buy", "20260709", price=10.0))
    position = ledger.query_positions("20260709")["600000"]

    assert report.actual_price == pytest.approx(10.01)
    assert position["avg_cost"] > report.actual_price


def test_snapshot_matches_prefixed_positions_with_raw_quote_codes(tmp_path) -> None:
    """腾讯六位代码行情必须能更新带市场前缀的账本持仓净值。"""
    ledger = _ledger(tmp_path)
    ledger.place_order(_order("sh600000", "buy", "20260709", price=10.0))

    snapshot = ledger.query_snapshot({"600000": 12.0})

    assert snapshot.positions[0]["current_price"] == 12.0
    assert snapshot.positions[0]["market_value"] == 1200.0


def test_round_trip_realized_profit_includes_buy_and_sell_costs(tmp_path) -> None:
    """完整卖出后的已实现收益必须等于账户现金变化，不能漏掉买入佣金。"""
    ledger = _ledger(tmp_path)
    ledger.place_order(_order("600000", "buy", "20260708", price=10.0))
    sell = ledger.place_order(_order("600000", "sell", "20260709", price=10.2))

    assert sell.status == "filled"
    assert sell.profit == pytest.approx(ledger.query_cash() - 50_000, abs=0.01)
