"""robust_v2 运行态只读观测边界回归测试。"""

from __future__ import annotations

import json
import sqlite3

import scripts.paper_status as paper_status
import web.app as web_app
from scripts.paper_status import build_status
from scripts.paper_v2_healthcheck import run_v2_healthcheck
from trading.ledger import PaperLedger


def test_web_backtest_hides_series_when_cache_is_unavailable(
    monkeypatch, tmp_path
) -> None:
    """缓存层拒绝旧策略产物时，API 不得重新标记可用或泄露旧曲线。"""
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "backtest_latest.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-07-10 13:06:08",
                "series": [{"name": "旧策略", "equity": [{"value": 50_000}]}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class UnavailableStatus:
        """模拟缓存层已识别到旧策略产物。"""

        available = False

        @staticmethod
        def to_dict() -> dict[str, object]:
            """返回与 BacktestCacheStatus 一致的最小协议。"""
            return {
                "available": False,
                "generating": False,
                "message": "现有回测不是 robust_v2",
            }

    monkeypatch.setattr(web_app, "REPORT_DIR", str(report_dir))
    monkeypatch.setattr(
        web_app,
        "ensure_backtest_cache",
        lambda *_args, **_kwargs: UnavailableStatus(),
    )
    handler = object.__new__(web_app.QuantHandler)

    payload = handler._api_backtest()

    assert payload["available"] is False
    assert payload["series"] == []
    assert payload["message"] == "现有回测不是 robust_v2"


def test_v2_healthcheck_rejects_zero_byte_ledger_without_modifying_it(
    tmp_path,
) -> None:
    """零字节占位文件必须失败，健康检查不得借机初始化账本。"""
    ledger_path = tmp_path / "paper_v2.db"
    ledger_path.touch()
    before = ledger_path.read_bytes()
    before_entries = sorted(path.name for path in tmp_path.iterdir())

    result = run_v2_healthcheck(ledger_path)

    assert result.ok is False
    assert any("为空" in failure for failure in result.failures)
    assert ledger_path.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == before_entries


def test_v2_healthcheck_rejects_missing_tables_without_migrating(
    tmp_path,
) -> None:
    """部分数据库结构必须失败，健康检查不得自动补表。"""
    ledger_path = tmp_path / "paper_v2.db"
    connection = sqlite3.connect(ledger_path)
    connection.execute(
        """
        CREATE TABLE accounts (
            account_id TEXT PRIMARY KEY,
            initial_cash REAL NOT NULL,
            cash REAL NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    connection.close()
    before = ledger_path.read_bytes()

    result = run_v2_healthcheck(ledger_path)

    assert result.ok is False
    assert any("缺少必要表" in failure for failure in result.failures)
    assert ledger_path.read_bytes() == before
    connection = sqlite3.connect(ledger_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()
    assert tables == {"accounts"}


def test_v2_healthcheck_rejects_missing_account_without_creating_it(
    tmp_path,
) -> None:
    """指定账户不存在时必须失败，健康检查不得插入默认资金账户。"""
    ledger_path = tmp_path / "paper_v2.db"
    ledger = PaperLedger(ledger_path, initial_cash=50_000)
    ledger.connect()
    ledger.close()
    before = ledger_path.read_bytes()

    result = run_v2_healthcheck(ledger_path, account_id="missing-account")

    assert result.ok is False
    assert any("账户不存在" in failure for failure in result.failures)
    assert ledger_path.read_bytes() == before
    connection = sqlite3.connect(ledger_path)
    try:
        accounts = connection.execute(
            "SELECT account_id FROM accounts ORDER BY account_id"
        ).fetchall()
    finally:
        connection.close()
    assert accounts == [("paper_v2",)]


def test_v2_healthcheck_valid_ledger_does_not_create_sqlite_sidecars(
    tmp_path,
) -> None:
    """合法账本的只读检查也不得创建 WAL/SHM 或修改主文件。"""
    ledger_path = tmp_path / "paper_v2.db"
    ledger = PaperLedger(ledger_path, initial_cash=50_000)
    ledger.connect()
    ledger.record_snapshot(
        {},
        snapshot_date="20260720",
        data_version="test",
        strategy_version="robust_v2",
    )
    ledger.acquire_lease("daemon:test", 90)
    try:
        before = {
            path.name: path.read_bytes()
            for path in tmp_path.iterdir()
            if path.is_file()
        }
        result = run_v2_healthcheck(ledger_path)
        after = {
            path.name: path.read_bytes()
            for path in tmp_path.iterdir()
            if path.is_file()
        }
    finally:
        ledger.close()

    assert result.ok is True
    assert result.metrics["latest_snapshot_date"] == "20260720"
    assert result.metrics["lease_active"] is True
    assert after.keys() == before.keys()
    assert after[ledger_path.name] == before[ledger_path.name]
    assert after[f"{ledger_path.name}-wal"] == before[f"{ledger_path.name}-wal"]


def test_v2_healthcheck_rejects_wrong_initial_capital_and_strategy_position(
    tmp_path,
) -> None:
    """一月 5 万账户不能静默接受错误本金或旧策略持仓。"""
    wrong_cash_path = tmp_path / "wrong_cash.db"
    wrong_cash = PaperLedger(wrong_cash_path, initial_cash=40_000)
    wrong_cash.connect()
    wrong_cash.close()

    cash_result = run_v2_healthcheck(wrong_cash_path)
    assert cash_result.ok is False
    assert any("账户初始资金不符" in failure for failure in cash_result.failures)

    wrong_strategy_path = tmp_path / "wrong_strategy.db"
    ledger = PaperLedger(wrong_strategy_path, initial_cash=50_000)
    ledger.connect()
    ledger.close()
    connection = sqlite3.connect(wrong_strategy_path)
    try:
        connection.execute(
            """
            INSERT INTO positions(
                account_id, code, name, shares, avg_cost, last_price,
                strategy_version, updated_at
            ) VALUES ('paper_v2', '600000', '浦发银行', 100, 10, 10,
                      'legacy_combo', '2026-07-23 15:05:00')
            """
        )
        connection.commit()
    finally:
        connection.close()

    strategy_result = run_v2_healthcheck(wrong_strategy_path)
    assert strategy_result.ok is False
    assert any("持仓策略版本不符" in failure for failure in strategy_result.failures)


def test_paper_status_v2_uses_same_explicit_latest_snapshot_range(
    monkeypatch,
    tmp_path,
) -> None:
    """V2 复盘和验收必须共享以最新快照为锚点的最近 30 日区间。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ledger_path = data_dir / "paper_v2.db"
    ledger = PaperLedger(ledger_path, initial_cash=50_000)
    ledger.connect()
    for snapshot_date in ("20260701", "20260720"):
        ledger.record_snapshot(
            {},
            snapshot_date=snapshot_date,
            data_version="test",
            strategy_version="robust_v2",
        )
    ledger.close()
    captured: dict[str, tuple[str, str]] = {}
    real_build_review = paper_status.build_review
    real_acceptance = paper_status.run_v2_acceptance

    def capture_review(*args, **kwargs):
        captured["review"] = (kwargs["start_date"], kwargs["end_date"])
        return real_build_review(*args, **kwargs)

    def capture_acceptance(*args, **kwargs):
        captured["acceptance"] = (kwargs["start_date"], kwargs["end_date"])
        return real_acceptance(*args, **kwargs)

    monkeypatch.setattr(paper_status, "build_review", capture_review)
    monkeypatch.setattr(paper_status, "run_v2_acceptance", capture_acceptance)

    status = build_status(tmp_path, days=30, min_snapshot_days=1)

    assert status.errors == []
    assert status.health["metrics"]["latest_snapshot_date"] == "20260720"
    assert captured["review"] == ("20260621", "20260720")
    assert captured["acceptance"] == captured["review"]
    assert status.review["start_date"] == "20260701"
    assert status.review["end_date"] == "20260720"
    assert status.acceptance["start_date"] == "20260621"
    assert status.acceptance["end_date"] == "20260720"


def test_paper_status_does_not_initialize_invalid_v2_ledger(tmp_path) -> None:
    """无效 V2 账本应保留明确失败，且不得继续复盘或验收触发初始化。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ledger_path = data_dir / "paper_v2.db"
    ledger_path.touch()
    before = ledger_path.read_bytes()

    status = build_status(tmp_path)

    assert status.health["ok"] is False
    assert status.review == {}
    assert status.acceptance == {}
    assert any("账本文件为空" in error for error in status.errors)
    assert ledger_path.read_bytes() == before
