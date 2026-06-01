"""最大回撤阈值扫描:验证 MAX_DRAWDOWN_THRESHOLD 对策略活跃度与绩效的影响。

固定配置:移动止损开、MA20 大盘择时关(已由 ab_backtest 验证为较优组合),
仅扫描风控的最大回撤阈值(及一组配套放宽单日亏损阈值),观察"策略冻结"是否缓解。

用法: python -m scripts.dd_sweep [start] [end]
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as cfg
import strategies.exit_rules as exit_rules
from risk.control import RiskController
from backtest.portfolio_backtest import PortfolioBacktester
from data.loader import DataLoader
from scripts.ab_backtest import UNIVERSE

logging.basicConfig(level=logging.ERROR, format="%(asctime)s [%(levelname)s] %(message)s")


def _set_thresholds(max_dd: float, daily_loss: float):
    """通过覆盖方法默认参数切换风控阈值(默认参数在 def 时绑定,需直接改 __defaults__)。"""
    RiskController.check_max_drawdown.__defaults__ = (max_dd,)
    RiskController.check_daily_loss.__defaults__ = (daily_loss,)


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "20230101"
    end = sys.argv[2] if len(sys.argv) > 2 else "20250101"

    # 固定较优组合:移动止损开、择时关
    cfg.ENABLE_MARKET_REGIME = False
    exit_rules.ENABLE_TRAILING_STOP = True

    print(f"加载数据... universe={len(UNIVERSE)} {start}~{end}")
    loader = DataLoader()
    try:
        s, e = start.replace("-", ""), end.replace("-", "")
        preload = (datetime.strptime(s, "%Y%m%d") - timedelta(days=180)).strftime("%Y%m%d")
        history_map = {}
        for code in UNIVERSE:
            try:
                df = loader.get_daily_data(code, preload, e, adjust_flag="2")
                if df is not None and not df.empty:
                    df = df.copy()
                    df["name"] = code
                    history_map[code] = df
            except Exception:  # noqa: BLE001
                pass
    finally:
        loader.close()

    # 交易日直接由历史数据并集推导(避免单独取日历失败),并裁剪到回测区间 [s, e]
    all_dates = set()
    for df in history_map.values():
        all_dates.update(str(d).replace("-", "")[:8] for d in df["date"])
    trading_days = sorted(d for d in all_dates if s <= d <= e)

    print(f"数据就绪: {len(history_map)} 只, {len(trading_days)} 天\n")

    # (最大回撤阈值, 单日亏损阈值)
    sweeps = [
        ("6%/2.5%(现状)", 0.06, 0.025),
        ("10%/4%", 0.10, 0.04),
        ("15%/5%", 0.15, 0.05),
        ("25%/8%", 0.25, 0.08),
        ("不限(对照)", 1.0, 1.0),
    ]

    cols = [
        ("阈值", "_label", "{}"),
        ("总收益", "total_return", "{:+.2%}"),
        ("最大回撤", "max_drawdown", "{:.2%}"),
        ("夏普", "sharpe_ratio", "{:.2f}"),
        ("卡尔玛", "calmar_ratio", "{:.2f}"),
        ("胜率", "win_rate", "{:.1%}"),
        ("交易数", "total_trades", "{}"),
        ("风控拒绝", "risk_events", "{}"),
    ]
    header = " | ".join(f"{c[0]:>10}" for c in cols)
    print(header)
    print("-" * len(header))
    for label, dd, dl in sweeps:
        _set_thresholds(dd, dl)
        bt = PortfolioBacktester(initial_capital=cfg.INITIAL_CAPITAL, top_n=10)
        r = bt.run(history_map, trading_days)
        r["_label"] = label
        print(" | ".join(f"{c[2].format(r.get(c[1], 0)):>10}" for c in cols))


if __name__ == "__main__":
    main()
