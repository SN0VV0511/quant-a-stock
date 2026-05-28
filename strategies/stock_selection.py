"""
主板量化选股策略
基于多因子打分：盈利质量、现金流质量、中期动量、低波动
"""

import logging
from datetime import datetime

import pandas as pd
import numpy as np

from config.settings import (
    DEFAULT_STOCK_POOL, MAX_SINGLE_STOCK, is_chinext, INITIAL_CAPITAL,
)

logger = logging.getLogger(__name__)

# 策略参数
MOMENTUM_PERIOD = 60        # 中期动量周期
VOLATILITY_PERIOD = 60      # 波动率计算周期
MA_PERIOD = 20              # 均线周期
STOP_LOSS_PCT = 0.07        # 止损比例 7%
TAKE_PROFIT_MA = True       # 跌破 MA20 止盈
MAX_STOCKS = 4              # 最多持有股票数
MIN_LISTED_DAYS = 120       # 最低上市天数
MIN_AVG_AMOUNT = 5000000    # 最低日均成交额（5 万元）
REBALANCE_DAY = -1           # 调仓日（-1=每个交易日）

# 因子权重
FACTOR_WEIGHTS = {
    "momentum": 0.30,       # 中期动量
    "low_vol": 0.25,        # 低波动
    "quality": 0.25,        # 盈利质量（ROE）
    "cashflow": 0.20,       # 现金流质量
}


class MainboardStockStrategy:
    """主板低频量化选股策略"""

    def __init__(self, stock_pool=None):
        self.stock_pool = stock_pool or DEFAULT_STOCK_POOL
        self.name = "主板量化选股"

    def calculate_momentum_score(self, df, period=60):
        """计算中期动量得分

        Returns:
            float: 收益率
        """
        if df is None or len(df) < period + 1:
            return None
        close = df["close"].values
        if close[-period - 1] <= 0:
            return None
        return (close[-1] - close[-period - 1]) / close[-period - 1]

    def calculate_volatility_score(self, df, period=60):
        """计算低波动得分（波动率越低得分越高）

        Returns:
            float: 年化波动率
        """
        if df is None or len(df) < period + 1:
            return None
        returns = df["close"].pct_change().dropna().tail(period)
        if returns.empty or returns.std() == 0:
            return None
        return returns.std() * np.sqrt(252)

    def calculate_quality_score(self, profit_df):
        """计算盈利质量得分（基于 ROE）

        Args:
            profit_df: BaoStock 盈利数据

        Returns:
            float: ROE 值（越高越好）
        """
        if profit_df is None or profit_df.empty:
            return None
        roe = profit_df["roeAvg"].iloc[0] if "roeAvg" in profit_df.columns else None
        if roe is not None and pd.notna(roe):
            return float(roe)
        return None

    def calculate_cashflow_score(self, cashflow_df):
        """计算现金流质量得分（经营现金流/净利润）

        Args:
            cashflow_df: BaoStock 现金流数据

        Returns:
            float: 经营现金流/净利润 比值
        """
        if cashflow_df is None or cashflow_df.empty:
            return None
        ocf_ratio = cashflow_df["ocfToProfit"].iloc[0] if "ocfToProfit" in cashflow_df.columns else None
        if ocf_ratio is not None and pd.notna(ocf_ratio):
            return float(ocf_ratio)
        return None

    def filter_stock(self, code, df, stock_info=None):
        """过滤不符合条件的股票

        Args:
            code: 股票代码
            df: 日线数据
            stock_info: 基本面信息

        Returns:
            (passed, reason)
        """
        # ST 过滤
        if stock_info:
            name = stock_info.get("code_name", "")
            if "ST" in name.upper():
                return False, f"ST 股票: {name}"

        # 上市天数过滤（简化：用数据长度估算）
        if df is None or len(df) < MIN_LISTED_DAYS:
            return False, f"数据不足 {MIN_LISTED_DAYS} 天"

        # 流动性过滤
        recent = df.tail(20)
        if "amount" in recent.columns:
            avg_amount = recent["amount"].mean()
            if pd.notna(avg_amount) and avg_amount < MIN_AVG_AMOUNT:
                return False, f"流动性不足（日均 {avg_amount:.0f}）"

        # 停牌过滤：最近一天成交量为 0
        if "volume" in df.columns:
            last_volume = df["volume"].iloc[-1]
            if pd.notna(last_volume) and last_volume <= 0:
                return False, "疑似停牌"

        return True, "通过"

    def calculate_scores(self, stock_data_dict, current_date, fundamental_data=None):
        """计算各股票的综合得分

        Args:
            stock_data_dict: {code: DataFrame} 日线数据
            current_date: YYYYMMDD
            fundamental_data: {code: {"profit": df, "cashflow": df}} 基本面数据

        Returns:
            list of dict: [{code, name, score, factors}]
        """
        current_date_clean = current_date.replace("-", "")
        results = []

        for code, df in stock_data_dict.items():
            name = self.stock_pool.get(code, {}).get("name", code)

            if df is None or df.empty:
                continue

            df_filtered = df[df["date"] <= current_date_clean].copy()
            if len(df_filtered) < MA_PERIOD:
                continue

            # 过滤
            stock_info = None
            if fundamental_data and code in fundamental_data:
                stock_info = fundamental_data[code].get("basic")

            passed, reason = self.filter_stock(code, df_filtered, stock_info)
            if not passed:
                logger.debug(f"{name}({code}): 过滤 - {reason}")
                continue

            # 计算各因子
            momentum = self.calculate_momentum_score(df_filtered, MOMENTUM_PERIOD)
            volatility = self.calculate_volatility_score(df_filtered, VOLATILITY_PERIOD)

            quality = None
            cashflow = None
            if fundamental_data and code in fundamental_data:
                profit_df = fundamental_data[code].get("profit")
                cashflow_df = fundamental_data[code].get("cashflow")
                if profit_df is not None:
                    quality = self.calculate_quality_score(profit_df)
                if cashflow_df is not None:
                    cashflow = self.calculate_cashflow_score(cashflow_df)

            # 因子排名得分（percentile rank）
            # 先收集有效因子值，稍后统一排名
            results.append({
                "code": code,
                "name": name,
                "factors": {
                    "momentum": momentum,
                    "low_vol": volatility,
                    "quality": quality,
                    "cashflow": cashflow,
                },
                "close": df_filtered["close"].iloc[-1],
                "raw_score": 0.0,
            })

        if not results:
            return results

        # 对各因子进行百分位排名
        for factor_name in ["momentum", "low_vol", "quality", "cashflow"]:
            values = {}
            for i, r in enumerate(results):
                val = r["factors"][factor_name]
                if val is not None and pd.notna(val):
                    values[i] = val

            if not values:
                continue

            # 低波动因子：值越低越好
            reverse = (factor_name == "low_vol")

            sorted_indices = sorted(values.keys(), key=lambda k: values[k], reverse=not reverse)
            n = len(sorted_indices)

            for rank, idx in enumerate(sorted_indices):
                percentile = (n - rank) / n if reverse else (rank + 1) / n
                results[idx]["factors"][f"{factor_name}_rank"] = percentile

        # 计算综合得分
        for r in results:
            score = 0.0
            total_weight = 0.0
            for factor_name, weight in FACTOR_WEIGHTS.items():
                rank_key = f"{factor_name}_rank"
                if rank_key in r["factors"] and r["factors"][rank_key] is not None:
                    score += r["factors"][rank_key] * weight
                    total_weight += weight
            if total_weight > 0:
                score /= total_weight
            r["raw_score"] = round(score, 4)

        # 按得分排名
        results.sort(key=lambda x: x["raw_score"], reverse=True)

        return results

    def generate_signals(self, current_portfolio, scores, date):
        """生成买入/卖出信号

        Args:
            current_portfolio: 当前持仓
            scores: calculate_scores 的结果
            date: YYYYMMDD

        Returns:
            list of dict: [{code, signal, reason, score}]
        """
        signals = []
        date_clean = date.replace("-", "")

        # 当前持有的主板股票
        held_stock_codes = {code for code in current_portfolio if not code.startswith("sh51") and not code.startswith("sz159")}

        # 取前 MAX_STOCKS 名
        top_codes = {r["code"] for r in scores[:MAX_STOCKS]}

        # 卖出信号：持仓中不在前 N 名的
        for code in held_stock_codes:
            if code not in top_codes:
                pos = current_portfolio.get(code, {})
                sig = {
                    "code": code,
                    "signal": "sell",
                    "reason": "排名下滑，调出",
                    "score": 0,
                    "close": 0,
                }
                # 查找得分
                for r in scores:
                    if r["code"] == code:
                        sig["score"] = r["raw_score"]
                        sig["close"] = r.get("close", 0)
                        sig["reason"] = f"排名下滑（得分 {r['raw_score']:.3f}），调出"
                        break
                signals.append(sig)

        # 止损检查
        for code in held_stock_codes:
            pos = current_portfolio.get(code, {})
            if not pos:
                continue
            avg_cost = pos.get("avg_cost", 0)
            # 在 scores 中找当前价格
            for r in scores:
                if r["code"] == code and r.get("close"):
                    pnl = (r["close"] - avg_cost) / avg_cost if avg_cost > 0 else 0
                    if pnl <= -STOP_LOSS_PCT:
                        signals.append({
                            "code": code,
                            "signal": "sell",
                            "reason": f"触发止损（亏损 {pnl:.2%}）",
                            "score": r["raw_score"],
                            "close": r["close"],
                        })
                    break

        # 买入信号：前 N 名中未持仓的
        for r in scores[:MAX_STOCKS]:
            if r["code"] not in held_stock_codes:
                signals.append({
                    "code": r["code"],
                    "name": r["name"],
                    "signal": "buy",
                    "reason": f"综合得分排名买入（得分 {r['raw_score']:.3f}）",
                    "score": r["raw_score"],
                    "close": r.get("close", 0),
                })

        # 持有信号
        for r in scores:
            if r["code"] in held_stock_codes and r["code"] in top_codes:
                signals.append({
                    "code": r["code"],
                    "name": r["name"],
                    "signal": "hold",
                    "reason": f"继续持有（得分 {r['raw_score']:.3f}）",
                    "score": r["raw_score"],
                    "close": r.get("close", 0),
                })

        return signals

    def should_rebalance(self, date_str):
        """是否需要调仓（每个交易日）"""
        # 每个交易日都调仓
        return True

    def generate_orders(self, stock_data_dict, current_portfolio, current_date,
                        fundamental_data=None):
        """生成交易指令

        Returns:
            list of dict: [{code, action, shares, price, reason, strategy}]
        """
        orders = []

        scores = self.calculate_scores(stock_data_dict, current_date, fundamental_data)
        signals = self.generate_signals(current_portfolio, scores, current_date)

        if not self.should_rebalance(current_date):
            # 非调仓日只做止损
            for sig in signals:
                if sig["signal"] == "sell" and "止损" in sig.get("reason", ""):
                    pos = current_portfolio.get(sig["code"], {})
                    orders.append({
                        "code": sig["code"],
                        "action": "sell",
                        "shares": pos.get("shares", 0),
                        "price": sig.get("close", 0),
                        "reason": sig["reason"],
                        "strategy": self.name,
                    })
            return orders

        for sig in signals:
            if sig["signal"] == "sell":
                pos = current_portfolio.get(sig["code"], {})
                orders.append({
                    "code": sig["code"],
                    "action": "sell",
                    "shares": pos.get("shares", 0),
                    "price": sig.get("close", 0),
                    "reason": sig["reason"],
                    "strategy": self.name,
                })
            elif sig["signal"] == "buy":
                orders.append({
                    "code": sig["code"],
                    "action": "buy",
                    "shares": 0,  # 由风控模块计算
                    "price": sig.get("close", 0),
                    "reason": sig["reason"],
                    "strategy": self.name,
                })

        return orders
