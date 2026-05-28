"""
每日模拟盘执行脚本
流程：交易日判断 -> 更新数据 -> 策略信号 -> 风控 -> 执行 -> 对账 -> 日报
"""

import os
import sys
import logging
from datetime import datetime, timedelta

# 添加项目根目录到 sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import (
    INITIAL_CAPITAL, DEFAULT_ETF_PROXY_POOL, DEFAULT_STOCK_POOL,
    DEFAULT_UNIVERSE, get_etf_codes, get_stock_codes, get_all_codes,
    is_etf, MAX_SINGLE_ETF, MAX_SINGLE_STOCK,
)
from data.loader import DataLoader
from data.ak_loader import AKDataLoader
from rules.position import PositionManager
from risk.control import RiskController
from strategies.etf_momentum import ETFRotationStrategy
from strategies.stock_selection import MainboardStockStrategy
from strategies.market_scanner import MarketScanner
from reports.generator import ReportGenerator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(os.path.dirname(__file__), "logs", "daily_runner.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("daily_runner")


class DailyRunner:
    """每日模拟盘执行器"""

    def __init__(self):
        self.loader = DataLoader()
        self.ak_loader = AKDataLoader()
        self.portfolio = PositionManager()
        self.risk_ctrl = RiskController()
        self.reporter = ReportGenerator()
        self.etf_strategy = ETFRotationStrategy()
        self.stock_strategy = MainboardStockStrategy()
        self.scanner = MarketScanner(loader=self.ak_loader)

    def run(self, date_str=None):
        """执行每日流程

        Args:
            date_str: 指定日期 YYYYMMDD，None 则使用今天

        Returns:
            dict: 执行结果
        """
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")
        date_str = date_str.replace("-", "")

        logger.info(f"========== 每日执行开始 {date_str} ==========")

        result = {
            "date": date_str,
            "is_trading_day": False,
            "trades": [],
            "signals": [],
            "risk_events": [],
            "report": "",
        }

        try:
            # 1. 检查是否为交易日
            if not self.loader.is_trading_day(date_str):
                logger.info(f"{date_str} 不是交易日，跳过")
                result["report"] = f"{date_str} 不是交易日"
                return result

            result["is_trading_day"] = True

            # 2. 记录日初市值
            self.risk_ctrl.set_daily_start(self.portfolio)
            logger.info(f"日初市值: {self.portfolio.get_total_value():,.2f} 元")

            # 3. 更新行情数据
            all_codes = get_all_codes()
            current_prices = self.loader.get_batch_latest_prices(all_codes)
            logger.info(f"获取到 {len(current_prices)} 只标的价格")

            if not current_prices:
                logger.error("未获取到任何价格数据")
                result["report"] = "行情数据获取失败"
                return result

            # 4. 保存日初快照
            self.portfolio.save_snapshot(date_str, current_prices)

            # 5. 准备策略数据
            # 加载近 120 天数据用于策略计算
            start_dt = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=180)).strftime("%Y%m%d")

            etf_data = {}
            for code in get_etf_codes():
                try:
                    df = self.loader.get_daily_data(code, start_dt, date_str, adjust_flag="2")
                    if df is not None and not df.empty:
                        etf_data[code] = df
                except Exception as e:
                    logger.warning(f"加载 ETF 数据失败 {code}: {e}")

            stock_data = {}
            for code in get_stock_codes():
                try:
                    df = self.loader.get_daily_data(code, start_dt, date_str, adjust_flag="2")
                    if df is not None and not df.empty:
                        stock_data[code] = df
                except Exception as e:
                    logger.warning(f"加载股票数据失败 {code}: {e}")

            # 6. 运行 ETF 动量策略
            current_positions = self.portfolio.get_all_positions()
            all_orders = []

            try:
                etf_orders = self.etf_strategy.generate_orders(
                    etf_data, current_positions, date_str
                )
                all_orders.extend(etf_orders)
                logger.info(f"ETF 策略生成 {len(etf_orders)} 个订单")
            except Exception as e:
                logger.error(f"ETF 策略执行失败: {e}")

            # 7. 全市场扫描选股（替代原有固定股票池选股）
            scanner_orders = []
            try:
                # 周一才做扫描调仓
                from datetime import datetime as dt
                current_dt = dt.strptime(date_str, "%Y%m%d")
                if current_dt.weekday() == 0:  # 周一
                    logger.info("周一扫描日，执行全市场扫描...")
                    scan_results = self.scanner.scan(date=date_str, top_n=5)
                    if scan_results:
                        logger.info(f"扫描选出 {len(scan_results)} 只股票")
                        # 卖出不在扫描结果中的持仓
                        scan_codes = {r["code"] for r in scan_results}
                        for code, pos in list(current_positions.items()):
                            if is_etf(code):
                                continue
                            if code not in scan_codes:
                                scanner_orders.append({
                                    "code": code,
                                    "action": "sell",
                                    "shares": pos["shares"],
                                    "price": 0,
                                    "reason": f"扫描调仓卖出（不在Top5）",
                                    "strategy": "全市场扫描",
                                })
                        # 买入新扫描到的股票
                        for r in scan_results:
                            code = r["code"]
                            if code not in current_positions:
                                scanner_orders.append({
                                    "code": code,
                                    "action": "buy",
                                    "shares": 0,
                                    "price": r["price"],
                                    "reason": f"扫描选入 #{r['rank']} 动量={r['momentum']:+.2%}",
                                    "strategy": "全市场扫描",
                                })
                    else:
                        logger.warning("扫描无结果")
                else:
                    # 非周一，检查止损
                    scan_results = self.scanner.scan(date=date_str, top_n=5)
                    if scan_results:
                        scan_codes = {r["code"] for r in scan_results}
                        for code, pos in list(current_positions.items()):
                            if is_etf(code):
                                continue
                            # 跌出 Top5 且亏损超过 7%，触发止损
                            if code not in scan_codes:
                                # 获取当前价格
                                if code in current_prices:
                                    cur_price = current_prices[code]
                                    pnl = (cur_price - pos["avg_cost"]) / pos["avg_cost"]
                                    if pnl < -0.07:
                                        scanner_orders.append({
                                            "code": code,
                                            "action": "sell",
                                            "shares": pos["shares"],
                                            "price": cur_price,
                                            "reason": f"止损卖出（亏损 {pnl:.1%}，跌出Top5）",
                                            "strategy": "全市场扫描",
                                        })

                all_orders.extend(scanner_orders)
                logger.info(f"扫描策略生成 {len(scanner_orders)} 个订单")
            except Exception as e:
                logger.error(f"扫描策略执行失败: {e}")

            # 8. 为买入订单计算具体数量
            for order in all_orders:
                if order["action"] == "buy" and order.get("shares", 0) <= 0:
                    # 根据策略来源判断仓位限制
                    strategy_name = order.get("strategy", "")
                    if "ETF" in strategy_name or "动量" in strategy_name:
                        max_ratio = MAX_SINGLE_ETF
                    else:
                        max_ratio = MAX_SINGLE_STOCK
                    total_value = self.portfolio.get_total_value(current_prices)
                    price = order.get("price", 0)
                    if price <= 0 and order["code"] in current_prices:
                        price = current_prices[order["code"]]
                        order["price"] = price
                    if price > 0:
                        order["shares"] = self.portfolio.rules.calc_lot_size(
                            price, self.portfolio.get_cash(),
                            max_ratio=max_ratio, total_value=total_value
                        )

            # 9. 构建市场数据（用于风控）
            market_data = {}
            for code, price in current_prices.items():
                market_data[code] = {
                    "current_price": price,
                    "prev_close": None,
                    "is_st": False,
                    "is_suspended": False,
                }

            # 10. 风控过滤
            approved, rejected = self.risk_ctrl.filter_orders(
                all_orders, self.portfolio, market_data
            )

            result["risk_events"] = rejected
            for rej in rejected:
                logger.info(f"风控拒绝: {rej['order']['code']} {rej['order']['action']} - {rej['reason']}")

            # 11. 执行交易
            today_trades = []
            for order in approved:
                code = order["code"]
                name = DEFAULT_UNIVERSE.get(code, {}).get("name", code)
                price = order["price"]

                # 使用实际收盘价
                if code in current_prices:
                    price = current_prices[code]
                    order["price"] = price

                if order["action"] == "buy":
                    trade_result = self.portfolio.buy(
                        code, name, price, order["shares"],
                        date_str, order.get("strategy", "daily")
                    )
                    if trade_result["success"]:
                        today_trades.append({
                            "direction": "buy",
                            "code": code,
                            "name": name,
                            "price": price,
                            "shares": trade_result["shares"],
                            "amount": round(price * trade_result["shares"], 2),
                            "cost": trade_result["cost_detail"]["total"],
                            "strategy": order.get("strategy", ""),
                        })
                        logger.info(f"买入 {name}({code}) {trade_result['shares']}股 @ {price:.3f}")

                elif order["action"] == "sell":
                    trade_result = self.portfolio.sell(
                        code, price, order["shares"],
                        date_str, order.get("strategy", "daily")
                    )
                    if trade_result["success"]:
                        today_trades.append({
                            "direction": "sell",
                            "code": code,
                            "name": name,
                            "price": price,
                            "shares": trade_result["shares"],
                            "amount": round(price * trade_result["shares"], 2),
                            "profit": trade_result["profit"],
                            "strategy": order.get("strategy", ""),
                        })
                        logger.info(f"卖出 {name}({code}) {trade_result['shares']}股 @ {price:.3f} 盈亏 {trade_result['profit']:+,.2f}")

            result["trades"] = today_trades

            # 12. 更新收盘价快照
            self.portfolio.save_snapshot(date_str, current_prices)

            # 13. 对账
            self._reconcile(date_str, current_prices)

            # 14. 生成日报
            signals = []
            for order in all_orders:
                signals.append({
                    "code": order["code"],
                    "signal": order["action"],
                    "reason": order.get("reason", ""),
                })
            result["signals"] = signals

            report = self.reporter.daily_report(
                date_str, self.portfolio,
                today_trades, signals, rejected
            )
            result["report"] = report

            logger.info(f"========== 每日执行完成 {date_str} ==========")

        except Exception as e:
            logger.error(f"每日执行异常: {e}", exc_info=True)
            result["error"] = str(e)

        finally:
            self.loader.close()
            self.ak_loader.close()

        return result

    def _reconcile(self, date_str, current_prices):
        """对账检查

        Args:
            date_str: 日期
            current_prices: 当前价格
        """
        # 检查市值一致性
        total_value = self.portfolio.get_total_value(current_prices)
        cash = self.portfolio.get_cash()

        positions_value = 0.0
        for code, pos in self.portfolio.get_all_positions().items():
            price = current_prices.get(code, pos["avg_cost"])
            positions_value += price * pos["shares"]

        expected_total = cash + positions_value
        diff = abs(total_value - expected_total)

        if diff > 0.01:
            logger.warning(f"对账差异: 总值 {total_value:.2f} != 现金 {cash:.2f} + 持仓 {positions_value:.2f}，差异 {diff:.2f}")
        else:
            logger.info(f"对账通过: 总值 {total_value:,.2f} 元")


def main():
    """命令行入口"""
    import argparse
    parser = argparse.ArgumentParser(description="A 股量化系统 - 每日执行")
    parser.add_argument("--date", help="指定日期 YYYYMMDD", default=None)
    parser.add_argument("--dry-run", action="store_true", help="模拟运行（不保存状态）")
    args = parser.parse_args()

    runner = DailyRunner()
    result = runner.run(args.date)

    if result.get("report"):
        print(result["report"])
    else:
        print(f"执行完成: {result['date']}")
        if result.get("error"):
            print(f"错误: {result['error']}")


if __name__ == "__main__":
    main()
