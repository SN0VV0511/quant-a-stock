"""
实盘盯盘系统 - 双线程版
09:00  开盘前：全市场扫描选股 → 建立今日候选池
09:30  开盘：
       线程1（盯盘线）: 每3-5秒实时刷新，止损止盈 + 买入信号
       线程2（扫描线）: 每10分钟全盘扫描，动态更新候选池
11:30  午休
13:00  下午继续
15:00  收盘：生成日报 + 持仓快照
"""

import os
import sys
import json
import time
import logging
import threading
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config.settings import (
    INITIAL_CAPITAL, COMMISSION_RATE, COMMISSION_MIN, STAMP_TAX_RATE,
    TRANSFER_FEE_RATE, SLIPPAGE_STOCK, LOT_SIZE, MAX_SINGLE_STOCK,
    MAX_TOTAL_POSITION, CASH_BUFFER, DATA_DIR, STATE_FILE,
    TRADE_LOG_FILE, REPORT_DIR, LOG_DIR,
)
from data.ak_loader import AKDataLoader
from strategies.combo_signal import ComboSignalStrategy
from strategies.market_scanner import MarketScanner
from risk.control import RiskController

# 日志配置
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "live.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ==================== 交易日判断 ====================

def is_trading_day():
    today = datetime.now()
    if today.weekday() >= 5:
        return False
    return True


# ==================== 持仓管理（线程安全） ====================

class PositionManager:
    def __init__(self):
        self.cash = INITIAL_CAPITAL
        self.positions = {}
        self.trade_log = []
        self._lock = threading.Lock()
        self._load_state()

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    state = json.load(f)
                self.cash = state.get("cash", INITIAL_CAPITAL)
                self.positions = state.get("positions", {})
                logger.info(f"加载持仓: 现金={self.cash:.2f}, 持仓={len(self.positions)}只")
            except Exception as e:
                logger.warning(f"加载状态失败: {e}")

    def save_state(self):
        state = {
            "cash": self.cash,
            "positions": self.positions,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

    def get_total_value(self, prices=None):
        with self._lock:
            total = self.cash
            for code, pos in self.positions.items():
                price = pos.get("current_price", pos["avg_cost"])
                if prices and code in prices:
                    price = prices[code]
                total += pos["shares"] * price
            return total

    def update_prices(self, prices):
        with self._lock:
            for code in self.positions:
                if code in prices:
                    self.positions[code]["current_price"] = prices[code]

    def get_position_codes(self):
        with self._lock:
            return list(self.positions.keys())

    def get_positions_snapshot(self):
        with self._lock:
            return dict(self.positions)

    def get_cash(self):
        with self._lock:
            return self.cash

    def buy(self, code, price, shares, strategy_name="", date=None):
        with self._lock:
            if date is None:
                date = datetime.now().strftime("%Y%m%d")
            cost = price * shares
            commission = max(cost * COMMISSION_RATE, COMMISSION_MIN)
            transfer_fee = cost * TRANSFER_FEE_RATE
            total_cost = cost + commission + transfer_fee

            if total_cost > self.cash:
                logger.warning(f"资金不足: 需要{total_cost:.2f}, 可用{self.cash:.2f}")
                return False

            actual_price = price * (1 + SLIPPAGE_STOCK)

            self.cash -= total_cost
            if code in self.positions:
                old = self.positions[code]
                total_shares = old["shares"] + shares
                old_cost = old["avg_cost"] * old["shares"]
                self.positions[code] = {
                    "shares": total_shares,
                    "avg_cost": (old_cost + actual_price * shares) / total_shares,
                    "buy_date": old["buy_date"],
                    "current_price": actual_price,
                }
            else:
                self.positions[code] = {
                    "shares": shares,
                    "avg_cost": actual_price,
                    "buy_date": date,
                    "current_price": actual_price,
                }

            trade = {
                "date": date,
                "time": datetime.now().strftime("%H:%M:%S"),
                "code": code,
                "action": "buy",
                "price": round(actual_price, 3),
                "shares": shares,
                "amount": round(total_cost, 2),
                "strategy": strategy_name,
            }
            self.trade_log.append(trade)
            self._append_trade_log(trade)
            logger.info(f"[买入] {code} {shares}股 @ {actual_price:.3f} ({strategy_name})")
            self.save_state()
            return True

    def sell(self, code, price, shares, strategy_name="", date=None):
        with self._lock:
            if date is None:
                date = datetime.now().strftime("%Y%m%d")
            if code not in self.positions:
                return False

            pos = self.positions[code]
            sell_shares = min(shares, pos["shares"])

            cost = price * sell_shares
            commission = max(cost * COMMISSION_RATE, COMMISSION_MIN)
            stamp_tax = cost * STAMP_TAX_RATE
            transfer_fee = cost * TRANSFER_FEE_RATE
            total_cost = commission + stamp_tax + transfer_fee

            actual_price = price * (1 - SLIPPAGE_STOCK)
            proceeds = cost - total_cost
            self.cash += proceeds

            pnl = (actual_price - pos["avg_cost"]) / pos["avg_cost"]

            trade = {
                "date": date,
                "time": datetime.now().strftime("%H:%M:%S"),
                "code": code,
                "action": "sell",
                "price": round(actual_price, 3),
                "shares": sell_shares,
                "amount": round(proceeds, 2),
                "pnl": round(pnl, 4),
                "strategy": strategy_name,
            }
            self.trade_log.append(trade)
            self._append_trade_log(trade)

            remaining = pos["shares"] - sell_shares
            if remaining <= 0:
                del self.positions[code]
            else:
                self.positions[code]["shares"] = remaining

            logger.info(f"[卖出] {code} {sell_shares}股 @ {actual_price:.3f} 盈亏={pnl:.2%} ({strategy_name})")
            self.save_state()
            return True

    def _append_trade_log(self, trade):
        os.makedirs(os.path.dirname(TRADE_LOG_FILE), exist_ok=True)
        with open(TRADE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(trade, ensure_ascii=False) + "\n")


# ==================== 共享状态 ====================

class SharedState:
    """线程间共享的候选池状态"""
    def __init__(self):
        self._lock = threading.Lock()
        self.top_stocks = []        # [{code, name, score, ...}]
        self.candidate_codes = []   # [code, ...]
        self.candidate_hist = {}    # {code: DataFrame} 候选股历史数据缓存
        self.last_scan_time = None
        self.scanning = False       # 扫描进行中标记

    def update(self, stocks):
        with self._lock:
            self.top_stocks = stocks
            self.candidate_codes = [s["code"] for s in stocks]
            self.last_scan_time = datetime.now()

    def update_hist(self, code, df):
        with self._lock:
            self.candidate_hist[code] = df

    def get_hist(self, code):
        with self._lock:
            return self.candidate_hist.get(code)

    def get_candidates(self):
        with self._lock:
            return list(self.candidate_codes)

    def get_stocks(self):
        with self._lock:
            return list(self.top_stocks)

    def is_scanning(self):
        with self._lock:
            return self.scanning

    def set_scanning(self, val):
        with self._lock:
            self.scanning = val


# ==================== 线程1：盯盘线（3-5秒刷新） ====================

def watch_thread(pm, shared, loader, combo, interval=4):
    """盯盘线：实时监控持仓止损止盈 + 候选股买入信号

    Args:
        pm: PositionManager
        shared: SharedState
        loader: AKDataLoader
        combo: ComboSignalStrategy
        interval: 刷新间隔（秒）
    """
    logger.info("盯盘线启动")

    while True:
        now = datetime.now()
        market_close = now.replace(hour=15, minute=0, second=0)
        lunch_start = now.replace(hour=11, minute=30, second=0)
        lunch_end = now.replace(hour=13, minute=0, second=0)

        # 收盘退出
        if now >= market_close:
            logger.info("盯盘线：收盘，退出")
            break

        # 午休暂停
        if lunch_start <= now < lunch_end:
            time.sleep(30)
            continue

        try:
            candidates = shared.get_candidates()
            pos_codes = pm.get_position_codes()
            watch_codes_raw = list(set(pos_codes + candidates))

            if not watch_codes_raw:
                time.sleep(interval)
                continue

            # 统一代码格式：sh.600039/sh601318 → 601318
            def norm(c):
                if c.startswith("sh.") or c.startswith("sz."): return c[3:]
                if c.startswith("sh") or c.startswith("sz"): return c[2:]
                return c

            code_map = {norm(c): c for c in watch_codes_raw}  # raw → original
            watch_codes = list(code_map.keys())

            # 批量获取实时价格（腾讯接口，~1-2秒）
            prices_raw = loader.get_realtime_batch(watch_codes)
            # 映射回原始代码
            prices = {code_map.get(k, k): v for k, v in prices_raw.items()}
            pm.update_prices(prices)

            # ---- 持仓止损止盈 ----
            positions = pm.get_positions_snapshot()
            for code in list(positions.keys()):
                pos = positions[code]
                current = prices.get(code, pos.get("current_price", 0))
                if current <= 0:
                    continue

                pnl_pct = (current - pos["avg_cost"]) / pos["avg_cost"]

                # 止损 7%
                if pnl_pct <= -0.07:
                    if pm.sell(code, current, pos["shares"], "止损"):
                        logger.warning(f"[止损] {code} 亏损={pnl_pct:.2%}")
                    continue

                # 止盈 10% + 均线确认
                if pnl_pct >= 0.10:
                    hist = loader.get_stock_data(code, days=30)
                    if hist is not None and len(hist) >= 20:
                        ma20 = pd.to_numeric(hist["close"], errors="coerce").tail(20).mean()
                        if current < ma20:
                            if pm.sell(code, current, pos["shares"], "止盈"):
                                logger.info(f"[止盈] {code} 盈利={pnl_pct:.2%} 跌破MA20")
                            continue

            # ---- 候选股买入信号 ----
            for code in candidates:
                if code in positions:
                    continue

                # 优先用预加载的历史数据，没有再实时拉
                hist = shared.get_hist(code)
                if hist is None or len(hist) < 25:
                    hist = loader.get_stock_data(code, days=60)
                    if hist is not None:
                        shared.update_hist(code, hist)
                if hist is None or len(hist) < 25:
                    continue

                # 把腾讯实时价拼入历史数据，用实时价算信号
                q = prices.get(code) or prices.get(norm(code)) or {}
                real_price = q.get("price", 0) if isinstance(q, dict) else 0
                if real_price > 0:
                    import pandas as _pd
                    new_row = hist.iloc[-1:].copy()
                    new_row["close"] = real_price
                    hist = _pd.concat([hist, new_row], ignore_index=True)

                result = combo.check_realtime(hist)
                if result["signal"] == "buy":
                    q = prices.get(code) or prices.get(norm(code)) or {}
                    price = q.get("price", 0) if isinstance(q, dict) else 0
                    if price <= 0:
                        continue
                    available = pm.get_cash() * (1 - CASH_BUFFER) * MAX_SINGLE_STOCK
                    shares = int(available / price / LOT_SIZE) * LOT_SIZE
                    if shares >= LOT_SIZE:
                        stocks = shared.get_stocks()
                        name = next((s["name"] for s in stocks if s["code"] == code), code)
                        if pm.buy(code, price, shares, f"组合策略({name})"):
                            logger.info(f"[买入] {name}({code}) {shares}股 @ {price:.3f} 原因:{result['reason']}")

        except Exception as e:
            logger.error(f"盯盘线异常: {e}", exc_info=True)

        time.sleep(interval)

    logger.info("盯盘线退出")


# ==================== 线程2：扫描线（全盘扫描） ====================

def scan_thread(pm, shared, scanner, top_n=20, scan_interval=600):
    """扫描线：定期全盘扫描，动态更新候选池

    Args:
        pm: PositionManager
        shared: SharedState
        scanner: MarketScanner
        top_n: 候选股数量
        scan_interval: 扫描间隔（秒），默认10分钟
    """
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

        # 等待扫描间隔
        time.sleep(scan_interval)

        if now >= market_close:
            break

        _do_scan(shared, scanner, top_n)

    logger.info("扫描线退出")


def _do_scan(shared, scanner, top_n):
    """执行一次全盘扫描"""
    if shared.is_scanning():
        logger.info("上一次扫描未完成，跳过")
        return

    shared.set_scanning(True)
    try:
        logger.info("===== 开始全盘扫描 =====")
        stocks = scanner.scan(top_n=top_n, momentum_period=60)
        shared.update(stocks)
        logger.info(f"扫描完成，候选股 {len(stocks)} 只:")
        for s in stocks[:10]:
            logger.info(f"  #{s['rank']} {s['name']}({s['code']}) 动量={s['momentum']:+.2%} 得分={s['score']:.4f}")

        # 预加载候选股历史数据
        logger.info("预加载候选股历史数据...")
        from data.ak_loader import AKDataLoader
        hist_loader = AKDataLoader()
        for s in stocks:
            df = hist_loader.get_stock_history(s["code"], days=60)
            if df is not None:
                shared.update_hist(s["code"], df)
            time.sleep(0.05)
        hist_loader.close()
        logger.info(f"历史数据预加载完成: {len(stocks)} 只")

    except Exception as e:
        logger.error(f"扫描失败: {e}", exc_info=True)
    finally:
        shared.set_scanning(False)


# ==================== 主程序 ====================

def main():
    if not is_trading_day():
        logger.info("今天不是交易日，退出")
        return

    now = datetime.now()
    logger.info("=" * 60)
    logger.info(f"实盘盯盘系统启动（双线程版）: {now.strftime('%Y-%m-%d %H:%M:%S')}")

    pm = PositionManager()
    loader = AKDataLoader()
    combo = ComboSignalStrategy()
    scanner = MarketScanner(loader=loader)
    shared = SharedState()

    # 时间节点
    pre_market = now.replace(hour=9, minute=0, second=0)
    market_open = now.replace(hour=9, minute=30, second=0)
    market_close = now.replace(hour=15, minute=0, second=0)

    # 如果还没到开盘时间，等待
    if now < pre_market:
        wait_sec = (pre_market - now).total_seconds()
        logger.info(f"等待开盘前扫描... {wait_sec:.0f}秒后开始")
        time.sleep(wait_sec)

    # 开盘前首次扫描
    _do_scan(shared, scanner, top_n=20)

    # 等待开盘
    if datetime.now() < market_open:
        logger.info("等待开盘...")
        while datetime.now() < market_open:
            time.sleep(5)

    # 启动双线程
    logger.info("===== 开盘：启动双线程 =====")

    t_watch = threading.Thread(
        target=watch_thread,
        args=(pm, shared, loader, combo, 4),  # 4秒刷新
        name="盯盘线",
        daemon=True,
    )
    t_scan = threading.Thread(
        target=scan_thread,
        args=(pm, shared, scanner, 20, 600),  # 10分钟扫一次
        name="扫描线",
        daemon=True,
    )

    t_watch.start()
    t_scan.start()

    # 主线程等待收盘
    while datetime.now() < market_close:
        time.sleep(30)
        # 每5分钟打印一次总状态
        if datetime.now().minute % 5 == 0 and datetime.now().second < 5:
            total = pm.get_total_value()
            pnl = (total - INITIAL_CAPITAL) / INITIAL_CAPITAL
            pos_count = len(pm.get_position_codes())
            cand_count = len(shared.get_candidates())
            logger.info(f"[状态] 总资产={total:.2f} 收益={pnl:.2%} 持仓={pos_count}只 候选={cand_count}只")

    # 收盘：等待线程退出
    logger.info("===== 收盘 =====")
    t_watch.join(timeout=30)
    t_scan.join(timeout=30)

    # 生成日报
    _generate_report(pm, loader)


def _generate_report(pm, loader):
    """生成收盘日报"""
    logger.info("生成日报...")
    pos_codes = pm.get_position_codes()
    if pos_codes:
        prices = loader.get_realtime_batch(pos_codes)
        pm.update_prices(prices)

    total_value = pm.get_total_value()
    pnl = (total_value - INITIAL_CAPITAL) / INITIAL_CAPITAL

    today_str = datetime.now().strftime("%Y%m%d")
    today_trades = [t for t in pm.trade_log if t.get("date") == today_str]

    report_lines = [
        "=" * 50,
        f"量化日报 - {datetime.now().strftime('%Y-%m-%d')}（双线程版）",
        "=" * 50,
        f"初始资金: ¥{INITIAL_CAPITAL:,.2f}",
        f"当前总值: ¥{total_value:,.2f}",
        f"总收益率: {pnl:+.2%}",
        f"今日交易: {len(today_trades)}笔",
        "",
        "持仓明细:",
    ]

    positions = pm.get_positions_snapshot()
    for code, pos in positions.items():
        current = pos.get("current_price", pos["avg_cost"])
        pnl_pct = (current - pos["avg_cost"]) / pos["avg_cost"]
        report_lines.append(
            f"  {code}: {pos['shares']}股 成本={pos['avg_cost']:.3f} "
            f"现价={current:.3f} 盈亏={pnl_pct:+.2%}"
        )

    if not positions:
        report_lines.append("  (空仓)")

    report_lines.append("")
    report_lines.append("今日交易:")
    for t in today_trades:
        report_lines.append(
            f"  {t['time']} {t['action']} {t['code']} {t['shares']}股 @ {t['price']:.3f}"
        )
    if not today_trades:
        report_lines.append("  (无交易)")

    report_lines.append("=" * 50)
    report_text = "\n".join(report_lines)
    logger.info("\n" + report_text)

    report_file = os.path.join(REPORT_DIR, f"daily_{today_str}.txt")
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(report_text)
    logger.info(f"日报已保存: {report_file}")

    pm.save_state()
    logger.info("盯盘结束")


if __name__ == "__main__":
    main()
