"""paper_v2 SQLite、租约、信号和账户一致性健康检查。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config.settings import (  # noqa: E402
    INITIAL_CAPITAL,
    ROBUST_V2_ACCOUNT_ID,
    ROBUST_V2_MAX_SINGLE_ETF,
    ROBUST_V2_MAX_SINGLE_STOCK,
    ROBUST_V2_MAX_TOTAL_POSITION,
    ROBUST_V2_STRATEGY_VERSION,
    is_etf,
    is_supported_trading_target,
)
from scripts.paper_healthcheck import HealthcheckResult  # noqa: E402


_REQUIRED_TABLES = frozenset(
    {
        "metadata",
        "accounts",
        "leases",
        "runs",
        "signals",
        "orders",
        "trades",
        "positions",
        "lots",
        "nav_snapshots",
        "daily_jobs",
    }
)


def _open_readonly_ledger(
    path: Path,
) -> tuple[sqlite3.Connection, tempfile.TemporaryDirectory[str]]:
    """通过 SQLite backup 获取含 WAL 的事务一致只读快照。"""
    wal_path = path.with_name(f"{path.name}-wal")
    shm_path = path.with_name(f"{path.name}-shm")
    for _attempt in range(3):
        wal_exists = wal_path.is_file()
        if wal_exists and not shm_path.is_file():
            raise RuntimeError("账本 WAL 存在但 SHM 缺失，拒绝创建源 sidecar")

        # 无 WAL 时 immutable 不会触发 WAL/SHM 创建；若备份期间出现 WAL，
        # 丢弃该候选并改用能读取 WAL 的普通只读连接重试。
        source_options = "mode=ro" if wal_exists else "mode=ro&immutable=1"
        source = sqlite3.connect(
            f"{path.as_uri()}?{source_options}",
            uri=True,
            timeout=2,
            isolation_level=None,
        )
        source.execute("PRAGMA query_only = ON")
        temporary = tempfile.TemporaryDirectory(prefix="paper-v2-health-")
        snapshot = Path(temporary.name) / path.name
        destination = sqlite3.connect(snapshot)
        try:
            source.backup(destination)
        except Exception:
            temporary.cleanup()
            raise
        finally:
            destination.close()
            source.close()

        if not wal_exists and wal_path.is_file():
            temporary.cleanup()
            continue

        connection = sqlite3.connect(
            f"{snapshot.as_uri()}?mode=ro",
            uri=True,
            timeout=2,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection, temporary
    raise RuntimeError("账本在只读备份期间持续切换 WAL，无法取得一致快照")


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
    try:
        valid_file = path.is_file() and path.stat().st_size > 0
    except OSError as exc:
        result.fail(f"paper_v2 账本不可用: {exc}")
        return result
    if not valid_file:
        result.fail("paper_v2 账本不可用: 账本文件为空或不是普通文件")
        return result

    connection: sqlite3.Connection | None = None
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        connection, temporary = _open_readonly_ledger(path)
        integrity = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        if integrity != ["ok"]:
            raise RuntimeError(f"账本一致性检查失败: {integrity}")

        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing_tables = sorted(_REQUIRED_TABLES - tables)
        if missing_tables:
            raise RuntimeError(f"账本缺少必要表: {', '.join(missing_tables)}")

        schema_row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        if schema_row is None or not str(schema_row["value"]).strip():
            raise RuntimeError("账本缺少 schema_version")

        account = connection.execute(
            "SELECT initial_cash, cash FROM accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if account is None:
            raise RuntimeError(f"账户不存在: {account_id}")
        initial_cash = float(account["initial_cash"])
        cash = float(account["cash"])
        positions = connection.execute(
            """
            SELECT code, name, shares, avg_cost, last_price, strategy_version
            FROM positions
            WHERE account_id = ?
            ORDER BY code
            """,
            (account_id,),
        ).fetchall()

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
        snapshot_summary = connection.execute(
            """
            SELECT COUNT(DISTINCT snapshot_date) AS snapshot_days,
                   MAX(snapshot_date) AS latest_snapshot_date
            FROM nav_snapshots
            WHERE account_id = ?
            """,
            (account_id,),
        ).fetchone()
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        result.fail(f"paper_v2 账本不可用: {exc}")
        return result
    finally:
        if connection is not None:
            connection.close()
        if temporary is not None:
            temporary.cleanup()

    if cash < 0:
        result.fail(f"账户现金为负数: {cash:.2f}")
    if abs(initial_cash - INITIAL_CAPITAL) > 0.01:
        result.fail(f"账户初始资金不符: {initial_cash:.2f} != {INITIAL_CAPITAL:.2f}")
    market_value = 0.0
    for position in positions:
        code = str(position["code"])
        if not is_supported_trading_target(code):
            result.fail(f"持仓标的不在账户权限范围: {code}")
        shares = int(position["shares"] or 0)
        if shares <= 0:
            result.fail(f"持仓数量非法: {code}={shares}")
        if str(position["strategy_version"]) != ROBUST_V2_STRATEGY_VERSION:
            result.fail(f"持仓策略版本不符: {code}={position['strategy_version']}")
        market_value += float(position["last_price"] or 0) * shares
    total_value = cash + market_value
    position_ratio = market_value / total_value if total_value > 0 else 0.0
    for position in positions:
        code = str(position["code"])
        shares = int(position["shares"] or 0)
        ratio = (
            float(position["last_price"] or 0) * shares / total_value
            if total_value > 0
            else 0.0
        )
        limit = ROBUST_V2_MAX_SINGLE_ETF if is_etf(code) else ROBUST_V2_MAX_SINGLE_STOCK
        if ratio > limit + 0.01:
            result.fail(f"单票仓位超限: {code} {ratio:.2%} > {limit:.2%}")
    if position_ratio > ROBUST_V2_MAX_TOTAL_POSITION + 0.01:
        result.fail(
            f"总仓位超限: {position_ratio:.2%} > {ROBUST_V2_MAX_TOTAL_POSITION:.2%}"
        )

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
            "ledger_readable": True,
            "schema_version": str(schema_row["value"]),
            "initial_cash": round(initial_cash, 2),
            "cash": round(cash, 2),
            "total_value": round(total_value, 2),
            "position_ratio": round(position_ratio, 6),
            "position_count": len(positions),
            "active_writer_runs": active_runs,
            "lease_active": lease_active,
            "lease_holder": str(lease["holder_id"]) if lease is not None else "",
            "lease_heartbeat_at": (
                str(lease["heartbeat_at"]) if lease is not None else ""
            ),
            "retryable_signals": retryable_signals,
            "failed_daily_jobs": failed_jobs,
            "snapshot_days": int(snapshot_summary["snapshot_days"] or 0),
            "latest_snapshot_date": str(snapshot_summary["latest_snapshot_date"] or ""),
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
