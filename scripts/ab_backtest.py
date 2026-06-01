"""组合策略 A/B 回测对比脚本。

固定一篮子流动性较好的大盘股作为回测 universe,对比以下配置对夏普/回撤的影响:
    1. 基线        :关闭大盘择时 + 关闭移动止损
    2. +大盘择时    :仅开启大盘择时
    3. +移动止损    :仅开启移动止损
    4. 全开        :大盘择时 + 移动止损

数据只拉取一次,通过运行时切换配置开关复用,保证四组对比公平。

用法:
    python -m scripts.ab_backtest [start] [end]
    例: python -m scripts.ab_backtest 20230101 20250101
"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as cfg
import strategies.exit_rules as exit_rules
from backtest.portfolio_backtest import PortfolioBacktester
from data.ak_loader import AKDataLoader
from data.loader import DataLoader

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ab_backtest")

# 一篮子流动性较好的大盘/中盘股(跨行业),作为回测 universe 近似
UNIVERSE = [
    "600519", "600036", "601318", "600276", "600900", "601166", "600030", "600887",
    "601012", "600031", "600585", "600009", "601888", "603259", "600690", "600048",
    "601988", "601398", "601288", "600028", "601857", "600050", "601628", "601668",
    "000858", "000651", "000333", "000725", "002415", "300750", "002594", "000001",
    "002304", "300059", "002475", "000063", "002230", "300760", "000661", "002714",
    "600104", "601899", "603501", "688981", "688111", "600436", "603288", "002352",
    "300015", "600406",
]


def _run_one(label, history_map, trading_days, index_history, *, regime: bool, trailing: bool):
    """在指定开关下运行一次回测并返回指标。"""
    cfg.ENABLE_MARKET_REGIME = regime
    exit_rules.ENABLE_TRAILING_STOP = trailing
    bt = PortfolioBacktester(initial_capital=cfg.INITIAL_CAPITAL, top_n=10)
    # regime 关闭时不传指数;开启时传入
    result = bt.run(history_map, trading_days, index_history=index_history if regime else None)
    result["_label"] = label
    return result


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "20230101"
    end = sys.argv[2] if len(sys.argv) > 2 else "20250101"

    print(f"加载回测数据中... universe={len(UNIVERSE)} 只, {start}~{end}")
    bt = PortfolioBacktester(initial_capital=cfg.INITIAL_CAPITAL, top_n=10)

    loader = DataLoader()
    from datetime import datetime, timedelta
    try:
        s = start.replace("-", "")
        e = end.replace("-", "")
        trading_days = loader.get_trading_calendar(s, e)
        preload_start = (datetime.strptime(s, "%Y%m%d") - timedelta(days=180)).strftime("%Y%m%d")
        history_map = {}
        for code in UNIVERSE:
            try:
                df = loader.get_daily_data(code, preload_start, e, adjust_flag="2")
                if df is not None and not df.empty:
                    df = df.copy()
                    df["name"] = code
                    history_map[code] = df
            except Exception as exc:  # noqa: BLE001
                logger.warning("加载失败 %s: %s", code, exc)
    finally:
        loader.close()

    ak = AKDataLoader()
    try:
        index_history = ak.get_index_history(
            cfg.MARKET_INDEX_CODE, start_date=preload_start, end_date=e
        )
    finally:
        ak.close()

    print(f"数据就绪: {len(history_map)}/{len(UNIVERSE)} 只, {len(trading_days)} 个交易日, "
          f"指数={'有' if index_history is not None else '无'}\n")

    configs = [
        ("基线(都关)", False, False),
        ("+大盘择时", True, False),
        ("+移动止损", False, True),
        ("全开", True, True),
    ]
    results = [
        _run_one(label, history_map, trading_days, index_history, regime=r, trailing=t)
        for label, r, t in configs
    ]

    # 输出对照表
    cols = [
        ("配置", "_label", "{}"),
        ("总收益", "total_return", "{:+.2%}"),
        ("年化", "annual_return", "{:+.2%}"),
        ("最大回撤", "max_drawdown", "{:.2%}"),
        ("夏普", "sharpe_ratio", "{:.2f}"),
        ("卡尔玛", "calmar_ratio", "{:.2f}"),
        ("胜率", "win_rate", "{:.1%}"),
        ("盈亏比", "profit_factor", "{:.2f}"),
        ("交易数", "total_trades", "{}"),
        ("风控", "risk_events", "{}"),
    ]
    header = " | ".join(f"{c[0]:>8}" for c in cols)
    print(header)
    print("-" * len(header))
    for r in results:
        row = " | ".join(f"{c[2].format(r.get(c[1], 0)):>8}" for c in cols)
        print(row)


if __name__ == "__main__":
    main()
