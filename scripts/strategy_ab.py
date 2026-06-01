"""动量策略 vs 小市值价值策略 A/B 回测对比。

同一份历史数据、同一执行/风控/成本内核,对比两种选股逻辑在牛/熊窗口的表现:
    - 动量:全市场扫描动量选股 + Combo 择时(现有线上策略)
    - 小市值价值:小市值 + 低PB + 短期反转 + 国九条风控,周度调仓(新策略)

universe 用 BaoStock 全市场股票按步长抽样近似,数据一次性加载并附带 pb / 流通市值 /
ST / 停牌字段,供因子策略使用。

用法: python -m scripts.strategy_ab [universe_size]
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import config.settings as cfg
from backtest.portfolio_backtest import PortfolioBacktester
from backtest.factor_backtest import FactorBacktester
from data.ak_loader import AKDataLoader
from data.loader import DataLoader

logging.basicConfig(level=logging.ERROR, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("strategy_ab")

_EXT_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg,"
    "peTTM,pbMRQ,isST,tradestatus"
)


def _build_universe(size: int) -> list[str]:
    """从 BaoStock 全市场股票按步长抽样,得到一个分散的回测 universe。"""
    ak = AKDataLoader()
    try:
        stocks = ak.get_all_stocks()
    finally:
        ak.close()
    codes = [s["code"] for s in stocks]
    if len(codes) <= size:
        return codes
    step = len(codes) // size
    return codes[::step][:size]


def _load_history(codes, preload_start, end):
    """加载扩展字段历史并附加 pb / 流通市值 / ST / 停牌列。"""
    loader = DataLoader()
    history = {}
    try:
        for code in codes:
            try:
                df = loader.get_daily_data(code, preload_start, end,
                                           adjust_flag="2", fields=_EXT_FIELDS)
                if df is None or df.empty:
                    continue
                df = df.copy()
                for col in ("pbMRQ", "isST", "tradestatus", "turn", "volume", "close"):
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                df["pb"] = df.get("pbMRQ")
                df["is_st"] = df.get("isST", 0)
                # 流通市值(元)= close * 流通股本 = close * volume / (turn/100)
                turn = df["turn"].where(df["turn"] > 0)
                df["mktcap"] = df["close"] * df["volume"] / (turn / 100.0)
                df["name"] = code
                history[code] = df
            except Exception as exc:  # noqa: BLE001
                logger.warning("加载失败 %s: %s", code, exc)
    finally:
        loader.close()
    return history


def _trading_days(history, lo, hi):
    days = set()
    for df in history.values():
        days.update(str(d).replace("-", "")[:8] for d in df["date"])
    return sorted(d for d in days if lo <= d <= hi)


def _row(label, r):
    cols = [
        (label, "{}", None),
        (r.get("total_return", 0), "{:+.2%}", None),
        (r.get("max_drawdown", 0), "{:.2%}", None),
        (r.get("sharpe_ratio", 0), "{:.2f}", None),
        (r.get("calmar_ratio", 0), "{:.2f}", None),
        (r.get("win_rate", 0), "{:.1%}", None),
        (r.get("total_trades", 0), "{}", None),
    ]
    return " | ".join(f"{fmt.format(v):>10}" for v, fmt, _ in cols)


def _print_window(title, history, days):
    print(f"\n=== {title}（{len(days)} 个交易日） ===")
    print(" | ".join(f"{h:>10}" for h in
                      ["策略", "总收益", "最大回撤", "夏普", "卡尔玛", "胜率", "交易数"]))
    print("-" * 80)
    mom = PortfolioBacktester(initial_capital=cfg.INITIAL_CAPITAL, top_n=10).run(history, days)
    fac = FactorBacktester(initial_capital=cfg.INITIAL_CAPITAL).run(history, days)
    print(_row("动量Combo", mom))
    print(_row("小市值价值", fac))
    return mom, fac


def _save_ab(title, mom, fac):
    """将动量 vs 小市值价值两条曲线写入 reports/backtest_latest.json 供仪表盘展示。"""
    import json
    from datetime import datetime as _dt

    def series(name, r):
        keys = ("total_return", "annual_return", "max_drawdown", "sharpe_ratio",
                "calmar_ratio", "win_rate", "profit_factor", "total_trades")
        return {
            "name": name,
            "metrics": {k: r.get(k) for k in keys},
            "equity": [{"date": d["date"], "value": d["total_value"]}
                       for d in r.get("daily_values", [])],
        }

    payload = {
        "generated_at": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": title,
        "series": [series("动量Combo", mom), series("小市值价值", fac)],
    }
    path = os.path.join(cfg.REPORT_DIR, "backtest_latest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"\n已写入仪表盘回测数据: {path}")


def main():
    size = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    cfg.ENABLE_MARKET_REGIME = False  # A/B 基线统一关闭择时

    print(f"抽样 universe (目标 {size} 只)...")
    codes = _build_universe(size)
    print(f"universe: {len(codes)} 只,加载扩展历史(含估值/市值/ST)...")

    full_lo, full_hi = "20230101", "20250101"
    preload = (datetime.strptime(full_lo, "%Y%m%d") - timedelta(days=200)).strftime("%Y%m%d")
    history = _load_history(codes, preload, full_hi)
    print(f"数据就绪: {len(history)}/{len(codes)} 只")

    _print_window("熊市窗口 2023-01~2025-01", history,
                  _trading_days(history, "20230101", "20250101"))
    bull_days = _trading_days(history, "20240801", "20250101")
    mom, fac = _print_window("反弹窗口 2024-08~2025-01", history, bull_days)
    # 把反弹窗口的对比落地给仪表盘
    _save_ab("反弹窗口 2024-08~2025-01", mom, fac)


if __name__ == "__main__":
    main()
