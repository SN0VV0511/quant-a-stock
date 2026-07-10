"""paper_v2 20/60 交易日运维与绩效验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config.settings import ROBUST_V2_ACCOUNT_ID, ROBUST_V2_LEDGER_PATH  # noqa: E402
from config.settings import get_stock_board, is_etf  # noqa: E402
from trading.ledger import PaperLedger  # noqa: E402


@dataclass(frozen=True)
class AcceptanceResult:
    """观察期验收结果。"""

    ok: bool
    account_id: str
    run_id: str | None
    start_date: str
    end_date: str
    observed_days: int
    t1_violations: int
    board_buy_violations: int
    duplicate_idempotency_keys: int
    active_writer_runs: int
    unsupported_order_sources: int
    stale_quote_orders: int
    ledger_reset_events: int
    defensive_exit_count: int
    max_monthly_orders: int
    cost_to_nav: float
    max_drawdown: float
    total_return: float
    net_profit: float
    calmar_ratio: float
    turnover_rate: float
    benchmark_return: float | None
    failures: tuple[str, ...]


def _t1_violations(trades: list[dict[str, Any]]) -> int:
    """从账户成立起按 FIFO 重放批次，统计现实中不可能的卖出。"""
    lots: dict[str, list[dict[str, Any]]] = defaultdict(list)
    violations = 0
    for trade in sorted(
        trades, key=lambda row: (str(row["trade_date"]), str(row["created_at"]))
    ):
        code = str(trade["code"])
        shares = int(trade["shares"])
        date = str(trade["trade_date"])
        if trade["action"] == "buy":
            lots[code].append({"date": date, "shares": shares})
            continue
        available = sum(
            int(lot["shares"]) for lot in lots[code] if str(lot["date"]) < date
        )
        if shares > available:
            violations += 1
            continue
        remaining = shares
        for lot in lots[code]:
            if str(lot["date"]) >= date or remaining <= 0:
                continue
            take = min(remaining, int(lot["shares"]))
            lot["shares"] -= take
            remaining -= take
    return violations


def run_acceptance(
    ledger_path: Path,
    *,
    start_date: str,
    end_date: str,
    account_id: str = ROBUST_V2_ACCOUNT_ID,
    run_id: str | None = None,
    min_observed_days: int = 20,
    max_monthly_orders: int = 24,
    max_cost_to_nav: float = 0.005,
    max_drawdown: float = 0.10,
) -> AcceptanceResult:
    """执行结构违规、运维稳定性和绩效门槛检查。"""
    if not ledger_path.expanduser().exists():
        raise FileNotFoundError(f"paper_v2 账本不存在: {ledger_path}")
    ledger = PaperLedger(ledger_path, account_id=account_id)
    try:
        ledger.connect()
        review = ledger.build_review(start_date, end_date, run_id=run_id)
        snapshots = list(review.snapshots)
        period_trades = list(review.trades)
        all_trades = ledger.query_trades(end_date=end_date)
    finally:
        ledger.close()
    values = [float(row["total_value"]) for row in snapshots]
    observed_days = len({str(row["snapshot_date"]) for row in snapshots})
    running_peak = values[0] if values else 0.0
    observed_drawdown = 0.0
    for value in values:
        running_peak = max(running_peak, value)
        if running_peak > 0:
            observed_drawdown = max(
                observed_drawdown, (running_peak - value) / running_peak
            )
    total_return = (
        values[-1] / values[0] - 1 if len(values) >= 2 and values[0] > 0 else 0.0
    )
    benchmark_values = [
        float(row["benchmark_return"])
        for row in snapshots
        if row.get("benchmark_return") is not None
    ]
    benchmark_return = None
    if benchmark_values:
        compounded = 1.0
        for value in benchmark_values:
            compounded *= 1 + value
        benchmark_return = compounded - 1

    board_violations = sum(
        1
        for trade in period_trades
        if trade["action"] == "buy"
        and not is_etf(str(trade["code"]))
        and get_stock_board(str(trade["code"])) != "mainboard"
    )
    month_counts: dict[str, int] = defaultdict(int)
    for trade in period_trades:
        month_counts[str(trade["trade_date"])[:6]] += 1
    observed_monthly_orders = max(month_counts.values(), default=0)
    total_cost = sum(float(trade["total_cost"] or 0) for trade in period_trades)
    average_nav = sum(values) / len(values) if values else 0.0
    cost_ratio = total_cost / average_nav if average_nav > 0 else 0.0
    traded_amount = sum(float(trade["amount"] or 0) for trade in period_trades)
    turnover_rate = traded_amount / average_nav if average_nav > 0 else 0.0
    if observed_days > 1 and total_return > -1:
        annual_return = (1 + total_return) ** (252 / observed_days) - 1
    else:
        annual_return = 0.0
    calmar_ratio = annual_return / observed_drawdown if observed_drawdown > 0 else 0.0
    initial_cash = 50_000.0
    reset_events = sum(
        1
        for previous, current in zip(values, values[1:])
        if abs(current - initial_cash) <= 0.01 and abs(previous - initial_cash) > 1.0
    )

    connection = sqlite3.connect(ledger_path)
    connection.row_factory = sqlite3.Row
    try:
        duplicate_keys = int(connection.execute("""
            SELECT COUNT(*) FROM (
                SELECT idempotency_key FROM orders GROUP BY idempotency_key HAVING COUNT(*) > 1
            )
            """).fetchone()[0])
        active_runs = int(
            connection.execute(
                "SELECT COUNT(*) FROM runs WHERE account_id = ? AND status = 'running'",
                (account_id,),
            ).fetchone()[0]
        )
        clauses = [
            "account_id = ?",
            "trade_date >= ?",
            "trade_date <= ?",
            "status = 'filled'",
        ]
        params: list[Any] = [account_id, start_date, end_date]
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        rows = connection.execute(
            f"SELECT source, reason, raw_json FROM orders WHERE {' AND '.join(clauses)}",
            params,
        ).fetchall()
    finally:
        connection.close()
    unsupported_sources = sum(
        1
        for row in rows
        if row["source"] not in {"target_allocator", "robust_v2_monitor"}
    )
    stale_orders = 0
    for row in rows:
        try:
            raw = json.loads(str(row["raw_json"] or "{}"))
        except json.JSONDecodeError:
            raw = {}
        metadata = raw.get("order_metadata", {})
        if (
            not isinstance(metadata, dict)
            or metadata.get("data_health_checked") is not True
        ):
            stale_orders += 1
    defensive_count = sum(row["reason"] == "COMBO_DEFENSIVE_EXIT" for row in rows)

    t1_count = _t1_violations(all_trades)
    failures: list[str] = []
    if observed_days < min_observed_days:
        failures.append(f"观察交易日不足: {observed_days} < {min_observed_days}")
    if t1_count:
        failures.append(f"发现 {t1_count} 笔 T+1 违规成交")
    if board_violations:
        failures.append(f"发现 {board_violations} 笔限制板块买入")
    if duplicate_keys:
        failures.append(f"发现 {duplicate_keys} 个重复幂等键")
    if active_runs > 1:
        failures.append(f"发现 {active_runs} 个同时运行的写会话")
    if unsupported_sources:
        failures.append(f"发现 {unsupported_sources} 笔旧策略或未校验来源成交")
    if stale_orders:
        failures.append(f"发现 {stale_orders} 笔缺少行情健康校验的成交")
    if reset_events:
        failures.append(f"发现 {reset_events} 次疑似净值重置到 50,000 元")
    if defensive_count:
        failures.append(f"发现 {defensive_count} 笔 COMBO_DEFENSIVE_EXIT")
    if observed_monthly_orders > max_monthly_orders:
        failures.append(
            f"月订单超过上限: {observed_monthly_orders} > {max_monthly_orders}"
        )
    if cost_ratio > max_cost_to_nav:
        failures.append(f"交易成本占净值过高: {cost_ratio:.2%} > {max_cost_to_nav:.2%}")
    if observed_days >= 60 and observed_drawdown > max_drawdown:
        failures.append(
            f"60 日最大回撤超标: {observed_drawdown:.2%} > {max_drawdown:.2%}"
        )
    return AcceptanceResult(
        ok=not failures,
        account_id=account_id,
        run_id=run_id,
        start_date=start_date,
        end_date=end_date,
        observed_days=observed_days,
        t1_violations=t1_count,
        board_buy_violations=board_violations,
        duplicate_idempotency_keys=duplicate_keys,
        active_writer_runs=active_runs,
        unsupported_order_sources=unsupported_sources,
        stale_quote_orders=stale_orders,
        ledger_reset_events=reset_events,
        defensive_exit_count=defensive_count,
        max_monthly_orders=observed_monthly_orders,
        cost_to_nav=round(cost_ratio, 6),
        max_drawdown=round(observed_drawdown, 6),
        total_return=round(total_return, 6),
        net_profit=round(values[-1] - values[0], 2) if len(values) >= 2 else 0.0,
        calmar_ratio=round(calmar_ratio, 4),
        turnover_rate=round(turnover_rate, 6),
        benchmark_return=(
            round(benchmark_return, 6) if benchmark_return is not None else None
        ),
        failures=tuple(failures),
    )


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="paper_v2 20/60 日验收")
    parser.add_argument("--ledger", default=ROBUST_V2_LEDGER_PATH)
    parser.add_argument("--start", required=True, help="开始日期 YYYYMMDD")
    parser.add_argument("--end", required=True, help="结束日期 YYYYMMDD")
    parser.add_argument("--account-id", default=ROBUST_V2_ACCOUNT_ID)
    parser.add_argument("--run-id")
    parser.add_argument("--min-days", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    """命令行入口。"""
    args = _parse_args()
    result = run_acceptance(
        Path(args.ledger),
        start_date=args.start,
        end_date=args.end,
        account_id=args.account_id,
        run_id=args.run_id,
        min_observed_days=args.min_days,
    )
    sys.stdout.write(json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n")
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
