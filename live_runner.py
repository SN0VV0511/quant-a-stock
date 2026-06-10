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
    MIN_POSITION_RATIO,
    REBUY_COOLDOWN_SECONDS,
    ENTRY_INTERVAL_SECONDS,
    REPORT_DIR,
    RPS_HISTORY_DAYS,
    RPS_STATE_FILE,
    ENABLE_MARKET_REGIME,
    MARKET_INDEX_CODE,
    MARKET_REGIME_MA,
    SMALLCAP_TOP_N,
    SMALLCAP_REBALANCE_DAYS,
    SMALLCAP_REVERSAL_DAYS,
    SMALLCAP_MIN_MKTCAP,
    SMALLCAP_MAX_MKTCAP,
    SMALLCAP_MIN_PRICE,
    SMALLCAP_MAX_PRICE,
    SMALLCAP_MIN_PB,
    get_industry_index_names,
    get_rps_etf_codes,
    is_etf,
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
from strategies.small_cap_value import build_factor_rows, score_small_cap_value  # noqa: E402
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


SMALLCAP_STATE_FILE = os.path.join(os.path.dirname(__file__), "data", "small_cap_state.json")


def _read_small_cap_state() -> dict:
    """读取小市值调仓状态。"""
    if not os.path.exists(SMALLCAP_STATE_FILE):
        return {}
    try:
        with open(SMALLCAP_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_small_cap_state(state: dict) -> None:
    """写入小市值调仓状态。"""
    os.makedirs(os.path.dirname(SMALLCAP_STATE_FILE), exist_ok=True)
    with open(SMALLCAP_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _run_weekly_small_cap(
    broker,
    loader,
    risk_ctrl,
    recorder,
    shared,
    today: str,
) -> dict:
    """小市值价值周度调仓。

    每 SMALLCAP_REBALANCE_DAYS 个交易日调仓一次,从候选池中按因子打分选 top N 等权持有。
    """
    state = _read_small_cap_state()
    if state.get("date") == today:
        logger.info("小市值价值今日已调仓，跳过")
        return state

    # 判断是否为调仓日(用交易日计数)
    import config.settings as cfg
    trading_days = state.get("trading_days", 0)
    if trading_days > 0 and trading_days % SMALLCAP_REBALANCE_DAYS != 0:
        logger.info("小市值价值非调仓日(trading_day=%d),跳过", trading_days)
        state["trading_days"] = trading_days + 1
        state["date"] = today
        _write_small_cap_state(state)
        return state

    logger.info("===== 开始小市值价值周度调仓 =====")

    # 获取候选池
    candidates = shared.get_candidates()
    if not candidates:
        logger.warning("小市值价值: 候选池为空，跳过调仓")
        state["status"] = "no_candidates"
        state["date"] = today
        state["trading_days"] = trading_days + 1
        _write_small_cap_state(state)
        return state

    # 加载扩展历史
    codes = [c.get("code", "") for c in candidates if c.get("code")]
    from data.loader import DataLoader
    dl = DataLoader()
    history = {}
    name_map = {}
    try:
        for code in codes:
            try:
                df = dl.get_daily_data(code, adjust_flag="2")
                if df is None or df.empty:
                    continue
                df = df.copy()
                for col in ("pbMRQ", "isST", "tradestatus", "turn", "volume", "close"):
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                df["pb"] = df.get("pbMRQ")
                df["is_st"] = df.get("isST", 0)
                turn = df["turn"].where(df["turn"] > 0)
                df["mktcap"] = df["close"] * df["volume"] / (turn / 100.0)
                df["name"] = code
                history[code] = df
                name_map[code] = code
            except Exception as exc:  # noqa: BLE001
                logger.warning("小市值价值加载失败 %s: %s", code, exc)
    finally:
        dl.close()

    if len(history) < SMALLCAP_TOP_N:
        logger.warning("小市值价值: 数据不足(%d只),跳过调仓", len(history))
        state["status"] = "insufficient_data"
        state["date"] = today
        state["trading_days"] = trading_days + 1
        _write_small_cap_state(state)
        return state

    # 打分选股
    rows = build_factor_rows(history, reversal_days=SMALLCAP_REVERSAL_DAYS, name_map=name_map)
    target = score_small_cap_value(rows, top_n=SMALLCAP_TOP_N)
    target_codes = {t["code"] for t in target}

    logger.info("小市值价值目标池: %s", [f"{t['code']}(rank{t['rank']})" for t in target[:5]])

    # 获取当前持仓
    portfolio = broker.portfolio
    positions = portfolio.get_all_positions()
    held_codes = set(positions)

    orders: list[dict] = []
    # 卖出不在目标池的
    for code in held_codes - target_codes:
        pos = positions[code]
        orders.append({
            "code": code, "name": pos.get("name", code),
            "action": "sell", "shares": pos.get("shares", 0),
            "price": pos.get("current_price", 0),
            "strategy": "小市值价值", "reason": "调出目标池",
        })

    # 买入目标池但未持有的
    for t in target:
        if t["code"] not in held_codes:
            orders.append({
                "code": t["code"], "name": t.get("name", t["code"]),
                "action": "buy",
                "strategy": "全市场扫描+小市值价值",
                "reason": f"小市值价值 rank{t['rank']}",
            })

    # 执行订单
    executed = 0
    for order in orders:
        intent = OrderIntent(
            code=order["code"],
            name=order.get("name", order["code"]),
            action=order["action"],
            shares=order.get("shares"),
            price=order.get("price"),
            strategy=order["strategy"],
            reason=order["reason"],
        )
        decision = risk_ctrl.evaluate(intent, portfolio)
        recorder.record("risk_check", {
            "code": intent.code,
            "action": intent.action,
            "decision": decision.decision,
            "reason": decision.reason,
        })
        if decision.decision != "approve":
            logger.info("风控拒绝: %s(%s) %s - %s", intent.name or intent.code, intent.code, intent.action, decision.reason)
            continue
        report = broker.execute(intent)
        recorder.record("execution", report.to_dict())
        if report.status == "filled":
            executed += 1
            logger.info(
                "[成交] %s %s %s股 @ %.4f 策略=%s",
                intent.action, intent.code, report.filled_shares,
                report.avg_price, intent.strategy,
            )

    state = {
        "date": today,
        "trading_days": trading_days + 1,
        "status": "done",
        "target_count": len(target),
        "orders": len(orders),
        "executed": executed,
        "target_codes": list(target_codes),
    }
    _write_small_cap_state(state)
    logger.info("小市值价值完成: 目标%d只 订单%d 成交%d", len(target), len(orders), executed)
    return state


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
        self.last_entry_time: float = 0  # 上次买入时间戳,用于建仓间隔控制
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

    def set_exit_cooldown_until_close(self, code: str) -> None:
        """跌停等极端情况:冷却到收盘,当天不再尝试卖出。"""
        with self._lock:
            now = now_local()
            close_time = now.replace(hour=15, minute=0, second=0, microsecond=0)
            if now >= close_time:
                close_time += timedelta(days=1)
            self.rejected_exits[code] = close_time.timestamp()

    def mark_rebuy_cooldown(self, code: str, seconds: int) -> None:
        """标记某标的刚被卖出,在冷却期内禁止再次开仓(防止日内来回刷单)。
        seconds=0 表示当天不再买回(收盘后自动重置)。"""
        with self._lock:
            if seconds <= 0:
                # 当天不再买回:冷却到明天 9:30
                now = now_local()
                tomorrow_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
                if now >= tomorrow_open:
                    tomorrow_open += timedelta(days=1)
                self.rebuy_cooldown[code] = tomorrow_open.timestamp()
            else:
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
) -> tuple[ExecutionReport | None, str]:
    """执行标准订单：记录信号、风控审批、虚拟成交。
    返回 (report, reject_reason)。"""
    recorder.record("signal", order.to_dict())
    approved, rejected = risk_ctrl.filter_order_intents([order], broker.portfolio, market_data)
    for decision in rejected:
        recorder.record("risk_rejected", decision.to_dict())
        logger.info("风控拒绝: %s(%s) %s - %s", order.name or order.code, order.code, order.action, decision.reason)

    if not approved:
        return None, rejected[0].reason if rejected else ""

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
    return report, ""


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
            raw = f.read().strip()
        if not raw:
            return {}
        data = json.loads(raw)
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
    try:
        state = _read_rps_state()
    except Exception:
        return False
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

    # 收盘后不执行 RPS 轮动（避免收盘后重启导致重复调仓）
    now = now_local()
    market_close = now.replace(hour=15, minute=0, second=0, microsecond=0)
    if now >= market_close:
        logger.info("ETF/RPS 已过收盘时间，跳过")
        return {"date": today, "status": "after_hours", "completed": True}

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

        # 数据完整性校验：行业数据不足时降级到腾讯接口
        etf_ratio = len(etf_history) / max(len(etf_codes), 1)
        industry_ratio = len(industry_history) / max(len(industry_names), 1)
        min_ratio = 0.5

        if industry_ratio < min_ratio:
            logger.warning(
                "AKShare 行业数据不足 (%d/%d, %.0f%%), 降级到腾讯接口",
                len(industry_history), len(industry_names), industry_ratio * 100,
            )
            tencent_industry = loader.get_industry_index_history_tencent(
                industry_names, days=RPS_HISTORY_DAYS
            )
            if tencent_industry:
                # 合并：腾讯数据覆盖 AKShare 缺失的
                for name, df in tencent_industry.items():
                    if name not in industry_history:
                        industry_history[name] = df
                industry_ratio = len(industry_history) / max(len(industry_names), 1)
                logger.info(
                    "腾讯兜底后行业数据: %d/%d (%.0f%%)",
                    len(industry_history), len(industry_names), industry_ratio * 100,
                )

        if etf_ratio < min_ratio or industry_ratio < min_ratio:
            state.update({
                "status": "insufficient_data",
                "completed": True,
                "updated_at": now_local().strftime("%Y-%m-%d %H:%M:%S"),
                "etf_loaded": len(etf_history),
                "industry_loaded": len(industry_history),
                "message": (
                    f"数据不足跳过交易: ETF {len(etf_history)}/{len(etf_codes)} "
                    f"({etf_ratio:.0%}) 行业 {len(industry_history)}/{len(industry_names)} "
                    f"({industry_ratio:.0%})"
                ),
            })
            _write_rps_state(state)
            recorder.record("rps_rotation_skipped", state)
            logger.warning(
                "ETF/RPS 数据不足跳过交易: ETF %d/%d (%d%%) 行业 %d/%d (%d%%)",
                len(etf_history), len(etf_codes), int(etf_ratio * 100),
                len(industry_history), len(industry_names), int(industry_ratio * 100),
            )
            return state

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
            # 二次持仓去重: 防止 generate_orders 和执行之间状态变化
            current_positions = broker.query_positions()
            if code in current_positions:
                logger.info("RPS 跳过已持仓 ETF: %s", code)
                continue
            report, _ = _submit_order(order, broker, risk_ctrl, market_data, recorder)
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
    """处理持仓止损/止盈(策略卖出 / 硬止损 / 移动止损 / 时间止损 / 止盈)。

    ETF 持仓也走趋势退出(移动止损/MA20跌破/止盈),不再由 RPS 排名踢出。
    """
    import config.settings as _cfg

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
            if is_etf(code):
                hist = loader.get_etf_history(code, days=60)
            else:
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

        # T+1: 今日买入的持仓跳过退出评估,不可卖出
        if holding_days <= 0:
            continue

        # 开盘保护: 开盘后 N 分钟内不执行止损,避免跳空误触发(止盈不受影响)
        now_ts = now_local()
        market_open_today = now_ts.replace(hour=9, minute=30, second=0, microsecond=0)
        minutes_since_open = (now_ts - market_open_today).total_seconds() / 60
        opening_protect = 0 < minutes_since_open <= _cfg.OPENING_STOP_PROTECT_MINUTES

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

        # 开盘保护期内,只允许止盈,不允许止损
        if decision is not None and opening_protect:
            strategy_name = decision[0]
            if "止损" in strategy_name and "止盈" not in strategy_name:
                logger.info(
                    "[开盘保护] %s(%s) 触发%s但开盘%.0f分钟内跳过",
                    pos.get("name", code), code, strategy_name, minutes_since_open,
                )
                decision = None

        # 开盘急速止盈: 开盘后 N 分钟内，持仓盈利超阈值则立即卖出
        # 例外：如果前一日涨停（今日高开是正常延续），不触发急速止盈
        if decision is None and avg_cost > 0:
            profit_pct = (float(current) / avg_cost - 1) * 100
            now_ts = now_local()
            market_open_today = now_ts.replace(hour=9, minute=30, second=0, microsecond=0)
            minutes_since_open = (now_ts - market_open_today).total_seconds() / 60
            if (0 < minutes_since_open <= _cfg.OPENING_TAKEPROFIT_MINUTES
                    and profit_pct >= _cfg.OPENING_TAKEPROFIT_PCT):
                # 检查前一日是否涨停（9.5% 以上涨幅）
                prev_limit_up = False
                try:
                    if is_etf(code):
                        prev_hist = loader.get_etf_history(code, days=3)
                    else:
                        prev_hist = loader.get_stock_data(normalize_code(code), days=3)
                    if prev_hist is not None and len(prev_hist) >= 2:
                        prev_close = float(prev_hist.iloc[-2]["close"])
                        prev_open = float(prev_hist.iloc[-2]["open"])
                        if prev_open > 0 and (prev_close / prev_open - 1) >= 0.095:
                            prev_limit_up = True
                except Exception:
                    pass
                if not prev_limit_up:
                    decision = ("开盘急速止盈",
                                f"开盘{minutes_since_open:.0f}分钟 盈利{profit_pct:.1f}%≥{_cfg.OPENING_TAKEPROFIT_PCT}%")
                    logger.info(
                        "[开盘止盈] %s(%s) 盈利%.1f%% 开盘%.0f分钟",
                        pos.get("name", code), code, profit_pct, minutes_since_open,
                    )
                else:
                    logger.info(
                        "[开盘止盈跳过] %s(%s) 前日涨停，今日高开%.1f%%，不触发急速止盈",
                        pos.get("name", code), code, profit_pct,
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
    """提交卖出订单，并在风控拒绝时进入冷却期。跌停等极端情况冷却到收盘。"""
    report, reject_reason = _submit_order(order, broker, risk_ctrl, market_data, recorder)
    if report is None and shared is not None:
        if "跌停" in reject_reason:
            shared.set_exit_cooldown_until_close(order.code)
        else:
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

        # 建仓间隔控制:每笔买入至少间隔 N 秒,避免一分钟打满仓位
        now_entry = time.time()
        if shared.last_entry_time > 0 and (now_entry - shared.last_entry_time) < ENTRY_INTERVAL_SECONDS:
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
        result_code, _ = _submit_order(order, broker, risk_ctrl, market_data, recorder)
        if result_code is not None and result_code.is_success:
            shared.last_entry_time = time.time()  # 记录买入时间,控制建仓节奏
        if result_code is None:
            # Risk rejected — cooldown for 5 minutes
            shared.rejected_stocks[code] = time.time() + 300

    # 最低仓位补仓:持仓占比低于阈值时,从候选池涨幅前 3 主动补仓
    total_value = broker.portfolio.get_total_value(prices)
    cash = broker.query_cash()
    position_value = total_value - cash
    position_ratio = position_value / total_value if total_value > 0 else 0.0
    if position_ratio < MIN_POSITION_RATIO and shared.is_risk_on():
        top_gainers = sorted(stocks, key=lambda s: s.get("momentum", 0), reverse=True)[:3]
        for stock in top_gainers:
            code = stock["code"]
            if normalize_code(code) in position_norms:
                continue
            if shared.is_rebuy_cooling_down(code):
                continue
            if time.time() < shared.rejected_stocks.get(code, 0):
                continue

            price = prices.get(code)
            if price is None or price <= 0:
                continue

            shares = broker.portfolio.rules.calc_lot_size(
                price,
                broker.query_cash(),
                max_ratio=MAX_SINGLE_STOCK,
                total_value=broker.portfolio.get_total_value(prices),
            )
            if shares < LOT_SIZE:
                recorder.record("signal_skipped", {
                    "code": code,
                    "reason": "最低仓位补仓可买数量不足一手",
                    "price": price,
                    "cash": broker.query_cash(),
                })
                continue

            order = OrderIntent(
                code=code,
                name=stock.get("name", code),
                action="buy",
                price=float(price),
                shares=shares,
                date=today,
                strategy="最低仓位补仓",
                reason="持仓比例不足30%",
                source="watch_thread",
            )
            result_code, _ = _submit_order(order, broker, risk_ctrl, market_data, recorder)
            if result_code is None:
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

    # 小市值价值调仓状态
    sc_state = _read_small_cap_state()
    report_lines.extend(["", "小市值价值:"])
    if sc_state.get("date") == today:
        report_lines.append(
            f"  状态={sc_state.get('status', '--')} "
            f"目标={sc_state.get('target_count', 0)}只 "
            f"订单={sc_state.get('orders', 0)} 成交={sc_state.get('executed', 0)}"
        )
    else:
        report_lines.append("  今日未调仓")

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

    # PID 锁：防止重复启动
    pid_file = os.path.join(DATA_DIR, "live_runner.pid")
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                old_pid = int(f.read().strip())
            if old_pid != os.getpid():  # 不是自己（os.execv 重启后 PID 不变）
                os.kill(old_pid, 0)  # 检查进程是否存在
                logger.error("live_runner 已在运行 (PID=%d)，退出", old_pid)
                return
        except (OSError, ValueError):
            pass  # 旧进程已死，继续
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    if not args.ignore_calendar and not is_trading_day():
        logger.info("今天不是交易日，退出")
        _cleanup_pid(pid_file)
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
    pre_scan_at = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=0, second=0, microsecond=0)

    if now >= market_close:
        # 收盘后不退出，sleep 到明天开盘再循环，避免 restart: always 空转
        next_open = (now + timedelta(days=1)).replace(hour=9, minute=15, second=0, microsecond=0)
        wait_seconds = (next_open - now).total_seconds()
        logger.info("已过收盘时间，休眠到明天 %s (%.0f 秒)", next_open.strftime("%H:%M"), wait_seconds)
        loader.close()
        broker.close()
        time.sleep(wait_seconds)
        # 休眠结束后重新启动（exec 不改变 PID，先清理 PID 文件避免误判重复）
        _cleanup_pid(pid_file)
        os.execv(sys.executable, [sys.executable] + sys.argv)
        return

    if now < market_open:
        # 9:15 预扫描：用昨收数据提前形成候选池，开盘后立即验证
        if now >= pre_scan_at:
            logger.info("===== 开盘前预扫描 =====")
            _do_scan(shared, scanner, recorder, top_n=args.top_n)
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

    # 小市值价值周度调仓
    _run_weekly_small_cap(
        broker=broker,
        loader=loader,
        risk_ctrl=risk_ctrl,
        recorder=recorder,
        shared=shared,
        today=today,
    )

    logger.info("===== 开盘：启动盯盘线 =====")
    t_watch = threading.Thread(
        target=watch_thread,
        args=(broker, shared, loader, combo, risk_ctrl, recorder, args.watch_interval),
        name="盯盘线",
        daemon=True,
    )
    t_watch.start()

    # 开盘后立即用实时价重新扫描验证候选池（覆盖预扫描的昨收数据）
    logger.info("===== 开盘后实时确认扫描 =====")
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
        _last_status_key = ""
        while now_local() < market_close:
            time.sleep(30)
            snapshot = broker.query_snapshot()
            cand_count = len(shared.get_candidates())
            status_key = f"{snapshot.total_value:.2f}_{snapshot.position_count}_{cand_count}"
            if status_key != _last_status_key:
                logger.info(
                    "[状态] 总资产=%.2f 收益=%.2f%% 持仓=%s只 候选=%s只",
                    snapshot.total_value,
                    snapshot.pnl_pct * 100,
                    snapshot.position_count,
                    cand_count,
                )
                _last_status_key = status_key
    finally:
        logger.info("===== 收盘 =====")
        t_watch.join(timeout=30)
        t_scan.join(timeout=30)
        _generate_report(broker, loader, recorder)
        loader.close()
        broker.close()
        logger.info("盯盘结束")
        _cleanup_pid(pid_file)


def _cleanup_pid(pid_file: str) -> None:
    """清理 PID 文件。"""
    try:
        os.remove(pid_file)
    except OSError:
        pass


if __name__ == "__main__":
    main()
