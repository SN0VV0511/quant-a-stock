"""小市值价值策略回测器(周度调仓),复用组合回测器的执行/风控/计价/指标内核。

继承 ``PortfolioBacktester``,仅重写订单生成 ``_build_orders``:每 N 个交易日做一次
全量调仓——按"小市值+低PB+短期反转"因子选 Top-N 等权持有,卖出调出标的、买入调入标的;
其余 T+1 成交、交易成本、风控、净值与绩效计算完全复用,保证与动量策略 A/B 对比公平。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from config.settings import (
    INITIAL_CAPITAL,
    MAX_SINGLE_STOCK,
    MAX_TOTAL_POSITION,
    SMALLCAP_TOP_N,
    SMALLCAP_REBALANCE_DAYS,
    SMALLCAP_REVERSAL_DAYS,
)
from backtest.portfolio_backtest import PortfolioBacktester
from strategies.small_cap_value import score_small_cap_value, build_factor_rows

# 买入策略名含"全市场扫描"以绕过固定白名单(与线上风控约定一致)
_BUY_STRATEGY = "全市场扫描+小市值价值"


class FactorBacktester(PortfolioBacktester):
    """小市值价值因子 + 周度调仓回测器。"""

    def __init__(
        self,
        initial_capital: float = INITIAL_CAPITAL,
        top_n: int = SMALLCAP_TOP_N,
        rebalance_days: int = SMALLCAP_REBALANCE_DAYS,
        reversal_days: int = SMALLCAP_REVERSAL_DAYS,
    ) -> None:
        # 等权目标受单票和总仓位双重约束,避免调仓订单被风控整体拒绝。
        target_single_ratio = min(MAX_SINGLE_STOCK, MAX_TOTAL_POSITION / max(1, top_n))
        super().__init__(
            initial_capital=initial_capital,
            top_n=top_n,
            rebalance_every=rebalance_days,
            max_single_stock=target_single_ratio,
        )
        self.reversal_days = reversal_days

    def _build_orders(
        self, frames: dict, date: str, close_today: dict[str, float],
        portfolio, day_index: int, index_frame: dict | None,
    ) -> list[dict[str, Any]]:
        """周度因子调仓:非调仓日持有不动;调仓日按目标组合差额买卖。"""
        if day_index % self.rebalance_every != 0:
            return []  # 非调仓日持有

        holdings = portfolio.get_all_positions()

        # 大盘择时:risk-off 全部清仓(小市值策略择时有效,默认随 config 开关)
        if not self._regime_on(index_frame, date):
            return [
                {"code": c, "action": "sell", "name": p.get("name", c),
                 "strategy": "大盘择时清仓", "reason": "大盘 risk-off"}
                for c, p in holdings.items()
            ]

        target = self._rank_factor(frames, date)
        target_codes = {t["code"] for t in target}
        held = set(holdings)

        orders: list[dict[str, Any]] = []
        # 调出:持有但不在目标
        for code in held - target_codes:
            orders.append({
                "code": code, "action": "sell",
                "name": holdings[code].get("name", code),
                "strategy": "小市值调仓", "reason": "调出目标池",
            })
        # 调入:目标但未持有
        for t in target:
            if t["code"] not in held:
                orders.append({
                    "code": t["code"], "action": "buy", "name": t.get("name", t["code"]),
                    "strategy": _BUY_STRATEGY, "reason": f"小市值价值 rank{t['rank']}",
                })
        return orders

    def _rank_factor(self, frames: dict, date: str) -> list[dict[str, Any]]:
        """用截至 ``date`` 的切片构建标的快照并做小市值价值打分。"""
        sliced = {}
        name_map = {}
        for code, fr in frames.items():
            sl = self._slice(fr, date)
            if sl is not None:
                sliced[code] = sl
                name_map[code] = fr.get("name", code)
        rows = build_factor_rows(sliced, reversal_days=self.reversal_days, name_map=name_map)
        return score_small_cap_value(rows, top_n=self.top_n)
