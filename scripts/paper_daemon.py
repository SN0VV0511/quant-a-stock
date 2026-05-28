"""虚拟盘观察期守护脚本。

该脚本用于一个月观察期的无人值守运行：在交易日 09:00-15:00 时间窗内启动
`live_runner.py --broker paper`，收盘后执行健康检查和复盘摘要。它不引入额外调度
依赖，适合先用终端、tmux、launchd 或 cron 托管。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Literal

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from data.holidays import is_trading_day as is_calendar_trading_day
from scripts.monthly_review import build_review
from scripts.paper_healthcheck import run_healthcheck


LOGGER = logging.getLogger("paper_daemon")
SessionState = Literal["before_session", "in_session", "after_session", "non_trading_day"]


@dataclass(frozen=True)
class DaemonConfig:
    """守护进程配置。"""

    root_dir: Path
    broker: str = "paper"
    watch_interval: int = 4
    scan_interval: int = 600
    top_n: int = 20
    poll_seconds: int = 300
    once: bool = False
    dry_run: bool = False
    ignore_calendar: bool = False
    review_days: int = 30


@dataclass(frozen=True)
class DaemonDecision:
    """单次调度决策。"""

    state: SessionState
    should_run: bool
    sleep_seconds: int
    reason: str


def _parse_hhmm(value: str) -> dt_time:
    """解析 HH:MM 时间。"""
    try:
        hour, minute = value.split(":", 1)
        return dt_time(hour=int(hour), minute=int(minute))
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError(f"时间格式应为 HH:MM: {value}") from exc


def is_trading_day(date_str: str, ignore_calendar: bool = False) -> bool:
    """判断是否为交易日。"""
    if ignore_calendar:
        return True
    try:
        return is_calendar_trading_day(date_str)
    except Exception as exc:
        LOGGER.warning("交易日历判断失败，回退到周末判断: %s", exc)
        return datetime.strptime(date_str, "%Y%m%d").weekday() < 5


def decide_next_action(
    now: datetime,
    trading_day: bool,
    pre_market: dt_time,
    market_close: dt_time,
    poll_seconds: int,
) -> DaemonDecision:
    """根据当前时间决定是否启动虚拟盘。"""
    if not trading_day:
        return DaemonDecision(
            state="non_trading_day",
            should_run=False,
            sleep_seconds=poll_seconds,
            reason="非交易日，等待下一轮检查",
        )

    if now.time() < pre_market:
        start_at = now.replace(hour=pre_market.hour, minute=pre_market.minute, second=0, microsecond=0)
        wait_seconds = max(1, int((start_at - now).total_seconds()))
        return DaemonDecision(
            state="before_session",
            should_run=False,
            sleep_seconds=wait_seconds,
            reason=f"未到盘前启动时间 {pre_market.strftime('%H:%M')}",
        )

    if now.time() >= market_close:
        return DaemonDecision(
            state="after_session",
            should_run=False,
            sleep_seconds=poll_seconds,
            reason=f"已过收盘时间 {market_close.strftime('%H:%M')}",
        )

    return DaemonDecision(
        state="in_session",
        should_run=True,
        sleep_seconds=0,
        reason="处于交易日运行窗口",
    )


def build_live_command(config: DaemonConfig) -> list[str]:
    """构建 live_runner 命令。"""
    command = [
        sys.executable,
        str(config.root_dir / "live_runner.py"),
        "--broker",
        config.broker,
        "--watch-interval",
        str(config.watch_interval),
        "--scan-interval",
        str(config.scan_interval),
        "--top-n",
        str(config.top_n),
    ]
    if config.ignore_calendar:
        command.append("--ignore-calendar")
    return command


def run_one_session(config: DaemonConfig) -> int:
    """启动一次虚拟盘会话并执行收盘后检查。"""
    command = build_live_command(config)
    LOGGER.info("启动虚拟盘会话: %s", " ".join(command))
    if config.dry_run:
        return 0

    completed = subprocess.run(command, cwd=str(config.root_dir), check=False)
    health = run_healthcheck(
        config.root_dir,
        strict_snapshot=True,
        strict_events=True,
        strict_report=True,
    )
    review = build_review(config.root_dir, days=config.review_days)

    LOGGER.info(
        "收盘检查: health_ok=%s, %s日收益=%.2f%%, 最大回撤=%.2f%%, 交易=%s笔",
        health.ok,
        config.review_days,
        review.total_return * 100,
        review.max_drawdown * 100,
        review.trade_count,
    )
    for failure in health.failures:
        LOGGER.error("健康检查失败: %s", failure)
    for warning in health.warnings:
        LOGGER.warning("健康检查警告: %s", warning)

    if completed.returncode != 0:
        return completed.returncode
    return 0 if health.ok else 2


def run_daemon(
    config: DaemonConfig,
    pre_market: dt_time,
    market_close: dt_time,
    now_provider=datetime.now,
    sleep_func=time.sleep,
) -> int:
    """运行虚拟盘守护循环。"""
    while True:
        now = now_provider()
        date_str = now.strftime("%Y%m%d")
        trading_day = is_trading_day(date_str, config.ignore_calendar)
        decision = decide_next_action(now, trading_day, pre_market, market_close, config.poll_seconds)
        LOGGER.info(
            "调度检查: date=%s state=%s run=%s reason=%s",
            date_str,
            decision.state,
            decision.should_run,
            decision.reason,
        )

        if decision.should_run:
            code = run_one_session(config)
            if config.once or code != 0:
                return code
            sleep_func(config.poll_seconds)
            continue

        if config.once or config.dry_run:
            return 0
        sleep_func(decision.sleep_seconds)


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="A 股虚拟盘观察期守护脚本")
    parser.add_argument("--root", default=ROOT_DIR, help="项目根目录")
    parser.add_argument("--broker", default="paper", choices=["paper"], help="观察期只允许虚拟盘")
    parser.add_argument("--watch-interval", type=int, default=4, help="盯盘刷新秒数")
    parser.add_argument("--scan-interval", type=int, default=600, help="扫描间隔秒数")
    parser.add_argument("--top-n", type=int, default=20, help="候选股数量")
    parser.add_argument("--pre-market", type=_parse_hhmm, default=dt_time(9, 0), help="盘前启动时间 HH:MM")
    parser.add_argument("--market-close", type=_parse_hhmm, default=dt_time(15, 0), help="收盘时间 HH:MM")
    parser.add_argument("--poll-seconds", type=int, default=300, help="非运行窗口轮询秒数")
    parser.add_argument("--review-days", type=int, default=30, help="收盘后复盘天数")
    parser.add_argument("--once", action="store_true", help="只做一次调度判断")
    parser.add_argument("--dry-run", action="store_true", help="只打印调度和命令，不启动 live_runner")
    parser.add_argument("--ignore-calendar", action="store_true", help="忽略交易日历，便于联调")
    parser.add_argument("--json", action="store_true", help="输出一次 dry-run 调度 JSON")
    return parser.parse_args()


def main() -> int:
    """命令行入口。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = _parse_args()
    config = DaemonConfig(
        root_dir=Path(args.root).resolve(),
        broker=args.broker,
        watch_interval=args.watch_interval,
        scan_interval=args.scan_interval,
        top_n=args.top_n,
        poll_seconds=args.poll_seconds,
        once=args.once,
        dry_run=args.dry_run,
        ignore_calendar=args.ignore_calendar,
        review_days=args.review_days,
    )

    if args.json:
        now = datetime.now()
        decision = decide_next_action(
            now,
            is_trading_day(now.strftime("%Y%m%d"), config.ignore_calendar),
            args.pre_market,
            args.market_close,
            config.poll_seconds,
        )
        payload = {
            "config": {**asdict(config), "root_dir": str(config.root_dir)},
            "decision": asdict(decision),
            "live_command": build_live_command(config),
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
        return 0

    return run_daemon(config, args.pre_market, args.market_close)


if __name__ == "__main__":
    raise SystemExit(main())
