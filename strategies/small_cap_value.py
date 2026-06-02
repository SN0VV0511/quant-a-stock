"""小市值价值选股因子(纯函数,无 IO)。

研究依据(2025-2026 A股量化共识):
    - A股价格动量长期为负 IC,市场以**短期反转**为主——"涨多了会跌";
    - **小市值 + 低估值(低 PB) + 短期反转** 是 A股长期有效的因子组合;
    - 2024「国九条」退市新规后,纯微盘策略风险剧增,必须叠加严格风控过滤。

打分:对小市值、低 PB、短期反转三个因子做横截面**分位 rank**(对厚尾因子比 z-score
更稳健)后加权,小市值权重最高。先经国九条风控过滤,再排序取 Top-N。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from config.settings import (
    SMALLCAP_TOP_N,
    SMALLCAP_MIN_MKTCAP,
    SMALLCAP_MAX_MKTCAP,
    SMALLCAP_W_SIZE,
    SMALLCAP_W_PB,
    SMALLCAP_W_REVERSAL,
    SMALLCAP_W_OBV,
    SMALLCAP_MIN_PRICE,
    SMALLCAP_MAX_PRICE,
    SMALLCAP_MIN_PB,
    SMALLCAP_REVERSAL_DAYS,
)
from strategies.indicators import calculate_obv_trend


def _rank_pct(values: list[float], ascending: bool = True) -> list[float]:
    """返回每个元素的分位排名(0~1)。ascending=True 时值越小分位越高(得分越高)。

    用"小值优先得高分"的口径:size/pb/reversal 都是越小越好,故 ascending=True
    时返回 (1 - 正常升序分位),即最小值得 ~1 分。
    """
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [1.0]
    order = sorted(range(n), key=lambda i: values[i])
    pct = [0.0] * n
    for rank, idx in enumerate(order):
        # rank 0(最小)→ 分位 0;归一化到 [0,1]
        p = rank / (n - 1)
        pct[idx] = (1.0 - p) if ascending else p
    return pct


def passes_risk_filter(row: dict[str, Any]) -> bool:
    """国九条风控过滤:剔除 ST、停牌、面值/净资产为负风险、市值越界等高风险标的。

    Args:
        row: 含 price, is_st, is_suspended, pb, mktcap 的标的快照。

    Returns:
        True 表示通过风控,可纳入候选。
    """
    if row.get("is_st"):
        return False
    if row.get("is_suspended"):
        return False
    price = row.get("price", 0) or 0
    if price < SMALLCAP_MIN_PRICE:           # 面值退市缓冲
        return False
    if price > SMALLCAP_MAX_PRICE:           # 小资金单手金额控制
        return False
    pb = row.get("pb")
    if pb is None or pb <= SMALLCAP_MIN_PB:   # 净资产为负退市风险
        return False
    mktcap = row.get("mktcap")
    if mktcap is None or mktcap <= 0:
        return False
    if not (SMALLCAP_MIN_MKTCAP <= mktcap <= SMALLCAP_MAX_MKTCAP):
        return False
    return True


def _col(last, df, name):
    """从行中安全读取列值,列缺失或 NaN 返回 None。"""
    if name not in df.columns:
        return None
    val = last.get(name)
    return None if pd.isna(val) else val


def build_factor_rows(history_map, reversal_days=SMALLCAP_REVERSAL_DAYS, name_map=None):
    """从扩展历史构建小市值价值因子快照行(纯函数,回测与实盘共用)。

    每个 DataFrame 的最后一行视为"当前",reversal = close[-1]/close[-(N+1)]-1。
    兼容两种列命名:停牌可来自 ``is_suspended`` 布尔列或 ``tradestatus``(0=停牌)。

    Args:
        history_map: ``{code: DataFrame}``,需含 close,以及 pb/mktcap/is_st 列。
        reversal_days: 短期反转回看交易日数。
        name_map: ``{code: name}`` 展示用,缺失回退代码。

    Returns:
        list[dict]: 供 ``score_small_cap_value`` 使用的快照行。
    """
    name_map = name_map or {}
    rows = []
    for code, df in history_map.items():
        if df is None or len(df) < reversal_days + 1:
            continue
        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        if len(close) < reversal_days + 1:
            continue
        last = df.iloc[-1]
        reversal = float(close.iloc[-1] / close.iloc[-(reversal_days + 1)] - 1)

        if "is_suspended" in df.columns:
            suspended = bool(_col(last, df, "is_suspended"))
        elif "tradestatus" in df.columns:
            ts = _col(last, df, "tradestatus")
            suspended = (ts == 0) if ts is not None else False
        else:
            suspended = False

        pb = _col(last, df, "pb")
        mktcap = _col(last, df, "mktcap")
        obv_trend = 0.0
        if "volume" in df.columns:
            obv_trend = calculate_obv_trend(df, lookback=min(reversal_days, len(df) - 1))

        rows.append({
            "code": code,
            "name": name_map.get(code) or (df["name"].iloc[0] if "name" in df.columns else code),
            "price": float(close.iloc[-1]),
            "pb": float(pb) if pb is not None else None,
            "mktcap": float(mktcap) if mktcap is not None else None,
            "is_st": bool(_col(last, df, "is_st") or 0),
            "is_suspended": suspended,
            "reversal": reversal,
            "obv_trend": obv_trend,
        })
    return rows


def score_small_cap_value(
    rows: list[dict[str, Any]],
    top_n: int = SMALLCAP_TOP_N,
    weights: tuple[float, ...] | None = None,
) -> list[dict[str, Any]]:
    """对标的快照做"小市值+低PB+短期反转"打分并排序(纯函数)。

    Args:
        rows: 每只标的的当日快照 dict,需含:
            code, name, price, mktcap(流通市值,元), pb(市净率),
            reversal(过去 N 日收益率,越小越优), is_st, is_suspended。
        top_n: 返回前 N 名(等权持仓数)。
        weights: (w_size, w_pb, w_reversal[, w_obv]),None 时取 config 默认。

    Returns:
        list[dict]: 通过风控且得分最高的 top_n 只,含 score 与 rank,按 score 降序。
    """
    if weights is None:
        weights = (SMALLCAP_W_SIZE, SMALLCAP_W_PB, SMALLCAP_W_REVERSAL, SMALLCAP_W_OBV)
    if len(weights) == 3:
        w_size, w_pb, w_rev = weights
        w_obv = 0.0
    elif len(weights) == 4:
        w_size, w_pb, w_rev, w_obv = weights
    else:
        raise ValueError(f"小市值权重数量必须为 3 或 4: {weights}")

    pool = [r for r in rows if passes_risk_filter(r)]
    if not pool:
        return []

    # 三个因子均"值越小越优":市值小、PB 低、过去收益低(反转)
    size_score = _rank_pct([float(r["mktcap"]) for r in pool], ascending=True)
    pb_score = _rank_pct([float(r["pb"]) for r in pool], ascending=True)
    rev_score = _rank_pct([float(r.get("reversal", 0.0)) for r in pool], ascending=True)
    obv_score = _rank_pct([float(r.get("obv_trend", 0.0)) for r in pool], ascending=False)

    for i, r in enumerate(pool):
        r["score"] = round(
            size_score[i] * w_size
            + pb_score[i] * w_pb
            + rev_score[i] * w_rev
            + obv_score[i] * w_obv,
            4,
        )
        r["obv_score"] = round(obv_score[i], 4)

    pool.sort(key=lambda x: x["score"], reverse=True)
    final = pool[:top_n]
    for i, r in enumerate(final):
        r["rank"] = i + 1
    return final
