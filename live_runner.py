"""
A 股虚拟盘实时盯盘系统。

流程：
1. 开盘前全市场扫描，形成候选池。
2. 盘中低频盯盘，持仓止损/止盈，候选股触发买入信号。
3. 所有订单先经过风控，再交给 Broker；当前默认只使用虚拟盘 Broker。
4. 信号、风控、成交、快照均写入结构化事件，便于一个月观察期复盘。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config.settings import (  # noqa: E402
    BROKER_MODE,
    CASH_BUFFER,
    DEFAULT_INDUSTRY_INDEX_POOL,
    DEFAULT_RPS_ETF_POOL,
    ENABLE_RPS_ROTATION,
    INITIAL_CAPITAL,
    ATR_PERIOD,
    LIVE_INITIAL_SCAN_DELAY_MINUTES,
    LIVE_SCAN_INTERVAL_SECONDS,
    LIVE_WATCH_INTERVAL_SECONDS,
    LOT_SIZE,
    MAX_SINGLE_ETF,
    MAX_SINGLE_STOCK,
    MAX_TOTAL_POSITION,
    REBUY_COOLDOWN_SECONDS,
    REPORT_DIR,
    RPS_HISTORY_DAYS,
    RPS_STATE_FILE,
    ENABLE_MARKET_REGIME,
    MARKET_INDEX_CODE,
    MARKET_REGIME_MA,
    get_industry_index_names,
    get_rps_etf_codes,
    normalize_a_share_code,
)
from config.logging_setup import setup_logger  # noqa: E402
from config.time_utils import now_local, today_yyyymmdd  # noqa: E402
from data.ak_loader import AKDataLoader  # noqa: E402
from data.holidays import is_trading_day as is_calendar_trading_day  # noqa: E402
from risk.control import RiskController  # noqa: E402
from strategies.combo_signal import ComboSignalStrategy  # noqa: E402
from strategies.exit_rules import evaluate_exit  # noqa: E402
from strategies.indicators import calculate_atr  # noqa: E402
from strategies.market_regime import is_risk_on  # noqa: E402
from strategies.market_scanner import MarketScanner  # noqa: E402
from strategies.rps_rotation import RPSRotationStrategy, calculate_rps_scores  # noqa: E402
from trading.brokers import PaperBrokerAdapter, create_broker  # noqa: E402
from trading.models import ExecutionReport, OrderIntent, RiskDecision  # noqa: E402
from trading.observability import EventRecorder  # noqa: E402


def _setup_logger() -> logging.Logger:
    """初始化实时运行日志。

    ``live.log`` 为累计日志,使用轮转避免无限增长;``live_today.log`` 为当日日志。
    """
    return setup_logger(
        "live_runner",
        rotating_files=("live.log",),
        plain_files=("live_today.log",),
    )


logger = _setup_logger()


class SharedState:
    """线程间共享候选池。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.top_stocks: list[dict[str, Any]] = []
        self.candidate_codes: list[str] = []
        self.candidate_hist: dict[str, pd.DataFrame] = {}
        self.rejected_stocks: dict[str, float] = {}  # 代码 -> 冷却结束时间戳
        self.rejected_exits: dict[str, float] = {}
        self.rebuy_cooldown: dict[str, float] = {}  # 卖出后再买冷却 代码 -> 冷却结束时间戳
        self.last_scan_time: datetime | None = None
        self.scanning = False
        self.risk_on = True  # 大盘择时状态,由扫描线程刷新

    def update(self, stocks: list[dict[str, Any]]) -> None:
        """更新候选股池。"""
        with self._lock:
            self.top_stocks = stocks
            self.candidate_codes = [s["code"] for s in stocks]
            self.rejected_stocks.clear()  # 新一轮扫描后重新评估候选买入
            self.last_scan_time = now_local()

    def update_hist(self, code: str, df: pd.DataFrame) -> None:
        """更新候选股历史数据缓存。"""
        with self._lock:
            self.candidate_hist[code] = df

    def get_hist(self, code: str) -> pd.DataFrame | None:
        """读取候选股历史数据。"""
        with self._lock:
            return self.candidate_hist.get(code)

    def get_candidates(self) -> list[str]:
        """读取候选代码。"""
        with self._lock:
            return list(self.candidate_codes)

    def get_stocks(self) -> list[dict[str, Any]]:
        """读取候选股详情。"""
        with self._lock:
            return list(self.top_stocks)

    def is_scanning(self) -> bool:
        """是否正在扫描。"""
        with self._lock:
            return self.scanning

    def set_scanning(self, value: bool) -> None:
        """设置扫描状态。"""
        with self._lock:
            self.scanning = value

    def is_risk_on(self) -> bool:
        """读取大盘择时状态(risk-off 时暂停开新仓)。"""
        with self._lock:
            return self.risk_on

    def set_risk_on(self, value: bool) -> None:
        """设置大盘择时状态。"""
        with self._lock:
            self.risk_on = value

    def is_exit_cooling_down(self, code: str) -> bool:
        """判断卖出拒单是否仍处于冷却期。"""
        with self._lock:
            return time.time() < self.rejected_exits.get(code, 0)

    def set_exit_cooldown(self, code: str, seconds: int = 300) -> None:
        """设置卖出拒单冷却，避免同一持仓在盯盘循环中重复刷屏。"""
        with self._lock:
            self.rejected_exits[code] = time.time() + seconds

    def mark_rebuy_cooldown(self, code: str, seconds: int) -> None:
        """标记某标的刚被卖出,在冷却期内禁止再次开仓(防止日内来回刷单)。"""
        with self._lock:
            self.rebuy_cooldown[code] = time.time() + seconds

    def is_rebuy_cooling_down(self, code: str) -> bool:
        """判断某标的是否仍处于卖出后的再买冷却期。"""
        with self._lock:
            return time.time() < self.rebuy_cooldown.get(code, 0)


def normalize_code(code: str) -> str:
    """统一为不带市场前缀的沪深 A 股 6 位代码。"""
    try:
        return normalize_a_share_code(code)
    except ValueError:
        logger.warning("忽略无法归一化的非沪深 A 股代码: %s", code)
        return str(code).strip().lower()


def _holding_days(buy_date: str | None, today: str) -> int:
    """计算持仓自然日数(用于时间止损),解析失败返回 0。"""
    if not buy_date:
        return 0
    try:
        d0 = datetime.strptime(str(buy_date), "%Y%m%d")
        d1 = datetime.strptime(str(today), "%Y%m%d")
        return max(0, (d1 - d0).days)
    except (ValueError, TypeError):
        return 0


def is_trading_day(date_str: str | None = None) -> bool:
    """判断是否为交易日。"""
    target = date_str or today_yyyymmdd()
    try:
        return is_calendar_trading_day(target)
    except Exception as exc:
        logger.warning("交易日历判断失败，回退到周末判断: %s", exc)
        return datetime.strptime(target, "%Y%m%d").weekday() < 5


def _initial_scan_time(now: datetime, delay_minutes: int = LIVE_INITIAL_SCAN_DELAY_MINUTES) -> datetime:
    """返回首次全市场扫描时间。

    首次扫描放在连续竞价开始后,避免 9:00 集合竞价前后的成交量和涨跌幅
    数据不稳定,导致候选池为空或被昨日报价污染。
    """
    delay = max(0, delay_minutes)
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    return market_open + timedelta(minutes=delay)


def _wait_until(target: datetime, reason: str, interval: int = 30) -> None:
    """等待到指定时间,用于控制盘中启动节奏。"""
    while now_local() < target:
        remain = (target - now_local()).total_seconds()
        logger.info("%s... %.0f 秒后继续", reason, max(0, remain))
        time.sleep(min(interval, max(1, remain)))


def _map_quotes_to_codes(
    original_codes: list[str],
    quotes_raw: dict[str, dict[str, Any]],
) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
    """将腾讯原始代码行情映射回持仓/候选池代码。"""
    prices: dict[str, float] = {}
    mapped_quotes: dict[str, dict[str, Any]] = {}
    for original in original_codes:
        raw = normalize_code(original)
        quote = quotes_raw.get(raw)
        if not quote:
            continue
        prices[original] = float(quote.get("price", 0) or 0)
        mapped_quotes[original] = quote
    return prices, mapped_quotes


def _build_market_data(mapped_quotes: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """构建风控需要的行情数据。"""
    market_data: dict[str, dict[str, Any]] = {}
    for code, quote in mapped_quotes.items():
        name = str(quote.get("name", ""))
        market_data[code] = {
            "current_price": float(quote.get("price", 0) or 0),
            "prev_close": float(quote.get("prev_close", 0) or 0),
            "open": float(quote.get("open", 0) or 0),
            "high": float(quote.get("high", 0) or 0),
            "low": float(quote.get("low", 0) or 0),
            "volume": float(quote.get("volume", 0) or 0),
            "is_st": "ST" in name.upper() or "退" in name,
            "is_suspended": float(quote.get("price", 0) or 0) <= 0,
        }
    return market_data


def _build_realtime_hist(
    hist: pd.DataFrame | None,
    price: float | None,
    quote: dict[str, Any] | None = None,
) -> pd.DataFrame | None:
    """将最新实时价拼成一根临时 K 线,供 ComboSignal 实时判断。

    买入与卖出两条路径必须共用本函数,保证同一 tick 上对同一标的的
    ``check_realtime`` 输入完全一致——否则买路径看到实时价、卖路径看到昨收,
    会同时给出 buy 与 sell,导致日内反复买卖。
    """
    if hist is None or price is None or price <= 0:
        return hist
    quote = quote or {}
    new_row = hist.iloc[-1:].copy()
    new_row["close"] = price
    if "open" in new_row.columns and quote.get("open", 0):
        new_row["open"] = float(quote["open"])
    if "high" in new_row.columns:
        high_values = [price]
        if quote.get("high", 0):
            high_values.append(float(quote["high"]))
        new_row["high"] = max(high_values)
    if "low" in new_row.columns:
        low_values = [price]
        if quote.get("low", 0):
            low_values.append(float(quote["low"]))
        new_row["low"] = min(low_values)
    if "volume" in new_row.columns and quote.get("volume", 0):
        new_row["volume"] = float(quote["volume"])
    return pd.concat([hist, new_row], ignore_index=True)


def _submit_order(
    order: OrderIntent,
    broker: PaperBrokerAdapter,
    risk_ctrl: RiskController,
    market_data: dict[str, dict[str, Any]],
    recorder: EventRecorder,
) -> ExecutionReport | None:
    """执行标准订单：记录信号、风控审批、虚拟成交。"""
    recorder.record("signal", order.to_dict())
    approved, rejected = risk_ctrl.filter_order_intents([order], broker.portfolio, market_data)
    for decision in rejected:
        recorder.record("risk_rejected", decision.to_dict())
        logger.info("风控拒绝: %s %s - %s", order.code, order.action, decision.reason)

    if not approved:
        return None

    decision = RiskDecision(order=order, approved=True, reason="通过")
    recorder.record("risk_approved", decision.to_dict())
    report = broker.place_order(order)
    recorder.record("execution", report.to_dict())

    if report.is_success:
        ok, reason = risk_ctrl.check_execution_quality(order.to_order_dict(), report.to_dict())
        if not ok:
            recorder.record("execution_quality_warning", {"order": order.to_dict(), "reason": reason})
        logger.info(
            "[成交] %s %s %s股 @ %.4f 策略=%s",
            order.action,
            order.code,
            report.shares,
            report.actual_price,
            order.strategy,
        )
    else:
        logger.warning("[拒单] %s %s %s", order.action, order.code, report.message)
    return report


def _get_current_quotes(
    loader: AKDataLoader,
    codes: list[str],
) -> tuple[dict[str, float], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """批量获取并映射实时行情。"""
    unique_codes = sorted({code for code in codes if code})
    raw_codes = [normalize_code(code) for code in unique_codes]
    quotes_raw = loader.get_realtime_quotes(raw_codes)
    prices, mapped_quotes = _map_quotes_to_codes(unique_codes, quotes_raw)
    market_data = _build_market_data(mapped_quotes)
    return prices, mapped_quotes, market_data


def _read_rps_state() -> dict[str, Any]:
    """读取 ETF/RPS 日频状态。"""
    if not os.path.exists(RPS_STATE_FILE):
        return {}
    try:
        with open(RPS_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("读取 RPS 状态失败: %s", exc)
        return {}


def _write_rps_state(state: dict[str, Any]) -> None:
    """写入 ETF/RPS 日频状态，供前端和验收读取。"""
    os.makedirs(os.path.dirname(RPS_STATE_FILE), exist_ok=True)
    tmp_path = f"{RPS_STATE_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp_path, RPS_STATE_FILE)


def _should_skip_rps(today: str, force: bool) -> bool:
    """判断当天 RPS 是否已完成，避免服务重启重复调仓。"""
    if force:
        return False
    state = _read_rps_state()
    return (
        state.get("date") == today
        and state.get("status") == "ok"
        and state.get("completed") is True
    )


def _latest_history_market_data(
    history_map: dict[str, pd.DataFrame],
) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
    """从历史行情尾部构造价格与风控行情，作为实时行情失败时的兜底。"""
    prices: dict[str, float] = {}
    market_data: dict[str, dict[str, Any]] = {}
    for code, df in history_map.items():
        if df is None or df.empty or "close" not in df.columns:
            continue
        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        if close.empty:
            continue
        price = float(close.iloc[-1])
        prev_close = float(close.iloc[-2]) if len(close) >= 2 else price
        if price <= 0:
            continue
        prices[code] = price
        market_data[code] = {
            "current_price": price,
            "prev_close": prev_close,
            "is_st": False,
            "is_suspended": False,
        }
    return prices, market_data


def _merge_market_data(
    base: dict[str, dict[str, Any]],
    fallback: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """合并风控行情，实时行情优先，历史尾部兜底。"""
    merged = dict(fallback)
    merged.update(base)
    return merged


def _calculate_buy_shares(
    broker: PaperBrokerAdapter,
    price: float,
    prices: dict[str, float],
    target_count: int,
) -> int:
    """按 ETF/RPS 目标数量计算买入份额。"""
    if price <= 0:
        return 0
    target_ratio = min(MAX_SINGLE_ETF, MAX_TOTAL_POSITION / max(1, target_count))
    return broker.portfolio.rules.calc_lot_size(
        price,
        broker.query_cash(),
        max_ratio=target_ratio,
        total_value=broker.portfolio.get_total_value(prices),
    )


def _run_daily_rps_rotation(
    broker: PaperBrokerAdapter,
    loader: AKDataLoader,
    risk_ctrl: RiskController,
    recorder: EventRecorder,
    today: str,
    force: bool = False,
) -> dict[str, Any]:
    """执行 ETF/RPS 日频轮动并写入观察期状态。

    本函数是观察期实盘模拟链路的一部分:信号、风控、成交都会进入
    ``trade_events.jsonl``。行业指数只记录强弱观察结果,不会生成订单。
    """
    started_at = now_local().strftime("%Y-%m-%d %H:%M:%S")
    if not ENABLE_RPS_ROTATION:
        state = {
            "date": today,
            "status": "disabled",
            "completed": True,
            "started_at": started_at,
            "updated_at": started_at,
            "message": "ENABLE_RPS_ROTATION=false",
            "etf_signals": [],
            "industry_signals": [],
            "orders": [],
        }
        _write_rps_state(state)
        recorder.record("rps_rotation_skipped", state)
        logger.info("ETF/RPS 轮动已关闭")
        return state

    if _should_skip_rps(today, force):
        state = _read_rps_state()
        recorder.record("rps_rotation_skipped", {
            "date": today,
            "reason": "今日已完成，跳过重复调仓",
        })
        logger.info("ETF/RPS 今日已完成，跳过重复调仓")
        return state

    state: dict[str, Any] = {
        "date": today,
        "status": "running",
        "completed": False,
        "started_at": started_at,
        "updated_at": started_at,
        "data_source": "AKShare: fund_etf_hist_em + stock_board_industry_hist_em",
        "etf_pool_size": len(DEFAULT_RPS_ETF_POOL),
        "industry_pool_size": len(DEFAULT_INDUSTRY_INDEX_POOL),
        "etf_loaded": 0,
        "industry_loaded": 0,
        "etf_signals": [],
        "industry_signals": [],
        "orders": [],
        "errors": [],
    }
    _write_rps_state(state)

    try:
        logger.info("===== 开始 ETF/RPS 日频轮动 =====")
        etf_codes = get_rps_etf_codes()
        etf_history = loader.get_batch_etf_history(etf_codes, days=RPS_HISTORY_DAYS, max_batch=len(etf_codes))
        industry_names = get_industry_index_names()
        industry_history = loader.get_batch_industry_index_history(
            industry_names,
            days=RPS_HISTORY_DAYS,
            max_batch=len(industry_names),
        )

        strategy = RPSRotationStrategy(target_pool=DEFAULT_RPS_ETF_POOL)
        etf_signals = strategy.calculate_signals(etf_history, today)
        industry_signals = calculate_rps_scores(
            industry_history,
            lookback=strategy.lookback,
            top_n=min(10, max(1, len(industry_history))),
            min_rps=0,
            min_avg_volume=0,
            name_map={name: name for name in industry_history},
        )
        orders_raw = strategy.generate_orders(etf_history, broker.query_positions(), today)

        fallback_prices, fallback_market_data = _latest_history_market_data(etf_history)
        quote_prices, _, quote_market_data = _get_current_quotes(loader, list(set(etf_codes + [o["code"] for o in orders_raw])))
        prices = {**fallback_prices, **quote_prices}
        market_data = _merge_market_data(quote_market_data, fallback_market_data)
        broker.portfolio.update_prices(prices)

        selected_count = max(1, len([s for s in etf_signals if s.get("code")]))
        submitted_orders: list[dict[str, Any]] = []
        for raw_order in orders_raw:
            code = str(raw_order["code"])
            price = float(prices.get(code) or raw_order.get("price") or 0)
            if price <= 0:
                skipped = {
                    "code": code,
                    "action": raw_order.get("action"),
                    "status": "skipped",
                    "reason": "缺少 ETF 有效价格",
                }
                submitted_orders.append(skipped)
                recorder.record("signal_skipped", skipped)
                continue

            shares = int(raw_order.get("shares", 0) or 0)
            if raw_order["action"] == "buy":
                shares = _calculate_buy_shares(broker, price, prices, selected_count)
                if shares < LOT_SIZE:
                    skipped = {
                        "code": code,
                        "action": "buy",
                        "status": "skipped",
                        "reason": "ETF/RPS 可买数量不足一手",
                        "price": price,
                        "cash": broker.query_cash(),
                    }
                    submitted_orders.append(skipped)
                    recorder.record("signal_skipped", skipped)
                    continue

            order = OrderIntent(
                code=code,
                name=str(raw_order.get("name", DEFAULT_RPS_ETF_POOL.get(code, {}).get("name", code))),
                action=raw_order["action"],
                price=price,
                shares=shares,
                date=today,
                strategy=str(raw_order.get("strategy", "ETF/行业RPS轮动")),
                reason=str(raw_order.get("reason", "")),
                source="daily_rps_rotation",
                metadata={
                    "rps_signals": etf_signals,
                    "industry_top": industry_signals[:5],
                    "data_source": "AKShare",
                },
            )
            report = _submit_order(order, broker, risk_ctrl, market_data, recorder)
            submitted_orders.append({
                "code": order.code,
                "name": order.name,
                "action": order.action,
                "price": order.price,
                "shares": order.shares,
                "status": report.status if report else "risk_rejected",
                "message": report.message if report else "风控拒绝",
                "reason": order.reason,
            })

        state.update({
            "status": "ok",
            "completed": True,
            "updated_at": now_local().strftime("%Y-%m-%d %H:%M:%S"),
            "etf_loaded": len(etf_history),
            "industry_loaded": len(industry_history),
            "etf_signals": etf_signals,
            "industry_signals": industry_signals,
            "orders": submitted_orders,
        })
        _write_rps_state(state)
        recorder.record("rps_rotation_completed", state)
        logger.info(
            "ETF/RPS 完成: ETF数据 %d/%d 行业数据 %d/%d 信号 %d 订单 %d",
            len(etf_history),
            len(etf_codes),
            len(industry_history),
            len(industry_names),
            len(etf_signals),
            len(submitted_orders),
        )
        return state
    except Exception as exc:
        state.update({
            "status": "error",
            "completed": False,
            "updated_at": now_local().strftime("%Y-%m-%d %H:%M:%S"),
            "errors": [str(exc)],
        })
        _write_rps_state(state)
        recorder.record("rps_rotation_failed", state)
        logger.error("ETF/RPS 日频轮动失败: %s", exc, exc_info=True)
        return state


def watch_thread(
    broker: PaperBrokerAdapter,
    shared: SharedState,
    loader: AKDataLoader,
    combo: ComboSignalStrategy,
    risk_ctrl: RiskController,
    recorder: EventRecorder,
    interval: int,
) -> None:
    """盯盘线程：止损止盈和候选股买入。"""
    logger.info("盯盘线启动")
    last_snapshot_minute = -1

    while True:
        now = now_local()
        market_close = now.replace(hour=15, minute=0, second=0)
        lunch_start = now.replace(hour=11, minute=30, second=0)
        lunch_end = now.replace(hour=13, minute=0, second=0)

        if now >= market_close:
            logger.info("盯盘线：收盘，退出")
            break
        if lunch_start <= now < lunch_end:
            time.sleep(30)
            continue

        try:
            positions = broker.query_positions()
            candidates = shared.get_candidates()
            watch_codes = list(set(list(positions.keys()) + candidates))
            if not watch_codes:
                time.sleep(interval)
                continue

            prices, _, market_data = _get_current_quotes(loader, watch_codes)
            broker.portfolio.update_prices(prices)

            _handle_position_exits(
                broker,
                positions,
                prices,
                loader,
                risk_ctrl,
                market_data,
                recorder,
                shared=shared,
                combo=combo,
            )
            _handle_candidate_entries(broker, shared, prices, loader, combo, risk_ctrl, market_data, recorder)

            if now.minute != last_snapshot_minute and now.minute % 5 == 0:
                snapshot = broker.query_snapshot(prices)
                recorder.record("portfolio_snapshot", snapshot.to_dict())
                last_snapshot_minute = now.minute

        except Exception as exc:
            logger.error("盯盘线异常: %s", exc, exc_info=True)

        time.sleep(interval)

    logger.info("盯盘线退出")


def _handle_position_exits(
    broker: PaperBrokerAdapter,
    positions: dict[str, dict[str, Any]],
    prices: dict[str, float],
    loader: AKDataLoader,
    risk_ctrl: RiskController,
    market_data: dict[str, dict[str, Any]],
    recorder: EventRecorder,
    shared: SharedState | None = None,
    combo: ComboSignalStrategy | None = None,
) -> None:
    """处理持仓止损/止盈(策略卖出 / 硬止损 / 移动止损 / 时间止损 / 止盈)。"""
    today = today_yyyymmdd()
    for code, pos in positions.items():
        if shared is not None and shared.is_exit_cooling_down(code):
            continue

        current = prices.get(code) or pos.get("current_price") or pos.get("avg_cost", 0)
        avg_cost = float(pos.get("avg_cost", 0) or 0)
        shares = int(pos.get("shares", 0) or 0)
        if current <= 0 or avg_cost <= 0 or shares <= 0:
            continue

        # 汇总退出判定所需输入:Combo 信号、MA20、峰值价、持有天数
        combo_sell = False
        combo_reason = ""
        ma20 = None
        atr = None
        try:
            hist = loader.get_stock_data(normalize_code(code), days=60)
            # 与买入路径共用实时价 K 线,避免买卖信号因输入不同而互相矛盾
            rt_hist = _build_realtime_hist(hist, float(current), market_data.get(code))
            if rt_hist is not None and len(rt_hist) > 25:
                if combo is not None:
                    sig = combo.check_realtime(rt_hist)
                    combo_sell = sig.get("signal") == "sell"
                    combo_reason = sig.get("reason", "策略信号")
            if rt_hist is not None and len(rt_hist) >= 20:
                ma20 = float(pd.to_numeric(rt_hist["close"], errors="coerce").tail(20).mean())
            if rt_hist is not None and {"high", "low", "close"} <= set(rt_hist.columns):
                atr_value = calculate_atr(rt_hist, period=ATR_PERIOD).iloc[-1]
                atr = float(atr_value) if pd.notna(atr_value) else None
        except Exception as exc:
            logger.warning("持仓退出指标检查失败: %s %s", code, exc, exc_info=True)

        peak_price = float(pos.get("peak_price", avg_cost) or avg_cost)
        holding_days = _holding_days(pos.get("buy_date"), today)

        decision = evaluate_exit(
            avg_cost=avg_cost,
            price=float(current),
            peak_price=peak_price,
            ma20=ma20,
            atr=atr,
            holding_days=holding_days,
            combo_sell=combo_sell,
            combo_reason=combo_reason,
        )
        if decision is None:
            continue

        strategy, reason = decision
        order = OrderIntent(
            code=code,
            name=str(pos.get("name", code)),
            action="sell",
            price=float(current),
            shares=shares,
            date=today,
            strategy=strategy,
            reason=reason,
            source="watch_thread",
        )
        report = _submit_exit_order(order, broker, risk_ctrl, market_data, recorder, shared)
        # 卖出成交后进入再买冷却,杜绝"卖出→立刻买回"的日内刷单
        if report is not None and report.is_success and shared is not None:
            shared.mark_rebuy_cooldown(code, REBUY_COOLDOWN_SECONDS)


def _submit_exit_order(
    order: OrderIntent,
    broker: PaperBrokerAdapter,
    risk_ctrl: RiskController,
    market_data: dict[str, dict[str, Any]],
    recorder: EventRecorder,
    shared: SharedState | None,
) -> ExecutionReport | None:
    """提交卖出订单，并在风控拒绝时进入冷却期。"""
    report = _submit_order(order, broker, risk_ctrl, market_data, recorder)
    if report is None and shared is not None:
        shared.set_exit_cooldown(order.code)
    return report


def _handle_candidate_entries(
    broker: PaperBrokerAdapter,
    shared: SharedState,
    prices: dict[str, float],
    loader: AKDataLoader,
    combo: ComboSignalStrategy,
    risk_ctrl: RiskController,
    market_data: dict[str, dict[str, Any]],
    recorder: EventRecorder,
) -> None:
    """处理候选股买入信号。"""
    today = today_yyyymmdd()

    # 大盘择时:系统性下跌期暂停开新仓,只保留持仓退出
    if not shared.is_risk_on():
        return

    positions = broker.query_positions()
    position_norms = {normalize_code(code) for code in positions}
    stocks = shared.get_stocks()

    for code in shared.get_candidates():
        if normalize_code(code) in position_norms:
            continue

        # 卖出后再买冷却:刚清仓的标的不立即重新开仓
        if shared.is_rebuy_cooling_down(code):
            continue

        # Check cooldown for risk-rejected stocks (5 min)
        now_ts = time.time()
        cooldown_until = shared.rejected_stocks.get(code, 0)
        if now_ts < cooldown_until:
            continue

        hist = shared.get_hist(code)
        if hist is None or len(hist) < 25:
            hist = loader.get_stock_data(normalize_code(code), days=60)
            if hist is not None:
                shared.update_hist(code, hist)
        if hist is None or len(hist) < 25:
            continue

        price = prices.get(code)
        if price is None:
            continue

        realtime_hist = _build_realtime_hist(hist, price, market_data.get(code))

        result = combo.check_realtime(realtime_hist)
        if result["signal"] != "buy":
            continue

        total_value = broker.portfolio.get_total_value(prices)
        shares = broker.portfolio.rules.calc_lot_size(
            price,
            broker.query_cash(),
            max_ratio=MAX_SINGLE_STOCK,
            total_value=total_value,
        )
        if shares < LOT_SIZE:
            recorder.record("signal_skipped", {
                "code": code,
                "reason": "可买数量不足一手",
                "price": price,
                "cash": broker.query_cash(),
            })
            continue

        name = next((s["name"] for s in stocks if s["code"] == code), code)
        order = OrderIntent(
            code=code,
            name=name,
            action="buy",
            price=float(price),
            shares=shares,
            date=today,
            strategy="全市场扫描+组合策略",
            reason=str(result["reason"]),
            source="watch_thread",
            metadata={
                "rsi": result.get("rsi"),
                "volume_ratio": result.get("volume_ratio"),
                "cash_buffer": CASH_BUFFER,
            },
        )
        result_code = _submit_order(order, broker, risk_ctrl, market_data, recorder)
        if result_code is None:
            # Risk rejected — cooldown for 5 minutes
            shared.rejected_stocks[code] = time.time() + 300


def scan_thread(
    shared: SharedState,
    scanner: MarketScanner,
    recorder: EventRecorder,
    top_n: int,
    scan_interval: int,
) -> None:
    """扫描线程：定期更新候选池。"""
    logger.info("扫描线启动")

    while True:
        now = now_local()
        market_close = now.replace(hour=15, minute=0, second=0)
        lunch_start = now.replace(hour=11, minute=30, second=0)
        lunch_end = now.replace(hour=13, minute=0, second=0)

        if now >= market_close:
            logger.info("扫描线：收盘，退出")
            break
        if lunch_start <= now < lunch_end:
            time.sleep(30)
            continue

        time.sleep(scan_interval)
        if now_local() >= market_close:
            break
        _do_scan(shared, scanner, recorder, top_n)

    logger.info("扫描线退出")


def _do_scan(
    shared: SharedState,
    scanner: MarketScanner,
    recorder: EventRecorder,
    top_n: int,
) -> None:
    """执行一次全市场扫描。"""
    if shared.is_scanning():
        logger.info("上一次扫描未完成，跳过")
        return

    shared.set_scanning(True)
    try:
        logger.info("===== 开始全盘扫描 =====")
        stocks = scanner.scan(top_n=top_n, momentum_period=60)
        shared.update(stocks)
        recorder.record("scan_completed", {"top_n": top_n, "stocks": stocks})

        logger.info("扫描完成，候选股 %s 只:", len(stocks))
        for stock in stocks:
            logger.info(
                "  #%s %s(%s) 动量=%+.2f%% 得分=%.4f",
                stock["rank"],
                stock["name"],
                stock["code"],
                stock["momentum"] * 100,
                stock["score"],
            )

        hist_loader = AKDataLoader()
        try:
            for stock in stocks:
                df = hist_loader.get_stock_history(stock["code"], days=60)
                if df is not None:
                    shared.update_hist(stock["code"], df)
                time.sleep(0.05)

            # 刷新大盘择时状态:指数跌破均线则暂停开新仓
            _refresh_market_regime(hist_loader, shared, recorder)
        finally:
            hist_loader.close()

    except Exception as exc:
        logger.error("扫描失败: %s", exc, exc_info=True)
        recorder.record("scan_failed", {"error": str(exc)})
    finally:
        shared.set_scanning(False)


def _refresh_market_regime(
    loader: AKDataLoader,
    shared: SharedState,
    recorder: EventRecorder,
) -> None:
    """根据基准指数刷新大盘择时状态。取数失败时保持放行(risk-on)。"""
    if not ENABLE_MARKET_REGIME:
        shared.set_risk_on(True)
        return
    try:
        index_df = loader.get_index_history(MARKET_INDEX_CODE, days=MARKET_REGIME_MA + 40)
        risk_on = is_risk_on(index_df, ma_period=MARKET_REGIME_MA)
        shared.set_risk_on(risk_on)
        if not risk_on:
            logger.info("大盘择时: %s 跌破 MA%d,暂停开新仓", MARKET_INDEX_CODE, MARKET_REGIME_MA)
        recorder.record("market_regime", {"index": MARKET_INDEX_CODE, "risk_on": risk_on})
    except Exception as exc:
        logger.warning("大盘择时刷新失败,默认放行: %s", exc)
        shared.set_risk_on(True)


def _generate_report(
    broker: PaperBrokerAdapter,
    loader: AKDataLoader,
    recorder: EventRecorder,
) -> None:
    """生成收盘日报并保存账户快照。"""
    logger.info("生成日报...")
    positions = broker.query_positions()
    prices: dict[str, float] = {}
    if positions:
        prices, _, _ = _get_current_quotes(loader, list(positions.keys()))
        broker.portfolio.update_prices(prices)

    today = today_yyyymmdd()
    broker.portfolio.save_snapshot(today, prices)
    snapshot = broker.query_snapshot(prices)
    recorder.record("portfolio_snapshot_close", snapshot.to_dict())

    today_trades = broker.portfolio.get_trades_by_date(today)
    report_lines = [
        "=" * 50,
        f"量化日报 - {now_local().strftime('%Y-%m-%d')}（虚拟盘）",
        "=" * 50,
        f"初始资金: ¥{INITIAL_CAPITAL:,.2f}",
        f"当前总值: ¥{snapshot.total_value:,.2f}",
        f"总收益率: {snapshot.pnl_pct:+.2%}",
        f"今日交易: {len(today_trades)}笔",
        "",
        "持仓明细:",
    ]

    if snapshot.positions:
        for pos in snapshot.positions:
            report_lines.append(
                f"  {pos['code']}: {pos['shares']}股 成本={pos['avg_cost']:.3f} "
                f"现价={pos['current_price']:.3f} 盈亏={pos['profit_pct']:+.2%}"
            )
    else:
        report_lines.append("  (空仓)")

    report_lines.extend(["", "今日交易:"])
    for trade in today_trades:
        action = trade.get("action") or trade.get("direction")
        report_lines.append(
            f"  {trade.get('time', '')} {action} {trade['code']} "
            f"{trade['shares']}股 @ {trade.get('actual_price', trade['price']):.4f}"
        )
    if not today_trades:
        report_lines.append("  (无交易)")

    rps_state = _read_rps_state()
    report_lines.extend(["", "ETF/RPS 日频:"])
    if rps_state.get("date") == today:
        report_lines.append(
            f"  状态={rps_state.get('status', '--')} "
            f"ETF数据={rps_state.get('etf_loaded', 0)}/{rps_state.get('etf_pool_size', 0)} "
            f"行业数据={rps_state.get('industry_loaded', 0)}/{rps_state.get('industry_pool_size', 0)} "
            f"订单={len(rps_state.get('orders', []))}"
        )
        for signal in list(rps_state.get("etf_signals", []))[:3]:
            report_lines.append(
                f"  ETF入选 {signal.get('name', signal.get('code'))} "
                f"RPS={float(signal.get('rps', 0)):.0f} 动量={float(signal.get('momentum', 0)):+.2%}"
            )
    else:
        report_lines.append("  今日未记录 RPS 状态")

    report_lines.append("=" * 50)
    report_text = "\n".join(report_lines)
    logger.info("\n%s", report_text)

    os.makedirs(REPORT_DIR, exist_ok=True)
    report_file = os.path.join(REPORT_DIR, f"daily_{today}.txt")
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(report_text)
    logger.info("日报已保存: %s", report_file)


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="A 股量化虚拟盘实时盯盘")
    parser.add_argument("--broker", default=BROKER_MODE, choices=["paper", "qmt"], help="交易通道，默认读取 BROKER_MODE")
    parser.add_argument("--watch-interval", type=int, default=LIVE_WATCH_INTERVAL_SECONDS, help="盯盘刷新秒数")
    parser.add_argument("--scan-interval", type=int, default=LIVE_SCAN_INTERVAL_SECONDS, help="扫描间隔秒数")
    parser.add_argument("--initial-scan-delay-minutes", type=int, default=LIVE_INITIAL_SCAN_DELAY_MINUTES,
                        help="开盘后首次全市场确认扫描延迟分钟数")
    parser.add_argument("--top-n", type=int, default=30, help="候选股数量")
    parser.add_argument("--force-rps", action="store_true", help="强制重跑当天 ETF/RPS 日频轮动")
    parser.add_argument("--ignore-calendar", action="store_true", help="忽略交易日判断，便于联调")
    return parser.parse_args()


def main() -> None:
    """命令行入口。"""
    args = _parse_args()
    if not args.ignore_calendar and not is_trading_day():
        logger.info("今天不是交易日，退出")
        return

    broker = create_broker(args.broker)
    broker.connect()
    if not isinstance(broker, PaperBrokerAdapter):
        raise RuntimeError("实时盯盘当前只允许 paper 虚拟盘；QMT 适配器仅用于 dry-run 接口联调")

    loader = AKDataLoader()
    scanner = MarketScanner(loader=loader)
    combo = ComboSignalStrategy()
    risk_ctrl = RiskController()
    recorder = EventRecorder()
    shared = SharedState()
    risk_ctrl.set_daily_start(broker.portfolio)

    now = now_local()
    logger.info("=" * 60)
    logger.info("虚拟盘盯盘系统启动: %s", now.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("Broker=%s watch_interval=%ss scan_interval=%ss", args.broker, args.watch_interval, args.scan_interval)

    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    first_scan_at = _initial_scan_time(now, args.initial_scan_delay_minutes)
    market_close = now.replace(hour=15, minute=0, second=0, microsecond=0)

    if now >= market_close:
        logger.info("已过收盘时间，退出")
        loader.close()
        broker.close()
        return

    if now < market_open:
        _wait_until(market_open, "等待开盘启动盯盘")

    today = today_yyyymmdd()
    _run_daily_rps_rotation(
        broker=broker,
        loader=loader,
        risk_ctrl=risk_ctrl,
        recorder=recorder,
        today=today,
        force=args.force_rps,
    )

    logger.info("===== 开盘：启动盯盘线 =====")
    t_watch = threading.Thread(
        target=watch_thread,
        args=(broker, shared, loader, combo, risk_ctrl, recorder, args.watch_interval),
        name="盯盘线",
        daemon=True,
    )
    t_watch.start()

    if now_local() < first_scan_at:
        _wait_until(first_scan_at, "等待开盘后确认扫描")

    _do_scan(shared, scanner, recorder, top_n=args.top_n)

    logger.info("===== 启动扫描线 =====")
    t_scan = threading.Thread(
        target=scan_thread,
        args=(shared, scanner, recorder, args.top_n, args.scan_interval),
        name="扫描线",
        daemon=True,
    )
    t_scan.start()

    try:
        while now_local() < market_close:
            time.sleep(30)
            snapshot = broker.query_snapshot()
            logger.info(
                "[状态] 总资产=%.2f 收益=%.2f%% 持仓=%s只 候选=%s只",
                snapshot.total_value,
                snapshot.pnl_pct * 100,
                snapshot.position_count,
                len(shared.get_candidates()),
            )
    finally:
        logger.info("===== 收盘 =====")
        t_watch.join(timeout=30)
        t_scan.join(timeout=30)
        _generate_report(broker, loader, recorder)
        loader.close()
        broker.close()
        logger.info("盯盘结束")


if __name__ == "__main__":
    main()
