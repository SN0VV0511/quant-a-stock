"""SQLite 单账本虚拟盘。

该账本是 ``paper_v2`` 的唯一事实来源。账户现金、分批持仓、信号、幂等订单、成交、
净值和单实例写租约都在同一个 SQLite 文件中更新，重启不会重置账户或重复下单。
"""

from __future__ import annotations

import contextlib
import copy
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, cast

from config.settings import (
    INITIAL_CAPITAL,
    ROBUST_V2_ACCOUNT_ID,
    get_trading_permission_rejection_reason,
    is_supported_trading_target,
)
from config.time_utils import format_local, now_local, today_yyyymmdd
from rules.engine import TradingRules
from trading.instruments import get_instrument_profile
from trading.models import (
    ExecutionReport,
    OrderIntent,
    PortfolioSnapshot,
    StrategyTag,
    TargetPortfolio,
    TargetPosition,
)

SCHEMA_VERSION = 1


class LeaseUnavailableError(RuntimeError):
    """账户写租约已被其他实例持有。"""


@dataclass(frozen=True)
class LedgerRun:
    """一次可审计运行会话。"""

    run_id: str
    account_id: str
    strategy_version: str
    code_commit: str
    config_hash: str
    data_version: str
    started_at: str


@dataclass(frozen=True)
class LedgerReview:
    """同一账户和运行批次的复盘明细。"""

    account_id: str
    run_id: str | None
    start_date: str
    end_date: str
    snapshots: tuple[dict[str, Any], ...]
    trades: tuple[dict[str, Any], ...]


class PaperLedger:
    """线程安全的 SQLite 虚拟盘账本。"""

    def __init__(
        self,
        path: str | Path,
        account_id: str = ROBUST_V2_ACCOUNT_ID,
        initial_cash: float = INITIAL_CAPITAL,
        *,
        enforce_t1: bool = True,
        strict_order_source: bool = True,
        rules: TradingRules | None = None,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("初始资金必须大于 0")
        if not account_id.strip():
            raise ValueError("账户标识不能为空")
        if not enforce_t1:
            raise ValueError("paper_v2 强制 T+1，不能关闭")
        self.path = Path(path).expanduser().resolve()
        self.account_id = account_id
        self.initial_cash = round(float(initial_cash), 2)
        self.enforce_t1 = enforce_t1
        self.strict_order_source = strict_order_source
        self.rules = rules or TradingRules()
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self.current_run_id: str | None = None

    @property
    def connected(self) -> bool:
        """账本是否已连接。"""
        return self._connection is not None

    def connect(self) -> None:
        """打开账本并以幂等方式初始化数据库结构。"""
        with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.path,
                timeout=10,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA busy_timeout = 10000")
            self._connection = connection
            self._initialize_schema()

    def close(self) -> None:
        """关闭数据库连接。"""
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _require_connection(self) -> sqlite3.Connection:
        """返回活动连接，未连接时明确失败。"""
        if self._connection is None:
            raise RuntimeError("paper_v2 账本尚未连接")
        return self._connection

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """使用立即事务串行化账户写操作。"""
        with self._lock:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _initialize_schema(self) -> None:
        """创建单账本结构和初始账户，不覆盖已有余额。"""
        connection = self._require_connection()
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                initial_cash REAL NOT NULL,
                cash REAL NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS leases (
                account_id TEXT PRIMARY KEY REFERENCES accounts(account_id),
                holder_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                expires_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES accounts(account_id),
                strategy_version TEXT NOT NULL,
                code_commit TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                data_version TEXT NOT NULL,
                container_id TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                ended_at TEXT,
                status TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS signals (
                signal_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES accounts(account_id),
                run_id TEXT REFERENCES runs(run_id),
                signal_date TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                snapshot_hash TEXT NOT NULL,
                target_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                executed_at TEXT,
                UNIQUE(account_id, strategy_version, signal_date)
            );

            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                account_id TEXT NOT NULL REFERENCES accounts(account_id),
                run_id TEXT REFERENCES runs(run_id),
                signal_date TEXT,
                trade_date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                action TEXT NOT NULL,
                requested_price REAL NOT NULL,
                actual_price REAL NOT NULL,
                requested_shares INTEGER NOT NULL,
                filled_shares INTEGER NOT NULL,
                amount REAL NOT NULL,
                total_cost REAL NOT NULL,
                profit REAL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                strategy TEXT NOT NULL,
                strategy_tag TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                reason TEXT NOT NULL,
                source TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                filled_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_orders_account_date
                ON orders(account_id, trade_date);

            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL UNIQUE REFERENCES orders(order_id),
                account_id TEXT NOT NULL REFERENCES accounts(account_id),
                run_id TEXT REFERENCES runs(run_id),
                trade_date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                action TEXT NOT NULL,
                price REAL NOT NULL,
                actual_price REAL NOT NULL,
                shares INTEGER NOT NULL,
                amount REAL NOT NULL,
                gross_pnl REAL,
                net_pnl REAL,
                commission REAL NOT NULL,
                stamp_tax REAL NOT NULL,
                transfer_fee REAL NOT NULL,
                slippage REAL NOT NULL,
                total_cost REAL NOT NULL,
                strategy_version TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trades_account_date
                ON trades(account_id, trade_date);

            CREATE TABLE IF NOT EXISTS positions (
                account_id TEXT NOT NULL REFERENCES accounts(account_id),
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                shares INTEGER NOT NULL,
                avg_cost REAL NOT NULL,
                last_price REAL NOT NULL,
                strategy_version TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(account_id, code)
            );

            CREATE TABLE IF NOT EXISTS lots (
                lot_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES accounts(account_id),
                code TEXT NOT NULL,
                buy_date TEXT NOT NULL,
                original_shares INTEGER NOT NULL,
                remaining_shares INTEGER NOT NULL,
                cost_per_share REAL NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lots_sellable
                ON lots(account_id, code, buy_date, remaining_shares);

            CREATE TABLE IF NOT EXISTS nav_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES accounts(account_id),
                run_id TEXT,
                snapshot_date TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                cash REAL NOT NULL,
                market_value REAL NOT NULL,
                total_value REAL NOT NULL,
                gross_pnl REAL NOT NULL,
                net_pnl REAL NOT NULL,
                commission REAL NOT NULL,
                stamp_tax REAL NOT NULL,
                slippage REAL NOT NULL,
                turnover REAL NOT NULL,
                benchmark_return REAL,
                data_version TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                UNIQUE(account_id, run_id, snapshot_date)
            );
            CREATE INDEX IF NOT EXISTS idx_nav_account_date
                ON nav_snapshots(account_id, snapshot_date);
            """)
        now = format_local()
        with self._transaction() as transaction:
            transaction.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            transaction.execute(
                """
                INSERT OR IGNORE INTO accounts(account_id, initial_cash, cash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (self.account_id, self.initial_cash, self.initial_cash, now, now),
            )

    def start_run(
        self,
        *,
        strategy_version: str,
        code_commit: str,
        config_hash: str,
        data_version: str,
        container_id: str = "",
        run_id: str | None = None,
    ) -> LedgerRun:
        """记录运行会话及启动审计信息。"""
        identifier = run_id or f"RUN-{uuid.uuid4().hex}"
        started_at = format_local()
        with self._transaction() as connection:
            # 能成功走到这里的实例应已持有写租约；把崩溃遗留的 running 会话明确归档，
            # 避免日报和验收把一次重启误判为两个并发写实例。
            connection.execute(
                """
                UPDATE runs SET ended_at = ?, status = 'abandoned'
                WHERE account_id = ? AND status = 'running'
                """,
                (started_at, self.account_id),
            )
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, account_id, strategy_version, code_commit, config_hash,
                    data_version, container_id, started_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running')
                """,
                (
                    identifier,
                    self.account_id,
                    strategy_version,
                    code_commit,
                    config_hash,
                    data_version,
                    container_id,
                    started_at,
                ),
            )
        self.current_run_id = identifier
        return LedgerRun(
            run_id=identifier,
            account_id=self.account_id,
            strategy_version=strategy_version,
            code_commit=code_commit,
            config_hash=config_hash,
            data_version=data_version,
            started_at=started_at,
        )

    def finish_run(self, run_id: str, status: str = "completed") -> None:
        """结束运行会话。"""
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE runs SET ended_at = ?, status = ? WHERE run_id = ? AND account_id = ?",
                (format_local(), status, run_id, self.account_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"不存在运行批次: {run_id}")
        if self.current_run_id == run_id:
            self.current_run_id = None

    @staticmethod
    def _timestamp(now: datetime | None) -> tuple[datetime, float]:
        """返回租约计算使用的时间及秒时间戳。"""
        current = now or now_local()
        return current, current.timestamp()

    def acquire_lease(
        self,
        holder_id: str,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> None:
        """获取账户唯一写租约；其他活动实例存在时拒绝启动。"""
        if not holder_id.strip():
            raise ValueError("租约持有者标识不能为空")
        if ttl_seconds <= 0:
            raise ValueError("租约有效期必须大于 0")
        current, timestamp = self._timestamp(now)
        current_text = current.strftime("%Y-%m-%d %H:%M:%S")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT holder_id, expires_at FROM leases WHERE account_id = ?",
                (self.account_id,),
            ).fetchone()
            if (
                row is not None
                and row["holder_id"] != holder_id
                and float(row["expires_at"]) > timestamp
            ):
                raise LeaseUnavailableError(
                    f"账户 {self.account_id} 写租约由 {row['holder_id']} 持有"
                )
            connection.execute(
                """
                INSERT INTO leases(account_id, holder_id, acquired_at, heartbeat_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    holder_id = excluded.holder_id,
                    acquired_at = excluded.acquired_at,
                    heartbeat_at = excluded.heartbeat_at,
                    expires_at = excluded.expires_at
                """,
                (
                    self.account_id,
                    holder_id,
                    current_text,
                    current_text,
                    timestamp + ttl_seconds,
                ),
            )

    def heartbeat_lease(
        self,
        holder_id: str,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> None:
        """续租当前实例的账户写租约。"""
        current, timestamp = self._timestamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT holder_id, expires_at FROM leases WHERE account_id = ?",
                (self.account_id,),
            ).fetchone()
            if (
                row is None
                or row["holder_id"] != holder_id
                or float(row["expires_at"]) <= timestamp
            ):
                raise LeaseUnavailableError("当前实例不持有有效写租约")
            connection.execute(
                "UPDATE leases SET heartbeat_at = ?, expires_at = ? WHERE account_id = ?",
                (
                    current.strftime("%Y-%m-%d %H:%M:%S"),
                    timestamp + ttl_seconds,
                    self.account_id,
                ),
            )

    def release_lease(self, holder_id: str) -> bool:
        """仅由持有者释放写租约。"""
        with self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM leases WHERE account_id = ? AND holder_id = ?",
                (self.account_id, holder_id),
            )
            return cursor.rowcount == 1

    def record_signal(self, target: TargetPortfolio, run_id: str | None = None) -> str:
        """幂等记录收盘目标组合；同日不同目标直接失败。"""
        if target.account_id != self.account_id:
            raise ValueError(
                f"目标组合账户与账本不一致: {target.account_id} != {self.account_id}"
            )
        payload = json.dumps(
            target.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        effective_run_id = run_id or self.current_run_id
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT signal_id, target_json FROM signals
                WHERE account_id = ? AND strategy_version = ? AND signal_date = ?
                """,
                (self.account_id, target.strategy_version, target.signal_date),
            ).fetchone()
            if existing is not None:
                existing_payload = json.loads(str(existing["target_json"]))
                new_payload = json.loads(payload)
                existing_payload.pop("generated_at", None)
                new_payload.pop("generated_at", None)
                if existing_payload != new_payload:
                    raise ValueError("同一交易日已存在不同目标组合，拒绝覆盖审计证据")
                return str(existing["signal_id"])
            signal_id = f"SIG-{uuid.uuid4().hex}"
            connection.execute(
                """
                INSERT INTO signals(
                    signal_id, account_id, run_id, signal_date, strategy_version,
                    snapshot_hash, target_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id,
                    self.account_id,
                    effective_run_id,
                    target.signal_date,
                    target.strategy_version,
                    target.source_snapshot_hash,
                    payload,
                    format_local(),
                ),
            )
            return signal_id

    def latest_signal_date(self, strategy_version: str | None = None) -> str | None:
        """查询账户最近一次收盘目标日期。"""
        connection = self._require_connection()
        if strategy_version:
            row = connection.execute(
                """
                SELECT MAX(signal_date) AS signal_date FROM signals
                WHERE account_id = ? AND strategy_version = ?
                """,
                (self.account_id, strategy_version),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT MAX(signal_date) AS signal_date FROM signals WHERE account_id = ?",
                (self.account_id,),
            ).fetchone()
        if row is None or row["signal_date"] is None:
            return None
        return str(row["signal_date"])

    def latest_signal_hash(self, strategy_version: str | None = None) -> str | None:
        """查询最近目标组合使用的数据版本哈希。"""
        connection = self._require_connection()
        clauses = ["account_id = ?"]
        params: list[Any] = [self.account_id]
        if strategy_version:
            clauses.append("strategy_version = ?")
            params.append(strategy_version)
        row = connection.execute(
            f"""
            SELECT snapshot_hash FROM signals
            WHERE {' AND '.join(clauses)}
            ORDER BY signal_date DESC LIMIT 1
            """,
            params,
        ).fetchone()
        return str(row["snapshot_hash"]) if row is not None else None

    @staticmethod
    def _target_from_json(payload: str) -> TargetPortfolio:
        """从账本 JSON 恢复目标组合。"""
        data = json.loads(payload)
        positions = tuple(
            TargetPosition(**position) for position in data.pop("positions")
        )
        return TargetPortfolio(positions=positions, **data)

    def pending_target(self, execution_date: str) -> tuple[str, TargetPortfolio] | None:
        """返回可在指定日期执行的最新未执行目标组合。"""
        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT signal_id, target_json FROM signals
            WHERE account_id = ? AND signal_date < ? AND executed_at IS NULL
            ORDER BY signal_date DESC LIMIT 1
            """,
            (self.account_id, execution_date),
        ).fetchone()
        if row is None:
            return None
        return str(row["signal_id"]), self._target_from_json(str(row["target_json"]))

    def mark_signal_executed(self, signal_id: str) -> None:
        """标记目标组合及其更旧未执行目标均已处理，防止跨日追旧单。"""
        with self._transaction() as connection:
            signal = connection.execute(
                "SELECT signal_date, strategy_version FROM signals WHERE signal_id = ? AND account_id = ?",
                (signal_id, self.account_id),
            ).fetchone()
            if signal is None:
                raise ValueError(f"不存在信号: {signal_id}")
            cursor = connection.execute(
                """
                UPDATE signals SET executed_at = ?
                WHERE account_id = ? AND strategy_version = ?
                  AND signal_date <= ? AND executed_at IS NULL
                """,
                (
                    format_local(),
                    self.account_id,
                    signal["strategy_version"],
                    signal["signal_date"],
                ),
            )
            if cursor.rowcount < 0:
                raise RuntimeError(f"信号状态更新异常: {signal_id}")

    def query_cash(self) -> float:
        """查询可用现金。"""
        connection = self._require_connection()
        row = connection.execute(
            "SELECT cash FROM accounts WHERE account_id = ?",
            (self.account_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"账户不存在: {self.account_id}")
        return float(row["cash"])

    def query_positions(
        self, as_of_date: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """查询持仓，并按买入批次计算指定日期可卖数量。"""
        date = as_of_date or today_yyyymmdd()
        connection = self._require_connection()
        rows = connection.execute(
            "SELECT * FROM positions WHERE account_id = ? ORDER BY code",
            (self.account_id,),
        ).fetchall()
        positions: dict[str, dict[str, Any]] = {}
        for row in rows:
            lot = connection.execute(
                """
                SELECT MIN(buy_date) AS buy_date,
                       COALESCE(SUM(CASE WHEN buy_date < ? THEN remaining_shares ELSE 0 END), 0) AS sellable
                FROM lots
                WHERE account_id = ? AND code = ? AND remaining_shares > 0
                """,
                (date, self.account_id, row["code"]),
            ).fetchone()
            positions[str(row["code"])] = {
                "code": str(row["code"]),
                "name": str(row["name"]),
                "shares": int(row["shares"]),
                "total_qty": int(row["shares"]),
                "sellable_qty": int(lot["sellable"] if lot is not None else 0),
                "avg_cost": float(row["avg_cost"]),
                "current_price": float(row["last_price"]),
                "buy_date": str(
                    lot["buy_date"] if lot is not None and lot["buy_date"] else ""
                ),
                "strategy": "A股稳健策略V2",
                "strategy_tag": "robust_v2",
                "strategy_version": str(row["strategy_version"]),
            }
        return positions

    def _existing_order(
        self, connection: sqlite3.Connection, key: str
    ) -> ExecutionReport | None:
        """查询幂等键对应的既有回报。"""
        row = connection.execute(
            "SELECT * FROM orders WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return self._report_from_row(row, replay=True)

    @staticmethod
    def _report_from_row(row: sqlite3.Row, *, replay: bool = False) -> ExecutionReport:
        """从订单行恢复标准成交回报。"""
        raw = json.loads(str(row["raw_json"])) if row["raw_json"] else {}
        if replay:
            raw = {**raw, "idempotent_replay": True}
        return ExecutionReport(
            order_id=str(row["order_id"]),
            status=row["status"],
            code=str(row["code"]),
            action=row["action"],
            price=float(row["requested_price"]),
            actual_price=float(row["actual_price"]),
            shares=int(row["filled_shares"] or row["requested_shares"]),
            amount=float(row["amount"]),
            cost=float(row["total_cost"]),
            strategy=str(row["strategy"]),
            strategy_tag=cast(StrategyTag, row["strategy_tag"]),
            sell_reason=str(row["reason"]) if row["action"] == "sell" else "",
            message=str(row["message"]),
            profit=float(row["profit"]) if row["profit"] is not None else None,
            date=str(row["trade_date"]),
            timestamp=str(row["filled_at"] or row["created_at"]),
            raw=raw,
        )

    def _insert_order(
        self,
        connection: sqlite3.Connection,
        order: OrderIntent,
        *,
        order_id: str,
        status: str,
        message: str,
        actual_price: float = 0.0,
        filled_shares: int = 0,
        amount: float = 0.0,
        total_cost: float = 0.0,
        profit: float | None = None,
        raw: Mapping[str, Any] | None = None,
    ) -> ExecutionReport:
        """写入订单并返回标准回报。"""
        trade_date = order.date or today_yyyymmdd()
        filled_at = format_local() if status == "filled" else None
        connection.execute(
            """
            INSERT INTO orders(
                order_id, idempotency_key, account_id, run_id, signal_date, trade_date,
                code, name, action, requested_price, actual_price, requested_shares,
                filled_shares, amount, total_cost, profit, status, message, strategy,
                strategy_tag, strategy_version, reason, source, raw_json, created_at, filled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                order.idempotency_key,
                self.account_id,
                self.current_run_id,
                order.signal_date,
                trade_date,
                order.code,
                order.name,
                order.action,
                order.price,
                actual_price,
                order.shares,
                filled_shares,
                round(amount, 2),
                round(total_cost, 2),
                round(profit, 2) if profit is not None else None,
                status,
                message,
                order.strategy,
                order.strategy_tag,
                order.strategy_version,
                order.reason,
                order.source,
                json.dumps(
                    {
                        "order_metadata": copy.deepcopy(order.metadata),
                        **dict(raw or {}),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                order.created_at,
                filled_at,
            ),
        )
        row = connection.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("订单写入后无法读取")
        return self._report_from_row(row)

    def _reject(
        self,
        connection: sqlite3.Connection,
        order: OrderIntent,
        order_id: str,
        message: str,
    ) -> ExecutionReport:
        """持久化拒单，确保重启后同一意图仍保持幂等。"""
        return self._insert_order(
            connection,
            order,
            order_id=order_id,
            status="rejected",
            message=message,
            raw={"requested_shares": order.shares},
        )

    def place_order(self, order: OrderIntent) -> ExecutionReport:
        """在单个事务内撮合订单并更新现金、批次持仓和成交。"""
        order_id = f"PAPER-V2-{uuid.uuid4().hex[:16]}"
        with self._transaction() as connection:
            existing = self._existing_order(connection, order.idempotency_key)
            if existing is not None:
                return existing
            if order.account_id != self.account_id:
                return self._reject(
                    connection,
                    order,
                    order_id,
                    f"订单账户与账本不一致: {order.account_id} != {self.account_id}",
                )
            if self.strict_order_source and order.strategy_version == "robust_v2":
                allowed_sources = {"target_allocator", "robust_v2_monitor"}
                if order.source not in allowed_sources:
                    return self._reject(
                        connection,
                        order,
                        order_id,
                        "robust_v2 订单必须来自统一目标分配器或灾难止损监控",
                    )
                if order.source == "robust_v2_monitor" and order.action != "sell":
                    return self._reject(
                        connection,
                        order,
                        order_id,
                        "灾难止损监控只能提交卖单",
                    )
            if order.price <= 0 or order.shares <= 0:
                return self._reject(
                    connection, order, order_id, "委托价格和数量必须大于 0"
                )
            trade_date = order.date or today_yyyymmdd()
            if len(trade_date) != 8 or not trade_date.isdigit():
                return self._reject(
                    connection, order, order_id, f"无效交易日期: {trade_date}"
                )
            try:
                profile = get_instrument_profile(order.code, name=order.name)
            except ValueError as exc:
                return self._reject(connection, order, order_id, str(exc))
            if order.action == "buy" and order.shares % profile.lot_size != 0:
                return self._reject(
                    connection, order, order_id, "买入数量必须为 100 的整数倍"
                )
            if order.action == "buy" and not is_supported_trading_target(order.code):
                reason = get_trading_permission_rejection_reason(order.code)
                return self._reject(
                    connection, order, order_id, reason or "当前账户无该标的买入权限"
                )
            if order.action == "buy":
                return self._execute_buy(connection, order, order_id, trade_date)
            return self._execute_sell(connection, order, order_id, trade_date)

    def _execute_buy(
        self,
        connection: sqlite3.Connection,
        order: OrderIntent,
        order_id: str,
        trade_date: str,
    ) -> ExecutionReport:
        """成交买单并新增不可当日卖出的持仓批次。"""
        amount = order.price * order.shares
        costs = self.rules.calc_total_cost(amount, "buy", code=order.code)
        debit = amount + float(costs["total"])
        account = connection.execute(
            "SELECT cash FROM accounts WHERE account_id = ?",
            (self.account_id,),
        ).fetchone()
        cash = float(account["cash"]) if account is not None else 0.0
        if debit > cash + 1e-9:
            return self._reject(connection, order, order_id, "可用现金不足")

        actual_price = debit / order.shares
        existing = connection.execute(
            "SELECT shares, avg_cost FROM positions WHERE account_id = ? AND code = ?",
            (self.account_id, order.code),
        ).fetchone()
        old_shares = int(existing["shares"]) if existing is not None else 0
        old_cost = (
            float(existing["avg_cost"]) * old_shares if existing is not None else 0.0
        )
        total_shares = old_shares + order.shares
        average_cost = (old_cost + debit) / total_shares
        now = format_local()
        connection.execute(
            """
            INSERT INTO positions(
                account_id, code, name, shares, avg_cost, last_price, strategy_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, code) DO UPDATE SET
                name = excluded.name,
                shares = excluded.shares,
                avg_cost = excluded.avg_cost,
                last_price = excluded.last_price,
                strategy_version = excluded.strategy_version,
                updated_at = excluded.updated_at
            """,
            (
                self.account_id,
                order.code,
                order.name or order.code,
                total_shares,
                round(average_cost, 6),
                order.price,
                order.strategy_version,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO lots(
                lot_id, account_id, code, buy_date, original_shares,
                remaining_shares, cost_per_share, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"LOT-{uuid.uuid4().hex}",
                self.account_id,
                order.code,
                trade_date,
                order.shares,
                order.shares,
                round(actual_price, 8),
                now,
            ),
        )
        connection.execute(
            "UPDATE accounts SET cash = ?, updated_at = ? WHERE account_id = ?",
            (round(cash - debit, 2), now, self.account_id),
        )
        report = self._insert_order(
            connection,
            order,
            order_id=order_id,
            status="filled",
            message="paper_v2 虚拟盘成交",
            actual_price=actual_price,
            filled_shares=order.shares,
            amount=amount,
            total_cost=float(costs["total"]),
            raw={"cost_detail": costs, "debit": round(debit, 2)},
        )
        self._insert_trade(
            connection,
            report,
            order,
            costs,
            gross_pnl=None,
            net_pnl=None,
        )
        return report

    def _execute_sell(
        self,
        connection: sqlite3.Connection,
        order: OrderIntent,
        order_id: str,
        trade_date: str,
    ) -> ExecutionReport:
        """按 FIFO 消耗 T+1 可卖批次并成交卖单。"""
        position = connection.execute(
            "SELECT * FROM positions WHERE account_id = ? AND code = ?",
            (self.account_id, order.code),
        ).fetchone()
        if position is None:
            return self._reject(connection, order, order_id, "无持仓")
        lots = connection.execute(
            """
            SELECT * FROM lots
            WHERE account_id = ? AND code = ? AND remaining_shares > 0 AND buy_date < ?
            ORDER BY buy_date, created_at, lot_id
            """,
            (self.account_id, order.code, trade_date),
        ).fetchall()
        sellable = sum(int(lot["remaining_shares"]) for lot in lots)
        shares = min(order.shares, sellable, int(position["shares"]))
        if shares <= 0:
            return self._reject(
                connection,
                order,
                order_id,
                "T+1 locked, signal recorded but cannot sell",
            )

        remaining = shares
        cost_basis = 0.0
        for lot in lots:
            take = min(remaining, int(lot["remaining_shares"]))
            if take <= 0:
                continue
            cost_basis += take * float(lot["cost_per_share"])
            connection.execute(
                "UPDATE lots SET remaining_shares = remaining_shares - ? WHERE lot_id = ?",
                (take, lot["lot_id"]),
            )
            remaining -= take
            if remaining == 0:
                break
        if remaining != 0:
            raise RuntimeError("可卖批次消耗不完整，事务已回滚")

        amount = order.price * shares
        costs = self.rules.calc_total_cost(amount, "sell", code=order.code)
        proceeds = amount - float(costs["total"])
        actual_price = proceeds / shares
        gross_pnl = amount - cost_basis
        net_pnl = proceeds - cost_basis
        account = connection.execute(
            "SELECT cash FROM accounts WHERE account_id = ?",
            (self.account_id,),
        ).fetchone()
        cash = float(account["cash"]) if account is not None else 0.0
        now = format_local()
        new_shares = int(position["shares"]) - shares
        if new_shares <= 0:
            connection.execute(
                "DELETE FROM positions WHERE account_id = ? AND code = ?",
                (self.account_id, order.code),
            )
        else:
            remaining_lots = connection.execute(
                """
                SELECT COALESCE(SUM(remaining_shares), 0) AS shares,
                       COALESCE(SUM(remaining_shares * cost_per_share), 0) AS cost
                FROM lots WHERE account_id = ? AND code = ? AND remaining_shares > 0
                """,
                (self.account_id, order.code),
            ).fetchone()
            lot_shares = int(
                remaining_lots["shares"] if remaining_lots is not None else 0
            )
            if lot_shares != new_shares:
                raise RuntimeError("持仓总数与剩余批次数不一致，事务已回滚")
            average_cost = float(remaining_lots["cost"]) / lot_shares
            connection.execute(
                """
                UPDATE positions SET shares = ?, avg_cost = ?, last_price = ?, updated_at = ?
                WHERE account_id = ? AND code = ?
                """,
                (
                    new_shares,
                    round(average_cost, 6),
                    order.price,
                    now,
                    self.account_id,
                    order.code,
                ),
            )
        connection.execute(
            "UPDATE accounts SET cash = ?, updated_at = ? WHERE account_id = ?",
            (round(cash + proceeds, 2), now, self.account_id),
        )
        raw = {
            "cost_detail": costs,
            "requested_shares": order.shares,
            "filled_shares": shares,
            "partial_t1_fill": shares < order.shares,
            "cost_basis": round(cost_basis, 2),
        }
        report = self._insert_order(
            connection,
            order,
            order_id=order_id,
            status="filled",
            message=(
                "paper_v2 虚拟盘成交"
                if shares == order.shares
                else "按 T+1 可卖批次部分成交"
            ),
            actual_price=actual_price,
            filled_shares=shares,
            amount=amount,
            total_cost=float(costs["total"]),
            profit=net_pnl,
            raw=raw,
        )
        self._insert_trade(
            connection,
            report,
            order,
            costs,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
        )
        return report

    def _insert_trade(
        self,
        connection: sqlite3.Connection,
        report: ExecutionReport,
        order: OrderIntent,
        costs: Mapping[str, Any],
        *,
        gross_pnl: float | None,
        net_pnl: float | None,
    ) -> None:
        """写入不可重复的成交明细。"""
        connection.execute(
            """
            INSERT INTO trades(
                trade_id, order_id, account_id, run_id, trade_date, code, name,
                action, price, actual_price, shares, amount, gross_pnl, net_pnl,
                commission, stamp_tax, transfer_fee, slippage, total_cost,
                strategy_version, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"TRD-{uuid.uuid4().hex}",
                report.order_id,
                self.account_id,
                self.current_run_id,
                report.date,
                report.code,
                order.name or report.code,
                report.action,
                report.price,
                report.actual_price,
                report.shares,
                report.amount,
                round(gross_pnl, 2) if gross_pnl is not None else None,
                round(net_pnl, 2) if net_pnl is not None else None,
                float(costs["commission"]),
                float(costs["stamp_tax"]),
                float(costs["transfer_fee"]),
                float(costs["slippage"]),
                float(costs["total"]),
                order.strategy_version,
                order.reason,
                report.timestamp,
            ),
        )

    def query_orders(self) -> list[ExecutionReport]:
        """按创建顺序查询全部订单回报。"""
        connection = self._require_connection()
        rows = connection.execute(
            "SELECT * FROM orders WHERE account_id = ? ORDER BY rowid",
            (self.account_id,),
        ).fetchall()
        return [self._report_from_row(row) for row in rows]

    def query_trades(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """查询指定账户、日期和可选运行批次的成交。"""
        clauses = ["account_id = ?"]
        params: list[Any] = [self.account_id]
        if start_date:
            clauses.append("trade_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("trade_date <= ?")
            params.append(end_date)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        connection = self._require_connection()
        rows = connection.execute(
            f"SELECT * FROM trades WHERE {' AND '.join(clauses)} ORDER BY trade_date, created_at, rowid",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def query_snapshot(
        self, prices: Mapping[str, float] | None = None
    ) -> PortfolioSnapshot:
        """按最新价格计算账户快照，不修改账本。"""
        positions = self.query_positions()
        current_prices = prices or {}
        items: list[dict[str, Any]] = []
        market_value = 0.0
        for code, position in positions.items():
            price = float(
                current_prices.get(code, position["current_price"])
                or position["current_price"]
            )
            value = price * int(position["shares"])
            profit = (price - float(position["avg_cost"])) * int(position["shares"])
            market_value += value
            items.append(
                {
                    **copy.deepcopy(position),
                    "current_price": price,
                    "market_value": round(value, 2),
                    "profit": round(profit, 2),
                    "profit_pct": (
                        round(price / float(position["avg_cost"]) - 1, 6)
                        if float(position["avg_cost"]) > 0
                        else 0.0
                    ),
                }
            )
        cash = self.query_cash()
        total_value = cash + market_value
        connection = self._require_connection()
        peak = connection.execute(
            "SELECT MAX(total_value) AS peak FROM nav_snapshots WHERE account_id = ?",
            (self.account_id,),
        ).fetchone()
        historical_peak = (
            float(peak["peak"] or self.initial_cash)
            if peak is not None
            else self.initial_cash
        )
        running_peak = max(historical_peak, total_value)
        drawdown = (
            (running_peak - total_value) / running_peak if running_peak > 0 else 0.0
        )
        return PortfolioSnapshot(
            cash=round(cash, 2),
            total_value=round(total_value, 2),
            position_ratio=(
                round(market_value / total_value, 6) if total_value > 0 else 0.0
            ),
            position_count=len(items),
            positions=items,
            drawdown=round(drawdown, 6),
            pnl=round(total_value - self.initial_cash, 2),
            pnl_pct=round(total_value / self.initial_cash - 1, 6),
            source="paper_v2",
        )

    def record_snapshot(
        self,
        prices: Mapping[str, float] | None,
        *,
        snapshot_date: str,
        data_version: str,
        strategy_version: str,
        benchmark_return: float | None = None,
        run_id: str | None = None,
    ) -> PortfolioSnapshot:
        """写入每日唯一净值点及成本、换手和基准口径。"""
        snapshot = self.query_snapshot(prices)
        effective_run_id = run_id or self.current_run_id or ""
        connection = self._require_connection()
        totals = connection.execute(
            """
            SELECT COALESCE(SUM(total_cost), 0) AS costs,
                   COALESCE(SUM(commission), 0) AS commission,
                   COALESCE(SUM(stamp_tax), 0) AS stamp_tax,
                   COALESCE(SUM(slippage), 0) AS slippage,
                   COALESCE(SUM(amount), 0) AS turnover
            FROM trades WHERE account_id = ? AND (? = '' OR run_id = ?)
            """,
            (self.account_id, effective_run_id, effective_run_id),
        ).fetchone()
        total_cost = float(totals["costs"] if totals is not None else 0.0)
        net_pnl = snapshot.total_value - self.initial_cash
        gross_pnl = net_pnl + total_cost
        market_value = snapshot.total_value - snapshot.cash
        captured_at = format_local()
        with self._transaction() as transaction:
            transaction.execute(
                """
                INSERT INTO nav_snapshots(
                    snapshot_id, account_id, run_id, snapshot_date, captured_at,
                    cash, market_value, total_value, gross_pnl, net_pnl, commission,
                    stamp_tax, slippage, turnover, benchmark_return, data_version,
                    strategy_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, run_id, snapshot_date) DO UPDATE SET
                    captured_at = excluded.captured_at,
                    cash = excluded.cash,
                    market_value = excluded.market_value,
                    total_value = excluded.total_value,
                    gross_pnl = excluded.gross_pnl,
                    net_pnl = excluded.net_pnl,
                    commission = excluded.commission,
                    stamp_tax = excluded.stamp_tax,
                    slippage = excluded.slippage,
                    turnover = excluded.turnover,
                    benchmark_return = excluded.benchmark_return,
                    data_version = excluded.data_version,
                    strategy_version = excluded.strategy_version
                """,
                (
                    f"NAV-{uuid.uuid4().hex}",
                    self.account_id,
                    effective_run_id,
                    snapshot_date,
                    captured_at,
                    snapshot.cash,
                    market_value,
                    snapshot.total_value,
                    round(gross_pnl, 2),
                    round(net_pnl, 2),
                    float(totals["commission"] if totals is not None else 0.0),
                    float(totals["stamp_tax"] if totals is not None else 0.0),
                    float(totals["slippage"] if totals is not None else 0.0),
                    float(totals["turnover"] if totals is not None else 0.0),
                    benchmark_return,
                    data_version,
                    strategy_version,
                ),
            )
        return snapshot

    def build_review(
        self,
        start_date: str,
        end_date: str,
        *,
        run_id: str | None = None,
    ) -> LedgerReview:
        """只读取同一 account_id/run_id 的显式日期复盘数据。"""
        if start_date > end_date:
            raise ValueError("复盘开始日期不能晚于结束日期")
        clauses = ["account_id = ?", "snapshot_date >= ?", "snapshot_date <= ?"]
        params: list[Any] = [self.account_id, start_date, end_date]
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        connection = self._require_connection()
        rows = connection.execute(
            f"SELECT * FROM nav_snapshots WHERE {' AND '.join(clauses)} ORDER BY snapshot_date",
            params,
        ).fetchall()
        return LedgerReview(
            account_id=self.account_id,
            run_id=run_id,
            start_date=start_date,
            end_date=end_date,
            snapshots=tuple(dict(row) for row in rows),
            trades=tuple(self.query_trades(start_date, end_date, run_id)),
        )

    def cancel_order(self, order_id: str) -> bool:
        """paper_v2 订单同步成交或拒绝，不支持撤单。"""
        return False
