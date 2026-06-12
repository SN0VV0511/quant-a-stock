"""因子有效性分析:IC(信息系数)与分层收益(纯函数,无 IO)。

诊断报告 A1:扫描策略追涨 60 日动量,而小市值策略注释称"A股价格动量长期负 IC、
以短期反转为主"——两套选股逻辑哲学冲突,却同时跑在一个账户。本模块用历史数据对
各候选因子算 IC 与分层收益,用数据判定哪些因子在当前样本真正有效,为策略收敛(P4)
提供依据,而非靠直觉。

IC 定义:每个调仓期,因子值(T 日横截面)与下期收益(T→T+period)的 Spearman 秩相关。
约定每个因子提取器返回"越大越看多"方向(反转/小市值/低PB 取负),使正 IC 恒表示
"该方向有效"。判据经验值:|IC 均值|>0.03 且 |IR|>0.3 视为有一定有效性,分层收益单调
则进一步佐证。
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pandas as pd


def _spearman(x: list[float], y: list[float]) -> float:
    """Spearman 秩相关(对秩做 Pearson);样本不足或零方差返回 nan。"""
    if len(x) < 3:
        return float("nan")
    sx = pd.Series(x).rank()
    sy = pd.Series(y).rank()
    if sx.std() == 0 or sy.std() == 0:
        return float("nan")
    return float(sx.corr(sy))


def _f_momentum(df: pd.DataFrame, lookback: int = 60) -> float | None:
    """60 日动量(追涨方向:越大越看多)。"""
    c = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(c) < lookback + 1:
        return None
    base = float(c.iloc[-(lookback + 1)])
    return float(c.iloc[-1] / base - 1) if base > 0 else None


def _f_reversal(df: pd.DataFrame, lookback: int = 20) -> float | None:
    """短期反转(过去收益取负:跌得多→因子值高→预期反弹)。"""
    c = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(c) < lookback + 1:
        return None
    base = float(c.iloc[-(lookback + 1)])
    return float(-(c.iloc[-1] / base - 1)) if base > 0 else None


def _f_size(df: pd.DataFrame) -> float | None:
    """小市值(取负对数市值:市值越小→因子值越高)。"""
    if "mktcap" not in df.columns:
        return None
    v = pd.to_numeric(df["mktcap"], errors="coerce").dropna()
    if v.empty or float(v.iloc[-1]) <= 0:
        return None
    return float(-np.log(float(v.iloc[-1])))


def _f_low_pb(df: pd.DataFrame) -> float | None:
    """低估值(取负 PB:PB 越低→因子值越高)。"""
    if "pb" not in df.columns:
        return None
    v = pd.to_numeric(df["pb"], errors="coerce").dropna()
    if v.empty or float(v.iloc[-1]) <= 0:
        return None
    return float(-float(v.iloc[-1]))


FACTORS: dict[str, Callable[[pd.DataFrame], float | None]] = {
    "momentum_60": _f_momentum,
    "reversal_20": _f_reversal,
    "size_small": _f_size,
    "low_pb": _f_low_pb,
}


def compute_factor_ic(
    history_map: dict[str, pd.DataFrame],
    trading_days: list[str],
    period: int = 20,
    n_layers: int = 5,
    factors: dict[str, Callable] | None = None,
) -> dict[str, dict[str, Any]]:
    """计算各因子的 IC 序列、统计与分层收益。

    Args:
        history_map: {code: DataFrame(含 date, close[, pb, mktcap])},前复权口径。
        trading_days: 升序交易日列表(YYYYMMDD 或带横线)。
        period: 调仓/前瞻周期(交易日)。
        n_layers: 分层数。
        factors: {name: extractor},None 用默认 FACTORS。

    Returns:
        {factor: {ic_mean, ic_ir, positive_ratio, n_periods, layer_returns}};
        layer_returns 从因子值最低层到最高层,有效因子应单调递增。
    """
    factors = factors or FACTORS
    frames: dict[str, pd.DataFrame] = {}
    for code, df in history_map.items():
        if df is None or df.empty or "close" not in df.columns:
            continue
        d = df.copy()
        d["date"] = d["date"].map(lambda x: str(x).replace("-", "")[:8])
        frames[code] = d.sort_values("date").reset_index(drop=True)

    days = sorted({str(x).replace("-", "")[:8] for x in trading_days})
    acc = {name: {"ic": [], "layers": [[] for _ in range(n_layers)]} for name in factors}

    for i in range(0, len(days) - period, period):
        t, t_fwd = days[i], days[i + period]
        rows: list[tuple[dict[str, float], float]] = []
        for d in frames.values():
            sub = d[d["date"] <= t]
            fwd = d[d["date"] == t_fwd]
            if sub.empty or fwd.empty:
                continue
            c_sub = pd.to_numeric(sub["close"], errors="coerce").dropna()
            if c_sub.empty:
                continue
            close_t = float(c_sub.iloc[-1])
            close_fwd = float(pd.to_numeric(fwd["close"], errors="coerce").iloc[-1])
            if close_t <= 0 or close_fwd <= 0:
                continue
            fv = {}
            for name, fn in factors.items():
                val = fn(sub)
                if val is not None and np.isfinite(val):
                    fv[name] = val
            if fv:
                rows.append((fv, close_fwd / close_t - 1))

        for name in factors:
            xs = [r[0][name] for r in rows if name in r[0]]
            ys = [r[1] for r in rows if name in r[0]]
            if len(xs) < n_layers:
                continue
            ic = _spearman(xs, ys)
            if np.isfinite(ic):
                acc[name]["ic"].append(ic)
            order = np.argsort(xs)
            layer_size = len(order) / n_layers
            for rank, idx in enumerate(order):
                layer = min(int(rank / layer_size), n_layers - 1)
                acc[name]["layers"][layer].append(ys[idx])

    out: dict[str, dict[str, Any]] = {}
    for name, data in acc.items():
        ic_arr = np.array(data["ic"], dtype=float)
        out[name] = {
            "ic_mean": round(float(ic_arr.mean()), 4) if ic_arr.size else None,
            "ic_ir": (round(float(ic_arr.mean() / ic_arr.std()), 4)
                      if ic_arr.size > 1 and ic_arr.std() > 0 else None),
            "positive_ratio": round(float((ic_arr > 0).mean()), 4) if ic_arr.size else None,
            "n_periods": int(ic_arr.size),
            "layer_returns": [round(float(np.mean(layer)), 4) if layer else None
                              for layer in data["layers"]],
        }
    return out
