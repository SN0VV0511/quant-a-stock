"""7 月 9 日旧日报 V2 规则回放测试。"""

from __future__ import annotations

from scripts.replay_daily_report_v2 import parse_daily_report, replay_report


def test_replay_blocks_same_day_sell_and_removes_defensive_exit(tmp_path) -> None:
    """同日买卖被 T+1 阻止，盘中微亏防守退出在 V2 中为零。"""
    report = tmp_path / "daily_20260709.txt"
    report.write_text(
        """🔄 今日交易 (4 笔):
  买入 甲公司 300股 @ 10.000 金额3,000元
  卖出 甲公司 300股 @ 9.990 金额2,997元
    盈亏: -13.00 元
    strategy_tag=combo_trend sell_reason=COMBO_DEFENSIVE_EXIT
  买入 乙公司 100股 @ 20.000 金额2,000元
  卖出 丙公司 100股 @ 30.000 金额3,000元
    盈亏: +20.00 元
    strategy_tag=combo_trend sell_reason=TREND_BREAK_EXIT
""",
        encoding="utf-8",
    )

    trades = parse_daily_report(report.read_text(encoding="utf-8"))
    replay = replay_report(report)

    assert len(trades) == 4
    assert replay.same_day_round_trip_sells == 1
    assert replay.t1_blocked_shares == 300
    assert replay.original_defensive_exit_count == 1
    assert replay.v2_defensive_exit_count == 0
    assert replay.v2_intraday_ordinary_exit_count == 0
