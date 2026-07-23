"""目标组合到整手订单的唯一分配器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from config.settings import (
    MIN_ETF_ORDER_AMOUNT,
    ROBUST_V2_BROAD_ETF_CODES,
    ROBUST_V2_MIN_STOCK_ORDER_AMOUNT,
)
from rules.engine import TradingRules
from trading.instruments import get_instrument_profile, normalized_security_code
from trading.models import OrderIntent, TargetPortfolio, TargetPosition


@dataclass(frozen=True)
class AllocationResult:
    """订单分配结果及未下单原因。"""

    orders: tuple[OrderIntent, ...]
    skipped: dict[str, str]
    account_value: float
    projected_cash: float


class PortfolioAllocator:
    """把目标权重转换为可成交整手订单。

    分配器先生成卖单，再在保留目标现金后生成买单。所有策略必须经过这里，避免
    Combo、RPS 和小市值策略各自计算仓位后共同操作账户。
    """

    def __init__(
        self,
        rules: TradingRules | None = None,
        rebalance_band: float = 0.02,
        min_stock_order: float = ROBUST_V2_MIN_STOCK_ORDER_AMOUNT,
        min_etf_order: float = MIN_ETF_ORDER_AMOUNT,
    ) -> None:
        if not 0 <= rebalance_band <= 0.1:
            raise ValueError("调仓容忍带必须位于 [0, 0.1]")
        self.rules = rules or TradingRules()
        self.rebalance_band = rebalance_band
        self.min_stock_order = min_stock_order
        self.min_etf_order = min_etf_order

    @staticmethod
    def _normalize_positions(
        positions: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, tuple[str, Mapping[str, Any]]]:
        """按六位代码建立当前持仓索引。"""
        result: dict[str, tuple[str, Mapping[str, Any]]] = {}
        for original_code, position in positions.items():
            result[normalized_security_code(original_code)] = (original_code, position)
        return result

    @staticmethod
    def _price_for(
        code: str,
        prices: Mapping[str, float],
        position: Mapping[str, Any] | None = None,
    ) -> float:
        """兼容六位和带市场前缀的价格键。"""
        raw = normalized_security_code(code)
        for key, value in prices.items():
            try:
                if normalized_security_code(key) == raw and float(value or 0) > 0:
                    return float(value)
            except ValueError:
                continue
        if position is not None:
            return float(
                position.get("current_price", position.get("avg_cost", 0)) or 0
            )
        return 0.0

    @staticmethod
    def _validate_target(target: TargetPortfolio) -> None:
        """在执行边界再次校验 robust_v2 仓位约束。"""
        etfs = [
            position for position in target.positions if position.asset_type == "etf"
        ]
        stocks = [
            position for position in target.positions if position.asset_type == "stock"
        ]
        if target.exposure > 0.8 + 1e-9 or target.cash_weight < 0.2 - 1e-9:
            raise ValueError("目标组合违反 80% 总仓位或 20% 现金下限")
        if len(etfs) > 2 or len(stocks) > 2:
            raise ValueError("目标组合最多允许 2 只 ETF 和 2 只股票")
        for position in etfs:
            raw_code = normalized_security_code(position.code)
            if raw_code not in ROBUST_V2_BROAD_ETF_CODES:
                raise ValueError(f"robust_v2 禁止买入非宽基 ETF: {position.code}")
            if position.target_weight > 0.24 + 1e-9:
                raise ValueError(f"ETF {position.code} 超过 24% 单票上限")
        for position in stocks:
            if position.target_weight > 0.16 + 1e-9:
                raise ValueError(f"股票 {position.code} 超过 16% 单票上限")
            if get_instrument_profile(position.code).board != "mainboard":
                raise ValueError(f"robust_v2 禁止买入非主板股票: {position.code}")

    def allocate(
        self,
        target: TargetPortfolio,
        cash: float,
        positions: Mapping[str, Mapping[str, Any]],
        prices: Mapping[str, float],
        execution_date: str,
        tradable_codes: set[str] | frozenset[str] | None = None,
    ) -> AllocationResult:
        """生成卖出优先、买入次之的整手订单。

        Args:
            target: T 日收盘生成的目标组合。
            cash: 当前可用现金。
            positions: 当前账户持仓。
            prices: T+1 执行时价格。
            execution_date: 实际委托日期。
            tradable_codes: 已通过数据口径和新鲜度检查的证券代码；None 表示不限制。
        """
        self._validate_target(target)
        if execution_date <= target.signal_date:
            raise ValueError(
                f"目标组合只能在下一交易日执行: signal={target.signal_date}, execute={execution_date}"
            )
        if cash < 0:
            raise ValueError("可用现金不能为负数")

        indexed_positions = self._normalize_positions(positions)
        allowed = (
            {normalized_security_code(code) for code in tradable_codes}
            if tradable_codes is not None
            else None
        )
        target_by_code: dict[str, TargetPosition] = {
            normalized_security_code(position.code): position
            for position in target.positions
        }
        market_value = 0.0
        for raw, (_, position) in indexed_positions.items():
            price = self._price_for(raw, prices, position)
            market_value += price * int(position.get("shares", 0) or 0)
        account_value = round(cash + market_value, 2)
        if account_value <= 0:
            raise ValueError("账户净值必须大于 0")

        skipped: dict[str, str] = {}
        sell_orders: list[OrderIntent] = []
        buy_orders: list[OrderIntent] = []
        projected_cash = float(cash)
        post_sell_shares: dict[str, int] = {
            raw: int(position.get("shares", 0) or 0)
            for raw, (_, position) in indexed_positions.items()
        }

        # 先降低超配和退出目标外持仓，已有持仓卖出不受板块买入权限限制。
        for raw_code, (original_code, position) in indexed_positions.items():
            if allowed is not None and raw_code not in allowed:
                skipped[original_code] = "行情未通过新鲜度或前收盘口径检查"
                continue
            price = self._price_for(original_code, prices, position)
            if price <= 0:
                skipped[original_code] = "缺少有效执行价格"
                continue
            current_shares = int(position.get("shares", 0) or 0)
            sellable = int(position.get("sellable_qty", current_shares) or 0)
            target_position = target_by_code.get(raw_code)
            target_shares = 0
            if target_position is not None:
                target_shares = (
                    int(account_value * target_position.target_weight / price / 100)
                    * 100
                )
            shares_to_sell = min(max(current_shares - target_shares, 0), sellable)
            if shares_to_sell <= 0:
                if current_shares > target_shares and sellable <= 0:
                    skipped[original_code] = "T+1 锁定，记录信号但本日不可卖"
                continue
            amount = shares_to_sell * price
            if (
                target_position is not None
                and amount / account_value < self.rebalance_band
            ):
                skipped[original_code] = "偏离未超过调仓容忍带"
                continue
            reason = target_position.reason if target_position else "WEEKLY_TARGET_EXIT"
            intent = OrderIntent(
                account_id=target.account_id,
                strategy_version=target.strategy_version,
                signal_date=target.signal_date,
                code=original_code,
                action="sell",
                price=price,
                shares=shares_to_sell,
                name=str(position.get("name", original_code)),
                strategy="A股稳健策略V2",
                strategy_tag="robust_v2",
                reason=reason,
                date=execution_date,
                source="target_allocator",
                metadata={
                    "target_weight": (
                        target_position.target_weight if target_position else 0.0
                    ),
                    "snapshot_hash": target.source_snapshot_hash,
                },
            )
            sell_orders.append(intent)
            post_sell_shares[raw_code] = current_shares - shares_to_sell
            costs = self.rules.calc_total_cost(
                amount,
                direction="sell",
                code=original_code,
            )
            projected_cash += amount - float(costs["total"])

        required_cash = account_value * target.cash_weight
        for target_position in target.positions:
            raw_code = normalized_security_code(target_position.code)
            if allowed is not None and raw_code not in allowed:
                skipped[target_position.code] = "行情未通过新鲜度或前收盘口径检查"
                continue
            current_entry = indexed_positions.get(raw_code)
            current_position = current_entry[1] if current_entry else None
            price = self._price_for(target_position.code, prices, current_position)
            if price <= 0:
                skipped[target_position.code] = "缺少有效执行价格"
                continue
            target_shares = (
                int(account_value * target_position.target_weight / price / 100) * 100
            )
            current_shares = post_sell_shares.get(raw_code, 0)
            desired = max(target_shares - current_shares, 0)
            desired = desired // 100 * 100
            if desired <= 0:
                continue
            amount = desired * price
            min_order = (
                self.min_etf_order
                if target_position.asset_type == "etf"
                else self.min_stock_order
            )
            if amount < min_order:
                skipped[target_position.code] = (
                    f"买入金额 {amount:.2f} 低于最低订单 {min_order:.2f}"
                )
                continue
            if amount / account_value < self.rebalance_band:
                skipped[target_position.code] = "偏离未超过调仓容忍带"
                continue

            usable_cash = max(projected_cash - required_cash, 0.0)
            affordable = desired
            while affordable > 0:
                candidate_amount = affordable * price
                costs = self.rules.calc_total_cost(
                    candidate_amount,
                    direction="buy",
                    code=target_position.code,
                )
                if candidate_amount + float(costs["total"]) <= usable_cash + 1e-9:
                    break
                affordable -= 100
            if affordable <= 0 or affordable * price < min_order:
                skipped[target_position.code] = "保留 20% 现金后资金不足"
                continue
            amount = affordable * price
            costs = self.rules.calc_total_cost(
                amount,
                direction="buy",
                code=target_position.code,
            )
            buy_orders.append(
                OrderIntent(
                    account_id=target.account_id,
                    strategy_version=target.strategy_version,
                    signal_date=target.signal_date,
                    code=target_position.code,
                    action="buy",
                    price=price,
                    shares=affordable,
                    name=target_position.name,
                    strategy="A股稳健策略V2",
                    strategy_tag="robust_v2",
                    reason=target_position.reason,
                    date=execution_date,
                    source="target_allocator",
                    metadata={
                        "target_weight": target_position.target_weight,
                        "snapshot_hash": target.source_snapshot_hash,
                    },
                )
            )
            projected_cash -= amount + float(costs["total"])

        return AllocationResult(
            orders=tuple(sell_orders + buy_orders),
            skipped=skipped,
            account_value=account_value,
            projected_cash=round(projected_cash, 2),
        )
