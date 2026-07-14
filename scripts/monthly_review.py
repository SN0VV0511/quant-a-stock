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

from config.settings import ROBUST_V2_ACCOUNT_ID
from config.time_utils import today_yyyymmdd
from trading.ledger import PaperLedger

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
    gross_profit: float = 0.0
    net_profit: float = 0.0
    commission: float = 0.0
    stamp_tax: float = 0.0
    slippage: float = 0.0
    total_cost: float = 0.0
    turnover_rate: float = 0.0
    benchmark_return: float | None = None
    data_version: str = "legacy-json"
    strategy_version: str = "legacy"
    account_id: str = "legacy"
    run_id: str | None = None


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
            if isinstance(summary, dict) and isinstance(
                summary.get("total_value"), (int, float)
            ):
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
    """按数据中最新日期回看 N 天，避免测试和补报受系统当前日期漂移影响。"""
    parsed_dates = [
        parsed
        for parsed in (
            [_parse_date(point[0]) for point in points]
            + [_parse_date(str(trade.get("date", ""))) for trade in trades]
        )
        if parsed is not None
    ]
    anchor = max(parsed_dates) if parsed_dates else datetime.now()
    cutoff = anchor - timedelta(days=days)
    filtered_points = [
        point
        for point in points
        if (parsed := _parse_date(point[0])) is None or parsed >= cutoff
    ]
    filtered_trades = [
        trade
        for trade in trades
        if (
            (parsed := _parse_date(str(trade.get("date", "")))) is None
            or parsed >= cutoff
        )
    ]
    return filtered_points, filtered_trades


def _filter_by_range(
    points: list[tuple[str, float]],
    trades: list[dict[str, Any]],
    start_date: str,
    end_date: str,
) -> tuple[list[tuple[str, float]], list[dict[str, Any]]]:
    """按显式起止日期做确定性过滤。"""
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if start is None or end is None:
        raise ValueError("开始和结束日期必须为 YYYYMMDD 或 YYYY-MM-DD")
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    filtered_points = [
        point
        for point in points
        if (parsed := _parse_date(point[0])) is not None and start <= parsed <= end
    ]
    filtered_trades = [
        trade
        for trade in trades
        if (parsed := _parse_date(str(trade.get("date", "")))) is not None
        and start <= parsed <= end
    ]
    return filtered_points, filtered_trades


def _summarize(
    points: list[tuple[str, float]],
    trades: list[dict[str, Any]],
    *,
    days: int,
    fallback_date: str,
    fallback_value: float,
    metadata: dict[str, Any] | None = None,
) -> ReviewSummary:
    """按统一口径计算 JSON 和 SQLite 复盘摘要。"""
    metadata = metadata or {}
    if not points:
        points = [(fallback_date, fallback_value)]

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

    buy_count = sum(
        1
        for trade in trades
        if (trade.get("action") or trade.get("direction")) == "buy"
    )
    sell_trades = [
        trade
        for trade in trades
        if (trade.get("action") or trade.get("direction")) == "sell"
    ]
    realized = sum(
        float(trade.get("profit", trade.get("net_pnl", 0)) or 0)
        for trade in sell_trades
    )
    wins = sum(
        1
        for trade in sell_trades
        if float(trade.get("profit", trade.get("net_pnl", 0)) or 0) > 0
    )
    win_rate = wins / len(sell_trades) if sell_trades else 0.0

    commission = sum(float(trade.get("commission", 0) or 0) for trade in trades)
    stamp_tax = sum(float(trade.get("stamp_tax", 0) or 0) for trade in trades)
    slippage = sum(float(trade.get("slippage", 0) or 0) for trade in trades)
    total_cost = sum(
        float(trade.get("total_cost", trade.get("cost", 0)) or 0) for trade in trades
    )
    average_value = sum(values) / len(values) if values else 0.0
    traded_amount = sum(float(trade.get("amount", 0) or 0) for trade in trades)
    gross_profit = sum(float(trade.get("gross_pnl", 0) or 0) for trade in sell_trades)
    if not gross_profit and realized:
        gross_profit = realized + total_cost

    return ReviewSummary(
        start_date=points[0][0],
        end_date=points[-1][0],
        days=days,
        initial_value=round(initial_value, 2),
        final_value=round(final_value, 2),
        total_return=(
            round((final_value - initial_value) / initial_value, 4)
            if initial_value > 0
            else 0.0
        ),
        max_drawdown=round(max_drawdown, 4),
        trade_count=len(trades),
        buy_count=buy_count,
        sell_count=len(sell_trades),
        win_rate=round(win_rate, 4),
        realized_profit=round(realized, 2),
        gross_profit=round(gross_profit, 2),
        net_profit=round(final_value - initial_value, 2),
        commission=round(commission, 2),
        stamp_tax=round(stamp_tax, 2),
        slippage=round(slippage, 2),
        total_cost=round(total_cost, 2),
        turnover_rate=(
            round(traded_amount / average_value, 4) if average_value > 0 else 0.0
        ),
        benchmark_return=metadata.get("benchmark_return"),
        data_version=str(metadata.get("data_version", "legacy-json")),
        strategy_version=str(metadata.get("strategy_version", "legacy")),
        account_id=str(metadata.get("account_id", "legacy")),
        run_id=metadata.get("run_id"),
    )


def _legacy_fallback_value(root_dir: Path) -> float:
    """从旧 JSON 状态估算无快照时的账户值。"""
    state = _load_json(root_dir / "data" / "portfolio_state.json")
    cash = float(state.get("cash", 0.0))
    positions = state.get("positions", {})
    position_value = 0.0
    if isinstance(positions, dict):
        for position in positions.values():
            if isinstance(position, dict):
                position_value += float(
                    position.get("current_price", position.get("avg_cost", 0)) or 0
                ) * int(position.get("shares", 0) or 0)
    return round(cash + position_value, 2)


def _build_ledger_review(
    ledger_path: Path,
    *,
    account_id: str,
    start_date: str,
    end_date: str,
    run_id: str | None,
) -> ReviewSummary:
    """从连续 SQLite 账户生成显式日期复盘，run_id 仅用于可选审计筛选。"""
    ledger = PaperLedger(ledger_path, account_id=account_id)
    try:
        ledger.connect()
        review = ledger.build_review(start_date, end_date, run_id=run_id)
        rows = list(review.snapshots)
        effective_run_id = run_id or (
            str(rows[-1].get("run_id") or "") if rows else None
        )
        points = [
            (str(row["snapshot_date"]), float(row["total_value"])) for row in rows
        ]
        trades = [
            {
                **trade,
                "date": trade["trade_date"],
                "profit": trade.get("net_pnl"),
                "cost": trade.get("total_cost"),
            }
            for trade in review.trades
        ]
        latest = rows[-1] if rows else {}
        benchmark_return: float | None = None
        benchmark_values = [
            float(row["benchmark_return"])
            for row in rows
            if row.get("benchmark_return") is not None
        ]
        if benchmark_values:
            compounded = 1.0
            for value in benchmark_values:
                compounded *= 1 + value
            benchmark_return = compounded - 1
        return _summarize(
            points,
            trades,
            days=(
                datetime.strptime(end_date, "%Y%m%d")
                - datetime.strptime(start_date, "%Y%m%d")
            ).days
            + 1,
            fallback_date=end_date,
            fallback_value=ledger.query_snapshot().total_value,
            metadata={
                "benchmark_return": benchmark_return,
                "data_version": latest.get("data_version", "unknown"),
                "strategy_version": latest.get("strategy_version", "robust_v2"),
                "account_id": account_id,
                "run_id": effective_run_id,
            },
        )
    finally:
        ledger.close()


def build_review(
    root_dir: Path,
    days: int = 30,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    ledger_path: Path | None = None,
    account_id: str = ROBUST_V2_ACCOUNT_ID,
    run_id: str | None = None,
) -> ReviewSummary:
    """生成观察期复盘；V2 推荐显式传入开始、结束日期。"""
    if (start_date is None) != (end_date is None):
        raise ValueError("开始和结束日期必须同时传入")
    normalized_start = start_date.replace("-", "") if start_date else None
    normalized_end = end_date.replace("-", "") if end_date else None
    candidate_ledger = ledger_path or (root_dir / "data" / "paper_v2.db")
    if ledger_path is not None and not candidate_ledger.exists():
        raise FileNotFoundError(f"指定的 paper_v2 账本不存在: {candidate_ledger}")
    if candidate_ledger.exists():
        if not normalized_start or not normalized_end:
            raise ValueError("paper_v2 月报必须显式传入开始和结束日期")
        return _build_ledger_review(
            candidate_ledger,
            account_id=account_id,
            start_date=normalized_start,
            end_date=normalized_end,
            run_id=run_id,
        )

    points = _snapshot_points(root_dir)
    trades = _trade_rows(root_dir)
    if normalized_start and normalized_end:
        points, trades = _filter_by_range(
            points, trades, normalized_start, normalized_end
        )
        effective_days = (
            datetime.strptime(normalized_end, "%Y%m%d")
            - datetime.strptime(normalized_start, "%Y%m%d")
        ).days + 1
        fallback_date = normalized_end
    else:
        points, trades = _filter_by_days(points, trades, days)
        effective_days = days
        fallback_date = today_yyyymmdd()
    return _summarize(
        points,
        trades,
        days=effective_days,
        fallback_date=fallback_date,
        fallback_value=_legacy_fallback_value(root_dir),
    )


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="生成虚拟盘观察期复盘摘要")
    parser.add_argument(
        "--root", default=str(Path(__file__).resolve().parents[1]), help="项目根目录"
    )
    parser.add_argument("--days", type=int, default=30, help="复盘最近 N 天")
    parser.add_argument("--start", help="明确开始日期 YYYYMMDD")
    parser.add_argument("--end", help="明确结束日期 YYYYMMDD")
    parser.add_argument("--ledger", help="paper_v2 SQLite 账本路径")
    parser.add_argument("--account-id", default=ROBUST_V2_ACCOUNT_ID, help="账户标识")
    parser.add_argument("--run-id", help="运行批次；区间含多个批次时必填")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser.parse_args()


def main() -> int:
    """命令行入口。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()
    summary = build_review(
        Path(args.root),
        args.days,
        start_date=args.start,
        end_date=args.end,
        ledger_path=Path(args.ledger) if args.ledger else None,
        account_id=args.account_id,
        run_id=args.run_id,
    )
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
