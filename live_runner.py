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
import logging
import os
import sys
import threading
import time
from datetime import datetime
from typing import Any

import pandas as pd

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config.settings import (  # noqa: E402
    BROKER_MODE,
    CASH_BUFFER,
    INITIAL_CAPITAL,
    LIVE_SCAN_INTERVAL_SECONDS,
    LIVE_WATCH_INTERVAL_SECONDS,
    LOG_DIR,
    LOT_SIZE,
    MAX_SINGLE_STOCK,
    REPORT_DIR,
    normalize_a_share_code,
)
from data.ak_loader import AKDataLoader  # noqa: E402
from data.holidays import is_trading_day as is_calendar_trading_day  # noqa: E402
from risk.control import RiskController  # noqa: E402
from strategies.combo_signal import ComboSignalStrategy  # noqa: E402
from strategies.market_scanner import MarketScanner  # noqa: E402
from trading.brokers import PaperBrokerAdapter, create_broker  # noqa: E402
from trading.models import ExecutionReport, OrderIntent, RiskDecision  # noqa: E402
from trading.observability import EventRecorder  # noqa: E402


def _setup_logger() -> logging.Logger:
    """初始化实时运行日志。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger("live_runner")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s")
    for filename in ("live.log", "live_today.log"):
        handler = logging.FileHandler(os.path.join(LOG_DIR, filename), encoding="utf-8")
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    return logger


logger = _setup_logger()


class SharedState:
    """线程间共享候选池。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.top_stocks: list[dict[str, Any]] = []
        self.candidate_codes: list[str] = []
        self.candidate_hist: dict[str, pd.DataFrame] = {}
        self.rejected_stocks: dict[str, float] = {}  # code -> cooldown_until timestamp
        self.last_scan_time: datetime | None = None
        self.scanning = False

    def update(self, stocks: list[dict[str, Any]]) -> None:
        """更新候选股池。"""
        with self._lock:
            self.top_stocks = stocks
            self.candidate_codes = [s["code"] for s in stocks]
            self.rejected_stocks.clear()  # Reset cooldown on new scan
            self.last_scan_time = datetime.now()

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


def normalize_code(code: str) -> str:
    """统一为不带市场前缀的沪深 A 股 6 位代码。"""
    try:
        return normalize_a_share_code(code)
    except ValueError:
        logger.warning("忽略无法归一化的非沪深 A 股代码: %s", code)
        return str(code).strip().lower()


def is_trading_day(date_str: str | None = None) -> bool:
    """判断是否为交易日。"""
    target = date_str or datetime.now().strftime("%Y%m%d")
    try:
        return is_calendar_trading_day(target)
    except Exception as exc:
        logger.warning("交易日历判断失败，回退到周末判断: %s", exc)
        return datetime.strptime(target, "%Y%m%d").weekday() < 5


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
            "is_st": "ST" in name.upper() or "退" in name,
            "is_suspended": float(quote.get("price", 0) or 0) <= 0,
        }
    return market_data


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
        now = datetime.now()
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

            _handle_position_exits(broker, positions, prices, loader, risk_ctrl, market_data, recorder, combo=combo)
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
    combo: ComboSignalStrategy = None,
) -> None:
    """处理持仓止损/止盈。"""
    today = datetime.now().strftime("%Y%m%d")
    for code, pos in positions.items():
        current = prices.get(code) or pos.get("current_price") or pos.get("avg_cost", 0)
        avg_cost = float(pos.get("avg_cost", 0) or 0)
        shares = int(pos.get("shares", 0) or 0)
        if current <= 0 or avg_cost <= 0 or shares <= 0:
            continue

        pnl_pct = (current - avg_cost) / avg_cost

        # 策略信号检查（RSI超买/死叉等）
        if combo is not None:
            try:
                hist = loader.get_stock_data(normalize_code(code), days=60)
                if hist is not None and len(hist) > 25:
                    sig = combo.check_realtime(hist)
                    if sig.get("signal") == "sell":
                        order = OrderIntent(
                            code=code,
                            name=str(pos.get("name", code)),
                            action="sell",
                            price=float(current),
                            shares=shares,
                            date=today,
                            strategy="策略卖出",
                            reason=sig.get("reason", "策略信号"),
                            source="watch_thread",
                        )
                        _submit_order(order, broker, risk_ctrl, market_data, recorder)
                        continue
            except Exception:
                pass

        if pnl_pct <= -0.07:
            order = OrderIntent(
                code=code,
                name=str(pos.get("name", code)),
                action="sell",
                price=float(current),
                shares=shares,
                date=today,
                strategy="止损",
                reason=f"盘中止损，亏损 {pnl_pct:.2%}",
                source="watch_thread",
            )
            _submit_order(order, broker, risk_ctrl, market_data, recorder)
            continue

        if pnl_pct >= 0.10:
            hist = loader.get_stock_data(normalize_code(code), days=30)
            if hist is None or len(hist) < 20:
                continue
            ma20 = pd.to_numeric(hist["close"], errors="coerce").tail(20).mean()
            if current < ma20:
                order = OrderIntent(
                    code=code,
                    name=str(pos.get("name", code)),
                    action="sell",
                    price=float(current),
                    shares=shares,
                    date=today,
                    strategy="止盈",
                    reason=f"盘中止盈，盈利 {pnl_pct:.2%} 且跌破 MA20",
                    source="watch_thread",
                )
                _submit_order(order, broker, risk_ctrl, market_data, recorder)


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
    today = datetime.now().strftime("%Y%m%d")
    positions = broker.query_positions()
    position_norms = {normalize_code(code) for code in positions}
    stocks = shared.get_stocks()

    for code in shared.get_candidates():
        if normalize_code(code) in position_norms:
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

        realtime_hist = hist
        if price > 0:
            new_row = hist.iloc[-1:].copy()
            new_row["close"] = price
            realtime_hist = pd.concat([hist, new_row], ignore_index=True)

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
            metadata={"rsi": result.get("rsi"), "cash_buffer": CASH_BUFFER},
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
        now = datetime.now()
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
        if datetime.now() >= market_close:
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
        finally:
            hist_loader.close()

    except Exception as exc:
        logger.error("扫描失败: %s", exc, exc_info=True)
        recorder.record("scan_failed", {"error": str(exc)})
    finally:
        shared.set_scanning(False)


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

    today = datetime.now().strftime("%Y%m%d")
    broker.portfolio.save_snapshot(today, prices)
    snapshot = broker.query_snapshot(prices)
    recorder.record("portfolio_snapshot_close", snapshot.to_dict())

    today_trades = broker.portfolio.get_trades_by_date(today)
    report_lines = [
        "=" * 50,
        f"量化日报 - {datetime.now().strftime('%Y-%m-%d')}（虚拟盘）",
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
    parser.add_argument("--top-n", type=int, default=30, help="候选股数量")
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

    now = datetime.now()
    logger.info("=" * 60)
    logger.info("虚拟盘盯盘系统启动: %s", now.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("Broker=%s watch_interval=%ss scan_interval=%ss", args.broker, args.watch_interval, args.scan_interval)

    pre_market = now.replace(hour=9, minute=0, second=0)
    market_open = now.replace(hour=9, minute=30, second=0)
    market_close = now.replace(hour=15, minute=0, second=0)

    if now < pre_market:
        wait_sec = (pre_market - now).total_seconds()
        logger.info("等待开盘前扫描... %.0f 秒后开始", wait_sec)
        time.sleep(wait_sec)

    _do_scan(shared, scanner, recorder, top_n=args.top_n)

    if datetime.now() < market_open:
        logger.info("等待开盘...")
        while datetime.now() < market_open:
            time.sleep(5)

    logger.info("===== 开盘：启动双线程 =====")
    t_watch = threading.Thread(
        target=watch_thread,
        args=(broker, shared, loader, combo, risk_ctrl, recorder, args.watch_interval),
        name="盯盘线",
        daemon=True,
    )
    t_scan = threading.Thread(
        target=scan_thread,
        args=(shared, scanner, recorder, args.top_n, args.scan_interval),
        name="扫描线",
        daemon=True,
    )
    t_watch.start()
    t_scan.start()

    try:
        while datetime.now() < market_close:
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
