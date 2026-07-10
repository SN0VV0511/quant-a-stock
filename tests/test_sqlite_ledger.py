"""paper_v2 SQLite 单账本回归测试。"""

from __future__ import annotations

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
