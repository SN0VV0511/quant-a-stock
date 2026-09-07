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
import tempfile
import threading
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import date as date_type
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Literal, Mapping

import pandas as pd

from config.logging_setup import setup_logger
from config.settings import (
    ALLOW_CHINEXT_STOCKS,
    ALLOW_MAIN_BOARD_STOCKS,
    ALLOW_STAR_MARKET_STOCKS,
    DAILY_LOSS_THRESHOLD,
    ENFORCE_T1,
    INITIAL_CAPITAL,
    MAX_DRAWDOWN_THRESHOLD,
    REPORT_DIR,
    ROBUST_V2_ACCOUNT_ID,
    ROBUST_V2_BACKUP_DIR,
    ROBUST_V2_BACKUP_RETENTION_DAYS,
    ROBUST_V2_BROAD_ETF_CODES,
    ROBUST_V2_DAILY_JOB_RETRY_SECONDS,
    ROBUST_V2_LEASE_TTL_SECONDS,
    ROBUST_V2_LEDGER_PATH,
    ROBUST_V2_MAX_EXECUTION_QUOTE_AGE_SECONDS,
    ROBUST_V2_MIN_ETF_HISTORY_COVERAGE,
    ROBUST_V2_MIN_HISTORY_COVERAGE,
    ROBUST_V2_MIN_MAINBOARD_UNIVERSE,
    ROBUST_V2_MIN_REALTIME_QUOTE_COVERAGE,
    ROBUST_V2_MONITOR_INTERVAL_SECONDS,
    ROBUST_V2_SELECTED_CONFIG_PATH,
    ROBUST_V2_SIGNAL_RETRY_SECONDS,
    ROBUST_V2_SIGNAL_STRUCTURAL_MAX_ATTEMPTS,
    ROBUST_V2_STOCK_CANDIDATE_LIMIT,
    ROBUST_V2_STRATEGY_VERSION,
    get_stock_board,
    is_etf,
    is_supported_trading_target,
)
from config.time_utils import now_local, today_yyyymmdd
from data.ak_loader import AKDataLoader
from data.holidays import (
    is_trading_day as calendar_is_trading_day,
    previous_trading_day,
)
from data.scan_store import ScanMode, StockScanSnapshot, StockScanStore
from news_overlay import (
    MIN_ACTION_CONFIDENCE,
    NewsOverlay,
    NewsVerdict,
    load_news_overlay,
    news_overlay_path,
)
from reports.ledger_report import build_daily_ledger_report, save_daily_ledger_report
from strategies.robust_v2 import (
    STOCK_FILTER_LABELS,
    RobustV2Config,
    RobustV2Strategy,
    StockScanResult,
    build_market_data_hash,
    validate_realtime_alignment,
)
from trading.allocator import AllocationResult, PortfolioAllocator
from trading.brokers import BrokerAdapter, SQLitePaperBrokerAdapter
from trading.ledger import PaperLedger
from trading.market import (
    ExecutionQuote,
    classify_trading_session,
    is_strategy_execution_session,
    order_is_tradable,
    validate_execution_quote,
)
from trading.instruments import normalized_security_code
from trading.models import (
    ExecutionReport,
    MarketSnapshot,
    OrderIntent,
    TargetPortfolio,
    TargetPosition,
)
from trading.schedule import rebalance_interval_elapsed

LOGGER = logging.getLogger("robust_runner")
ROOT_DIR = Path(__file__).resolve().parent
STOCK_PREFILTER_LABELS: dict[str, str] = {
    "missing_realtime_quote": "缺少实时行情",
    "st_or_delisting": "粗筛排除 ST 或退市风险",
    "price_out_of_range": "粗筛股价超出范围",
    "lot_too_expensive": "粗筛一手金额超过预算",
    "insufficient_liquidity": "粗筛实时成交额不足",
    "candidate_limit": "流动性排名超出 500 只上限",
}
# 新闻利好标的入选后的目标权重放大倍数,仍受单票与总仓位硬约束封顶。
NEWS_BOOST_FACTOR = 1.25


@dataclass(frozen=True)
class NewsOverlayPlan:
    """一次收盘目标生成时新闻研判覆盖层的可执行结论。"""

    risk_sell_codes: frozenset[str]
    boost_codes: frozenset[str]
    verdicts: dict[str, NewsVerdict]
    exit_orders: tuple[OrderIntent, ...]


@dataclass
class NewsMonitorState:
    """单个交易日盘中监控的新闻研判文件状态。"""

    mtime: float
    processed: frozenset[str]
    pending: tuple[NewsVerdict, ...] = ()


@dataclass(frozen=True)
class SignalData:
    """一次收盘选仓使用的版本化行情。"""

    snapshot: MarketSnapshot
    etf_history: dict[str, pd.DataFrame]
    stock_history: dict[str, pd.DataFrame]
    names: dict[str, str]
    quotes: dict[str, dict[str, Any]]
    universe: dict[str, int] = field(default_factory=dict)
    prefilter_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionOutcome:
    """一次目标执行结果。"""

    signal_id: str | None
    target: TargetPortfolio | None
    allocation: AllocationResult | None
    reports: tuple[ExecutionReport, ...]
    message: str


class DataCompletenessError(RuntimeError):
    """正式信号输入未达到可证明的完整性下限。"""


class LeaseHeartbeat:
    """在耗时行情扫描期间独立续租账户写锁。"""

    def __init__(
        self,
        ledger: Any,
        holder_id: str,
        ttl_seconds: int,
        *,
        interval_seconds: float | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("租约 TTL 必须大于 0")
        self.ledger = ledger
        self.holder_id = holder_id
        self.ttl_seconds = ttl_seconds
        self.interval_seconds = interval_seconds or max(1.0, ttl_seconds / 3)
        if self.interval_seconds >= ttl_seconds:
            raise ValueError("续租间隔必须小于租约 TTL")
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    def start(self) -> None:
        """启动守护续租线程。"""
        if self._thread is not None:
            raise RuntimeError("租约续租线程不能重复启动")
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-heartbeat-{self.holder_id[-12:]}",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        """按固定间隔续租；失败会留给主线程显式处理。"""
        while not self._stop_event.wait(self.interval_seconds):
            try:
                self.ledger.heartbeat_lease(self.holder_id, self.ttl_seconds)
            except Exception as exc:
                self._error = exc
                self._stop_event.set()
                return

    def raise_if_failed(self) -> None:
        """续租失败时阻断后续账户写入。"""
        if self._error is not None:
            raise RuntimeError("账户写租约续租失败，已停止交易") from self._error

    def stop(self) -> None:
        """停止续租线程并等待其退出。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 2))
        self.raise_if_failed()


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


def _history_contains_date(frame: pd.DataFrame, trade_date: str) -> bool:
    """判断单个行情表是否真实包含目标交易日。"""
    if frame is None or frame.empty:
        return False
    if "date" in frame.columns:
        normalized = frame["date"].astype(str).str.replace("-", "", regex=False).str[:8]
        return bool((normalized == trade_date).any())
    if isinstance(frame.index, pd.DatetimeIndex):
        return trade_date in set(frame.index.strftime("%Y%m%d"))
    return False


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
        code
        for code in ROBUST_V2_BROAD_ETF_CODES
        if not is_supported_trading_target(code)
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
            "etf_min_20d_return",
            "stock_min_earnings_yield",
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
        min_mainboard_universe: int = ROBUST_V2_MIN_MAINBOARD_UNIVERSE,
        min_realtime_quote_coverage: float = ROBUST_V2_MIN_REALTIME_QUOTE_COVERAGE,
        min_history_coverage: float = ROBUST_V2_MIN_HISTORY_COVERAGE,
        min_etf_history_coverage: float = ROBUST_V2_MIN_ETF_HISTORY_COVERAGE,
    ) -> None:
        if stock_candidate_limit <= 0:
            raise ValueError("股票候选数量必须大于 0")
        self.loader = loader
        self.root_dir = root_dir.resolve()
        self.stock_candidate_limit = stock_candidate_limit
        self.max_data_age_seconds = max_data_age_seconds
        self.min_mainboard_universe = min_mainboard_universe
        self.min_realtime_quote_coverage = min_realtime_quote_coverage
        self.min_history_coverage = min_history_coverage
        self.min_etf_history_coverage = min_etf_history_coverage
        if min_mainboard_universe <= 0:
            raise ValueError("主板股票池完整性下限必须大于 0")
        for name, value in {
            "实时行情覆盖率": min_realtime_quote_coverage,
            "历史行情覆盖率": min_history_coverage,
            "ETF 历史覆盖率": min_etf_history_coverage,
        }.items():
            if not 0 < value <= 1:
                raise ValueError(f"{name}必须位于 (0, 1]")

    def _stock_candidates(
        self, account_value: float
    ) -> tuple[
        list[str],
        dict[str, str],
        dict[str, dict[str, Any]],
        dict[str, int],
        dict[str, int],
    ]:
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
        prefilter_counts: Counter[str] = Counter()
        missing_quotes = len(set(codes) - set(quotes))
        if missing_quotes > 0:
            prefilter_counts["missing_realtime_quote"] = missing_quotes
        for code in codes:
            quote = quotes.get(code)
            if quote is None:
                continue
            name = str(quote.get("name") or names.get(code, code))
            price = float(quote.get("price", 0) or 0)
            volume = float(quote.get("volume", 0) or 0)
            if "ST" in name.upper() or "退" in name:
                prefilter_counts["st_or_delisting"] += 1
                continue
            if not 3 <= price <= 80:
                prefilter_counts["price_out_of_range"] += 1
                continue
            if price * 100 > account_value * 0.2:
                prefilter_counts["lot_too_expensive"] += 1
                continue
            amount_proxy = price * volume
            if amount_proxy < 100_000_000:
                prefilter_counts["insufficient_liquidity"] += 1
                continue
            ranked.append((amount_proxy, code))
            names[code] = name
        ranked.sort(reverse=True)
        if len(ranked) > self.stock_candidate_limit:
            prefilter_counts["candidate_limit"] = (
                len(ranked) - self.stock_candidate_limit
            )
        selected = [code for _, code in ranked[: self.stock_candidate_limit]]
        selected_quotes = {code: quotes[code] for code in selected if code in quotes}
        universe = {
            "mainboard_count": len(stocks),
            "realtime_quote_count": len(quotes),
            "tencent_quote_count": sum(
                str(quote.get("source", "")).lower() == "tencent"
                for quote in quotes.values()
            ),
            "rough_candidate_count": len(selected),
        }
        universe_info_getter = getattr(self.loader, "get_stock_universe_info", None)
        if callable(universe_info_getter):
            info = universe_info_getter()
            universe["universe_authoritative"] = int(
                bool(info.get("authoritative", False))
            )
            universe["universe_cache_stale"] = int(bool(info.get("stale", True)))
        ordered_prefilter_counts = {
            reason: prefilter_counts[reason]
            for reason in STOCK_PREFILTER_LABELS
            if prefilter_counts[reason] > 0
        }
        return selected, names, selected_quotes, universe, ordered_prefilter_counts

    def _require_complete_signal_data(
        self,
        *,
        universe: Mapping[str, int],
        etf_history_count: int,
    ) -> None:
        """数据不足时拒绝生成正式目标，避免把数据故障伪装成空候选。"""
        mainboard_count = int(universe.get("mainboard_count", 0))
        quote_count = int(universe.get("realtime_quote_count", 0))
        tencent_quote_count = int(universe.get("tencent_quote_count", 0))
        rough_count = int(universe.get("rough_candidate_count", 0))
        history_count = int(universe.get("history_loaded_count", 0))
        signal_date_history_count = int(
            universe.get("history_signal_date_count", history_count)
        )
        signal_date_etf_count = int(
            universe.get("etf_signal_date_count", etf_history_count)
        )
        if int(universe.get("universe_authoritative", 1)) != 1:
            raise DataCompletenessError("股票池来源不可证明完整，拒绝生成正式信号")
        if mainboard_count < self.min_mainboard_universe:
            raise DataCompletenessError(
                f"主板股票池仅 {mainboard_count} 只，低于完整性下限 "
                f"{self.min_mainboard_universe}"
            )
        quote_coverage = quote_count / mainboard_count if mainboard_count else 0.0
        if quote_coverage < self.min_realtime_quote_coverage:
            raise DataCompletenessError(
                f"实时行情覆盖率 {quote_coverage:.2%} 低于下限 "
                f"{self.min_realtime_quote_coverage:.2%}"
            )
        tencent_coverage = (
            tencent_quote_count / mainboard_count if mainboard_count else 0.0
        )
        if tencent_coverage < self.min_realtime_quote_coverage:
            raise DataCompletenessError(
                f"腾讯实时行情覆盖率 {tencent_coverage:.2%} 低于下限 "
                f"{self.min_realtime_quote_coverage:.2%}"
            )
        if rough_count <= 0:
            raise DataCompletenessError("实时粗筛未产生任何个股候选，拒绝生成正式信号")
        history_coverage = history_count / rough_count
        if history_coverage < self.min_history_coverage:
            raise DataCompletenessError(
                f"候选历史行情覆盖率 {history_coverage:.2%} 低于下限 "
                f"{self.min_history_coverage:.2%}"
            )
        signal_date_coverage = signal_date_history_count / rough_count
        if signal_date_coverage < self.min_history_coverage:
            raise DataCompletenessError(
                f"候选信号日行情覆盖率 {signal_date_coverage:.2%} 低于下限 "
                f"{self.min_history_coverage:.2%}"
            )
        required_etfs = max(
            1,
            int(
                len(ROBUST_V2_BROAD_ETF_CODES) * self.min_etf_history_coverage
                + 0.999999
            ),
        )
        if etf_history_count < required_etfs:
            raise DataCompletenessError(
                f"宽基 ETF 历史行情仅 {etf_history_count}/"
                f"{len(ROBUST_V2_BROAD_ETF_CODES)}，"
                f"至少需要 {required_etfs} 只"
            )
        if signal_date_etf_count < required_etfs:
            raise DataCompletenessError(
                f"宽基 ETF 信号日行情仅 {signal_date_etf_count}/"
                f"{len(ROBUST_V2_BROAD_ETF_CODES)}，至少需要 {required_etfs} 只"
            )

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
        etf_codes = sorted(ROBUST_V2_BROAD_ETF_CODES)
        etf_history = self.loader.get_batch_etf_history(
            etf_codes, days=420, adjust="qfq"
        )
        stock_codes, names, quotes, universe, prefilter_counts = self._stock_candidates(
            account_value
        )
        stock_history = self.loader.get_batch_history_ext(
            stock_codes,
            days=420,
            max_batch=self.stock_candidate_limit,
        )
        universe["history_loaded_count"] = len(stock_history)
        universe["history_signal_date_count"] = sum(
            _history_contains_date(frame, trade_date)
            for frame in stock_history.values()
        )
        universe["etf_signal_date_count"] = sum(
            _history_contains_date(frame, trade_date) for frame in etf_history.values()
        )
        try:
            self._require_complete_signal_data(
                universe=universe,
                etf_history_count=len(etf_history),
            )
        except DataCompletenessError as exc:
            # 日K源全部失败时把各源失败原因并入消息，便于定位反爬/网络故障。
            summary_getter = getattr(
                self.loader, "kline_source_failure_summary", None
            )
            summary = summary_getter() if callable(summary_getter) else ""
            if summary:
                raise DataCompletenessError(
                    f"{exc}；日K数据源尝试明细: {summary}"
                ) from exc
            raise
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
            universe=universe,
            prefilter_counts=prefilter_counts,
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
        if calendar_is_trading_day(cursor.strftime("%Y%m%d")):
            return False
        cursor += timedelta(days=1)
    return True


def _last_trading_day_of_month(day: date_type, ignore_calendar: bool = False) -> bool:
    """判断当天是否为当月最后一个交易日。"""
    cursor = day + timedelta(days=1)
    while cursor.month == day.month:
        if ignore_calendar:
            if cursor.weekday() < 5:
                return False
        elif calendar_is_trading_day(cursor.strftime("%Y%m%d")):
            return False
        cursor += timedelta(days=1)
    return True


def _is_rebalance_period_end(
    day: date_type,
    rebalance_days: int,
    *,
    ignore_calendar: bool = False,
) -> bool:
    """按配置选择周末或月末收盘边界。"""
    if rebalance_days == 20:
        return _last_trading_day_of_month(day, ignore_calendar)
    return _last_trading_day_of_week(day, ignore_calendar)


class RobustV2Runner:
    """低频信号、统一执行和灾难止损协调器。"""

    def __init__(
        self,
        broker: BrokerAdapter,
        loader: AKDataLoader,
        config: RobustV2Config | None = None,
        root_dir: Path = ROOT_DIR,
        ledger: PaperLedger | None = None,
    ) -> None:
        self.config = config or RobustV2Config()
        self.broker = broker
        self.loader = loader
        self.strategy = RobustV2Strategy(self.config)
        # 执行边界校验的数量上限必须与策略配置同源，避免策略放宽后被硬编码拦下。
        self.allocator = PortfolioAllocator(
            max_etf_count=self.config.max_etf_count,
            max_stock_count=self.config.max_stock_count,
        )
        self.data = RobustDataService(
            loader,
            root_dir,
            max_data_age_seconds=self.config.max_data_age_seconds,
        )
        self.root_dir = root_dir.resolve()
        self.scan_store = StockScanStore(self.root_dir / "data" / "scans")
        # 新闻研判盘中监控状态按交易日隔离,mtime 变化时增量重载。
        self._news_monitor_state: dict[str, NewsMonitorState] = {}
        broker_ledger = getattr(broker, "ledger", None)
        selected_ledger = ledger or broker_ledger
        if not isinstance(selected_ledger, PaperLedger):
            raise TypeError("robust_v2 必须显式提供独立审计账本")
        self._ledger = selected_ledger

    @property
    def ledger(self) -> PaperLedger:
        """返回与交易通道解耦的信号、运行和审计账本。"""
        return self._ledger

    def _validated_execution_quotes(
        self,
        codes: set[str],
        quotes: Mapping[str, Mapping[str, Any]],
        *,
        execution_date: str,
        current_time: datetime,
    ) -> tuple[dict[str, ExecutionQuote], dict[str, str]]:
        """对目标和持仓行情做交易所时间、停牌与价格制度校验。"""
        contexts: dict[str, ExecutionQuote] = {}
        rejected: dict[str, str] = {}
        quote_by_raw = {
            normalized_security_code(code): (code, quote)
            for code, quote in quotes.items()
        }
        for code in codes:
            raw = normalized_security_code(code)
            entry = quote_by_raw.get(raw)
            if entry is None:
                rejected[code] = "缺少实时行情"
                continue
            quote_code, quote = entry
            result = validate_execution_quote(
                quote_code,
                quote,
                execution_date=execution_date,
                now=current_time,
                max_age_seconds=ROBUST_V2_MAX_EXECUTION_QUOTE_AGE_SECONDS,
            )
            if result.context is None:
                rejected[code] = result.reason
                continue
            contexts[raw] = result.context
        return contexts, rejected

    def _buy_risk_block_reason(
        self,
        prices: Mapping[str, float],
        *,
        execution_date: str,
    ) -> str:
        """把全局日亏与回撤熔断接入 robust_v2 买入边界。"""
        snapshot = self.broker.query_snapshot(dict(prices))
        if snapshot.drawdown >= MAX_DRAWDOWN_THRESHOLD:
            return (
                f"账户回撤 {snapshot.drawdown:.2%} 达到熔断线 "
                f"{MAX_DRAWDOWN_THRESHOLD:.2%}，暂停新买入"
            )
        previous = self.ledger.latest_nav_before(execution_date)
        if previous is None:
            return ""
        previous_value = float(previous.get("total_value", 0) or 0)
        if previous_value <= 0:
            return ""
        daily_return = snapshot.total_value / previous_value - 1
        if daily_return <= -DAILY_LOSS_THRESHOLD:
            return (
                f"账户当日亏损 {daily_return:.2%} 达到熔断线 "
                f"-{DAILY_LOSS_THRESHOLD:.2%}，暂停新买入"
            )
        return ""

    def is_rebalance_due(
        self, trade_date: str, *, ignore_calendar: bool = False
    ) -> bool:
        """按周、双周或月度周期判断收盘目标是否到期。"""
        current = datetime.strptime(trade_date, "%Y%m%d").date()
        if not _is_rebalance_period_end(
            current,
            self.config.rebalance_days,
            ignore_calendar=ignore_calendar,
        ):
            return False
        latest = self.ledger.latest_signal_date(self.config.strategy_version)
        if latest is None:
            return True
        return rebalance_interval_elapsed(
            trade_date,
            latest,
            self.config.rebalance_days,
        )

    def next_scheduled_scan_at(self, from_date: str) -> str:
        """计算不早于指定日期的下一次调仓扫描时间。"""
        cursor = datetime.strptime(from_date, "%Y%m%d").date()
        latest_text = self.ledger.latest_signal_date(self.config.strategy_version)
        latest = (
            datetime.strptime(latest_text, "%Y%m%d").date()
            if latest_text is not None
            else None
        )
        for offset in range(46):
            candidate = cursor + timedelta(days=offset)
            date_text = candidate.strftime("%Y%m%d")
            if not _is_trading_day(date_text, ignore_calendar=False):
                continue
            if not _is_rebalance_period_end(
                candidate,
                self.config.rebalance_days,
            ):
                continue
            if rebalance_interval_elapsed(
                date_text,
                latest.strftime("%Y%m%d") if latest is not None else None,
                self.config.rebalance_days,
            ):
                return f"{candidate.isoformat()} 15:05:00"
        return ""

    def _stock_scan_snapshot(
        self,
        signal_data: SignalData,
        result: StockScanResult,
        *,
        account_value: float,
        mode: ScanMode,
        selected_codes: tuple[str, ...],
    ) -> StockScanSnapshot:
        """把策略扫描结果补齐行情、排名和调度信息。"""
        rows: list[dict[str, Any]] = []
        selected = set(selected_codes)
        for rank, candidate in enumerate(result.candidates, start=1):
            code = str(candidate["code"])
            quote = signal_data.quotes.get(code, {})
            current_price = _json_number(quote.get("price"))
            rows.append(
                {
                    **candidate,
                    "rank": rank,
                    "current_price": current_price or float(candidate["price"]),
                    "selected": code in selected,
                }
            )
        return StockScanSnapshot(
            strategy_version=self.config.strategy_version,
            trade_date=signal_data.snapshot.trade_date,
            generated_at=now_local().strftime("%Y-%m-%d %H:%M:%S"),
            mode=mode,
            status="completed",
            account_value=account_value,
            source_snapshot_hash=signal_data.snapshot.data_hash,
            universe=dict(signal_data.universe),
            input_count=result.input_count,
            eligible_count=result.eligible_count,
            prefilter_counts=dict(signal_data.prefilter_counts),
            prefilter_labels=dict(STOCK_PREFILTER_LABELS),
            filter_counts=dict(result.filter_counts),
            filter_labels=dict(STOCK_FILTER_LABELS),
            candidates=tuple(rows),
            selected_codes=selected_codes,
            next_scheduled_scan_at=self.next_scheduled_scan_at(
                signal_data.snapshot.trade_date
            ),
        )

    def _save_stock_scan(
        self,
        signal_data: SignalData,
        result: StockScanResult,
        *,
        account_value: float,
        mode: ScanMode,
        selected_codes: tuple[str, ...],
    ) -> Path:
        """保存扫描快照并记录关键计数。"""
        snapshot = self._stock_scan_snapshot(
            signal_data,
            result,
            account_value=account_value,
            mode=mode,
            selected_codes=selected_codes,
        )
        path = self.scan_store.save(snapshot)
        LOGGER.info(
            "个股扫描完成: mode=%s date=%s input=%d eligible=%d selected=%s file=%s",
            snapshot.mode,
            snapshot.trade_date,
            snapshot.input_count,
            snapshot.eligible_count,
            list(snapshot.selected_codes),
            path,
        )
        return path

    def preview_stock_scan(
        self,
        trade_date: str,
        *,
        mode: Literal["daily_observation", "manual_preview"] = "manual_preview",
    ) -> StockScanSnapshot:
        """运行只读交易预览；仅写审计文件，不创建信号或订单。"""
        account_value = self.broker.query_snapshot().total_value
        try:
            signal_data = self.data.load_signal_data(trade_date, account_value)
            result = self.strategy.scan_stocks(
                signal_data.stock_history,
                signal_data.snapshot,
                account_value,
                signal_data.names,
            )
            target_preview = self.strategy.generate_target(
                signal_data.snapshot,
                signal_data.etf_history,
                signal_data.stock_history,
                account_value,
                signal_data.names,
                stock_scan_result=result,
            )
            selected_codes = tuple(
                position.code
                for position in target_preview.positions
                if position.asset_type == "stock"
            )
            snapshot = self._stock_scan_snapshot(
                signal_data,
                result,
                account_value=account_value,
                mode=mode,
                selected_codes=selected_codes,
            )
            self.scan_store.save(snapshot)
            LOGGER.info(
                "安全预览扫描完成: date=%s eligible=%d would_select=%s",
                trade_date,
                snapshot.eligible_count,
                list(snapshot.selected_codes),
            )
            return snapshot
        except Exception as exc:
            failed = StockScanSnapshot(
                strategy_version=self.config.strategy_version,
                trade_date=trade_date,
                generated_at=now_local().strftime("%Y-%m-%d %H:%M:%S"),
                mode=mode,
                status="failed",
                account_value=max(account_value, 0),
                source_snapshot_hash="unavailable",
                universe={},
                input_count=0,
                eligible_count=0,
                prefilter_counts={},
                prefilter_labels=dict(STOCK_PREFILTER_LABELS),
                filter_counts={},
                filter_labels=dict(STOCK_FILTER_LABELS),
                candidates=(),
                selected_codes=(),
                next_scheduled_scan_at=self.next_scheduled_scan_at(trade_date),
                error=str(exc),
            )
            self.scan_store.save(failed)
            LOGGER.exception("安全预览扫描失败: date=%s", trade_date)
            raise

    def ensure_daily_candidate_scan(self, trade_date: str) -> StockScanSnapshot | None:
        """确保当日已有候选观察；正式调仓扫描存在时不重复拉取全市场。"""
        try:
            latest = self.scan_store.load_latest()
        except RuntimeError:
            latest = None
        if (
            latest is not None
            and latest.get("trade_date") == trade_date
            and latest.get("status") == "completed"
        ):
            return None
        return self.preview_stock_scan(trade_date, mode="daily_observation")

    @staticmethod
    def _held_position_index(
        positions: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, tuple[str, Mapping[str, Any]]]:
        """按六位代码建立当前持仓索引。"""
        held: dict[str, tuple[str, Mapping[str, Any]]] = {}
        for code, position in positions.items():
            try:
                held[normalized_security_code(code)] = (code, position)
            except ValueError:
                continue
        return held

    def _news_exit_order(
        self,
        code: str,
        position: Mapping[str, Any],
        verdict: NewsVerdict,
        trade_date: str,
        *,
        price: float,
        shares: int,
        source: str,
        extra_metadata: Mapping[str, Any] | None = None,
    ) -> OrderIntent:
        """复用 OrderIntent 生成新闻风险强制卖出订单。"""
        return OrderIntent(
            account_id=self.config.account_id,
            strategy_version=self.config.strategy_version,
            signal_date=trade_date,
            code=code,
            action="sell",
            price=price,
            shares=shares,
            name=str(position.get("name") or verdict.name or code),
            strategy=self.strategy.name,
            strategy_tag="robust_v2",
            reason="NEWS_RISK_EXIT",
            date=trade_date,
            source=source,
            metadata={
                **(extra_metadata or {}),
                "news_confidence": verdict.confidence,
                "news_reason": verdict.reason,
                "news_sources": list(verdict.sources),
            },
        )

    def _plan_news_overlay(
        self,
        overlay: NewsOverlay | None,
        *,
        positions: Mapping[str, Mapping[str, Any]],
        candidate_codes: tuple[str, ...],
        trade_date: str,
    ) -> NewsOverlayPlan:
        """把当日研判转换为目标生成前的覆盖动作;白名单外标的一律忽略。"""
        if overlay is None or not overlay.verdicts:
            return NewsOverlayPlan(frozenset(), frozenset(), {}, ())
        held = self._held_position_index(positions)
        candidates: set[str] = set()
        for code in candidate_codes:
            try:
                candidates.add(normalized_security_code(code))
            except ValueError:
                continue
        whitelist = frozenset(held) | candidates
        risk_sell: set[str] = set()
        boost: set[str] = set()
        verdicts: dict[str, NewsVerdict] = {}
        exit_orders: list[OrderIntent] = []
        for verdict in overlay.verdicts:
            if verdict.code not in whitelist:
                LOGGER.debug(
                    "新闻研判忽略白名单外标的: code=%s action=%s confidence=%.2f "
                    "reason=%s sources=%s",
                    verdict.code,
                    verdict.action,
                    verdict.confidence,
                    verdict.reason,
                    list(verdict.sources),
                )
                continue
            if verdict.action == "watch":
                LOGGER.debug(
                    "新闻研判仅观察不产生交易动作: code=%s confidence=%.2f "
                    "reason=%s sources=%s",
                    verdict.code,
                    verdict.confidence,
                    verdict.reason,
                    list(verdict.sources),
                )
                continue
            if verdict.confidence < MIN_ACTION_CONFIDENCE:
                LOGGER.debug(
                    "新闻研判置信度不足不产生交易动作: code=%s action=%s "
                    "confidence=%.2f reason=%s sources=%s",
                    verdict.code,
                    verdict.action,
                    verdict.confidence,
                    verdict.reason,
                    list(verdict.sources),
                )
                continue
            if verdict.action == "risk_sell":
                risk_sell.add(verdict.code)
                verdicts[verdict.code] = verdict
                LOGGER.info(
                    "新闻风险研判生效: code=%s name=%s action=risk_sell "
                    "confidence=%.2f reason=%s sources=%s",
                    verdict.code,
                    verdict.name,
                    verdict.confidence,
                    verdict.reason,
                    list(verdict.sources),
                )
                entry = held.get(verdict.code)
                if entry is None:
                    continue
                original_code, position = entry
                sellable = int(
                    position.get("sellable_qty", position.get("shares", 0)) or 0
                )
                price = float(position.get("current_price", 0) or 0)
                if sellable <= 0 or price <= 0:
                    LOGGER.info(
                        "新闻风险卖出当日无可卖数量(T+1 锁定或缺有效价格)，"
                        "已从目标剔除，待 T+1 统一执行: code=%s sellable=%d",
                        original_code,
                        sellable,
                    )
                    continue
                exit_orders.append(
                    self._news_exit_order(
                        original_code,
                        position,
                        verdict,
                        trade_date,
                        price=price,
                        shares=sellable,
                        source="news_overlay",
                    )
                )
                LOGGER.info(
                    "新闻强制卖出订单已生成: code=%s shares=%d price=%.2f "
                    "confidence=%.2f reason=%s sources=%s",
                    original_code,
                    sellable,
                    price,
                    verdict.confidence,
                    verdict.reason,
                    list(verdict.sources),
                )
            elif verdict.code in candidates:
                boost.add(verdict.code)
                verdicts[verdict.code] = verdict
                LOGGER.info(
                    "新闻利好研判生效: code=%s name=%s action=boost "
                    "confidence=%.2f reason=%s sources=%s",
                    verdict.code,
                    verdict.name,
                    verdict.confidence,
                    verdict.reason,
                    list(verdict.sources),
                )
            else:
                LOGGER.debug(
                    "新闻利好标的不在当日候选，不产生交易动作: code=%s "
                    "confidence=%.2f reason=%s sources=%s",
                    verdict.code,
                    verdict.confidence,
                    verdict.reason,
                    list(verdict.sources),
                )
        return NewsOverlayPlan(
            frozenset(risk_sell),
            frozenset(boost),
            verdicts,
            tuple(exit_orders),
        )

    def _apply_news_overlay_to_scan(
        self,
        stock_scan: StockScanResult,
        plan: NewsOverlayPlan,
    ) -> StockScanResult:
        """按研判重排候选:risk_sell 剔除、boost 置顶，其余相对顺序不变。"""
        if not plan.risk_sell_codes and not plan.boost_codes:
            return stock_scan
        boosted: list[dict[str, Any]] = []
        rest: list[dict[str, Any]] = []
        excluded: list[str] = []
        for row in stock_scan.candidates:
            try:
                raw = normalized_security_code(str(row["code"]))
            except ValueError:
                rest.append(row)
                continue
            if raw in plan.risk_sell_codes:
                excluded.append(raw)
                continue
            (boosted if raw in plan.boost_codes else rest).append(row)
        if excluded:
            LOGGER.info("新闻风险研判已从当日候选剔除: %s", sorted(excluded))
        if boosted:
            LOGGER.info("新闻利好研判候选置顶: %s", sorted(plan.boost_codes))
        return StockScanResult(
            input_count=stock_scan.input_count,
            candidates=tuple(boosted + rest),
            filter_counts=dict(stock_scan.filter_counts),
        )

    def _apply_news_overlay_to_target(
        self,
        target: TargetPortfolio,
        plan: NewsOverlayPlan,
    ) -> TargetPortfolio:
        """目标组合剔除风险标的，并对入选利好标的加权后封顶。"""
        if not plan.risk_sell_codes and not plan.boost_codes:
            return target
        kept: list[TargetPosition] = []
        dropped: list[str] = []
        for position in target.positions:
            if normalized_security_code(position.code) in plan.risk_sell_codes:
                dropped.append(position.code)
                continue
            kept.append(position)
        for code in dropped:
            verdict = plan.verdicts.get(normalized_security_code(code))
            LOGGER.info(
                "新闻风险研判已从目标组合剔除: code=%s confidence=%s reason=%s",
                code,
                f"{verdict.confidence:.2f}" if verdict else "N/A",
                verdict.reason if verdict else "N/A",
            )
        adjusted: list[TargetPosition] = []
        for position in kept:
            raw = normalized_security_code(position.code)
            if raw not in plan.boost_codes or position.asset_type != "stock":
                adjusted.append(position)
                continue
            others = sum(
                item.target_weight for item in kept if item is not position
            )
            boosted = min(
                position.target_weight * NEWS_BOOST_FACTOR,
                self.config.max_single_stock,
                self.config.max_total_position - others,
            )
            verdict = plan.verdicts.get(raw)
            if boosted <= position.target_weight:
                LOGGER.info(
                    "新闻利好加权触及单票或总仓位上限，保持原权重: code=%s "
                    "weight=%.2f%% confidence=%s reason=%s",
                    position.code,
                    position.target_weight * 100,
                    f"{verdict.confidence:.2f}" if verdict else "N/A",
                    verdict.reason if verdict else "N/A",
                )
                adjusted.append(position)
                continue
            LOGGER.info(
                "新闻利好加权: code=%s weight=%.2f%%->%.2f%% confidence=%s "
                "reason=%s sources=%s",
                position.code,
                position.target_weight * 100,
                boosted * 100,
                f"{verdict.confidence:.2f}" if verdict else "N/A",
                verdict.reason if verdict else "N/A",
                list(verdict.sources) if verdict else [],
            )
            adjusted.append(
                replace(
                    position,
                    target_weight=round(boosted, 6),
                    reason=f"{position.reason} NEWS_BOOST",
                )
            )
        exposure = sum(position.target_weight for position in adjusted)
        return replace(
            target,
            positions=tuple(adjusted),
            cash_weight=round(1 - exposure, 6),
        )

    def _news_exit_verdicts(self, signal_date: str) -> dict[str, NewsVerdict]:
        """读取信号日研判中可执行的风险卖出结论，供 T+1 执行时复用。"""
        overlay = load_news_overlay(signal_date, self.root_dir)
        if overlay is None:
            return {}
        return {
            verdict.code: verdict
            for verdict in overlay.verdicts
            if verdict.action == "risk_sell" and verdict.is_actionable
        }

    def _retag_news_exit_order(
        self, order: OrderIntent, exits: Mapping[str, NewsVerdict]
    ) -> OrderIntent:
        """把统一分配器生成的风险标的卖单改标为 NEWS_RISK_EXIT。"""
        if order.action != "sell":
            return order
        verdict = exits.get(normalized_security_code(order.code))
        if verdict is None:
            return order
        LOGGER.info(
            "新闻风险卖出执行: code=%s confidence=%.2f reason=%s sources=%s",
            order.code,
            verdict.confidence,
            verdict.reason,
            list(verdict.sources),
        )
        return replace(
            order,
            reason="NEWS_RISK_EXIT",
            metadata={
                **order.metadata,
                "news_confidence": verdict.confidence,
                "news_reason": verdict.reason,
                "news_sources": list(verdict.sources),
            },
        )

    def generate_close_target(
        self, trade_date: str, *, force: bool = False
    ) -> TargetPortfolio | None:
        """在配置周期的收盘生成目标组合并持久化，不直接下单。"""
        if not force and not self.is_rebalance_due(trade_date):
            LOGGER.info("%s 非 robust_v2 调仓收盘，跳过目标生成", trade_date)
            return None
        snapshot = self.broker.query_snapshot()
        signal_data = self.data.load_signal_data(trade_date, snapshot.total_value)
        stock_scan = self.strategy.scan_stocks(
            signal_data.stock_history,
            signal_data.snapshot,
            snapshot.total_value,
            signal_data.names,
        )
        # 新闻研判覆盖层在策略选仓前后介入:先剔除/置顶候选，再修正目标权重。
        overlay = load_news_overlay(trade_date, self.root_dir)
        news_plan = self._plan_news_overlay(
            overlay,
            positions=self.broker.query_positions(),
            candidate_codes=tuple(str(row["code"]) for row in stock_scan.candidates),
            trade_date=trade_date,
        )
        target = self.strategy.generate_target(
            signal_data.snapshot,
            signal_data.etf_history,
            signal_data.stock_history,
            snapshot.total_value,
            signal_data.names,
            stock_scan_result=self._apply_news_overlay_to_scan(stock_scan, news_plan),
        )
        target = self._apply_news_overlay_to_target(target, news_plan)
        signal_id = self.ledger.record_signal(target)
        if news_plan.exit_orders:
            LOGGER.info(
                "新闻风险卖出计划已并入 T+1 统一执行: %s",
                [
                    (order.code, order.shares, order.reason)
                    for order in news_plan.exit_orders
                ],
            )
        selected_stocks = tuple(
            position.code
            for position in target.positions
            if position.asset_type == "stock"
        )
        try:
            self._save_stock_scan(
                signal_data,
                stock_scan,
                account_value=snapshot.total_value,
                mode="scheduled",
                selected_codes=selected_stocks,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            LOGGER.exception("保存个股扫描快照失败，但已记录的目标组合保持有效")
        LOGGER.info(
            "收盘目标已记录: signal_id=%s date=%s exposure=%.2f%% cash=%.2f%% positions=%s",
            signal_id,
            target.signal_date,
            target.exposure * 100,
            target.cash_weight * 100,
            [position.code for position in target.positions],
        )
        return target

    def execute_pending_target(
        self,
        execution_date: str,
        *,
        current_time: datetime | None = None,
    ) -> ExecutionOutcome:
        """仅在信号的下一交易日执行，并对暂时性市场失败持续重试。"""
        execution_time = current_time or now_local()
        pending = self.ledger.pending_target(execution_date, now=execution_time)
        if pending is None:
            return ExecutionOutcome(None, None, None, (), "没有待执行目标")
        _candidate_id, candidate = pending
        expected_signal_date = previous_trading_day(execution_date)
        self.ledger.expire_pending_targets_before(
            expected_signal_date,
            strategy_version=self.config.strategy_version,
        )
        if candidate.signal_date != expected_signal_date:
            LOGGER.error(
                "拒绝跨日追旧目标: signal=%s expected=%s execute=%s",
                candidate.signal_date,
                expected_signal_date,
                execution_date,
            )
            return ExecutionOutcome(
                None,
                candidate,
                None,
                (),
                "待执行目标不是上一交易日信号，已拒绝追单",
            )

        claimed = self.ledger.claim_pending_target(
            execution_date,
            signal_date=expected_signal_date,
            now=execution_time,
        )
        if claimed is None:
            return ExecutionOutcome(None, None, None, (), "目标正在执行或等待重试")

        signal_id = claimed.signal_id
        target = claimed.target
        allocation: AllocationResult | None = None
        reports: tuple[ExecutionReport, ...] = ()
        try:
            positions = self.broker.query_positions()
            codes = {position.code for position in target.positions} | set(positions)
            snapshot, history, quotes = self.data.load_execution_data(
                codes, target.signal_date
            )
            contexts, quote_rejected = self._validated_execution_quotes(
                codes,
                quotes,
                execution_date=execution_date,
                current_time=execution_time,
            )
            healthy_quotes = {
                code: quote
                for code, quote in quotes.items()
                if normalized_security_code(code) in contexts
            }
            alignment = validate_realtime_alignment(snapshot, history, healthy_quotes)
            aligned_codes = {
                normalized_security_code(code) for code in alignment.valid_codes
            }
            incomplete_position_quotes = sorted(
                code
                for code in positions
                if normalized_security_code(code) not in contexts
                or normalized_security_code(code) not in aligned_codes
            )
            prices = {
                context.code: context.current_price for context in contexts.values()
            }
            buy_risk_block = self._buy_risk_block_reason(
                prices,
                execution_date=execution_date,
            )
            if incomplete_position_quotes:
                buy_risk_block = "持仓实时估值不完整，暂停所有新买入: " + ",".join(
                    incomplete_position_quotes
                )
            allocation = self.allocator.allocate(
                target,
                cash=self.broker.query_cash(),
                positions=positions,
                prices=prices,
                execution_date=execution_date,
                tradable_codes=set(alignment.valid_codes),
            )
            # 信号日研判中的风险卖出标的在 T+1 执行时改标为 NEWS_RISK_EXIT。
            news_exits = self._news_exit_verdicts(target.signal_date)
            checked_orders: list[OrderIntent] = []
            market_skipped: dict[str, str] = {}
            risk_skipped: dict[str, str] = {}
            if buy_risk_block:
                current_codes = {normalized_security_code(code) for code in positions}
                risk_skipped.update(
                    {
                        position.code: buy_risk_block
                        for position in target.positions
                        if normalized_security_code(position.code) not in current_codes
                    }
                )
            for order in allocation.orders:
                if order.action == "buy" and buy_risk_block:
                    risk_skipped[order.code] = buy_risk_block
                    continue
                context = contexts.get(normalized_security_code(order.code))
                if context is None:
                    market_skipped[order.code] = quote_rejected.get(
                        order.code,
                        "实时行情未通过执行校验",
                    )
                    continue
                tradable, reason = order_is_tradable(order.action, context)
                if not tradable:
                    market_skipped[order.code] = reason
                    continue
                order = self._retag_news_exit_order(order, news_exits)
                retry_key = hashlib.sha256(
                    (
                        f"{order.idempotency_key}|{execution_date}|"
                        f"attempt={claimed.attempt_count}"
                    ).encode("utf-8")
                ).hexdigest()
                checked_orders.append(
                    replace(
                        order,
                        price=context.current_price,
                        idempotency_key=retry_key,
                        metadata={
                            **order.metadata,
                            "data_health_checked": True,
                            "alignment_snapshot_hash": snapshot.data_hash,
                            "execution_quote": context.to_dict(),
                            "signal_attempt": claimed.attempt_count,
                        },
                    )
                )
            # 分配预算含预计卖出回款；必须等全部计划卖单实际足额成交后再买入。
            pending_sales = {
                normalized_security_code(order.code)
                for order in allocation.orders
                if order.action == "sell"
            }
            execution_reports: list[ExecutionReport] = []
            for order in checked_orders:
                if order.action == "buy" and pending_sales:
                    market_skipped[order.code] = (
                        "计划卖单未全部成交，暂停使用预计回款买入"
                    )
                    continue
                report = self.broker.place_order(order)
                execution_reports.append(report)
                if order.action == "sell":
                    if report.is_success and report.shares == order.shares:
                        pending_sales.discard(normalized_security_code(order.code))
                    else:
                        market_skipped[order.code] = (
                            f"卖单仅成交 {report.shares}/{order.shares}，等待剩余数量"
                            if report.is_success
                            else report.message or "卖单未全部成交"
                        )
            reports = tuple(execution_reports)
            transient = {
                **quote_rejected,
                **alignment.rejected,
                **market_skipped,
                **{
                    report.code: report.message or report.status
                    for report in reports
                    if report.status != "filled"
                },
            }
            all_skipped = {**allocation.skipped, **risk_skipped, **transient}
            allocation = replace(allocation, skipped=all_skipped)
            if transient:
                reason = "; ".join(
                    f"{code}:{message}" for code, message in sorted(transient.items())
                )
                self.ledger.mark_signal_retryable(
                    signal_id,
                    reason,
                    retry_after_seconds=ROBUST_V2_SIGNAL_RETRY_SECONDS,
                    now=execution_time,
                )
                LOGGER.warning(
                    "目标执行暂未完成，等待重试: signal=%s attempt=%d reasons=%s",
                    signal_id,
                    claimed.attempt_count,
                    transient,
                )
                return ExecutionOutcome(
                    signal_id,
                    target,
                    allocation,
                    reports,
                    "目标部分执行或行情受限，已安排重试",
                )

            self.ledger.mark_signal_completed(signal_id, "目标已完成或无需调仓")
            if risk_skipped:
                LOGGER.warning("目标买入被账户熔断阻止: %s", risk_skipped)
            LOGGER.info(
                "目标执行完成: signal=%s attempt=%d orders=%d filled=%d skipped=%s",
                signal_id,
                claimed.attempt_count,
                len(reports),
                sum(report.status == "filled" for report in reports),
                allocation.skipped,
            )
            return ExecutionOutcome(
                signal_id,
                target,
                allocation,
                reports,
                "目标执行完成",
            )
        except Exception as exc:
            # 验证类/结构性错误（如目标违反仓位约束）重试也不会自愈：
            # 达到上限后标记 failed 并移出重试队列，避免同一信号无限重试。
            if (
                isinstance(exc, ValueError)
                and claimed.attempt_count >= ROBUST_V2_SIGNAL_STRUCTURAL_MAX_ATTEMPTS
            ):
                reason = f"结构性错误连续 {claimed.attempt_count} 次执行失败: {exc}"
                self.ledger.mark_signal_failed(signal_id, reason)
                LOGGER.error(
                    "目标组合结构性校验失败，已达到 %d 次上限，信号标记 failed "
                    "并移出重试队列: signal=%s attempt=%d error=%s",
                    ROBUST_V2_SIGNAL_STRUCTURAL_MAX_ATTEMPTS,
                    signal_id,
                    claimed.attempt_count,
                    exc,
                )
                return ExecutionOutcome(
                    signal_id,
                    target,
                    allocation,
                    reports,
                    f"目标组合结构性错误，已停止重试: {exc}",
                )
            self.ledger.mark_signal_retryable(
                signal_id,
                str(exc),
                retry_after_seconds=ROBUST_V2_SIGNAL_RETRY_SECONDS,
                now=execution_time,
            )
            LOGGER.exception(
                "目标执行异常，信号已保留重试: signal=%s attempt=%d",
                signal_id,
                claimed.attempt_count,
            )
            return ExecutionOutcome(
                signal_id,
                target,
                allocation,
                reports,
                f"目标执行异常，已安排重试: {exc}",
            )

    def monitor_catastrophic_stops(
        self,
        trade_date: str,
        *,
        current_time: datetime | None = None,
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
        contexts, quote_rejected = self._validated_execution_quotes(
            codes,
            quotes,
            execution_date=trade_date,
            current_time=current_time or now_local(),
        )
        if quote_rejected:
            LOGGER.warning("灾难止损实时行情不可用: %s", quote_rejected)
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
        healthy_quotes = {
            code: quote
            for code, quote in quotes.items()
            if normalized_security_code(code) in contexts
        }
        alignment = validate_realtime_alignment(
            health_snapshot,
            history,
            healthy_quotes,
        )
        prices = {
            context.code: context.current_price
            for raw, context in contexts.items()
            if any(
                normalized_security_code(code) == raw for code in alignment.valid_codes
            )
        }
        orders = self.strategy.catastrophic_stop_orders(positions, prices, trade_date)
        checked_orders = []
        for order in orders:
            context = contexts.get(normalized_security_code(order.code))
            if context is None:
                continue
            tradable, reason = order_is_tradable(order.action, context)
            if not tradable:
                LOGGER.warning("灾难止损暂不可成交: %s", reason)
                continue
            checked_orders.append(
                replace(
                    order,
                    price=context.current_price,
                    metadata={
                        **order.metadata,
                        "data_health_checked": True,
                        "alignment_snapshot_hash": health_snapshot.data_hash,
                        "execution_quote": context.to_dict(),
                    },
                )
            )
        return tuple(self.broker.place_order(order) for order in checked_orders)

    def _execute_news_risk_exits(
        self,
        verdicts: tuple[NewsVerdict, ...],
        trade_date: str,
        *,
        current_time: datetime,
    ) -> tuple[tuple[ExecutionReport, ...], frozenset[str], tuple[NewsVerdict, ...]]:
        """对研判风险标的立即卖出;返回(成交回报, 已终结代码, 待重试研判)。

        行情暂缺或跌停不可卖属盘中可恢复状态，保留待下一轮监控重试;
        未持仓与 T+1 锁定视为已终结，不再重复触发。
        """
        positions = self.broker.query_positions()
        held = self._held_position_index(positions)
        resolved: set[str] = set()
        retry: list[NewsVerdict] = []
        targets: list[tuple[NewsVerdict, str, Mapping[str, Any]]] = []
        for verdict in verdicts:
            entry = held.get(verdict.code)
            if entry is None:
                LOGGER.debug(
                    "新闻风险研判标的未持仓，盘中忽略: code=%s action=risk_sell "
                    "confidence=%.2f reason=%s sources=%s",
                    verdict.code,
                    verdict.confidence,
                    verdict.reason,
                    list(verdict.sources),
                )
                resolved.add(verdict.code)
                continue
            targets.append((verdict, entry[0], entry[1]))
        if not targets:
            return (), frozenset(resolved), ()
        codes = {code for _, code, _ in targets}
        previous = (
            datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=1)
        ).strftime("%Y%m%d")
        snapshot, _history, quotes = self.data.load_execution_data(codes, previous)
        contexts, quote_rejected = self._validated_execution_quotes(
            codes,
            quotes,
            execution_date=trade_date,
            current_time=current_time,
        )
        if quote_rejected:
            LOGGER.warning(
                "新闻风险卖出实时行情暂不可用，本轮稍后重试: %s", quote_rejected
            )
        reports: list[ExecutionReport] = []
        for verdict, code, position in targets:
            sellable = int(
                position.get("sellable_qty", position.get("shares", 0)) or 0
            )
            if sellable <= 0:
                LOGGER.info(
                    "新闻风险卖出受 T+1 限制当日无可卖数量，交给后续执行路径: "
                    "code=%s confidence=%.2f reason=%s sources=%s",
                    code,
                    verdict.confidence,
                    verdict.reason,
                    list(verdict.sources),
                )
                resolved.add(verdict.code)
                continue
            context = contexts.get(verdict.code)
            if context is None:
                LOGGER.warning(
                    "新闻风险卖出缺少有效实时行情，本轮稍后重试: code=%s "
                    "confidence=%.2f reason=%s sources=%s",
                    code,
                    verdict.confidence,
                    verdict.reason,
                    list(verdict.sources),
                )
                retry.append(verdict)
                continue
            tradable, reason = order_is_tradable("sell", context)
            if not tradable:
                LOGGER.warning(
                    "新闻风险卖出暂不可成交，本轮稍后重试: code=%s 原因=%s "
                    "confidence=%.2f reason=%s",
                    code,
                    reason,
                    verdict.confidence,
                    verdict.reason,
                )
                retry.append(verdict)
                continue
            order = self._news_exit_order(
                code,
                position,
                verdict,
                trade_date,
                price=context.current_price,
                shares=sellable,
                source="robust_v2_monitor",
                extra_metadata={
                    "data_health_checked": True,
                    "alignment_snapshot_hash": snapshot.data_hash,
                    "execution_quote": context.to_dict(),
                },
            )
            report = self.broker.place_order(order)
            reports.append(report)
            resolved.add(verdict.code)
            LOGGER.info(
                "新闻风险卖出已提交: code=%s action=risk_sell confidence=%.2f "
                "reason=%s sources=%s shares=%d price=%.2f status=%s message=%s",
                code,
                verdict.confidence,
                verdict.reason,
                list(verdict.sources),
                order.shares,
                order.price,
                report.status,
                report.message,
            )
        return tuple(reports), frozenset(resolved), tuple(retry)

    def monitor_news_overlay(
        self,
        trade_date: str,
        *,
        current_time: datetime | None = None,
    ) -> tuple[ExecutionReport, ...]:
        """盘中监控研判文件 mtime，新出现的高置信风险持仓立即强制卖出。"""
        path = news_overlay_path(trade_date, self.root_dir)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return ()
        state = self._news_monitor_state.get(trade_date)
        if state is not None and state.mtime == mtime and not state.pending:
            return ()
        pending: list[NewsVerdict] = list(state.pending) if state is not None else []
        processed: set[str] = set(state.processed) if state is not None else set()
        if state is None or state.mtime != mtime:
            overlay = load_news_overlay(trade_date, self.root_dir)
            if overlay is None:
                LOGGER.warning(
                    "新闻研判文件 mtime 已变化但解析失败，保留旧状态等待再次覆盖: %s",
                    path,
                )
                return ()
            known = processed | {verdict.code for verdict in pending}
            fresh = [
                verdict
                for verdict in overlay.verdicts
                if verdict.action == "risk_sell"
                and verdict.is_actionable
                and verdict.code not in known
            ]
            if fresh:
                LOGGER.info(
                    "新闻研判文件更新，新增风险卖出研判: %s",
                    [
                        (verdict.code, f"{verdict.confidence:.2f}")
                        for verdict in fresh
                    ],
                )
            pending.extend(fresh)
        reports: tuple[ExecutionReport, ...] = ()
        if pending:
            reports, resolved, retry = self._execute_news_risk_exits(
                tuple(pending),
                trade_date,
                current_time=current_time or now_local(),
            )
            processed |= resolved
            pending = list(retry)
        # 日期滚动后清理旧状态，避免长期运行状态膨胀。
        self._news_monitor_state = {
            key: value
            for key, value in self._news_monitor_state.items()
            if key >= trade_date
        }
        self._news_monitor_state[trade_date] = NewsMonitorState(
            mtime=mtime,
            processed=frozenset(processed),
            pending=tuple(pending),
        )
        return reports

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
        report_path = save_daily_ledger_report(report, Path(REPORT_DIR))
        backup_dir = Path(ROBUST_V2_BACKUP_DIR)
        backup_path = self.ledger.backup_to(backup_dir / f"paper_v2_{trade_date}.db")
        retention_cutoff = now_local().timestamp() - (
            ROBUST_V2_BACKUP_RETENTION_DAYS * 86_400
        )
        for old_path in backup_dir.glob("paper_v2_*.db"):
            try:
                if (
                    old_path != backup_path
                    and old_path.stat().st_mtime < retention_cutoff
                ):
                    old_path.unlink()
            except OSError as exc:
                LOGGER.warning("清理过期账本备份失败 %s: %s", old_path, exc)
        LOGGER.info("账本每日备份: %s", backup_path)
        return report_path


def _configure_runtime(root_dir: Path) -> None:
    """初始化统一日志配置。"""
    setup_logger("robust_runner", rotating_files=("robust_v2.log",))
    LOGGER.info("运行目录: %s", root_dir)


def _is_trading_day(date: str, ignore_calendar: bool) -> bool:
    """读取交易日历；不可验证时向上抛错并停止交易。"""
    if ignore_calendar:
        return True
    return bool(calendar_is_trading_day(date))


def _latest_completed_trade_date(reference: datetime | None = None) -> str:
    """返回最近一个已完成收盘的交易日，避免盘中把当日当成完整日线。"""
    current = reference or now_local()
    cursor = current.date()
    date_text = cursor.strftime("%Y%m%d")
    if current.time() < dt_time(15, 5) or not _is_trading_day(
        date_text, ignore_calendar=False
    ):
        cursor -= timedelta(days=1)
    for _ in range(15):
        date_text = cursor.strftime("%Y%m%d")
        if _is_trading_day(date_text, ignore_calendar=False):
            return date_text
        cursor -= timedelta(days=1)
    raise RuntimeError("无法确定最近一个已完成收盘的交易日")


def _parse_observation_end_date(value: str | None) -> date_type | None:
    """解析虚拟盘观察期结束日；结束日当天仍保持运行。"""
    if value is None or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError("观察期结束日必须为有效的 YYYYMMDD") from exc


def _startup_log(
    broker: BrokerAdapter,
    ledger: PaperLedger,
    config: RobustV2Config,
) -> tuple[str, str]:
    """输出代码、配置、账本、权限、T+1 和容器标识。"""
    commit = _safe_git_commit(ROOT_DIR)
    digest = _config_hash(config)
    LOGGER.info(
        "STARTUP commit=%s config_hash=%s broker=%s ledger=%s account=%s T+1=%s "
        "permissions=main:%s,chinext:%s,star:%s container=%s",
        commit,
        digest,
        broker.broker_name,
        ledger.path,
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
    observation_end_date: str | None = None,
) -> int:
    """运行持有 SQLite 写租约的唯一低频守护实例。"""
    if poll_seconds <= 0 or poll_seconds > 60:
        raise ValueError("轮询间隔必须位于 1-60 秒")
    observation_end = _parse_observation_end_date(observation_end_date)
    if observation_end is not None and now_local().date() > observation_end:
        LOGGER.info("虚拟盘观察期已于 %s 结束，不再启动守护实例", observation_end)
        return 0
    validate_startup(runner.config)
    stop_event = threading.Event()

    def _stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    previous_sigterm = signal.signal(signal.SIGTERM, _stop)
    previous_sigint = signal.signal(signal.SIGINT, _stop)
    holder_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    lease_acquired = False
    try:
        runner.ledger.acquire_lease(holder_id, ROBUST_V2_LEASE_TTL_SECONDS)
        lease_acquired = True
        heartbeat = LeaseHeartbeat(
            runner.ledger,
            holder_id,
            ROBUST_V2_LEASE_TTL_SECONDS,
        )
        commit, digest = _startup_log(runner.broker, runner.ledger, runner.config)
        run = runner.ledger.start_run(
            strategy_version=runner.config.strategy_version,
            code_commit=commit,
            config_hash=digest,
            data_version="pending-close-snapshot",
            container_id=socket.gethostname(),
        )
        heartbeat.start()
    except Exception:
        if lease_acquired:
            runner.ledger.release_lease(holder_id)
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
        raise
    status = "completed"
    last_idle_state = ""
    try:
        while not stop_event.is_set():
            heartbeat.raise_if_failed()
            now = now_local()
            if observation_end is not None and now.date() > observation_end:
                LOGGER.info(
                    "虚拟盘观察期已于 %s 结束，正常停止守护实例", observation_end
                )
                break
            trade_date = now.strftime("%Y%m%d")
            if not _is_trading_day(trade_date, ignore_calendar):
                idle_state = f"non_trading:{trade_date}"
                if idle_state != last_idle_state:
                    LOGGER.info("%s 非交易日，只续租不交易", trade_date)
                    last_idle_state = idle_state
            elif is_strategy_execution_session(now):
                last_idle_state = ""
                runner.execute_pending_target(trade_date, current_time=now)
                runner.monitor_catastrophic_stops(trade_date, current_time=now)
                runner.monitor_news_overlay(trade_date, current_time=now)
            elif now.time() >= dt_time(15, 5):
                last_idle_state = "after_close"
                if runner.ledger.claim_daily_job("close_cycle", trade_date):
                    try:
                        target = runner.generate_close_target(trade_date)
                        data_version = (
                            target.source_snapshot_hash
                            if target is not None
                            else (
                                runner.ledger.latest_signal_hash(
                                    runner.config.strategy_version
                                )
                                or "no-signal"
                            )
                        )
                        path = runner.record_close_and_report(
                            trade_date,
                            data_version,
                        )
                    except Exception as exc:
                        runner.ledger.finish_daily_job(
                            "close_cycle",
                            trade_date,
                            error=str(exc),
                            retry_after_seconds=ROBUST_V2_DAILY_JOB_RETRY_SECONDS,
                        )
                        LOGGER.exception(
                            "收盘任务失败，本日下一轮将重试: date=%s",
                            trade_date,
                        )
                    else:
                        runner.ledger.finish_daily_job("close_cycle", trade_date)
                        LOGGER.info("收盘日报: %s", path)
                if runner.ledger.daily_job_completed(
                    "close_cycle", trade_date
                ) and runner.ledger.claim_daily_job("daily_candidate_scan", trade_date):
                    try:
                        scan = runner.ensure_daily_candidate_scan(trade_date)
                    except Exception as exc:
                        runner.ledger.finish_daily_job(
                            "daily_candidate_scan",
                            trade_date,
                            error=str(exc),
                            retry_after_seconds=ROBUST_V2_DAILY_JOB_RETRY_SECONDS,
                        )
                        LOGGER.exception(
                            "每日候选观察扫描失败，本日下一轮将重试: date=%s",
                            trade_date,
                        )
                    else:
                        runner.ledger.finish_daily_job(
                            "daily_candidate_scan", trade_date
                        )
                        if scan is not None:
                            LOGGER.info(
                                "每日候选观察已更新: date=%s eligible=%d selected=%s",
                                trade_date,
                                scan.eligible_count,
                                list(scan.selected_codes),
                            )
            else:
                idle_state = classify_trading_session(now)
                if idle_state != last_idle_state:
                    LOGGER.info(
                        "当前市场阶段=%s；只保持租约，不生成或执行订单",
                        idle_state,
                    )
                    last_idle_state = idle_state

            heartbeat.raise_if_failed()
            if once:
                break
            stop_event.wait(poll_seconds)
    except Exception:
        status = "failed"
        LOGGER.exception("robust_v2 守护实例异常退出")
        return 2
    finally:
        try:
            runner.ledger.finish_run(run.run_id, status=status)
        finally:
            try:
                heartbeat.stop()
            except RuntimeError:
                LOGGER.exception("停止租约续租线程时发现故障")
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
        choices=[
            "daemon",
            "signal",
            "execute",
            "monitor",
            "report",
            "status",
            "preview-scan",
        ],
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
    parser.add_argument(
        "--observation-end-date",
        help="虚拟盘观察期结束日 YYYYMMDD；结束日次日守护进程正常退出",
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
    trade_date = args.date or (
        _latest_completed_trade_date()
        if args.command == "preview-scan"
        else today_yyyymmdd()
    )
    try:
        if args.command == "daemon":
            return run_daemon(
                runner,
                once=args.once,
                ignore_calendar=args.ignore_calendar,
                poll_seconds=args.poll_seconds,
                observation_end_date=args.observation_end_date,
            )
        commit, digest = _startup_log(broker, runner.ledger, config)
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
        if args.command == "preview-scan":
            runner.preview_stock_scan(trade_date)
            return 0

        holder_id = (
            f"manual:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        runner.ledger.acquire_lease(holder_id, ROBUST_V2_LEASE_TTL_SECONDS)
        try:
            run = runner.ledger.start_run(
                strategy_version=config.strategy_version,
                code_commit=commit,
                config_hash=digest,
                data_version=f"manual-{args.command}",
                container_id=socket.gethostname(),
            )
            heartbeat = LeaseHeartbeat(
                runner.ledger,
                holder_id,
                ROBUST_V2_LEASE_TTL_SECONDS,
            )
            heartbeat.start()
        except Exception:
            runner.ledger.release_lease(holder_id)
            raise
        command_status = "completed"
        try:
            heartbeat.raise_if_failed()
            command_now = now_local()
            if args.command in {
                "execute",
                "monitor",
            } and not is_strategy_execution_session(command_now):
                raise RuntimeError(
                    "手工执行只允许在 09:35-11:30 或 13:00-15:00 连续竞价时段"
                )
            if args.command == "signal":
                runner.generate_close_target(trade_date, force=args.force)
            elif args.command == "execute":
                runner.execute_pending_target(trade_date, current_time=command_now)
            elif args.command == "monitor":
                runner.monitor_catastrophic_stops(trade_date, current_time=command_now)
                runner.monitor_news_overlay(trade_date, current_time=command_now)
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
            try:
                runner.ledger.finish_run(run.run_id, status=command_status)
            finally:
                try:
                    try:
                        heartbeat.stop()
                    except RuntimeError:
                        LOGGER.exception("停止手工任务租约续租线程时发现故障")
                finally:
                    runner.ledger.release_lease(holder_id)
        return 0
    finally:
        loader.close()
        broker.close()


if __name__ == "__main__":
    raise SystemExit(main())
