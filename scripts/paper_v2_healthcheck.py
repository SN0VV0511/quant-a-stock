"""paper_v2 SQLite、租约、信号和账户一致性健康检查。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config.settings import (  # noqa: E402
    ROBUST_V2_ACCOUNT_ID,
    ROBUST_V2_MAX_SINGLE_ETF,
    ROBUST_V2_MAX_SINGLE_STOCK,
    ROBUST_V2_MAX_TOTAL_POSITION,
    is_etf,
    is_supported_trading_target,
)
from scripts.paper_healthcheck import HealthcheckResult  # noqa: E402
from trading.ledger import PaperLedger  # noqa: E402


def run_v2_healthcheck(
    ledger_path: Path,
    *,
    account_id: str = ROBUST_V2_ACCOUNT_ID,
) -> HealthcheckResult:
    """检查 robust_v2 唯一事实源，不读取旧 JSON 账户文件。"""
    result = HealthcheckResult()
    path = ledger_path.expanduser().resolve()
    result.metrics["source"] = "paper_v2"
    result.metrics["ledger_path"] = str(path)
    if not path.exists():
        result.fail(f"缺少 paper_v2 账本: {path}")
        return result

    ledger = PaperLedger(path, account_id=account_id)
    try:
        ledger.connect()
        ledger.require_integrity()
        cash = ledger.query_cash()
        positions = ledger.query_positions()
        snapshot = ledger.query_snapshot()
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        result.fail(f"paper_v2 账本不可用: {exc}")
        return result
    finally:
        ledger.close()

    if cash < 0:
        result.fail(f"账户现金为负数: {cash:.2f}")
    for code, position in positions.items():
        if not is_supported_trading_target(code):
            result.fail(f"持仓标的不在账户权限范围: {code}")
        shares = int(position.get("shares", 0) or 0)
        if shares <= 0:
            result.fail(f"持仓数量非法: {code}={shares}")
        ratio = (
            float(position.get("current_price", 0) or 0) * shares / snapshot.total_value
            if snapshot.total_value > 0
            else 0.0
        )
        limit = ROBUST_V2_MAX_SINGLE_ETF if is_etf(code) else ROBUST_V2_MAX_SINGLE_STOCK
        if ratio > limit + 0.01:
            result.fail(f"单票仓位超限: {code} {ratio:.2%} > {limit:.2%}")
    if snapshot.position_ratio > ROBUST_V2_MAX_TOTAL_POSITION + 0.01:
        result.fail(
            f"总仓位超限: {snapshot.position_ratio:.2%} > "
            f"{ROBUST_V2_MAX_TOTAL_POSITION:.2%}"
        )

    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        active_runs = int(
            connection.execute(
                "SELECT COUNT(*) FROM runs WHERE account_id = ? AND status = 'running'",
                (account_id,),
            ).fetchone()[0]
        )
        lease = connection.execute(
            "SELECT holder_id, heartbeat_at, expires_at FROM leases WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        retryable_signals = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM signals
                WHERE account_id = ? AND status IN ('retryable', 'executing')
                """,
                (account_id,),
            ).fetchone()[0]
        )
        failed_jobs = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM daily_jobs
                WHERE account_id = ? AND status = 'failed'
                """,
                (account_id,),
            ).fetchone()[0]
        )
        snapshot_days = int(
            connection.execute(
                """
                SELECT COUNT(DISTINCT snapshot_date) FROM nav_snapshots
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()[0]
        )
    except sqlite3.Error as exc:
        result.fail(f"读取 paper_v2 运维状态失败: {exc}")
        return result
    finally:
        connection.close()

    if active_runs > 1:
        result.fail(f"存在 {active_runs} 个并发写运行")
    lease_active = bool(lease is not None and float(lease["expires_at"]) > time.time())
    if active_runs == 1 and not lease_active:
        result.fail("运行会话存在但账户写租约已失效")
    if active_runs == 0:
        result.warn("robust_v2 守护进程当前未运行")
    if retryable_signals:
        result.warn(f"当前有 {retryable_signals} 个信号正在执行或等待重试")
    if failed_jobs:
        result.warn(f"历史上有 {failed_jobs} 个收盘任务失败记录")

    result.metrics.update(
        {
            "cash": round(cash, 2),
            "total_value": snapshot.total_value,
            "position_ratio": snapshot.position_ratio,
            "position_count": snapshot.position_count,
            "active_writer_runs": active_runs,
            "lease_active": lease_active,
            "lease_holder": str(lease["holder_id"]) if lease is not None else "",
            "lease_heartbeat_at": (
                str(lease["heartbeat_at"]) if lease is not None else ""
            ),
            "retryable_signals": retryable_signals,
            "failed_daily_jobs": failed_jobs,
            "snapshot_days": snapshot_days,
        }
    )
    return result


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="paper_v2 账本与守护租约健康检查")
    parser.add_argument("--ledger", default="data/paper_v2.db", help="SQLite 账本路径")
    parser.add_argument("--account-id", default=ROBUST_V2_ACCOUNT_ID, help="账户标识")
    parser.add_argument(
        "--require-running", action="store_true", help="要求守护租约有效"
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser.parse_args()


def main() -> int:
    """执行健康检查并返回适合容器探针的退出码。"""
    args = _parse_args()
    result = run_v2_healthcheck(
        Path(args.ledger),
        account_id=str(args.account_id),
    )
    if args.require_running and not (
        result.metrics.get("active_writer_runs") == 1
        and result.metrics.get("lease_active") is True
    ):
        result.fail("robust_v2 守护进程租约未生效")
    payload = asdict(result)
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(("OK" if result.ok else "FAIL") + "\n")
        for message in result.failures:
            sys.stdout.write(f"- {message}\n")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
