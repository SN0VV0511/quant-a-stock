"""robust_v2 唯一虚拟盘运行入口。

运行职责严格分离：收盘后生成目标组合，下一交易日 09:35 后统一分配并执行；盘中
只做行情健康检查和灾难止损，不再每 4 秒生成普通技术买卖。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import date as date_type
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from config.logging_setup import setup_logger
from config.settings import (
    ALLOW_CHINEXT_STOCKS,
    ALLOW_MAIN_BOARD_STOCKS,
    ALLOW_STAR_MARKET_STOCKS,
    DEFAULT_RPS_ETF_POOL,
    ENFORCE_T1,
    INITIAL_CAPITAL,
    REPORT_DIR,
    ROBUST_V2_ACCOUNT_ID,
    ROBUST_V2_LEASE_TTL_SECONDS,
    ROBUST_V2_LEDGER_PATH,
    ROBUST_V2_MONITOR_INTERVAL_SECONDS,
    ROBUST_V2_SELECTED_CONFIG_PATH,
    ROBUST_V2_STOCK_CANDIDATE_LIMIT,
    ROBUST_V2_STRATEGY_VERSION,
    get_stock_board,
    is_etf,
    is_supported_trading_target,
)
from config.time_utils import now_local, today_yyyymmdd
from data.ak_loader import AKDataLoader
from data.holidays import is_trading_day as calendar_is_trading_day
from reports.ledger_report import build_daily_ledger_report, save_daily_ledger_report
from strategies.robust_v2 import (
    RobustV2Config,
    RobustV2Strategy,
    build_market_data_hash,
    validate_realtime_alignment,
)
from trading.allocator import AllocationResult, PortfolioAllocator
from trading.brokers import SQLitePaperBrokerAdapter
from trading.models import ExecutionReport, MarketSnapshot, TargetPortfolio

LOGGER = logging.getLogger("robust_runner")
ROOT_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class SignalData:
    """一次收盘选仓使用的版本化行情。"""

    snapshot: MarketSnapshot
    etf_history: dict[str, pd.DataFrame]
    stock_history: dict[str, pd.DataFrame]
    names: dict[str, str]
    quotes: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class ExecutionOutcome:
    """一次目标执行结果。"""

    signal_id: str | None
    target: TargetPortfolio | None
    allocation: AllocationResult | None
    reports: tuple[ExecutionReport, ...]
    message: str


def _latest_dates(history_map: Mapping[str, pd.DataFrame], cutoff: str) -> list[str]:
    """提取不晚于截止日的全部行情日期。"""
    dates: set[str] = set()
    for frame in history_map.values():
        if frame is None or frame.empty or "date" not in frame.columns:
            continue
        normalized = frame["date"].astype(str).str.replace("-", "", regex=False).str[:8]
        dates.update(
            value for value in normalized if len(value) == 8 and value <= cutoff
        )
    return sorted(dates)


def _previous_date(
    history_map: Mapping[str, pd.DataFrame], trade_date: str
) -> str | None:
    """从版本化行情中确定上一交易日。"""
    dates = _latest_dates(history_map, trade_date)
    earlier = [value for value in dates if value < trade_date]
    return earlier[-1] if earlier else None


def _freshness_seconds(latest_date: str | None, expected_date: str) -> int:
    """用行情日期与预期交易日差计算保守新鲜度。"""
    if latest_date is None:
        return 10**9
    latest = datetime.strptime(latest_date, "%Y%m%d")
    expected = datetime.strptime(expected_date, "%Y%m%d")
    return max(0, int((expected - latest).total_seconds()))


def _safe_git_commit(root_dir: Path) -> str:
    """读取当前提交号，失败时返回可审计占位值。"""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _config_hash(config: RobustV2Config) -> str:
    """生成运行参数和账户权限的稳定哈希。"""
    payload = {
        "strategy": asdict(config),
        "permissions": {
            "mainboard": ALLOW_MAIN_BOARD_STOCKS,
            "chinext": ALLOW_CHINEXT_STOCKS,
            "star": ALLOW_STAR_MARKET_STOCKS,
        },
        "enforce_t1": ENFORCE_T1,
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def validate_startup(config: RobustV2Config) -> None:
    """校验生产运行前不可妥协的安全约束。"""
    if not ENFORCE_T1:
        raise RuntimeError("robust_v2 检测到 ENFORCE_T1=false，拒绝启动")
    if config.account_id != ROBUST_V2_ACCOUNT_ID:
        raise RuntimeError("robust_v2 当前只允许写入 paper_v2 独立账户")
    if not ALLOW_MAIN_BOARD_STOCKS:
        LOGGER.warning("主板股票权限关闭，策略将自动退化为 ETF + 现金")
    if ALLOW_CHINEXT_STOCKS or ALLOW_STAR_MARKET_STOCKS:
        LOGGER.warning(
            "账户声明有双创权限，但 robust_v2 仍不会直接买入 300/301/688 股票"
        )
    unsupported = [
        code for code in DEFAULT_RPS_ETF_POOL if not is_supported_trading_target(code)
    ]
    if unsupported:
        raise RuntimeError(f"ETF 能力预检失败: {unsupported}")


def load_runtime_config(
    path: str | Path = ROBUST_V2_SELECTED_CONFIG_PATH,
) -> RobustV2Config:
    """读取滚动样本外选择结果；文件缺失时使用保守默认值。"""
    config = RobustV2Config()
    selected_path = Path(path).expanduser()
    if not selected_path.exists():
        LOGGER.warning(
            "未找到样本外选择配置，使用 robust_v2 推荐默认值: %s", selected_path
        )
        return config
    try:
        payload = json.loads(selected_path.read_text(encoding="utf-8"))
        selected = payload.get("selected_params", {})
        if not isinstance(selected, dict):
            raise ValueError("selected_params 必须是对象")
        allowed = {
            "enable_etf_trend_filter",
            "stock_reversal_days",
            "rebalance_days",
            "stock_stop_pct",
            "max_total_position",
        }
        overrides = {key: value for key, value in selected.items() if key in allowed}
        overrides["enable_stock_enhancement"] = not bool(
            payload.get("fallback_to_etf", False)
        )
        loaded = replace(config, **overrides)
        LOGGER.info("已加载样本外选择配置: %s", selected_path)
        return loaded
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError(f"读取 robust_v2 选择配置失败: {selected_path}") from exc


class RobustDataService:
    """为 robust_v2 装载并版本化收盘行情和可交易股票池。"""

    def __init__(
        self,
        loader: AKDataLoader,
        root_dir: Path = ROOT_DIR,
        stock_candidate_limit: int = ROBUST_V2_STOCK_CANDIDATE_LIMIT,
        max_data_age_seconds: int = 86_400,
    ) -> None:
        if stock_candidate_limit <= 0:
            raise ValueError("股票候选数量必须大于 0")
        self.loader = loader
        self.root_dir = root_dir.resolve()
        self.stock_candidate_limit = stock_candidate_limit
        self.max_data_age_seconds = max_data_age_seconds

    def _stock_candidates(
        self, account_value: float
    ) -> tuple[list[str], dict[str, str], dict[str, dict[str, Any]]]:
        """用实时流动性做 IO 粗筛，因子筛选仍使用 T 日收盘历史。"""
        stocks = [
            stock
            for stock in self.loader.get_all_stocks()
            if get_stock_board(stock["code"]) == "mainboard"
        ]
        names = {stock["code"]: stock.get("name", stock["code"]) for stock in stocks}
        codes = [stock["code"] for stock in stocks]
        quotes = self.loader.get_realtime_quotes(codes) if codes else {}
        ranked: list[tuple[float, str]] = []
        for code, quote in quotes.items():
            name = str(quote.get("name") or names.get(code, code))
            price = float(quote.get("price", 0) or 0)
            volume = float(quote.get("volume", 0) or 0)
            if "ST" in name.upper() or "退" in name:
                continue
            if not 3 <= price <= 80 or price * 100 > account_value * 0.2:
                continue
            amount_proxy = price * volume
            if amount_proxy < 100_000_000:
                continue
            ranked.append((amount_proxy, code))
            names[code] = name
        ranked.sort(reverse=True)
        selected = [code for _, code in ranked[: self.stock_candidate_limit]]
        selected_quotes = {code: quotes[code] for code in selected if code in quotes}
        return selected, names, selected_quotes

    def _persist_universe(
        self,
        snapshot: MarketSnapshot,
        stock_history: Mapping[str, pd.DataFrame],
        names: Mapping[str, str],
    ) -> None:
        """保存当日可交易池和财务字段版本，供后续无幸存者偏差回放。"""
        rows: list[dict[str, Any]] = []
        for code, frame in sorted(stock_history.items()):
            if frame is None or frame.empty:
                continue
            data = frame.copy()
            if "date" not in data.columns:
                continue
            normalized = (
                data["date"].astype(str).str.replace("-", "", regex=False).str[:8]
            )
            data = data.loc[normalized <= snapshot.trade_date]
            if data.empty:
                continue
            last = data.iloc[-1]
            rows.append(
                {
                    "code": code,
                    "name": names.get(code, code),
                    "date": str(last.get("date", snapshot.trade_date)).replace("-", "")[
                        :8
                    ],
                    "pb": _json_number(last.get("pb", last.get("pbMRQ"))),
                    "pe_ttm": _json_number(last.get("peTTM")),
                    "market_cap": _json_number(last.get("mktcap")),
                    "trade_status": _json_number(last.get("tradestatus")),
                }
            )
        payload = {
            "snapshot": snapshot.to_dict(),
            "strategy_version": ROBUST_V2_STRATEGY_VERSION,
            "stocks": rows,
        }
        target_dir = self.root_dir / "data" / "universe"
        target_dir.mkdir(parents=True, exist_ok=True)
        destination = target_dir / f"robust_v2_{snapshot.trade_date}.json"
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target_dir, delete=False
        ) as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            temporary = Path(stream.name)
        temporary.replace(destination)

    def load_signal_data(self, trade_date: str, account_value: float) -> SignalData:
        """加载收盘选仓数据并生成可审计快照。"""
        etf_codes = list(DEFAULT_RPS_ETF_POOL)
        etf_history = self.loader.get_batch_etf_history(
            etf_codes, days=420, adjust="qfq"
        )
        stock_codes, names, quotes = self._stock_candidates(account_value)
        stock_history = self.loader.get_batch_history_ext(
            stock_codes,
            days=420,
            max_batch=self.stock_candidate_limit,
        )
        all_history = {**etf_history, **stock_history}
        dates = _latest_dates(all_history, trade_date)
        latest_date = dates[-1] if dates else None
        data_hash = build_market_data_hash(all_history, trade_date)
        snapshot = MarketSnapshot(
            trade_date=trade_date,
            previous_trade_date=_previous_date(all_history, trade_date),
            source="AKShare+BaoStock+Tencent",
            adjustment="qfq",
            data_hash=data_hash,
            freshness_seconds=_freshness_seconds(latest_date, trade_date),
            max_freshness_seconds=self.max_data_age_seconds,
            metadata={
                "etf_count": len(etf_history),
                "stock_count": len(stock_history),
                "latest_history_date": latest_date,
            },
        )
        self._persist_universe(snapshot, stock_history, names)
        return SignalData(
            snapshot=snapshot,
            etf_history=etf_history,
            stock_history=stock_history,
            names=names,
            quotes=quotes,
        )

    def load_execution_data(
        self,
        codes: set[str],
        signal_date: str,
    ) -> tuple[MarketSnapshot, dict[str, pd.DataFrame], dict[str, dict[str, Any]]]:
        """加载目标标的实时价，并校验与信号日复权历史的前收盘一致性。"""
        etf_codes = [code for code in codes if is_etf(code)]
        stock_codes = [code for code in codes if not is_etf(code)]
        history: dict[str, pd.DataFrame] = {}
        history.update(
            self.loader.get_batch_etf_history(etf_codes, days=420, adjust="qfq")
        )
        history.update(
            self.loader.get_batch_history_ext(
                stock_codes, days=420, max_batch=len(stock_codes) or 1
            )
        )
        quotes = self.loader.get_realtime_quotes(list(codes)) if codes else {}
        snapshot = MarketSnapshot(
            trade_date=signal_date,
            previous_trade_date=_previous_date(history, signal_date),
            source="AKShare+BaoStock+Tencent",
            adjustment="qfq",
            data_hash=build_market_data_hash(history, signal_date),
            freshness_seconds=0,
            metadata={"execution_quote_count": len(quotes)},
        )
        return snapshot, history, quotes


def _json_number(value: Any) -> float | None:
    """把 pandas/NumPy 数值转换为 JSON 可序列化浮点数。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) else None


def _last_trading_day_of_week(day: date_type, ignore_calendar: bool = False) -> bool:
    """判断当天是否为本周最后一个交易日。"""
    if ignore_calendar:
        return day.weekday() == 4
    cursor = day + timedelta(days=1)
    while cursor.weekday() <= 4:
        try:
            if calendar_is_trading_day(cursor.strftime("%Y%m%d")):
                return False
        except Exception:
            if cursor.weekday() < 5:
                return False
        cursor += timedelta(days=1)
    return True


class RobustV2Runner:
    """低频信号、统一执行和灾难止损协调器。"""

    def __init__(
        self,
        broker: SQLitePaperBrokerAdapter,
        loader: AKDataLoader,
        config: RobustV2Config | None = None,
        root_dir: Path = ROOT_DIR,
    ) -> None:
        self.config = config or RobustV2Config()
        self.broker = broker
        self.loader = loader
        self.strategy = RobustV2Strategy(self.config)
        self.allocator = PortfolioAllocator()
        self.data = RobustDataService(
            loader,
            root_dir,
            max_data_age_seconds=self.config.max_data_age_seconds,
        )
        self.root_dir = root_dir.resolve()

    @property
    def ledger(self):
        """返回 paper_v2 单账本。"""
        return self.broker.ledger

    def is_rebalance_due(
        self, trade_date: str, *, ignore_calendar: bool = False
    ) -> bool:
        """按周/双周判断收盘目标是否到期。"""
        current = datetime.strptime(trade_date, "%Y%m%d").date()
        if not _last_trading_day_of_week(current, ignore_calendar):
            return False
        latest = self.ledger.latest_signal_date(self.config.strategy_version)
        if latest is None:
            return True
        previous = datetime.strptime(latest, "%Y%m%d").date()
        required_weeks = 1 if self.config.rebalance_days == 5 else 2
        week_delta = (current - previous).days // 7
        return week_delta >= required_weeks

    def generate_close_target(
        self, trade_date: str, *, force: bool = False
    ) -> TargetPortfolio | None:
        """在周度收盘生成目标组合并持久化，不直接下单。"""
        if not force and not self.is_rebalance_due(trade_date):
            LOGGER.info("%s 非 robust_v2 调仓收盘，跳过目标生成", trade_date)
            return None
        snapshot = self.broker.query_snapshot()
        signal_data = self.data.load_signal_data(trade_date, snapshot.total_value)
        target = self.strategy.generate_target(
            signal_data.snapshot,
            signal_data.etf_history,
            signal_data.stock_history,
            snapshot.total_value,
            signal_data.names,
        )
        signal_id = self.ledger.record_signal(target)
        LOGGER.info(
            "收盘目标已记录: signal_id=%s date=%s exposure=%.2f%% cash=%.2f%% positions=%s",
            signal_id,
            target.signal_date,
            target.exposure * 100,
            target.cash_weight * 100,
            [position.code for position in target.positions],
        )
        return target

    def execute_pending_target(self, execution_date: str) -> ExecutionOutcome:
        """在 T+1 09:35 后执行最近一个未执行目标。"""
        pending = self.ledger.pending_target(execution_date)
        if pending is None:
            return ExecutionOutcome(None, None, None, (), "没有待执行目标")
        signal_id, target = pending
        positions = self.broker.query_positions()
        codes = {position.code for position in target.positions} | set(positions)
        snapshot, history, quotes = self.data.load_execution_data(
            codes, target.signal_date
        )
        alignment = validate_realtime_alignment(snapshot, history, quotes)
        prices = {
            code: float(quote.get("price", 0) or 0) for code, quote in quotes.items()
        }
        allocation = self.allocator.allocate(
            target,
            cash=self.broker.query_cash(),
            positions=positions,
            prices=prices,
            execution_date=execution_date,
            tradable_codes=set(alignment.valid_codes),
        )
        checked_orders = tuple(
            replace(
                order,
                metadata={
                    **order.metadata,
                    "data_health_checked": True,
                    "alignment_snapshot_hash": snapshot.data_hash,
                },
            )
            for order in allocation.orders
        )
        reports = tuple(self.broker.place_order(order) for order in checked_orders)
        # 无论成交还是健康检查跳过，都结束该信号，防止旧目标跨日追单。
        self.ledger.mark_signal_executed(signal_id)
        LOGGER.info(
            "目标执行完成: signal=%s orders=%d filled=%d skipped=%s alignment_rejected=%s",
            signal_id,
            len(reports),
            sum(report.status == "filled" for report in reports),
            allocation.skipped,
            alignment.rejected,
        )
        return ExecutionOutcome(signal_id, target, allocation, reports, "目标执行完成")

    def monitor_catastrophic_stops(
        self, trade_date: str
    ) -> tuple[ExecutionReport, ...]:
        """盘中只执行灾难止损，不生成普通技术退出或追涨补仓。"""
        positions = self.broker.query_positions()
        if not positions:
            return ()
        codes = set(positions)
        previous = (
            datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=1)
        ).strftime("%Y%m%d")
        snapshot, history, quotes = self.data.load_execution_data(codes, previous)
        # 实际上一交易日可能跨周末，使用历史数据里的最近日期重新构建校验快照。
        dates = _latest_dates(history, previous)
        if not dates:
            LOGGER.error("灾难止损行情校验失败：缺少历史数据")
            return ()
        signal_date = dates[-1]
        health_snapshot = MarketSnapshot(
            trade_date=signal_date,
            previous_trade_date=_previous_date(history, signal_date),
            source=snapshot.source,
            adjustment=snapshot.adjustment,
            data_hash=build_market_data_hash(history, signal_date),
            freshness_seconds=0,
        )
        alignment = validate_realtime_alignment(health_snapshot, history, quotes)
        prices = {
            code: float(quote.get("price", 0) or 0)
            for code, quote in quotes.items()
            if code in alignment.valid_codes
        }
        orders = self.strategy.catastrophic_stop_orders(positions, prices, trade_date)
        checked_orders = (
            replace(
                order,
                metadata={
                    **order.metadata,
                    "data_health_checked": True,
                    "alignment_snapshot_hash": health_snapshot.data_hash,
                },
            )
            for order in orders
        )
        return tuple(self.broker.place_order(order) for order in checked_orders)

    def record_close_and_report(self, trade_date: str, data_version: str) -> Path:
        """记录唯一日净值并生成含成本、换手和版本号的日报。"""
        positions = self.broker.query_positions()
        quotes = self.loader.get_realtime_quotes(list(positions)) if positions else {}
        prices = {
            code: float(quote.get("price", 0) or 0) for code, quote in quotes.items()
        }
        benchmark = self.loader.get_index_history("sh000300", days=10)
        benchmark_return: float | None = None
        if benchmark is not None and len(benchmark) >= 2:
            close = pd.to_numeric(benchmark["close"], errors="coerce").dropna()
            if len(close) >= 2 and float(close.iloc[-2]) > 0:
                benchmark_return = float(close.iloc[-1] / close.iloc[-2] - 1)
        self.ledger.record_snapshot(
            prices,
            snapshot_date=trade_date,
            data_version=data_version,
            strategy_version=self.config.strategy_version,
            benchmark_return=benchmark_return,
        )
        report = build_daily_ledger_report(
            self.ledger, trade_date, run_id=self.ledger.current_run_id
        )
        return save_daily_ledger_report(report, Path(REPORT_DIR))


def _configure_runtime(root_dir: Path) -> None:
    """初始化统一日志配置。"""
    setup_logger("robust_runner", rotating_files=("robust_v2.log",))
    LOGGER.info("运行目录: %s", root_dir)


def _is_trading_day(date: str, ignore_calendar: bool) -> bool:
    """安全读取交易日历。"""
    if ignore_calendar:
        return True
    try:
        return bool(calendar_is_trading_day(date))
    except Exception as exc:
        LOGGER.warning("交易日历不可用，回退到工作日判断: %s", exc)
        return datetime.strptime(date, "%Y%m%d").weekday() < 5


def _startup_log(
    broker: SQLitePaperBrokerAdapter, config: RobustV2Config
) -> tuple[str, str]:
    """输出代码、配置、账本、权限、T+1 和容器标识。"""
    commit = _safe_git_commit(ROOT_DIR)
    digest = _config_hash(config)
    LOGGER.info(
        "STARTUP commit=%s config_hash=%s ledger=%s account=%s T+1=%s "
        "permissions=main:%s,chinext:%s,star:%s container=%s",
        commit,
        digest,
        broker.ledger.path,
        config.account_id,
        ENFORCE_T1,
        ALLOW_MAIN_BOARD_STOCKS,
        ALLOW_CHINEXT_STOCKS,
        ALLOW_STAR_MARKET_STOCKS,
        socket.gethostname(),
    )
    return commit, digest


def run_daemon(
    runner: RobustV2Runner,
    *,
    once: bool = False,
    ignore_calendar: bool = False,
    poll_seconds: int = ROBUST_V2_MONITOR_INTERVAL_SECONDS,
) -> int:
    """运行持有 SQLite 写租约的唯一低频守护实例。"""
    if poll_seconds <= 0 or poll_seconds > 60:
        raise ValueError("轮询间隔必须位于 1-60 秒")
    validate_startup(runner.config)
    holder_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    runner.ledger.acquire_lease(holder_id, ROBUST_V2_LEASE_TTL_SECONDS)
    commit, digest = _startup_log(runner.broker, runner.config)
    run = runner.ledger.start_run(
        strategy_version=runner.config.strategy_version,
        code_commit=commit,
        config_hash=digest,
        data_version="pending-close-snapshot",
        container_id=socket.gethostname(),
    )
    stopping = False
    reported_dates: set[str] = set()

    def _stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    previous_sigterm = signal.signal(signal.SIGTERM, _stop)
    previous_sigint = signal.signal(signal.SIGINT, _stop)
    status = "completed"
    try:
        while not stopping:
            now = now_local()
            trade_date = now.strftime("%Y%m%d")
            if not _is_trading_day(trade_date, ignore_calendar):
                LOGGER.info("%s 非交易日，只续租不交易", trade_date)
            elif dt_time(9, 35) <= now.time() < dt_time(15, 0):
                runner.execute_pending_target(trade_date)
                runner.monitor_catastrophic_stops(trade_date)
            elif now.time() >= dt_time(15, 5):
                # 收盘处理每天只在首次进入收盘窗口时执行一次，
                # 避免每分钟重复生成日报 / 重复打印“跳过目标生成”。
                if trade_date not in reported_dates:
                    target = runner.generate_close_target(trade_date)
                    data_version = (
                        target.source_snapshot_hash
                        if target is not None
                        else (
                            runner.ledger.latest_signal_hash(runner.config.strategy_version)
                            or "no-signal"
                        )
                    )
                    path = runner.record_close_and_report(trade_date, data_version)
                    LOGGER.info("收盘日报: %s", path)
                    reported_dates.add(trade_date)
                else:
                    LOGGER.debug("%s 收盘处理今日已执行，跳过重复日报", trade_date)
            else:
                LOGGER.info("当前不在 09:35-15:00 执行窗口或 15:05 后收盘窗口")

            runner.ledger.heartbeat_lease(holder_id, ROBUST_V2_LEASE_TTL_SECONDS)
            if once:
                break
            time.sleep(poll_seconds)
    except Exception:
        status = "failed"
        LOGGER.exception("robust_v2 守护实例异常退出")
        return 2
    finally:
        try:
            runner.ledger.finish_run(run.run_id, status=status)
        finally:
            runner.ledger.release_lease(holder_id)
            signal.signal(signal.SIGTERM, previous_sigterm)
            signal.signal(signal.SIGINT, previous_sigint)
    return 0


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="A 股 5 万元稳健虚拟盘 V2")
    parser.add_argument(
        "command",
        choices=["daemon", "signal", "execute", "monitor", "report", "status"],
        nargs="?",
        default="daemon",
    )
    parser.add_argument(
        "--ledger", default=ROBUST_V2_LEDGER_PATH, help="SQLite 账本路径"
    )
    parser.add_argument("--date", help="交易日 YYYYMMDD，默认今天")
    parser.add_argument("--force", action="store_true", help="强制生成收盘目标")
    parser.add_argument("--once", action="store_true", help="守护模式只执行一个周期")
    parser.add_argument(
        "--ignore-calendar", action="store_true", help="联调时忽略交易日历"
    )
    parser.add_argument(
        "--poll-seconds", type=int, default=ROBUST_V2_MONITOR_INTERVAL_SECONDS
    )
    return parser.parse_args()


def main() -> int:
    """命令行入口。"""
    args = _parse_args()
    _configure_runtime(ROOT_DIR)
    config = load_runtime_config()
    validate_startup(config)
    if not Path(args.ledger).expanduser().exists():
        raise RuntimeError(
            "paper_v2 账本尚未初始化；请先运行 "
            "python scripts/paper_v2_init.py --confirm --json"
        )
    broker = SQLitePaperBrokerAdapter(
        ledger_path=args.ledger,
        account_id=ROBUST_V2_ACCOUNT_ID,
        initial_cash=INITIAL_CAPITAL,
    )
    broker.connect()
    loader = AKDataLoader()
    runner = RobustV2Runner(broker, loader, config, ROOT_DIR)
    trade_date = args.date or today_yyyymmdd()
    try:
        if args.command == "daemon":
            return run_daemon(
                runner,
                once=args.once,
                ignore_calendar=args.ignore_calendar,
                poll_seconds=args.poll_seconds,
            )
        commit, digest = _startup_log(broker, config)
        if args.command == "status":
            snapshot = broker.query_snapshot()
            LOGGER.info(
                "账户=%s 总资产=%.2f 现金=%.2f 持仓=%d 最近信号=%s",
                config.account_id,
                snapshot.total_value,
                snapshot.cash,
                snapshot.position_count,
                runner.ledger.latest_signal_date(config.strategy_version),
            )
            return 0

        holder_id = (
            f"manual:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        runner.ledger.acquire_lease(holder_id, ROBUST_V2_LEASE_TTL_SECONDS)
        run = runner.ledger.start_run(
            strategy_version=config.strategy_version,
            code_commit=commit,
            config_hash=digest,
            data_version=f"manual-{args.command}",
            container_id=socket.gethostname(),
        )
        command_status = "completed"
        try:
            if args.command == "signal":
                runner.generate_close_target(trade_date, force=args.force)
            elif args.command == "execute":
                runner.execute_pending_target(trade_date)
            elif args.command == "monitor":
                runner.monitor_catastrophic_stops(trade_date)
            elif args.command == "report":
                latest = (
                    runner.ledger.latest_signal_hash(config.strategy_version)
                    or "manual"
                )
                path = runner.record_close_and_report(trade_date, latest)
                LOGGER.info("日报已生成: %s", path)
        except Exception:
            command_status = "failed"
            raise
        finally:
            runner.ledger.finish_run(run.run_id, status=command_status)
            runner.ledger.release_lease(holder_id)
        return 0
    finally:
        loader.close()
        broker.close()


if __name__ == "__main__":
    raise SystemExit(main())
