"""虚拟盘端到端烟测。

该脚本默认写入临时目录，完整验证：
信号 -> 风控审批 -> 虚拟成交 -> 交易流水 -> 账户快照 -> 健康检查 -> 复盘摘要。
它不访问真实行情源，也不会发送任何真实委托。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from risk.control import RiskController
from scripts.monthly_review import ReviewSummary, build_review
from scripts.paper_healthcheck import run_healthcheck
from trading.brokers import PaperBrokerAdapter
from trading.models import ExecutionReport, OrderIntent, RiskDecision
from trading.observability import EventRecorder


LOGGER = logging.getLogger("paper_smoke_run")


@dataclass(frozen=True)
class SmokeRunResult:
    """虚拟盘烟测结果。"""

    root_dir: str
    ok: bool
    buy_report: dict[str, Any]
    sell_report: dict[str, Any]
    review: dict[str, Any]
    healthcheck_failures: list[str]
    healthcheck_warnings: list[str]


def _record_order(
    order: OrderIntent,
    broker: PaperBrokerAdapter,
    risk_ctrl: RiskController,
    recorder: EventRecorder,
    market_data: dict[str, dict[str, Any]],
) -> ExecutionReport:
    """记录并执行一笔烟测订单。"""
    recorder.record("signal", order.to_dict())
    approved, rejected = risk_ctrl.filter_order_intents([order], broker.portfolio, market_data)
    if rejected:
        decision = rejected[0]
        recorder.record("risk_rejected", decision.to_dict())
        raise RuntimeError(f"烟测订单被风控拒绝: {decision.reason}")

    approved_order = approved[0]
    recorder.record("risk_approved", RiskDecision(
        order=approved_order,
        approved=True,
        reason="通过",
    ).to_dict())
    report = broker.place_order(approved_order)
    recorder.record("execution", report.to_dict())
    if not report.is_success:
        raise RuntimeError(f"烟测订单成交失败: {report.message}")
    return report


def run_smoke(root_dir: Path | None = None) -> SmokeRunResult:
    """运行虚拟盘端到端烟测。

    Args:
        root_dir: 烟测输出目录。为 None 时创建临时目录。

    Returns:
        SmokeRunResult: 烟测结果。
    """
    if root_dir is None:
        root_dir = Path(tempfile.mkdtemp(prefix="quant-a-stock-paper-smoke-"))
    data_dir = root_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    broker = PaperBrokerAdapter(
        state_file=str(data_dir / "portfolio_state.json"),
        trade_log_file=str(data_dir / "trade_log.json"),
        snapshot_log_file=str(data_dir / "portfolio_snapshots.jsonl"),
    )
    broker.connect()
    risk_ctrl = RiskController()
    risk_ctrl.set_daily_start(broker.portfolio)
    recorder = EventRecorder(str(data_dir / "trade_events.jsonl"))

    today = datetime.now().strftime("%Y%m%d")
    next_day = (datetime.now() + timedelta(days=1)).strftime("%Y%m%d")
    market_data = {
        "sh601988": {
            "current_price": 5.0,
            "prev_close": 4.95,
            "is_st": False,
            "is_suspended": False,
        }
    }

    buy_report = _record_order(
        OrderIntent(
            code="sh601988",
            action="buy",
            price=5.0,
            shares=100,
            name="中国银行",
            strategy="虚拟盘烟测",
            reason="固定样本买入信号",
            date=today,
            source="paper_smoke_run",
        ),
        broker,
        risk_ctrl,
        recorder,
        market_data,
    )
    broker.portfolio.update_prices({"sh601988": 5.0})
    broker.portfolio.save_snapshot(today, {"sh601988": 5.0})
    recorder.record("portfolio_snapshot", broker.query_snapshot({"sh601988": 5.0}).to_dict())

    market_data["sh601988"]["current_price"] = 5.2
    market_data["sh601988"]["prev_close"] = 5.0
    sell_report = _record_order(
        OrderIntent(
            code="sh601988",
            action="sell",
            price=5.2,
            shares=100,
            name="中国银行",
            strategy="虚拟盘烟测",
            reason="固定样本次日卖出信号",
            date=next_day,
            source="paper_smoke_run",
        ),
        broker,
        risk_ctrl,
        recorder,
        market_data,
    )
    broker.portfolio.save_snapshot(next_day, {})
    recorder.record("portfolio_snapshot", broker.query_snapshot({}).to_dict())
    broker.close()

    health = run_healthcheck(
        root_dir,
        max_snapshot_age_minutes=10_000_000,
        strict_snapshot=True,
        strict_events=True,
    )
    review: ReviewSummary = build_review(root_dir, days=30)
    return SmokeRunResult(
        root_dir=str(root_dir),
        ok=health.ok,
        buy_report=buy_report.to_dict(),
        sell_report=sell_report.to_dict(),
        review=asdict(review),
        healthcheck_failures=health.failures,
        healthcheck_warnings=health.warnings,
    )


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="运行虚拟盘端到端烟测")
    parser.add_argument("--root", help="烟测输出目录；不传则使用临时目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser.parse_args()


def main() -> int:
    """命令行入口。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()
    result = run_smoke(Path(args.root) if args.root else None)
    payload = asdict(result)
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
    else:
        LOGGER.info(
            "虚拟盘烟测%s: 输出目录=%s 收益=%.2f%% 回撤=%.2f%%",
            "通过" if result.ok else "失败",
            result.root_dir,
            result.review["total_return"] * 100,
            result.review["max_drawdown"] * 100,
        )
        for warning in result.healthcheck_warnings:
            LOGGER.warning(warning)
        for failure in result.healthcheck_failures:
            LOGGER.error(failure)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
