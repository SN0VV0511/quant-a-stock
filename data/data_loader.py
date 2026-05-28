"""
数据加载模块 - 用 BaoStock 获取 A 股行情数据（免费、稳定、无封 IP 问题）
"""
import logging
import os
import sys

import pandas as pd

try:
    import baostock as bs
except ImportError:  # pragma: no cover - 运行环境可能未安装行情依赖
    bs = None

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from config.settings import (
        DEFAULT_STOCK,
        BACKTEST_START,
        BACKTEST_END,
        normalize_a_share_code,
        to_baostock_code,
    )
except ImportError:
    DEFAULT_STOCK = "600519"
    BACKTEST_START = "20250101"
    BACKTEST_END = "20260523"

    def normalize_a_share_code(code):
        """配置模块不可用时的最小代码归一化兜底。"""
        normalized = str(code).strip().lower().replace(".", "")
        if normalized.startswith(("sh", "sz")):
            normalized = normalized[2:]
        if not normalized.isdigit() or len(normalized) != 6:
            raise ValueError(f"无效证券代码: {code}")
        return normalized

    def to_baostock_code(code):
        """配置模块不可用时的最小 BaoStock 代码兜底。"""
        raw = normalize_a_share_code(code)
        market = "sh" if raw.startswith(("600", "601", "603", "605", "688", "689")) else "sz"
        return f"{market}.{raw}"

logger = logging.getLogger(__name__)


def _require_baostock():
    """确保 BaoStock 依赖已安装。"""
    if bs is None:
        raise ImportError("缺少 baostock 依赖，请先执行: pip install -r requirements.txt")


def get_stock_data(symbol=None, start_date=None, end_date=None, adjust="1"):
    """
    获取 A 股日线行情数据

    参数:
        symbol: 股票代码，如 "600519"（贵州茅台）
        start_date: 开始日期，格式 "20240101"
        end_date: 结束日期，格式 "20250101"
        adjust: 复权方式 "1"=前复权 "2"=后复权 "3"=不复权（默认）

    返回:
        DataFrame，包含 date/open/close/high/low/volume 等
    """
    symbol = symbol or DEFAULT_STOCK
    start_date = start_date or BACKTEST_START
    end_date = end_date or BACKTEST_END

    raw_symbol = normalize_a_share_code(symbol)
    bs_code = to_baostock_code(symbol)

    start_fmt = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
    end_fmt = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"

    logger.info("正在获取 %s 行情数据 (%s ~ %s)", raw_symbol, start_fmt, end_fmt)

    # 登录
    _require_baostock()
    lg = bs.login()
    if lg.error_code != "0":
        raise Exception(f"BaoStock 登录失败: {lg.error_msg}")

    # 查询
    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,open,high,low,close,volume,amount,turn",
        start_date=start_fmt,
        end_date=end_fmt,
        frequency="d",
        adjustflag=adjust
    )

    if rs.error_code != "0":
        bs.logout()
        raise Exception(f"BaoStock 查询失败: {rs.error_msg}")

    data = []
    while rs.next():
        data.append(rs.get_row_data())

    bs.logout()

    if not data:
        raise Exception(f"未获取到 {symbol} 的数据")

    df = pd.DataFrame(data, columns=["date", "open", "high", "low", "close", "volume", "amount", "turnover"])

    # 类型转换
    for col in ["open", "high", "low", "close", "volume", "amount", "turnover"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    # 补充列（兼容回测引擎）
    df["pct_change"] = df["close"].pct_change() * 100
    df["change"] = df["close"].diff()

    logger.info("获取 %s 行情成功，共 %s 条数据", raw_symbol, len(df))
    return df


def get_stock_info(symbol=None):
    """获取股票基本信息"""
    _require_baostock()
    symbol = symbol or DEFAULT_STOCK
    bs_code = to_baostock_code(symbol)

    lg = bs.login()
    rs = bs.query_stock_basic(code=bs_code)
    info = {}
    if rs.next():
        row = rs.get_row_data()
        fields = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
        info = dict(zip(fields, row))
    bs.logout()
    return info


if __name__ == "__main__":
    df = get_stock_data("600519")
    print(df.head(10))
    print(f"\n📊 股票信息:")
    info = get_stock_info("600519")
    for k, v in info.items():
        print(f"  {k}: {v}")
