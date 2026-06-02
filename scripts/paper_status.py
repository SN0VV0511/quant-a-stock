"""虚拟盘观察期统一状态汇总。

日常巡检时不应该分别执行多个脚本再人工拼结果。本模块汇总后台服务、健康检查、
观察期复盘、QMT dry-run 前置验收和最新日志，作为终端和 Web API 的统一状态源。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.monthly_review import build_review
from scripts.paper_acceptance import run_acceptance
from scripts.paper_healthcheck import run_healthcheck
from scripts.paper_service import get_status
from config.time_utils import format_local

T = TypeVar("T")


@dataclass(frozen=True)
class ObservationStatus:
    """虚拟盘观察期统一状态。"""

    generated_at: str
    root_dir: str
    service: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)
    acceptance: dict[str, Any] = field(default_factory=dict)
    logs: dict[str, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def _tail_lines(path: Path, max_lines: int) -> list[str]:
    """读取文本文件最后 N 行。"""
    if max_lines <= 0 or not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()[-max_lines:]
    except OSError as exc:
        return [f"读取日志失败: {path} - {exc}"]


def _safe_section(name: str, errors: list[str], callback: Callable[[], T], fallback: T) -> T:
    """执行状态片段，失败时保留错误信息并返回兜底值。"""
    try:
        return callback()
    except Exception as exc:  # noqa: BLE001 - 状态汇总不能因单个片段失败整体不可用
        errors.append(f"{name} 失败: {exc}")
        return fallback


def build_status(
    root_dir: Path,
    days: int = 30,
    min_snapshot_days: int = 20,
    max_drawdown: float = 0.06,
    log_lines: int = 80,
) -> ObservationStatus:
    """构建虚拟盘观察期统一状态。

    Args:
        root_dir: 项目根目录。
        days: 复盘最近 N 天。
        min_snapshot_days: QMT dry-run 前置验收需要的最少快照天数。
        max_drawdown: QMT dry-run 前置验收允许的最大回撤。
        log_lines: 每个日志文件返回的最大行数。

    Returns:
        ObservationStatus: 可序列化状态汇总。
    """
    root_dir = root_dir.resolve()
    errors: list[str] = []

    service = _safe_section(
        "服务状态",
        errors,
        lambda: asdict(get_status(root_dir)),
        {},
    )
    health = _safe_section(
        "健康检查",
        errors,
        lambda: asdict(run_healthcheck(root_dir)),
        {},
    )
    review = _safe_section(
        "观察期复盘",
        errors,
        lambda: asdict(build_review(root_dir, days=days)),
        {},
    )
    acceptance = _safe_section(
        "观察期验收",
        errors,
        lambda: asdict(run_acceptance(
            root_dir,
            days=days,
            min_snapshot_days=min_snapshot_days,
            max_drawdown=max_drawdown,
        )),
        {},
    )

    logs = {
        "live_today": _tail_lines(root_dir / "logs" / "live_today.log", log_lines),
        "live": _tail_lines(root_dir / "logs" / "live.log", log_lines),
        "service": _tail_lines(root_dir / "logs" / "paper_daemon_service.log", log_lines),
    }

    return ObservationStatus(
        generated_at=format_local(),
        root_dir=str(root_dir),
        service=service,
        health=health,
        review=review,
        acceptance=acceptance,
        logs=logs,
        errors=errors,
    )


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="汇总 A 股虚拟盘观察期状态")
    parser.add_argument("--root", default=str(ROOT_DIR), help="项目根目录")
    parser.add_argument("--days", type=int, default=30, help="复盘最近 N 天")
    parser.add_argument("--min-snapshot-days", type=int, default=20, help="QMT dry-run 前置最少快照天数")
    parser.add_argument("--max-drawdown", type=float, default=0.06, help="QMT dry-run 前置最大允许回撤")
    parser.add_argument("--log-lines", type=int, default=80, help="每个日志文件返回的最大行数")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser.parse_args()


def main() -> int:
    """命令行入口。"""
    args = _parse_args()
    status = build_status(
        Path(args.root),
        days=args.days,
        min_snapshot_days=args.min_snapshot_days,
        max_drawdown=args.max_drawdown,
        log_lines=args.log_lines,
    )
    payload = asdict(status)
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
    else:
        service = status.service
        acceptance = status.acceptance
        review = status.review
        sys.stdout.write(
            "虚拟盘状态: "
            f"service_running={service.get('running', False)} "
            f"ready_for_qmt_dry_run={acceptance.get('ready_for_qmt_dry_run', False)} "
            f"return={float(review.get('total_return', 0) or 0):.2%} "
            f"drawdown={float(review.get('max_drawdown', 0) or 0):.2%}\n"
        )
        for error in status.errors:
            sys.stdout.write(f"ERROR {error}\n")
        for failure in acceptance.get("failures", []):
            sys.stdout.write(f"WAIT {failure}\n")
    return 0 if not status.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
