"""paper_v2 单账本日报生成器。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trading.ledger import PaperLedger


@dataclass(frozen=True)
class DailyLedgerReport:
    """可序列化的 robust_v2 日报数据。"""

    date: str
    account_id: str
    run_id: str
    strategy_version: str
    data_version: str
    total_value: float
    cash: float
    gross_profit: float
    net_profit: float
    commission: float
    stamp_tax: float
    slippage: float
    total_cost: float
    turnover_rate: float
    benchmark_return: float | None
    trade_count: int
    positions: tuple[dict[str, Any], ...]


def build_daily_ledger_report(
    ledger: PaperLedger,
    date: str,
    *,
    run_id: str | None = None,
) -> DailyLedgerReport:
    """从同一账户和运行批次计算单日毛/净收益及交易成本。"""
    review = ledger.build_review("00000000", date, run_id=run_id)
    snapshots = list(review.snapshots)
    if not snapshots:
        raise ValueError(f"账本中没有 {date} 的净值快照")
    latest = snapshots[-1]
    if str(latest["snapshot_date"]) != date:
        raise ValueError(f"账本中没有 {date} 的净值快照")
    previous = snapshots[-2] if len(snapshots) > 1 else None
    start_gross = float(previous["gross_pnl"]) if previous else 0.0
    start_net = float(previous["net_pnl"]) if previous else 0.0
    trades = ledger.query_trades(date, date, run_id)
    commission = sum(float(trade["commission"] or 0) for trade in trades)
    stamp_tax = sum(float(trade["stamp_tax"] or 0) for trade in trades)
    slippage = sum(float(trade["slippage"] or 0) for trade in trades)
    total_cost = sum(float(trade["total_cost"] or 0) for trade in trades)
    turnover = sum(float(trade["amount"] or 0) for trade in trades)
    base_value = (
        float(previous["total_value"]) if previous else float(latest["total_value"])
    )
    snapshot = ledger.query_snapshot()
    return DailyLedgerReport(
        date=date,
        account_id=ledger.account_id,
        run_id=str(latest.get("run_id") or ""),
        strategy_version=str(latest["strategy_version"]),
        data_version=str(latest["data_version"]),
        total_value=float(latest["total_value"]),
        cash=float(latest["cash"]),
        gross_profit=round(float(latest["gross_pnl"]) - start_gross, 2),
        net_profit=round(float(latest["net_pnl"]) - start_net, 2),
        commission=round(commission, 2),
        stamp_tax=round(stamp_tax, 2),
        slippage=round(slippage, 2),
        total_cost=round(total_cost, 2),
        turnover_rate=round(turnover / base_value, 6) if base_value > 0 else 0.0,
        benchmark_return=(
            float(latest["benchmark_return"])
            if latest.get("benchmark_return") is not None
            else None
        ),
        trade_count=len(trades),
        positions=tuple(snapshot.positions),
    )


def format_daily_ledger_report(report: DailyLedgerReport) -> str:
    """把单账本日报格式化为简洁中文文本。"""
    benchmark = (
        "--" if report.benchmark_return is None else f"{report.benchmark_return:+.2%}"
    )
    lines = [
        f"A 股稳健虚拟盘日报 {report.date}",
        "=" * 48,
        f"账户/批次: {report.account_id} / {report.run_id or '--'}",
        f"策略/数据: {report.strategy_version} / {report.data_version}",
        f"总资产: {report.total_value:,.2f} 元  现金: {report.cash:,.2f} 元",
        f"当日毛收益: {report.gross_profit:+,.2f} 元",
        f"当日净收益: {report.net_profit:+,.2f} 元  基准: {benchmark}",
        (
            f"成本: {report.total_cost:,.2f} 元 "
            f"(佣金 {report.commission:,.2f} / 印花税 {report.stamp_tax:,.2f} / "
            f"滑点 {report.slippage:,.2f})"
        ),
        f"换手率: {report.turnover_rate:.2%}  成交: {report.trade_count} 笔",
        "持仓:",
    ]
    if report.positions:
        for position in report.positions:
            lines.append(
                f"  {position['code']} {position['shares']}份/股 "
                f"成本 {position['avg_cost']:.4f} 现价 {position['current_price']:.4f}"
            )
    else:
        lines.append("  空仓")
    return "\n".join(lines) + "\n"


def save_daily_ledger_report(report: DailyLedgerReport, report_dir: Path) -> Path:
    """保存日报并返回文件路径。"""
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"daily_v2_{report.date}.txt"
    path.write_text(format_daily_ledger_report(report), encoding="utf-8")
    return path
