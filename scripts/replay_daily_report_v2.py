"""把旧版中文日报按 robust_v2 的 T+1 和盘中职责重新审计。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

TRADE_PATTERN = re.compile(r"^\s{2}(买入|卖出)\s+(.+?)\s+(\d+)股\s+@\s+([0-9.]+)")
PROFIT_PATTERN = re.compile(r"盈亏:\s*([+-]?[0-9,.]+)\s*元")
REASON_PATTERN = re.compile(r"sell_reason=([A-Z0-9_]+)")


@dataclass
class ParsedTrade:
    """从旧日报恢复的单笔成交。"""

    action: str
    name: str
    shares: int
    price: float
    profit: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class ReplaySummary:
    """robust_v2 规则回放摘要。"""

    source: str
    original_trade_count: int
    original_buy_count: int
    original_sell_count: int
    same_day_round_trip_sells: int
    t1_blocked_shares: int
    original_defensive_exit_count: int
    original_defensive_exit_profit: float
    v2_defensive_exit_count: int
    v2_intraday_ordinary_exit_count: int
    policy_notes: tuple[str, ...]


def parse_daily_report(text: str) -> list[ParsedTrade]:
    """解析旧版日报的成交、卖出盈亏和退出原因。"""
    trades: list[ParsedTrade] = []
    current: ParsedTrade | None = None
    for line in text.splitlines():
        match = TRADE_PATTERN.search(line)
        if match:
            current = ParsedTrade(
                action="buy" if match.group(1) == "买入" else "sell",
                name=match.group(2).strip(),
                shares=int(match.group(3)),
                price=float(match.group(4)),
            )
            trades.append(current)
            continue
        if current is None or current.action != "sell":
            continue
        profit_match = PROFIT_PATTERN.search(line)
        if profit_match:
            current.profit = float(profit_match.group(1).replace(",", ""))
        reason_match = REASON_PATTERN.search(line)
        if reason_match:
            current.reason = reason_match.group(1)
    return trades


def replay_report(path: Path) -> ReplaySummary:
    """按 robust_v2 规则审计旧日报，不重写任何历史证据。"""
    trades = parse_daily_report(path.read_text(encoding="utf-8"))
    bought: dict[str, int] = {}
    matched: dict[str, int] = {}
    round_trip_sells = 0
    blocked_shares = 0
    for trade in trades:
        if trade.action == "buy":
            bought[trade.name] = bought.get(trade.name, 0) + trade.shares
            continue
        remaining_same_day = bought.get(trade.name, 0) - matched.get(trade.name, 0)
        blocked = min(max(remaining_same_day, 0), trade.shares)
        if blocked > 0:
            round_trip_sells += 1
            blocked_shares += blocked
            matched[trade.name] = matched.get(trade.name, 0) + blocked

    defensive = [trade for trade in trades if trade.reason == "COMBO_DEFENSIVE_EXIT"]
    return ReplaySummary(
        source=str(path.resolve()),
        original_trade_count=len(trades),
        original_buy_count=sum(trade.action == "buy" for trade in trades),
        original_sell_count=sum(trade.action == "sell" for trade in trades),
        same_day_round_trip_sells=round_trip_sells,
        t1_blocked_shares=blocked_shares,
        original_defensive_exit_count=len(defensive),
        original_defensive_exit_profit=round(
            sum(float(trade.profit or 0) for trade in defensive), 2
        ),
        # robust_v2 的盘中监控代码只产生 CATASTROPHIC_STOP_LOSS。
        v2_defensive_exit_count=0,
        v2_intraday_ordinary_exit_count=0,
        policy_notes=(
            "同日买入批次全部由 SQLite lots 锁定到下一交易日",
            "COMBO_DEFENSIVE_EXIT 和 TREND_BREAK_EXIT 只作为旧策略研究证据，不接入 V2 账户",
            "普通调仓退出由收盘目标确认，下一交易日 09:35 后执行",
        ),
    )


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="按 robust_v2 规则回放旧版日报")
    parser.add_argument("report", type=Path, help="旧版 daily_YYYYMMDD.txt")
    parser.add_argument("--output", type=Path, help="可选 JSON 输出路径")
    return parser.parse_args()


def main() -> int:
    """命令行入口。"""
    args = _parse_args()
    summary = replay_report(args.report)
    payload = json.dumps(asdict(summary), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
