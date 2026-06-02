"""持仓管理器测试。"""

import json

from rules.position import PositionManager


def test_position_manager_buy_sell_t1_and_persistence(tmp_path) -> None:
    """持仓管理器应执行买入、阻止 T+1 当日卖出，并持久化交易流水。"""
    state_file = tmp_path / "state.json"
    trade_log = tmp_path / "trade_log.json"
    snapshot_log = tmp_path / "snapshots.jsonl"
    portfolio = PositionManager(
        state_file=str(state_file),
        trade_log_file=str(trade_log),
        snapshot_log_file=str(snapshot_log),
    )
    assert state_file.exists()
    assert trade_log.exists()
    assert snapshot_log.exists()

    buy = portfolio.buy("sh601988", "中国银行", 5.0, 100, "20260527", "单元测试")
    same_day_sell = portfolio.sell("sh601988", 5.1, 100, "20260527", "单元测试")
    next_day_sell = portfolio.sell("sh601988", 5.1, 100, "20260528", "单元测试")
    portfolio.save_snapshot("20260528", {"sh601988": 5.1})

    assert buy["success"] is True
    assert same_day_sell["success"] is False
    assert "T+1" in same_day_sell["reason"]
    assert next_day_sell["success"] is True
    assert portfolio.get_all_positions() == {}
    assert state_file.exists()
    assert snapshot_log.exists()

    lines = trade_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first_trade = json.loads(lines[0])
    assert first_trade["action"] == "buy"
    assert first_trade["direction"] == "buy"
