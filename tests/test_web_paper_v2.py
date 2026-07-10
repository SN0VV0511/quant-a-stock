"""Web 仪表盘读取 paper_v2 单账本测试。"""

from __future__ import annotations

import web.app as web_app
from trading.ledger import PaperLedger
from trading.models import OrderIntent


def test_web_reads_portfolio_orders_and_equity_from_sqlite(
    monkeypatch, tmp_path
) -> None:
    """存在 V2 账本时，页面数据不得继续回退到旧 JSON。"""
    path = tmp_path / "paper_v2.db"
    ledger = PaperLedger(path, initial_cash=50_000)
    ledger.connect()
    ledger.place_order(
        OrderIntent(
            account_id="paper_v2",
            strategy_version="robust_v2",
            signal_date="20260708",
            code="510300",
            action="buy",
            price=4.0,
            shares=1000,
            name="沪深300ETF",
            strategy="A股稳健策略V2",
            strategy_tag="robust_v2",
            reason="TEST_TARGET",
            date="20260709",
            source="target_allocator",
            metadata={"data_health_checked": True},
        )
    )
    ledger.record_snapshot(
        {"510300": 4.0},
        snapshot_date="20260709",
        data_version="hash",
        strategy_version="robust_v2",
    )
    ledger.close()
    monkeypatch.setattr(web_app, "ROBUST_V2_LEDGER_PATH", str(path))

    state = web_app.load_state()
    orders = web_app.load_trade_log()
    equity = web_app._load_v2_equity()

    assert state["source"] == "paper_v2"
    assert state["positions"]["510300"]["shares"] == 1000
    assert orders[0]["strategy_tag"] == "robust_v2"
    assert equity and equity[0]["value"] < 50_000
