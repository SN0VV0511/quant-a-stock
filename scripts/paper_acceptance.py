"""虚拟盘观察期验收门槛。

跑满观察期后，用这个脚本判断当前证据是否足够进入 QMT dry-run 联调。它只做
本地文件验收，不连接 QMT，也不发送真实委托。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.monthly_review import ReviewSummary, build_review
from scripts.paper_healthcheck import HealthcheckResult, run_healthcheck
from config.settings import ENABLE_RPS_ROTATION, RPS_STATE_FILE
from config.time_utils import now_local


@dataclass(frozen=True)
class AcceptanceResult:
    """观察期验收结果。"""

    ready_for_qmt_dry_run: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    snapshot_days: int = 0
    required_snapshot_days: int = 20
    review: dict[str, Any] = field(default_factory=dict)
    health_metrics: dict[str, Any] = field(default_factory=dict)
    rps: dict[str, Any] = field(default_factory=dict)


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
    """解析常用日期格式。"""
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _snapshot_day_count(root_dir: Path, days: int) -> int:
    """统计最近 N 天内有账户快照的自然日数量。"""
    cutoff = now_local() - timedelta(days=days)
    snapshot_dates = set()
    # 优先读 portfolio_snapshots.jsonl
    for row in _load_jsonl(root_dir / "data" / "portfolio_snapshots.jsonl"):
        date = str(row.get("date", ""))
        parsed = _parse_date(date)
        if parsed is not None and parsed >= cutoff:
            snapshot_dates.add(date)
    # 回退: 从 trade_events.jsonl 读取盘中快照
    if not snapshot_dates:
        for row in _load_jsonl(root_dir / "data" / "trade_events.jsonl"):
            if row.get("event_type") != "portfolio_snapshot":
                continue
            ts = str(row.get("timestamp", ""))
            parsed = _parse_date(ts)
            if parsed is not None and parsed >= cutoff:
                snapshot_dates.add(parsed.strftime("%Y%m%d"))
    return len(snapshot_dates)


def _load_rps_state(root_dir: Path) -> dict[str, Any]:
    """读取 ETF/RPS 日频状态。"""
    configured = Path(RPS_STATE_FILE)
    path = configured if configured.is_absolute() else root_dir / configured
    if not path.exists():
        fallback = root_dir / "data" / "rps_state.json"
        path = fallback
    if not path.exists():
        return {"available": False, "status": "missing"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["available"] = True
        return data
    except (json.JSONDecodeError, OSError) as exc:
        return {"available": False, "status": "error", "error": str(exc)}


def _check_rps_acceptance(root_dir: Path, days: int) -> tuple[dict[str, Any], list[str], list[str]]:
    """检查 ETF/RPS 是否具备观察期验收证据。"""
    rps_state = _load_rps_state(root_dir)
    failures: list[str] = []
    warnings: list[str] = []
    if not ENABLE_RPS_ROTATION:
        warnings.append("ETF/RPS 轮动已关闭，观察期不会验证该策略")
        return rps_state, failures, warnings
    if not rps_state.get("available"):
        failures.append("ETF/RPS 状态文件缺失或不可读")
        return rps_state, failures, warnings
    if rps_state.get("status") != "ok" or rps_state.get("completed") is not True:
        failures.append(f"ETF/RPS 最近一次运行未完成: {rps_state.get('status')}")
    parsed = _parse_date(str(rps_state.get("date", "")))
    if parsed is None:
        failures.append("ETF/RPS 状态缺少可解析日期")
    elif parsed < now_local() - timedelta(days=days):
        failures.append(f"ETF/RPS 状态过旧: {rps_state.get('date')}")
    if int(rps_state.get("etf_loaded", 0) or 0) == 0:
        failures.append("ETF/RPS 未成功加载 ETF 历史数据")
    if int(rps_state.get("industry_loaded", 0) or 0) == 0:
        warnings.append("ETF/RPS 未成功加载行业指数数据，行业强弱观察不可用")
    return rps_state, failures, warnings


def run_acceptance(
    root_dir: Path,
    days: int = 30,
    min_snapshot_days: int = 20,
    max_drawdown: float = 0.06,
    min_total_return: float | None = None,
    strict_report: bool = True,
) -> AcceptanceResult:
    """运行虚拟盘观察期验收。

    Args:
        root_dir: 项目根目录。
        days: 复盘最近 N 天。
        min_snapshot_days: 至少需要多少个有快照的交易日。
        max_drawdown: 可接受最大回撤。
        min_total_return: 可选最低总收益率。
        strict_report: 是否要求最新日报与最新快照一致。

    Returns:
        AcceptanceResult: 验收结果。
    """
    health: HealthcheckResult = run_healthcheck(
        root_dir,
        max_snapshot_age_minutes=days * 24 * 60,
        strict_snapshot=True,
        strict_events=True,
        strict_report=strict_report,
    )
    review: ReviewSummary = build_review(root_dir, days=days)
    snapshot_days = _snapshot_day_count(root_dir, days)
    rps_state, rps_failures, rps_warnings = _check_rps_acceptance(root_dir, days)

    failures = list(health.failures)
    warnings = list(health.warnings)
    failures.extend(rps_failures)
    warnings.extend(rps_warnings)
    if snapshot_days < min_snapshot_days:
        failures.append(f"快照天数不足: {snapshot_days} < {min_snapshot_days}")
    if review.max_drawdown > max_drawdown:
        failures.append(f"最大回撤超限: {review.max_drawdown:.2%} > {max_drawdown:.2%}")
    if min_total_return is not None and review.total_return < min_total_return:
        failures.append(f"总收益率未达标: {review.total_return:.2%} < {min_total_return:.2%}")
    if review.trade_count == 0:
        warnings.append("观察期内没有交易，策略触发频率需要人工确认")

    return AcceptanceResult(
        ready_for_qmt_dry_run=not failures,
        failures=failures,
        warnings=warnings,
        snapshot_days=snapshot_days,
        required_snapshot_days=min_snapshot_days,
        review=asdict(review),
        health_metrics=health.metrics,
        rps=rps_state,
    )


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="验证虚拟盘观察期是否满足 QMT dry-run 前置门槛")
    parser.add_argument("--root", default=str(ROOT_DIR), help="项目根目录")
    parser.add_argument("--days", type=int, default=30, help="复盘最近 N 天")
    parser.add_argument("--min-snapshot-days", type=int, default=20, help="最少快照天数")
    parser.add_argument("--max-drawdown", type=float, default=0.06, help="最大允许回撤，例如 0.06")
    parser.add_argument("--min-total-return", type=float, default=None, help="可选最低总收益率，例如 0.02")
    parser.add_argument("--no-strict-report", action="store_true", help="不校验日报与快照一致性")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser.parse_args()


def main() -> int:
    """命令行入口。"""
    args = _parse_args()
    result = run_acceptance(
        Path(args.root),
        days=args.days,
        min_snapshot_days=args.min_snapshot_days,
        max_drawdown=args.max_drawdown,
        min_total_return=args.min_total_return,
        strict_report=not args.no_strict_report,
    )
    payload = asdict(result)
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
    else:
        status = "通过" if result.ready_for_qmt_dry_run else "未通过"
        sys.stdout.write(
            f"虚拟盘观察期验收{status}: 快照天数 {result.snapshot_days}/"
            f"{result.required_snapshot_days}, 收益 {result.review.get('total_return', 0):.2%}, "
            f"回撤 {result.review.get('max_drawdown', 0):.2%}\n"
        )
        for item in result.failures:
            sys.stdout.write(f"FAIL {item}\n")
        for item in result.warnings:
            sys.stdout.write(f"WARN {item}\n")
    return 0 if result.ready_for_qmt_dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
