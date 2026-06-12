"""ETF/RPS 轮动回测器:复用实盘选股(calculate_rps_scores)与分层退出(evaluate_position_exit)。

替代 scripts/strategy_ab.py 中与实盘零共享的平行 _run_rps_backtest——后者用 lookback=60
+ 行业动量加权、close 价成交、无 T+1/风控/分层退出,与实盘(lookback=20、纯 ETF 分位、
etf_rotation 退出 policy)严重脱节,回测数字对实盘毫无参考价值。

本类继承 ``PortfolioBacktester``,只重写订单生成 ``_build_orders``:
    - 退出:每日对持仓走 ``etf_rotation`` policy(跌破 MA20 / MA20<MA60 / 12% 极端止损);
    - 轮动:周频调仓日用 ``calculate_rps_scores`` 选 top_n,排名跌出者卖、入选未持有者买。
其余 T+1 开盘撮合、交易成本、RiskController、净值与绩效计算完全复用,保证与动量/小市值
策略 A/B/C 对比在同一执行内核下公平。所有参数取自 config.settings,与实盘对齐。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from config.settings import (
    INITIAL_CAPITAL,
    MAX_SINGLE_ETF,
    RPS_TOP_N,
    RPS_LOOKBACK_DAYS,
    RPS_MIN_SCORE,
    RPS_MIN_AVG_VOLUME,
    ETF_REBALANCE_WEEKDAY,
)
from backtest.portfolio_backtest import PortfolioBacktester, _holding_days
from strategies.rps_rotation import calculate_rps_scores


class RPSBacktester(PortfolioBacktester):
    """ETF/RPS 日频轮动回测器(周频调仓 + etf_rotation 分层退出)。"""

    def __init__(
        self,
        initial_capital: float = INITIAL_CAPITAL,
        top_n: int = RPS_TOP_N,
        lookback: int = RPS_LOOKBACK_DAYS,
        min_rps: float = RPS_MIN_SCORE,
        min_avg_volume: float = RPS_MIN_AVG_VOLUME,
        rebalance_weekday: int = ETF_REBALANCE_WEEKDAY,
    ) -> None:
        # ETF 单票上限用 MAX_SINGLE_ETF(0.30),top_n=3 时约满仓三只,与实盘一致。
        super().__init__(
            initial_capital=initial_capital,
            top_n=top_n,
            max_single_stock=MAX_SINGLE_ETF,
        )
        self.lookback = lookback
        self.min_rps = min_rps
        self.min_avg_volume = min_avg_volume
        self.rebalance_weekday = rebalance_weekday

    def _build_orders(
        self, frames: dict, date: str, close_today: dict[str, float],
        portfolio, day_index: int, index_frame: dict | None,
    ) -> list[dict[str, Any]]:
        """生成 T+1 待执行订单:每日 etf_rotation 退出 + 周频 RPS 轮动。"""
        orders: list[dict[str, Any]] = []
        positions = portfolio.get_all_positions()

        # 1) 退出:ETF 走 etf_rotation policy(跌破 MA20 / MA20<MA60 / 极端止损)
        for code, pos in positions.items():
            price = close_today.get(code)
            avg_cost = float(pos.get("avg_cost", 0) or 0)
            if not price or price <= 0 or avg_cost <= 0:
                continue
            decision = self._exit_reason(
                frames,
                code,
                date,
                price,
                avg_cost,
                float(pos.get("peak_price", avg_cost) or avg_cost),
                _holding_days(pos.get("buy_date"), date),
                "rps_rotation",  # → get_exit_policy 映射为 etf_rotation
                portfolio.get_sellable_qty(code, date),
            )
            if decision:
                orders.append({
                    "code": code, "action": "sell", "name": pos.get("name", code),
                    "strategy": "ETF/RPS轮动", "strategy_tag": "rps_rotation",
                    "reason": decision.sell_reason,
                })
        sell_codes = {o["code"] for o in orders}

        # 2) 周频调仓日:用实盘 calculate_rps_scores 选 top_n
        if datetime.strptime(date, "%Y%m%d").weekday() != self.rebalance_weekday:
            return orders

        sliced: dict[str, Any] = {}
        name_map: dict[str, str] = {}
        for code, fr in frames.items():
            sl = self._slice(fr, date)
            if sl is not None:
                sliced[code] = sl
                name_map[code] = fr["name"]
        signals = calculate_rps_scores(
            sliced,
            lookback=self.lookback,
            top_n=self.top_n,
            min_rps=self.min_rps,
            min_avg_volume=self.min_avg_volume,
            name_map=name_map,
        )
        selected = {s["code"] for s in signals}
        held = set(positions)

        # 排名跌出候选池 → 轮动卖出(若尚未因退出规则卖出)
        for code in held - selected - sell_codes:
            orders.append({
                "code": code, "action": "sell", "name": positions[code].get("name", code),
                "strategy": "ETF/RPS轮动", "strategy_tag": "rps_rotation",
                "reason": "ETF_ROTATION_EXIT",
            })
            sell_codes.add(code)

        # 入选且未持有 → 买入
        for sig in signals:
            code = sig["code"]
            if code in held or code in sell_codes:
                continue
            orders.append({
                "code": code, "action": "buy", "name": sig.get("name", code),
                "strategy": "ETF/RPS轮动", "strategy_tag": "rps_rotation",
                "reason": f"RPS rank{sig.get('rank')} 动量{float(sig.get('momentum', 0)):+.2%}",
            })
        return orders
