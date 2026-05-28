"""
回测引擎
基于规则引擎的完整回测，支持 T+1、涨跌停、整手交易
"""

import logging
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

from config.settings import (
    INITIAL_CAPITAL, is_etf, DEFAULT_ETF_POOL, DEFAULT_STOCK_POOL,
    MAX_SINGLE_ETF, MAX_SINGLE_STOCK,
)
from rules.engine import TradingRules
from rules.position import PositionManager
from risk.control import RiskController
from data.loader import DataLoader

logger = logging.getLogger(__name__)


class BacktestEngine:
    """回测引擎"""

    def __init__(self, initial_capital=None, state_file=None):
        self.initial_capital = initial_capital or INITIAL_CAPITAL
        self.rules = TradingRules()
        self.risk_ctrl = RiskController()
        self.results = None

    def run(self, strategy, data_loader=None, start_date=None, end_date=None,
            universe=None, benchmark_code="sh000300"):
        """运行回测

        Args:
            strategy: 策略实例（需实现 calculate_signals / generate_orders）
            data_loader: DataLoader 实例
            start_date: YYYYMMDD
            end_date: YYYYMMDD
            universe: 标的池 dict {code: info}
            benchmark_code: 基准代码

        Returns:
            dict: 回测结果
        """
        if isinstance(strategy, pd.DataFrame):
            return self._run_signal_dataframe(strategy)

        if data_loader is None or start_date is None or end_date is None:
            raise ValueError("事件驱动回测需要提供 strategy、data_loader、start_date、end_date")

        start_date = start_date.replace("-", "")
        end_date = end_date.replace("-", "")

        # 获取交易日历
        trading_days = data_loader.get_trading_calendar(start_date, end_date)
        if not trading_days:
            logger.error("无法获取交易日历")
            return {}

        logger.info(f"回测期间: {start_date} - {end_date}，共 {len(trading_days)} 个交易日")

        # 加载数据
        if universe is None:
            universe = {}
            universe.update(DEFAULT_ETF_POOL)
            universe.update(DEFAULT_STOCK_POOL)

        data_cache = {}
        # 预加载所有标的数据（提前 120 天用于计算指标）
        preload_start = (datetime.strptime(start_date, "%Y%m%d") - timedelta(days=180)).strftime("%Y%m%d")
        for code in universe:
            try:
                df = data_loader.get_daily_data(code, preload_start, end_date, adjust_flag="2")
                if df is not None and not df.empty:
                    data_cache[code] = df
            except Exception as e:
                logger.warning(f"加载数据失败 {code}: {e}")

        # 初始化持仓管理器（使用临时文件避免污染真实状态）
        import tempfile
        temp_state = tempfile.mktemp(suffix=".json")
        portfolio = PositionManager(state_file=temp_state)
        portfolio.state["cash"] = self.initial_capital
        portfolio.save()

        # 回测主循环
        daily_values = []
        all_trades = []
        risk_events = []

        for i, date in enumerate(trading_days):
            # 获取当日各标的收盘价
            current_prices = {}
            for code, df in data_cache.items():
                day_data = df[df["date"] == date]
                if not day_data.empty:
                    price = day_data["close"].iloc[0]
                    if pd.notna(price) and price > 0:
                        current_prices[code] = float(price)

            if not current_prices:
                continue

            # 记录日初市值
            self.risk_ctrl.set_daily_start(portfolio)

            # 获取当前持仓
            current_positions = portfolio.get_all_positions()

            # 生成信号/订单
            try:
                if hasattr(strategy, "generate_orders"):
                    orders = strategy.generate_orders(
                        data_cache, current_positions, date
                    )
                elif hasattr(strategy, "calculate_signals"):
                    # 只对策略相关的标的生成信号
                    strategy_data = {code: data_cache.get(code) for code in universe if code in data_cache}
                    signals = strategy.calculate_signals(strategy_data, date)
                    orders = self._signals_to_orders(signals, current_positions)
                else:
                    orders = []
            except Exception as e:
                logger.error(f"策略执行失败 {date}: {e}")
                orders = []

            # 为买入订单计算具体股数
            for order in orders:
                if order["action"] == "buy" and order.get("shares", 0) <= 0:
                    max_ratio = MAX_SINGLE_ETF if is_etf(order["code"]) else MAX_SINGLE_STOCK
                    total_value = portfolio.get_total_value(current_prices)
                    order["shares"] = self.rules.calc_lot_size(
                        order["price"], portfolio.get_cash(),
                        max_ratio=max_ratio, total_value=total_value
                    )

            # 风控过滤
            market_data = {}
            for code, price in current_prices.items():
                prev_date_idx = i - 1 if i > 0 else 0
                prev_close = None
                if prev_date_idx < len(trading_days):
                    prev_date = trading_days[prev_date_idx]
                    if code in data_cache:
                        prev_data = data_cache[code]
                        prev_row = prev_data[prev_data["date"] == prev_date]
                        if not prev_row.empty:
                            prev_close = float(prev_row["close"].iloc[0]) if pd.notna(prev_row["close"].iloc[0]) else None

                market_data[code] = {
                    "current_price": price,
                    "prev_close": prev_close,
                    "is_st": False,
                    "is_suspended": False,
                }

            approved, rejected = self.risk_ctrl.filter_orders(
                orders, portfolio, market_data
            )

            for rej in rejected:
                risk_events.append({
                    "date": date,
                    "code": rej["order"]["code"],
                    "action": rej["order"]["action"],
                    "reason": rej["reason"],
                })

            # 执行交易
            for order in approved:
                code = order["code"]
                name = universe.get(code, {}).get("name", code)
                price = order["price"]

                # 使用实际收盘价（如果有的话，用 current_prices）
                if code in current_prices:
                    price = current_prices[code]

                order["price"] = price
                order["date"] = date

                if order["action"] == "buy":
                    result = portfolio.buy(
                        code, name, price, order["shares"],
                        date, order.get("strategy", "backtest")
                    )
                    if result["success"]:
                        all_trades.append({
                            "date": date,
                            "code": code,
                            "name": name,
                            "action": "buy",
                            "price": price,
                            "shares": result["shares"],
                            "cost": result["cost_detail"]["total"],
                        })
                elif order["action"] == "sell":
                    result = portfolio.sell(
                        code, price, order["shares"],
                        date, order.get("strategy", "backtest")
                    )
                    if result["success"]:
                        all_trades.append({
                            "date": date,
                            "code": code,
                            "name": name,
                            "action": "sell",
                            "price": price,
                            "shares": result["shares"],
                            "profit": result["profit"],
                        })

            # 记录每日市值
            total_value = portfolio.get_total_value(current_prices)
            daily_values.append({
                "date": date,
                "total_value": total_value,
                "cash": portfolio.get_cash(),
                "position_count": len(portfolio.get_all_positions()),
            })

            # 保存快照
            portfolio.save_snapshot(date, current_prices)

        # 清理临时文件
        import os
        if os.path.exists(temp_state):
            os.remove(temp_state)

        # 计算回测指标
        self.results = self._calculate_metrics(daily_values, all_trades, risk_events)
        return self.results

    def _run_signal_dataframe(self, df):
        """兼容旧版入口：基于已计算 signal 列的单标的 DataFrame 回测。

        Args:
            df: 包含 close、signal 列的 DataFrame。

        Returns:
            tuple: (result_df, summary)
        """
        required = {"close", "signal"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"回测数据缺少必要列: {sorted(missing)}")

        result = df.copy().reset_index(drop=True)
        cash = float(self.initial_capital)
        shares = 0
        buy_count = 0
        sell_count = 0
        total_cost = 0.0
        values = []

        for idx, row in result.iterrows():
            price = float(row["close"])
            signal = int(row.get("signal", 0))
            if price <= 0:
                values.append(cash + shares * price)
                continue

            if signal == 1 and shares == 0:
                buy_shares = self.rules.calc_lot_size(price, cash, total_value=cash)
                if buy_shares > 0:
                    amount = price * buy_shares
                    cost_detail = self.rules.calc_total_cost(amount, "buy", False)
                    total_cash_needed = amount + cost_detail["total"]
                    if total_cash_needed <= cash:
                        cash = round(cash - total_cash_needed, 2)
                        shares = buy_shares
                        buy_count += 1
                        total_cost += cost_detail["total"]
            elif signal == -1 and shares > 0:
                amount = price * shares
                cost_detail = self.rules.calc_total_cost(amount, "sell", False)
                proceeds = cost_detail["actual_amount"] - cost_detail["commission"] - cost_detail["stamp_tax"] - cost_detail["transfer_fee"]
                cash = round(cash + proceeds, 2)
                shares = 0
                sell_count += 1
                total_cost += cost_detail["total"]

            total_value = round(cash + shares * price, 2)
            values.append(total_value)
            result.loc[idx, "cash"] = cash
            result.loc[idx, "shares"] = shares
            result.loc[idx, "total_value"] = total_value

        if not values:
            raise ValueError("回测数据为空")

        result["total_value"] = result["total_value"].ffill().fillna(self.initial_capital)
        result["daily_return"] = result["total_value"].pct_change().fillna(0.0)
        running_max = result["total_value"].cummax()
        result["drawdown"] = (result["total_value"] - running_max) / running_max
        final_value = float(result["total_value"].iloc[-1])
        first_close = float(result["close"].iloc[0])
        last_close = float(result["close"].iloc[-1])
        strategy_return = (final_value - self.initial_capital) / self.initial_capital
        benchmark_return = (last_close - first_close) / first_close if first_close > 0 else 0.0
        drawdown = (running_max - result["total_value"]) / running_max
        max_drawdown = float(drawdown.max()) if not drawdown.empty else 0.0

        summary = {
            "初始资金": f"¥{self.initial_capital:,.2f}",
            "期末资金": f"¥{final_value:,.2f}",
            "策略收益": f"{strategy_return * 100:.2f}%",
            "基准收益": f"{benchmark_return * 100:.2f}%",
            "最大回撤": f"{max_drawdown * 100:.2f}%",
            "买入次数": f"{buy_count} 次",
            "卖出次数": f"{sell_count} 次",
            "总交易成本": f"¥{total_cost:,.2f}",
        }
        return result, summary

    def _signals_to_orders(self, signals, current_positions):
        """将信号转换为订单"""
        orders = []
        for sig in signals:
            if sig["signal"] == "buy" and sig["code"] not in current_positions:
                orders.append({
                    "code": sig["code"],
                    "action": "buy",
                    "shares": 0,
                    "price": sig.get("close", 0),
                    "reason": sig.get("reason", ""),
                    "strategy": sig.get("strategy", ""),
                })
            elif sig["signal"] == "sell" and sig["code"] in current_positions:
                pos = current_positions[sig["code"]]
                orders.append({
                    "code": sig["code"],
                    "action": "sell",
                    "shares": pos["shares"],
                    "price": sig.get("close", 0),
                    "reason": sig.get("reason", ""),
                    "strategy": sig.get("strategy", ""),
                })
        return orders

    def _calculate_metrics(self, daily_values, trades, risk_events):
        """计算回测指标"""
        if not daily_values:
            return {}

        df = pd.DataFrame(daily_values)
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")

        # 总收益率
        total_return = (df["total_value"].iloc[-1] - self.initial_capital) / self.initial_capital

        # 年化收益率
        trading_days = len(df)
        if trading_days > 1:
            annual_return = (1 + total_return) ** (252 / trading_days) - 1
        else:
            annual_return = 0.0

        # 日收益率
        df["daily_return"] = df["total_value"].pct_change()
        df.loc[df.index[0], "daily_return"] = 0.0

        # 最大回撤
        df["cummax"] = df["total_value"].cummax()
        df["drawdown"] = (df["cummax"] - df["total_value"]) / df["cummax"]
        max_drawdown = df["drawdown"].max()

        # 夏普比率（无风险利率按 2% 计算）
        if df["daily_return"].std() > 0:
            sharpe = (df["daily_return"].mean() - 0.02 / 252) / df["daily_return"].std() * np.sqrt(252)
        else:
            sharpe = 0.0

        # 卡尔玛比率
        calmar = annual_return / max_drawdown if max_drawdown > 0 else 0.0

        # 胜率
        winning_trades = [t for t in trades if t.get("profit", 0) > 0]
        losing_trades = [t for t in trades if t.get("profit", 0) < 0]
        total_closed = len(winning_trades) + len(losing_trades)
        win_rate = len(winning_trades) / total_closed if total_closed > 0 else 0.0

        # 盈亏比
        avg_win = np.mean([t["profit"] for t in winning_trades]) if winning_trades else 0.0
        avg_loss = abs(np.mean([t["profit"] for t in losing_trades])) if losing_trades else 1.0
        profit_factor = avg_win / avg_loss if avg_loss > 0 else 0.0

        # 交易次数
        buy_count = len([t for t in trades if t["action"] == "buy"])
        sell_count = len([t for t in trades if t["action"] == "sell"])

        # 总交易成本
        total_cost = sum(t.get("cost", 0) for t in trades)

        return {
            "initial_capital": self.initial_capital,
            "final_value": round(df["total_value"].iloc[-1], 2),
            "total_return": round(total_return, 4),
            "annual_return": round(annual_return, 4),
            "max_drawdown": round(max_drawdown, 4),
            "sharpe_ratio": round(sharpe, 2),
            "calmar_ratio": round(calmar, 2),
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 2),
            "total_trades": len(trades),
            "buy_count": buy_count,
            "sell_count": sell_count,
            "total_cost": round(total_cost, 2),
            "trading_days": trading_days,
            "risk_events": len(risk_events),
            "daily_values": df[["date", "total_value", "cash", "position_count"]].to_dict("records"),
            "trades": trades,
            "risk_events_detail": risk_events,
        }

    def print_summary(self):
        """打印回测结果摘要"""
        if not self.results:
            print("未运行回测")
            return

        r = self.results
        print("=" * 50)
        print("            回测结果摘要")
        print("=" * 50)
        print(f"初始资金:     {r['initial_capital']:>12,.2f} 元")
        print(f"最终市值:     {r['final_value']:>12,.2f} 元")
        print(f"总收益率:     {r['total_return']:>12.2%}")
        print(f"年化收益率:   {r['annual_return']:>12.2%}")
        print(f"最大回撤:     {r['max_drawdown']:>12.2%}")
        print(f"夏普比率:     {r['sharpe_ratio']:>12.2f}")
        print(f"卡尔玛比率:   {r['calmar_ratio']:>12.2f}")
        print(f"胜率:         {r['win_rate']:>12.2%}")
        print(f"盈亏比:       {r['profit_factor']:>12.2f}")
        print(f"交易次数:     {r['total_trades']:>12d}")
        print(f"  买入:       {r['buy_count']:>12d}")
        print(f"  卖出:       {r['sell_count']:>12d}")
        print(f"总交易成本:   {r['total_cost']:>12,.2f} 元")
        print(f"交易日数:     {r['trading_days']:>12d}")
        print(f"风控触发:     {r['risk_events']:>12d} 次")
        print("=" * 50)
