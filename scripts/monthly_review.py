"""虚拟盘观察期复盘报告。

读取账户快照和交易流水，输出最近 N 天的收益、最大回撤、交易次数和胜率。
这个脚本用于一个月虚拟盘观察结束后，决定是否进入 QMT dry-run 或实盘联调。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config.time_utils import now_local, today_yyyymmdd

LOGGER = logging.getLogger("monthly_review")


@dataclass(frozen=True)
class ReviewSummary:
    """观察期复盘摘要。"""

    start_date: str
    end_date: str
    days: int
    initial_value: float
    final_value: float
    total_return: float
    max_drawdown: float
    trade_count: int
    buy_count: int
    sell_count: int
    win_rate: float
    realized_profit: float


def _load_json(path: Path) -> dict[str, Any]:
    """读取 JSON 文件。"""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 文件。"""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _parse_date(value: str) -> datetime | None:
    """解析 YYYYMMDD 或 YYYY-MM-DD 日期。"""
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _snapshot_points(root_dir: Path) -> list[tuple[str, float]]:
    """读取账户快照点。"""
    data_dir = root_dir / "data"
    snapshots = _load_jsonl(data_dir / "portfolio_snapshots.jsonl")
    points: list[tuple[str, float]] = []
    for row in snapshots:
        date = str(row.get("date", ""))
        summary = row.get("summary", {})
        total_value = summary.get("total_value") if isinstance(summary, dict) else None
        if date and isinstance(total_value, (int, float)):
            points.append((date, float(total_value)))

    if points:
        return points

    state = _load_json(data_dir / "portfolio_state.json")
    daily_snapshots = state.get("daily_snapshots", {})
    if isinstance(daily_snapshots, dict):
        for date, summary in daily_snapshots.items():
            if isinstance(summary, dict) and isinstance(summary.get("total_value"), (int, float)):
                points.append((str(date), float(summary["total_value"])))
    return points


def _trade_rows(root_dir: Path) -> list[dict[str, Any]]:
    """读取交易流水。"""
    data_dir = root_dir / "data"
    rows = _load_jsonl(data_dir / "trade_log.json")
    state = _load_json(data_dir / "portfolio_state.json")
    state_trades = state.get("trades", [])
    if isinstance(state_trades, list):
        rows.extend([row for row in state_trades if isinstance(row, dict)])
    return _dedupe_trades(rows)


def _dedupe_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去重交易流水。"""
    seen = set()
    deduped: list[dict[str, Any]] = []
    for trade in trades:
        key = (
            trade.get("date"),
            trade.get("time"),
            trade.get("code"),
            trade.get("action") or trade.get("direction"),
            trade.get("shares"),
            trade.get("price"),
            trade.get("actual_price"),
            trade.get("amount"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(trade)
    return deduped


def _filter_by_days(
    points: list[tuple[str, float]],
    trades: list[dict[str, Any]],
    days: int,
) -> tuple[list[tuple[str, float]], list[dict[str, Any]]]:
    """按最近 N 天过滤快照和交易。"""
    cutoff = now_local() - timedelta(days=days)
    filtered_points = [
        point for point in points
        if (_parse_date(point[0]) is None or _parse_date(point[0]) >= cutoff)
    ]
    filtered_trades = [
        trade for trade in trades
        if (_parse_date(str(trade.get("date", ""))) is None or _parse_date(str(trade.get("date", ""))) >= cutoff)
    ]
    return filtered_points, filtered_trades


def build_review(root_dir: Path, days: int = 30) -> ReviewSummary:
    """生成观察期复盘摘要。"""
    points, trades = _filter_by_days(_snapshot_points(root_dir), _trade_rows(root_dir), days)
    if not points:
        state = _load_json(root_dir / "data" / "portfolio_state.json")
        cash = float(state.get("cash", 0.0))
        positions = state.get("positions", {})
        position_value = 0.0
        if isinstance(positions, dict):
            for pos in positions.values():
                if isinstance(pos, dict):
                    position_value += float(pos.get("current_price", pos.get("avg_cost", 0)) or 0) * int(pos.get("shares", 0) or 0)
        total_value = round(cash + position_value, 2)
        today = today_yyyymmdd()
        points = [(today, total_value)]

    points.sort(key=lambda item: item[0])
    values = [value for _, value in points]
    initial_value = values[0]
    final_value = values[-1]
    running_peak = initial_value
    max_drawdown = 0.0
    for value in values:
        running_peak = max(running_peak, value)
        if running_peak > 0:
            max_drawdown = max(max_drawdown, (running_peak - value) / running_peak)

    buy_count = sum(1 for trade in trades if (trade.get("action") or trade.get("direction")) == "buy")
    sell_trades = [trade for trade in trades if (trade.get("action") or trade.get("direction")) == "sell"]
    realized = sum(float(trade.get("profit", 0) or 0) for trade in sell_trades)
    wins = sum(1 for trade in sell_trades if float(trade.get("profit", 0) or 0) > 0)
    win_rate = wins / len(sell_trades) if sell_trades else 0.0

    return ReviewSummary(
        start_date=points[0][0],
        end_date=points[-1][0],
        days=days,
        initial_value=round(initial_value, 2),
        final_value=round(final_value, 2),
        total_return=round((final_value - initial_value) / initial_value, 4) if initial_value > 0 else 0.0,
        max_drawdown=round(max_drawdown, 4),
        trade_count=len(trades),
        buy_count=buy_count,
        sell_count=len(sell_trades),
        win_rate=round(win_rate, 4),
        realized_profit=round(realized, 2),
    )


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="生成虚拟盘观察期复盘摘要")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="项目根目录")
    parser.add_argument("--days", type=int, default=30, help="复盘最近 N 天")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser.parse_args()


def main() -> int:
    """命令行入口。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()
    summary = build_review(Path(args.root), args.days)
    payload = asdict(summary)
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        LOGGER.info(
            "虚拟盘复盘 %s-%s: 收益 %.2f%%, 最大回撤 %.2f%%, 交易 %s 笔, 胜率 %.2f%%",
            summary.start_date,
            summary.end_date,
            summary.total_return * 100,
            summary.max_drawdown * 100,
            summary.trade_count,
            summary.win_rate * 100,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
