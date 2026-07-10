"""paper_v2 归档、显式日期月报和日报测试。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from reports.ledger_report import build_daily_ledger_report, format_daily_ledger_report
from scripts.monthly_review import build_review
from scripts.paper_v2_init import initialize_paper_v2
from scripts.paper_v2_acceptance import run_acceptance
from trading.ledger import PaperLedger
from trading.models import OrderIntent


def _order(action: str, date: str, price: float) -> OrderIntent:
    """生成报告测试订单。"""
    return OrderIntent(
        account_id="paper_v2",
        strategy_version="robust_v2",
        signal_date=date,
        code="sh600000",
        action=action,  # type: ignore[arg-type]
        price=price,
        shares=100,
        name="浦发银行",
        strategy="A股稳健策略V2",
        strategy_tag="robust_v2",
        reason="TEST",
        date=date,
    )


def test_init_archives_copies_readonly_and_never_resets_existing_ledger(
    tmp_path: Path,
) -> None:
    """旧证据应复制到只读归档，原件保留，已有账本拒绝重置。"""
    state = tmp_path / "data" / "portfolio_state.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"cash": 51000}\n', encoding="utf-8")
    ledger_path = tmp_path / "data" / "paper_v2.db"

    preview = initialize_paper_v2(tmp_path, ledger_path=ledger_path, confirm=False)
    result = initialize_paper_v2(tmp_path, ledger_path=ledger_path, confirm=True)
    second = initialize_paper_v2(tmp_path, ledger_path=ledger_path, confirm=True)

    assert preview.changed is False
    assert result.changed is True
    assert state.exists()
    archived = Path(result.archive_dir or "") / "data" / "portfolio_state.json"
    assert archived.read_text(encoding="utf-8") == state.read_text(encoding="utf-8")
    assert archived.stat().st_mode & 0o222 == 0
    assert second.changed is False
    assert "拒绝重置" in second.message


def test_ledger_daily_and_explicit_monthly_report_include_cost_versions(
    tmp_path: Path,
) -> None:
    """日报/月报应带成本、数据版本、策略版本及同一运行批次。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ledger = PaperLedger(
        data_dir / "paper_v2.db",
        initial_cash=50_000,
        strict_order_source=False,
    )
    ledger.connect()
    run = ledger.start_run(
        strategy_version="robust_v2",
        code_commit="abc",
        config_hash="cfg",
        data_version="data-v1",
    )
    ledger.record_snapshot(
        {},
        snapshot_date="20260708",
        data_version="data-v1",
        strategy_version="robust_v2",
    )
    ledger.place_order(_order("buy", "20260708", 10.0))
    ledger.place_order(_order("sell", "20260709", 10.5))
    ledger.record_snapshot(
        {},
        snapshot_date="20260709",
        data_version="data-v2",
        strategy_version="robust_v2",
        benchmark_return=0.01,
    )

    daily = build_daily_ledger_report(ledger, "20260709", run_id=run.run_id)
    monthly = build_review(
        tmp_path,
        start_date="20260708",
        end_date="20260709",
        run_id=run.run_id,
    )

    assert daily.total_cost > 0
    assert daily.commission > 0
    assert daily.strategy_version == "robust_v2"
    assert daily.data_version == "data-v2"
    assert "换手率" in format_daily_ledger_report(daily)
    assert monthly.account_id == "paper_v2"
    assert monthly.run_id == run.run_id
    assert monthly.total_cost > 0
    assert monthly.strategy_version == "robust_v2"
    ledger.close()


def test_monthly_report_rejects_mixed_run_ids(tmp_path: Path) -> None:
    """未指定 run_id 时不得把两次部署会话拼成一份月报。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ledger = PaperLedger(data_dir / "paper_v2.db", initial_cash=50_000)
    ledger.connect()
    first = ledger.start_run(
        strategy_version="robust_v2",
        code_commit="a",
        config_hash="a",
        data_version="a",
    )
    ledger.record_snapshot(
        {}, snapshot_date="20260708", data_version="a", strategy_version="robust_v2"
    )
    ledger.finish_run(first.run_id)
    second = ledger.start_run(
        strategy_version="robust_v2",
        code_commit="b",
        config_hash="b",
        data_version="b",
    )
    ledger.record_snapshot(
        {}, snapshot_date="20260709", data_version="b", strategy_version="robust_v2"
    )
    ledger.finish_run(second.run_id)
    ledger.close()

    with pytest.raises(ValueError, match="多个 run_id"):
        build_review(tmp_path, start_date="20260708", end_date="20260709")


def test_acceptance_requires_twenty_observed_days_and_passes_clean_ledger(
    tmp_path: Path,
) -> None:
    """干净单账本满 20 个交易日后应通过运维门槛。"""
    ledger_path = tmp_path / "paper_v2.db"
    ledger = PaperLedger(ledger_path, initial_cash=50_000)
    ledger.connect()
    dates = [
        date.strftime("%Y%m%d") for date in pd.bdate_range("2026-06-01", periods=20)
    ]
    for date in dates:
        ledger.record_snapshot(
            {},
            snapshot_date=date,
            data_version="test",
            strategy_version="robust_v2",
        )
    ledger.close()

    result = run_acceptance(
        ledger_path,
        start_date=dates[0],
        end_date=dates[-1],
        min_observed_days=20,
    )

    assert result.ok is True
    assert result.observed_days == 20
    assert result.t1_violations == 0
    assert result.stale_quote_orders == 0
