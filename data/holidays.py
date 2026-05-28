"""
A 股交易日历 — 基于 AKShare（新浪财经源）
自动获取，覆盖到当年年底，无需手动维护
"""

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# 缓存
_trading_days_cache: set = None


def _load_trading_days() -> set:
    """从 AKShare 加载交易日历（带缓存）"""
    global _trading_days_cache
    if _trading_days_cache is not None:
        return _trading_days_cache

    try:
        import akshare as ak
        import pandas as pd

        df = ak.tool_trade_date_hist_sina()
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        # 只保留今年的数据就够了
        current_year = datetime.now().year
        df = df[df["trade_date"].dt.year >= current_year]
        _trading_days_cache = set(
            df["trade_date"].dt.strftime("%Y%m%d").tolist()
        )
        logger.info(f"AKShare 交易日历加载完成: {len(_trading_days_cache)} 个交易日")
    except Exception as e:
        logger.warning(f"AKShare 交易日历加载失败: {e}，回退到本地判断")
        _trading_days_cache = set()

    return _trading_days_cache


def is_a_stock_holiday(date_str: str) -> bool:
    """检查日期是否为 A 股休市日（含周末）

    Args:
        date_str: YYYYMMDD 格式

    Returns:
        bool: True 表示休市
    """
    date_str = date_str.replace("-", "")
    dt = datetime.strptime(date_str, "%Y%m%d")

    # 周末
    if dt.weekday() >= 5:
        return True

    # 用 AKShare 交易日历判断
    trading_days = _load_trading_days()
    if trading_days:
        return date_str not in trading_days

    # AKShare 不可用时，无法准确判断
    logger.warning(f"交易日历不可用，无法判断 {date_str}")
    return False


def is_trading_day(date_str: str) -> bool:
    """判断是否为交易日（对外接口）"""
    return not is_a_stock_holiday(date_str)
