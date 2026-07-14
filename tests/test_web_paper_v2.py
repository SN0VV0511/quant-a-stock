"""Web 仪表盘读取 paper_v2 单账本测试。"""

from __future__ import annotations

import web.app as web_app
from data.scan_store import StockScanSnapshot, StockScanStore
from trading.ledger import PaperLedger
from trading.models import OrderIntent


def _execution_metadata(date: str, price: float) -> dict[str, object]:
    """生成账本严格校验需要的当日行情证据。"""
    dashed = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    return {
        "data_health_checked": True,
        "execution_quote": {
            "current_price": price,
            "trade_date": date,
            "quote_time": f"{dashed} 09:36:00",
            "is_suspended": False,
            "can_buy": True,
            "can_sell": True,
            "limit_type": "normal",
        },
    }


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
            metadata=_execution_metadata("20260709", 4.0),
        )
    )
    ledger.record_snapshot(
        {"510300": 4.0},
        snapshot_date="20260709",
        data_version="hash",
        strategy_version="robust_v2",
    )
    ledger.acquire_lease("daemon:test", 90)
    ledger.close()
    monkeypatch.setattr(web_app, "ROBUST_V2_LEDGER_PATH", str(path))

    state = web_app.load_state()
    orders = web_app.load_trade_log()
    equity = web_app._load_v2_equity()

    assert state["source"] == "paper_v2"
    assert state["positions"]["510300"]["shares"] == 1000
    assert orders[0]["strategy_tag"] == "robust_v2"
    assert equity and equity[0]["value"] < 50_000
    assert web_app.is_process_running("robust_runner.py") is True


def test_web_profit_ranking_uses_realized_cost_for_partial_sell(
    monkeypatch, tmp_path
) -> None:
    """部分卖出收益率只能使用已卖批次成本，不能扣除全部历史买入。"""
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
            shares=2000,
            name="沪深300ETF",
            strategy="A股稳健策略V2",
            strategy_tag="robust_v2",
            reason="TEST_TARGET",
            date="20260709",
            source="target_allocator",
            metadata=_execution_metadata("20260709", 4.0),
        )
    )
    ledger.place_order(
        OrderIntent(
            account_id="paper_v2",
            strategy_version="robust_v2",
            signal_date="20260709",
            code="510300",
            action="sell",
            price=4.4,
            shares=1000,
            name="沪深300ETF",
            strategy="A股稳健策略V2",
            strategy_tag="robust_v2",
            reason="TEST_TARGET",
            date="20260710",
            source="target_allocator",
            metadata=_execution_metadata("20260710", 4.4),
        )
    )
    ledger.close()
    monkeypatch.setattr(web_app, "ROBUST_V2_LEDGER_PATH", str(path))

    ranking = web_app._load_v2_profit_ranking()

    assert ranking and ranking[0]["code"] == "510300"
    assert ranking[0]["shares_traded"] == 1000
    assert ranking[0]["net_profit"] > 0
    assert 0.08 < ranking[0]["roi"] < 0.11


def test_web_candidates_read_structured_scan_instead_of_parsing_logs(
    monkeypatch, tmp_path
) -> None:
    """候选接口必须读取扫描快照并保留过滤统计和拟选标记。"""
    scan_dir = tmp_path / "scans"
    store = StockScanStore(scan_dir)
    store.save(
        StockScanSnapshot(
            strategy_version="robust_v2",
            trade_date="20260710",
            generated_at="2026-07-10 15:06:00",
            mode="scheduled",
            status="completed",
            account_value=50_000,
            source_snapshot_hash="hash",
            universe={"mainboard_count": 3000, "rough_candidate_count": 500},
            input_count=500,
            eligible_count=1,
            prefilter_counts={"candidate_limit": 100},
            prefilter_labels={"candidate_limit": "流动性排名超出 500 只上限"},
            filter_counts={"below_ma120": 200},
            filter_labels={"below_ma120": "股价低于年线"},
            candidates=(
                {
                    "rank": 1,
                    "code": "sh600001",
                    "name": "测试股份",
                    "price": 10.0,
                    "current_price": 10.0,
                    "pb": 1.0,
                    "market_cap": 8_000_000_000,
                    "reversal": -0.03,
                    "gain_5d": 0.01,
                    "quality": 10.0,
                    "score": 0.9,
                    "selected": True,
                },
            ),
            selected_codes=("sh600001",),
            next_scheduled_scan_at="2026-07-17 15:05:00",
        )
    )
    monkeypatch.setattr(web_app, "SCAN_DIR", scan_dir)
    monkeypatch.setattr(
        web_app,
        "get_realtime_prices",
        lambda codes: {code: 10.25 for code in codes},
    )
    handler = object.__new__(web_app.QuantHandler)

    payload = handler._api_candidates()

    assert payload["status"] == "completed"
    assert payload["eligible_count"] == 1
    assert payload["prefilter_counts"] == {"candidate_limit": 100}
    assert payload["filter_counts"] == {"below_ma120": 200}
    assert payload["candidates"][0]["current_price"] == 10.25
    assert payload["candidates"][0]["selected"] is True


def test_web_scan_trigger_starts_safe_preview_command(monkeypatch, tmp_path) -> None:
    """手动按钮必须真实启动 preview-scan，且不得调用 signal --force。"""
    ledger_path = tmp_path / "paper_v2.db"
    ledger_path.touch()
    captured: dict[str, object] = {}

    class FakeProcess:
        """模拟仍在运行的预览子进程。"""

        def poll(self):
            """返回 None 表示进程仍在运行。"""
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(web_app, "ROBUST_V2_LEDGER_PATH", str(ledger_path))
    monkeypatch.setattr(web_app.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(web_app, "_PREVIEW_SCAN_PROCESS", None)
    handler = object.__new__(web_app.QuantHandler)

    payload, status = handler._api_scan_trigger()

    command = captured["command"]
    assert status == 202
    assert payload["status"] == "started"
    assert "preview-scan" in command
    assert "signal" not in command
    assert "--force" not in command
