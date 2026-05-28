"""
ETF 动量轮动策略
基于 20 日动量排名，周频调仓，均线过滤+波动率过滤
"""

import logging
from datetime import datetime

import pandas as pd
import numpy as np

from config.settings import DEFAULT_ETF_PROXY_POOL, INITIAL_CAPITAL, is_etf

logger = logging.getLogger(__name__)

# 策略参数
MOMENTUM_PERIOD = 20       # 动量计算周期
MA_SHORT = 20              # 短期均线
MA_LONG = 60               # 长期均线
VOLATILITY_WINDOW = 20     # 波动率计算窗口
MAX_VOLATILITY = 0.30      # 最大允许波动率（年化）
TOP_N = 2                  # 选前 N 名
STOP_LOSS_PCT = 0.06       # 止损比例
TAKE_PROFIT_PCT = 0.05     # 止盈触发
TRAILING_STOP_PCT = 0.03   # 移动止盈回撤
REBALANCE_DAY = 0          # 调仓日（周一=0）


class ETFRotationStrategy:
    """ETF 动量轮动策略"""

    def __init__(self, etf_pool=None):
        self.etf_pool = etf_pool or DEFAULT_ETF_PROXY_POOL
        self.name = "ETF动量轮动"
        self._high_water_marks = {}  # code -> 最高价（用于移动止盈）

    def calculate_momentum(self, df, period=20):
        """计算 N 日收益率（动量）

        Args:
            df: 包含 close 列的 DataFrame
            period: 计算周期

        Returns:
            float: 动量值（收益率）
        """
        if df is None or len(df) < period + 1:
            return None
        close = df["close"].values
        if close[-1] <= 0 or close[-period - 1] <= 0:
            return None
        return (close[-1] - close[-period - 1]) / close[-period - 1]

    def calculate_moving_average(self, df, period):
        """计算移动平均线"""
        if df is None or len(df) < period:
            return None
        return df["close"].rolling(window=period).mean().iloc[-1]

    def calculate_volatility(self, df, window=20):
        """计算年化波动率"""
        if df is None or len(df) < window + 1:
            return None
        returns = df["close"].pct_change().dropna().tail(window)
        if returns.empty:
            return None
        return returns.std() * np.sqrt(252)

    def check_ma_filter(self, df):
        """检查均线过滤条件：收盘价 > MA20 > MA60

        Returns:
            (passed, ma20, ma60, close_price)
        """
        if df is None or len(df) < MA_LONG:
            return False, None, None, None

        close_price = df["close"].iloc[-1]
        ma20 = self.calculate_moving_average(df, MA_SHORT)
        ma60 = self.calculate_moving_average(df, MA_LONG)

        if ma20 is None or ma60 is None:
            return False, None, None, close_price

        passed = close_price > ma20 > ma60
        return passed, ma20, ma60, close_price

    def calculate_signals(self, etf_data_dict, current_date):
        """计算交易信号

        Args:
            etf_data_dict: {code: DataFrame} 各 ETF 的日线数据
            current_date: 当前日期 YYYYMMDD

        Returns:
            list of dict: [{code, signal, reason, score}]
        """
        signals = []
        momentum_scores = {}

        current_date_clean = current_date.replace("-", "")

        for code, df in etf_data_dict.items():
            if df is None or df.empty:
                continue

            # 确保数据包含当前日期或之前的数据
            df_filtered = df[df["date"] <= current_date_clean].copy()
            if len(df_filtered) < MA_LONG + 1:
                logger.debug(f"{code}: 数据不足，跳过")
                continue

            # 1. 计算动量
            momentum = self.calculate_momentum(df_filtered, MOMENTUM_PERIOD)
            if momentum is None:
                continue

            # 2. 均线过滤
            ma_passed, ma20, ma60, close_price = self.check_ma_filter(df_filtered)

            # 3. 波动率过滤
            volatility = self.calculate_volatility(df_filtered, VOLATILITY_WINDOW)
            vol_ok = volatility is not None and volatility < MAX_VOLATILITY

            momentum_scores[code] = {
                "momentum": momentum,
                "ma_passed": ma_passed,
                "volatility": volatility,
                "vol_ok": vol_ok,
                "close": close_price,
                "ma20": ma20,
                "ma60": ma60,
            }

        if not momentum_scores:
            return signals

        # 按动量排名
        ranked = sorted(momentum_scores.items(), key=lambda x: x[1]["momentum"], reverse=True)

        # 选前 N 名，需要通过过滤条件
        selected = []
        for code, info in ranked:
            if info["ma_passed"] and info["vol_ok"]:
                selected.append(code)
                if len(selected) >= TOP_N:
                    break

        # 对所有 ETF 生成信号
        for code, info in momentum_scores.items():
            name = self.etf_pool.get(code, {}).get("name", code)

            if code in selected:
                signals.append({
                    "code": code,
                    "name": name,
                    "signal": "buy",
                    "reason": f"动量排名选中（{info['momentum']:.2%}），均线多头，波动率{info['volatility']:.2%}" if info["volatility"] else f"动量排名选中（{info['momentum']:.2%}）",
                    "score": info["momentum"],
                    "close": info["close"],
                })
            else:
                # 判断是否需要卖出
                if info["momentum"] is not None and info["momentum"] < 0:
                    signals.append({
                        "code": code,
                        "name": name,
                        "signal": "sell",
                        "reason": f"动量为负（{info['momentum']:.2%}）",
                        "score": info["momentum"],
                        "close": info["close"],
                    })
                elif not info["ma_passed"] and info["close"] is not None:
                    ma20_str = f"{info['ma20']:.3f}" if info['ma20'] else 'N/A'
                    signals.append({
                        "code": code,
                        "name": name,
                        "signal": "sell",
                        "reason": f"均线空头（收盘{info['close']:.3f}，MA20={ma20_str}）",
                        "score": info["momentum"],
                        "close": info["close"],
                    })
                else:
                    signals.append({
                        "code": code,
                        "name": name,
                        "signal": "hold",
                        "reason": f"未选中（动量{info['momentum']:.2%}）",
                        "score": info["momentum"],
                        "close": info["close"],
                    })

        return signals

    def check_stop_loss(self, code, df, buy_price, current_date):
        """检查止损/止盈条件

        Args:
            code: ETF 代码
            df: 日线数据
            buy_price: 买入价格
            current_date: 当前日期

        Returns:
            (should_sell, reason)
        """
        current_date_clean = current_date.replace("-", "")
        df_filtered = df[df["date"] <= current_date_clean]
        if df_filtered.empty:
            return False, ""

        current_price = df_filtered["close"].iloc[-1]
        if current_price <= 0 or buy_price <= 0:
            return False, ""

        pnl_pct = (current_price - buy_price) / buy_price

        # 止损
        if pnl_pct <= -STOP_LOSS_PCT:
            return True, f"触发止损（亏损 {pnl_pct:.2%}）"

        # 跌破 20 日均线
        if len(df_filtered) >= MA_SHORT:
            ma20 = df_filtered["close"].rolling(window=MA_SHORT).mean().iloc[-1]
            if current_price < ma20:
                return True, f"跌破 MA20（价格 {current_price:.3f} < MA20 {ma20:.3f}）"

        # 移动止盈
        if pnl_pct >= TAKE_PROFIT_PCT:
            hwm = self._high_water_marks.get(code, buy_price)
            if current_price > hwm:
                self._high_water_marks[code] = current_price
                hwm = current_price

            drawdown_from_hwm = (hwm - current_price) / hwm
            if drawdown_from_hwm >= TRAILING_STOP_PCT:
                return True, f"移动止盈（从高点回撤 {drawdown_from_hwm:.2%}，盈利 {pnl_pct:.2%}）"

        # 更新最高价
        if code not in self._high_water_marks or current_price > self._high_water_marks[code]:
            self._high_water_marks[code] = current_price

        return False, ""

    def should_rebalance(self, date_str):
        """是否需要调仓（每周一）

        Args:
            date_str: YYYYMMDD 格式

        Returns:
            bool
        """
        date_str = date_str.replace("-", "")
        dt = datetime.strptime(date_str, "%Y%m%d")
        return dt.weekday() == REBALANCE_DAY

    def generate_orders(self, etf_data_dict, current_portfolio, current_date):
        """生成交易指令

        Args:
            etf_data_dict: {code: DataFrame}
            current_portfolio: 当前持仓 {code: {shares, avg_cost, ...}}
            current_date: YYYYMMDD

        Returns:
            list of dict: [{code, action, shares, price, reason}]
        """
        orders = []
        signals = self.calculate_signals(etf_data_dict, current_date)

        # 构建信号映射
        signal_map = {s["code"]: s for s in signals}
        buy_codes = {s["code"] for s in signals if s["signal"] == "buy"}
        sell_codes = {s["code"] for s in signals if s["signal"] == "sell"}

        # 检查持仓的止损/止盈
        for code, pos in current_portfolio.items():
            if not is_etf(code):
                continue
            if code in etf_data_dict:
                should_sell, reason = self.check_stop_loss(
                    code, etf_data_dict[code], pos["avg_cost"], current_date
                )
                if should_sell:
                    orders.append({
                        "code": code,
                        "action": "sell",
                        "shares": pos["shares"],
                        "price": signal_map.get(code, {}).get("close", 0),
                        "reason": reason,
                        "strategy": self.name,
                    })

        # 周频调仓时执行信号
        if self.should_rebalance(current_date):
            # 卖出不在买入名单中的持仓
            for code in list(current_portfolio.keys()):
                if not is_etf(code):
                    continue
                if code not in buy_codes and code in sell_codes:
                    pos = current_portfolio[code]
                    orders.append({
                        "code": code,
                        "action": "sell",
                        "shares": pos["shares"],
                        "price": signal_map.get(code, {}).get("close", 0),
                        "reason": signal_map.get(code, {}).get("reason", "调仓卖出"),
                        "strategy": self.name,
                    })

            # 买入新标的
            for code in buy_codes:
                if code not in current_portfolio:
                    sig = signal_map.get(code, {})
                    orders.append({
                        "code": code,
                        "action": "buy",
                        "shares": 0,  # 由风控模块计算具体数量
                        "price": sig.get("close", 0),
                        "reason": sig.get("reason", "动量选入"),
                        "strategy": self.name,
                    })

        return orders
