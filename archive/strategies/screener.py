"""
全 A 股选股器（优化版）
先用历史数据批量筛选，再获取候选股实时行情
"""

import logging
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class StockScreener:
    """全 A 股选股器"""

    def __init__(self, top_n=5):
        self.top_n = top_n

    def screen(self, stocks, loader, date_str=None):
        """从全 A 股中筛选候选股

        流程：
        1. 批量获取历史数据
        2. 用历史数据计算动量、均线等指标
        3. 排序选出 Top N

        Args:
            stocks: 全 A 股列表
            loader: AKDataLoader 实例
            date_str: 日期 YYYYMMDD

        Returns:
            list of dict: Top N 候选股
        """
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")

        logger.info(f"开始全市场选股，输入 {len(stocks)} 只股票")

        # 只取代码列表
        codes = [s["code"] for s in stocks]
        code_name = {s["code"]: s["name"] for s in stocks}

        # 批量获取历史数据（这是主要耗时操作）
        logger.info(f"批量获取 {len(codes)} 只股票历史数据...")
        hist_data = loader.get_batch_history(codes, days=120, max_batch=5000)
        logger.info(f"获取到 {len(hist_data)} 只股票历史数据")

        # 计算每只股票的得分
        scored = []
        for code, df in hist_data.items():
            if df is None or len(df) < 60:
                continue

            try:
                score_info = self._calculate_score(df)
                if score_info:
                    scored.append({
                        "code": code,
                        "name": code_name.get(code, code),
                        "price": score_info["price"],
                        **score_info,
                    })
            except Exception:
                continue

        # 排序选出 Top N
        scored.sort(key=lambda x: x["total_score"], reverse=True)
        top = scored[:self.top_n]

        for i, s in enumerate(top):
            s["rank"] = i + 1

        logger.info(f"选股完成: 从 {len(scored)} 只中选出 Top {len(top)}")
        return top

    def _calculate_score(self, df):
        """计算股票综合得分

        Args:
            df: 历史数据 DataFrame

        Returns:
            dict: 得分信息
        """
        try:
            close = pd.to_numeric(df["close"], errors="coerce").dropna()
            if len(close) < 60:
                return None

            current = close.iloc[-1]
            if current <= 0:
                return None

            # 价格过滤
            if current < 3:
                return None

            # 成交额过滤（20 日均额 > 5000 万）
            amount = pd.to_numeric(df["amount"], errors="coerce").dropna()
            avg_amount = amount.tail(20).mean()
            if avg_amount < 50000000:
                return None

            # 涨跌停过滤
            pct = pd.to_numeric(df["pctChg"], errors="coerce").dropna()
            if len(pct) > 0 and abs(pct.iloc[-1]) >= 9.8:
                return None

            # 60 日动量
            close_60 = close.iloc[-61] if len(close) > 60 else close.iloc[0]
            momentum_60 = (current - close_60) / close_60 if close_60 > 0 else 0

            # 20 日动量
            close_20 = close.iloc[-21] if len(close) > 20 else close.iloc[0]
            momentum_20 = (current - close_20) / close_20 if close_20 > 0 else 0

            # 均线
            ma5 = close.tail(5).mean()
            ma10 = close.tail(10).mean()
            ma20 = close.tail(20).mean()
            ma60 = close.tail(60).mean()

            # 均线多头排列
            ma_bullish = ma5 > ma10 > ma20 > ma60
            ma_score = 0
            if ma_bullish:
                ma_score = 0.3
            elif current > ma20:
                ma_score = 0.1

            # 波动率（20 日年化）
            returns = close.pct_change().dropna().tail(20)
            volatility = returns.std() * np.sqrt(252) if len(returns) > 0 else 1

            # 成交量趋势
            volume = pd.to_numeric(df["volume"], errors="coerce").dropna()
            vol_5 = volume.tail(5).mean()
            vol_20 = volume.tail(20).mean()
            vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 1

            # 综合得分
            total_score = (
                momentum_60 * 0.35 +
                momentum_20 * 0.25 +
                ma_score +
                (vol_ratio - 1) * 0.1 +
                -volatility * 0.1
            )

            return {
                "price": round(current, 2),
                "momentum_60": round(momentum_60, 4),
                "momentum_20": round(momentum_20, 4),
                "ma5": round(ma5, 2),
                "ma10": round(ma10, 2),
                "ma20": round(ma20, 2),
                "ma60": round(ma60, 2),
                "ma_bullish": ma_bullish,
                "volatility": round(volatility, 4),
                "vol_ratio": round(vol_ratio, 2),
                "total_score": round(total_score, 4),
            }

        except Exception as e:
            return None
