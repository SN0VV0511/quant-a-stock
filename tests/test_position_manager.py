"""持仓管理器测试。"""

import json
import logging

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

    buy = portfolio.buy(
        "sh601988", "中国银行", 5.0, 100, "20260527", "单元测试",
        strategy_tag="momentum_breakout",
    )
    position = dict(portfolio.get_position("sh601988"))
    same_day_sell = portfolio.sell("sh601988", 5.1, 100, "20260527", "单元测试")
    next_day_sell = portfolio.sell("sh601988", 5.1, 100, "20260528", "单元测试")
    portfolio.save_snapshot("20260528", {"sh601988": 5.1})

    assert buy["success"] is True
    assert position["total_qty"] == 100
    assert position["sellable_qty"] == 0
    assert position["strategy_tag"] == "momentum_breakout"
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
    assert first_trade["strategy_tag"] == "momentum_breakout"


def test_legacy_position_defaults_strategy_tag(tmp_path, caplog) -> None:
    """旧持仓缺少标签时应迁移为 combo_trend 并记录日志。"""
    state_file = tmp_path / "legacy_state.json"
    state_file.write_text(json.dumps({
        "cash": 40_000,
        "positions": {
            "600000": {
                "name": "浦发银行",
                "shares": 100,
                "avg_cost": 10,
                "buy_date": "20260527",
            }
        },
        "trades": [],
        "daily_snapshots": {},
        "peak_value": 50_000,
    }), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        portfolio = PositionManager(
            state_file=str(state_file),
            trade_log_file=str(tmp_path / "trades.jsonl"),
            snapshot_log_file=str(tmp_path / "snapshots.jsonl"),
        )
    assert portfolio.get_position("600000")["strategy_tag"] == "combo_trend"
    assert "缺少 strategy_tag" in caplog.text
