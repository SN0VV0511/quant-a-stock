"""ETF / 行业代理 RPS 日频轮动策略。

RPS(Relative Price Strength) 用横截面收益率分位衡量相对强弱。当前项目尚未
接入真实 ETF/行业指数交易数据,因此本模块默认可用于 ETF 代理池或行业代理标的;
未来接入真实 ETF 后只需替换 ``target_pool`` 与历史数据输入。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from config.settings import (
    DEFAULT_ETF_PROXY_POOL,
    RPS_LOOKBACK_DAYS,
    RPS_MIN_AVG_VOLUME,
    RPS_MIN_SCORE,
    RPS_TOP_N,
)


def _norm_date(value: Any) -> str:
    """归一化日期为 YYYYMMDD。"""
    return str(value).strip().replace("-", "")[:8]


def _percentile_scores(values: list[float]) -> list[float]:
    """将数值转换为 0~100 横截面分位,值越大分位越高。"""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [100.0]

    order = sorted(range(n), key=lambda i: values[i])
    scores = [0.0] * n
    for rank, idx in enumerate(order):
        scores[idx] = round(rank / (n - 1) * 100, 2)
    return scores


def calculate_rps_scores(
    history_map: dict[str, pd.DataFrame],
    lookback: int = RPS_LOOKBACK_DAYS,
    top_n: int = RPS_TOP_N,
    min_rps: float = RPS_MIN_SCORE,
    min_avg_volume: float = RPS_MIN_AVG_VOLUME,
    name_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """计算 ETF/行业代理标的 RPS 排名。

    Args:
        history_map: ``{code: DataFrame}``,DataFrame 需含 ``close`` 列,建议含 ``volume``。
        lookback: 收益率回看交易日。
        top_n: 最多返回数量。
        min_rps: 最低 RPS 分位。
        min_avg_volume: 20 日均量下限;缺少 ``volume`` 时不做该过滤。
        name_map: ``{code: name}`` 展示用。

    Returns:
        按 RPS 降序排列的候选列表。

    Raises:
        ValueError: 参数非法时抛出。
    """
    if lookback <= 0:
        raise ValueError(f"RPS 回看周期必须为正整数: {lookback}")
    if top_n <= 0:
        raise ValueError(f"RPS top_n 必须为正整数: {top_n}")

    name_map = name_map or {}
    rows: list[dict[str, Any]] = []
    for code, df in history_map.items():
        if df is None or len(df) < lookback + 1 or "close" not in df.columns:
            continue
        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        if len(close) < lookback + 1:
            continue
        volume_ok = True
        avg_volume = None
        if "volume" in df.columns:
            volume = pd.to_numeric(df["volume"], errors="coerce").dropna()
            avg_volume = float(volume.tail(20).mean()) if len(volume) >= 20 else 0.0
            volume_ok = avg_volume >= min_avg_volume
        if not volume_ok:
            continue

        base = float(close.iloc[-(lookback + 1)])
        current = float(close.iloc[-1])
        if base <= 0 or current <= 0:
            continue
        momentum = current / base - 1
        ma20 = float(close.tail(20).mean()) if len(close) >= 20 else current
        rows.append({
            "code": code,
            "name": name_map.get(code, code),
            "price": round(current, 3),
            "momentum": round(momentum, 4),
            "ma20": round(ma20, 3),
            "trend_ok": current >= ma20,
            "avg_volume": int(avg_volume) if avg_volume is not None else None,
        })

    if not rows:
        return []

    rps_scores = _percentile_scores([float(r["momentum"]) for r in rows])
    for i, row in enumerate(rows):
        row["rps"] = rps_scores[i]

    selected = [r for r in rows if float(r["rps"]) >= min_rps and r["trend_ok"]]
    selected.sort(key=lambda r: (float(r["rps"]), float(r["momentum"])), reverse=True)
    final = selected[:top_n]
    for i, row in enumerate(final):
        row["rank"] = i + 1
    return final


class RPSRotationStrategy:
    """ETF/行业代理 RPS 日频轮动策略。"""

    def __init__(
        self,
        target_pool: dict[str, dict[str, str]] | None = None,
        lookback: int = RPS_LOOKBACK_DAYS,
        top_n: int = RPS_TOP_N,
        min_rps: float = RPS_MIN_SCORE,
    ) -> None:
        """初始化 RPS 轮动策略。

        Args:
            target_pool: 标的池,默认使用 ETF 代理池。
            lookback: RPS 回看周期。
            top_n: 每日最多持有/买入数量。
            min_rps: 入选最低 RPS 分位。
        """
        self.target_pool = target_pool or DEFAULT_ETF_PROXY_POOL
        self.lookback = lookback
        self.top_n = top_n
        self.min_rps = min_rps
        self.name = "ETF/行业RPS轮动"

    def calculate_signals(
        self,
        history_map: dict[str, pd.DataFrame],
        current_date: str,
    ) -> list[dict[str, Any]]:
        """按截至 ``current_date`` 的历史数据计算 RPS 买入候选。"""
        current = _norm_date(current_date)
        sliced: dict[str, pd.DataFrame] = {}
        name_map: dict[str, str] = {}
        for code, df in history_map.items():
            if df is None or df.empty or "date" not in df.columns:
                continue
            data = df.copy()
            data["date"] = data["date"].map(_norm_date)
            sl = data[data["date"] <= current]
            if not sl.empty:
                sliced[code] = sl
                name_map[code] = self.target_pool.get(code, {}).get("name", code)

        return calculate_rps_scores(
            sliced,
            lookback=self.lookback,
            top_n=self.top_n,
            min_rps=self.min_rps,
            name_map=name_map,
        )

    def generate_orders(
        self,
        history_map: dict[str, pd.DataFrame],
        current_portfolio: dict[str, dict[str, Any]],
        current_date: str,
    ) -> list[dict[str, Any]]:
        """生成日频调仓订单。

        Args:
            history_map: 标的历史数据。
            current_portfolio: 当前持仓。
            current_date: 当前日期。

        Returns:
            买卖订单列表,股数由回测/风控模块后续计算。
        """
        signals = self.calculate_signals(history_map, current_date)
        selected_codes = {s["code"] for s in signals}
        signal_map = {s["code"]: s for s in signals}
        tradable_codes = set(self.target_pool)
        orders: list[dict[str, Any]] = []

        for code, pos in current_portfolio.items():
            if code in tradable_codes and code not in selected_codes:
                orders.append({
                    "code": code,
                    "name": pos.get("name", code),
                    "action": "sell",
                    "shares": pos.get("shares", 0),
                    "price": pos.get("current_price", 0),
                    "strategy": self.name,
                    "reason": "RPS跌出目标池",
                })

        for signal in signals:
            code = signal["code"]
            if code in current_portfolio:
                continue
            orders.append({
                "code": code,
                "name": signal.get("name", code),
                "action": "buy",
                "shares": 0,
                "price": signal.get("price", 0),
                "strategy": self.name,
                "reason": f"RPS {signal['rps']:.0f} rank{signal['rank']}",
            })
        return orders
