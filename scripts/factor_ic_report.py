"""因子 IC 报告:用真实 A 股历史算动量/反转/小市值/低PB 的 IC 与分层收益。

复用 scripts.strategy_ab 的 universe 抽样与扩展字段历史加载(含 pb/流通市值),
调用 backtest.factor_ic.compute_factor_ic 输出因子有效性报告,为 P4 策略收敛决策提供
数据依据(用数据回答诊断报告 A1:A股动量到底有没有效、是否该做反转)。

用法: python -m scripts.factor_ic_report [universe_size] [period_days]
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as cfg  # noqa: E402
from scripts.strategy_ab import _build_universe, _load_history, _trading_days  # noqa: E402
from backtest.factor_ic import compute_factor_ic  # noqa: E402


def _fmt(v: float | None, pct: bool = False) -> str:
    if v is None:
        return "    NA"
    return f"{v:+.2%}" if pct else f"{v:+.4f}"


def main() -> None:
    size = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    period = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    lo = (datetime.now() - timedelta(days=1080)).strftime("%Y%m%d")
    hi = datetime.now().strftime("%Y%m%d")
    preload = (datetime.strptime(lo, "%Y%m%d") - timedelta(days=200)).strftime("%Y%m%d")

    print(f"抽样 universe(目标 {size} 只),加载扩展历史(含估值/市值)...")
    codes = _build_universe(size)
    history = _load_history(codes, preload, hi)
    days = _trading_days(history, lo, hi)
    print(f"数据就绪:{len(history)} 只,{len(days)} 个交易日,前瞻周期 {period} 日\n")

    ic = compute_factor_ic(history, days, period=period)

    print(f"=== 因子 IC 报告(窗口 {lo[:4]}-{lo[4:6]} ~ {hi[:4]}-{hi[4:6]}) ===")
    print(f"{'因子':<14}{'IC均值':>9}{'IC_IR':>9}{'正IC率':>9}{'期数':>6}   分层收益 低→高")
    print("-" * 90)
    for name, m in ic.items():
        layers = " ".join(_fmt(l, pct=True) for l in m["layer_returns"])
        print(f"{name:<14}{_fmt(m['ic_mean']):>9}{_fmt(m['ic_ir']):>9}"
              f"{_fmt(m['positive_ratio']):>9}{m['n_periods']:>6}   {layers}")
    print("-" * 90)
    print("判读:IC均值>0 表示该方向(动量/反转/小市值/低PB)有效;|IC|>0.03 且 |IR|>0.3 较显著;")
    print("分层收益从低到高单调递增 => 因子区分度好。动量与反转 IC 符号相反(同一价格序列取负)。")


if __name__ == "__main__":
    main()
