"""
风控模块
五层风控：资金管理、交易前、交易中、交易后、合规
"""

import logging
from datetime import datetime

from config.settings import (
    INITIAL_CAPITAL, MAX_TOTAL_POSITION, MAX_SINGLE_ETF, MAX_SINGLE_STOCK,
    CASH_BUFFER, DAILY_LOSS_THRESHOLD, MAX_DRAWDOWN_THRESHOLD, LOT_SIZE,
    is_etf, DEFAULT_UNIVERSE,
)

logger = logging.getLogger(__name__)


class RiskController:
    """五层风控"""

    def __init__(self):
        self.name = "风控引擎"
        self._daily_start_value = None
        self._strategy_returns = {}  # strategy -> [daily_returns]
        self._audit_log = []        # 参数变更审计

    # ==================== 第一层：资金管理 ====================

    def check_capital_allocation(self, order, portfolio):
        """检查资金分配

        Args:
            order: {code, action, shares, price}
            portfolio: PositionManager

        Returns:
            (approved, reason)
        """
        total_value = portfolio.get_total_value()
        cash = portfolio.get_cash()

        if order["action"] == "buy":
            order_amount = order["price"] * order["shares"]
            # 根据策略来源判断是否为 ETF 代理
            strategy_name = order.get("strategy", "")
            is_etf_proxy = "ETF" in strategy_name or "动量" in strategy_name
            is_etf_flag = is_etf(order["code"]) or is_etf_proxy

            # 检查现金
            if order_amount > cash * (1 - CASH_BUFFER):
                return False, f"现金不足（需要 {order_amount:.0f}，可用 {cash * (1 - CASH_BUFFER):.0f}）"

            # 检查单票上限
            max_ratio = MAX_SINGLE_ETF if is_etf_flag else MAX_SINGLE_STOCK
            within_limit, reason = portfolio.check_position_limit(
                order["code"], order_amount, total_value, max_ratio_override=max_ratio
            )
            if not within_limit:
                # 如果是最低一手（100股），放宽限制
                if order["shares"] == LOT_SIZE:
                    logger.info(f"{order['code']} 单手超出仓位限制，已放宽")
                else:
                    return False, reason

            # 检查总仓位
            within_total, ratio = portfolio.check_total_position_limit(order_amount)
            if not within_total:
                return False, f"超过总仓位限制 {MAX_TOTAL_POSITION:.0%}（当前 {ratio:.1%}）"

        return True, "通过"

    # ==================== 第二层：交易前风控 ====================

    def pre_trade_check(self, order, portfolio, market_data=None):
        """交易前风控检查

        Args:
            order: {code, action, shares, price, name}
            portfolio: PositionManager
            market_data: {code: {"prev_close": float, "is_st": bool, "is_suspended": bool}}

        Returns:
            (approved, reason)
        """
        code = order["code"]
        action = order["action"]

        # 1. 标的白名单检查（扫描策略跳过白名单限制）
        strategy_name = order.get("strategy", "")
        if strategy_name != "全市场扫描" and code not in DEFAULT_UNIVERSE:
            return False, f"标的 {code} 不在白名单中"

        # 2. 市场数据检查
        if market_data and code in market_data:
            md = market_data[code]

            # ST 过滤（买入时）
            if action == "buy" and md.get("is_st", False):
                return False, f"标的 {code} 为 ST，禁止买入"

            # 停牌过滤
            if md.get("is_suspended", False):
                return False, f"标的 {code} 停牌中"

            # 涨跌停检查
            if md.get("prev_close") and md.get("current_price"):
                from rules.engine import TradingRules
                rules = TradingRules()
                can_buy, can_sell, limit_type = rules.check_price_limit(
                    code, md["current_price"], md["prev_close"],
                    name=order.get("name", "")
                )
                if action == "buy" and not can_buy:
                    return False, f"标的 {code} {limit_type}，不可买入"
                if action == "sell" and not can_sell:
                    return False, f"标的 {code} {limit_type}，不可卖出"

        # 3. T+1 检查（卖出时）
        if action == "sell":
            can_sell, reason = portfolio.can_sell(code, order.get("date", datetime.now().strftime("%Y%m%d")))
            if not can_sell:
                return False, reason

        # 4. 持仓检查（卖出时）
        if action == "sell":
            pos = portfolio.get_position(code)
            if not pos:
                return False, f"标的 {code} 无持仓"
            if order.get("shares", 0) > pos["shares"]:
                order["shares"] = pos["shares"]

        # 5. 价格合理性
        if order.get("price", 0) <= 0:
            return False, f"价格异常: {order.get('price')}"

        # 6. 资金检查
        approved, reason = self.check_capital_allocation(order, portfolio)
        if not approved:
            return False, reason

        return True, "通过"

    # ==================== 第三层：交易中风控 ====================

    def check_execution_quality(self, order, execution):
        """检查执行质量

        Args:
            order: 原始订单
            execution: 实际执行结果

        Returns:
            (ok, reason)
        """
        # 检查滑点偏差
        if order["price"] > 0 and execution.get("actual_price", 0) > 0:
            slippage = abs(execution["actual_price"] - order["price"]) / order["price"]
            expected_slippage = 0.002 if not is_etf(order["code"]) else 0.001
            if slippage > expected_slippage * 3:
                logger.warning(f"异常滑点: {order['code']} 预期 {expected_slippage:.3%} 实际 {slippage:.3%}")
                return False, f"异常滑点 {slippage:.3%}"

        return True, "通过"

    # ==================== 第四层：交易后风控 ====================

    def check_daily_loss(self, portfolio, threshold=DAILY_LOSS_THRESHOLD):
        """检查单日亏损

        Args:
            portfolio: PositionManager
            threshold: 触发阈值

        Returns:
            (exceeded, loss_pct)
        """
        if self._daily_start_value is None:
            return False, 0.0

        current_value = portfolio.get_total_value()
        if self._daily_start_value <= 0:
            return False, 0.0

        loss_pct = (self._daily_start_value - current_value) / self._daily_start_value
        exceeded = loss_pct >= threshold

        if exceeded:
            logger.warning(f"单日亏损触发: {loss_pct:.2%} >= {threshold:.2%}")

        return exceeded, round(loss_pct, 4)

    def check_max_drawdown(self, portfolio, threshold=MAX_DRAWDOWN_THRESHOLD):
        """检查最大回撤

        Returns:
            (exceeded, drawdown)
        """
        drawdown = portfolio.get_drawdown()
        exceeded = drawdown >= threshold

        if exceeded:
            logger.warning(f"最大回撤触发: {drawdown:.2%} >= {threshold:.2%}")

        return exceeded, drawdown

    # ==================== 第五层：合规风控 ====================

    def audit_param_change(self, param_name, old_value, new_value, operator="system"):
        """记录参数变更审计"""
        entry = {
            "timestamp": datetime.now().strftime("%Y%m%d %H:%M:%S"),
            "param": param_name,
            "old": str(old_value),
            "new": str(new_value),
            "operator": operator,
        }
        self._audit_log.append(entry)
        logger.info(f"参数变更: {param_name} {old_value} -> {new_value}")

    def get_audit_log(self, n=20):
        """获取审计日志"""
        return self._audit_log[-n:]

    # ==================== 综合风控 ====================

    def should_reduce_position(self, portfolio):
        """是否需要降仓

        Returns:
            (should_reduce, reason)
        """
        exceeded, loss_pct = self.check_daily_loss(portfolio)
        if exceeded:
            return True, f"单日亏损 {loss_pct:.2%} 超过阈值 {DAILY_LOSS_THRESHOLD:.2%}，需降仓"

        exceeded, drawdown = self.check_max_drawdown(portfolio)
        if exceeded:
            return True, f"回撤 {drawdown:.2%} 超过阈值 {MAX_DRAWDOWN_THRESHOLD:.2%}，需降仓"

        return False, ""

    def should_pause_strategy(self, strategy_name, recent_returns):
        """是否暂停某策略

        Args:
            strategy_name: 策略名称
            recent_returns: 近期收益率列表

        Returns:
            (should_pause, reason)
        """
        if not recent_returns:
            return False, ""

        # 连续 5 次亏损
        if len(recent_returns) >= 5:
            recent_5 = recent_returns[-5:]
            if all(r < 0 for r in recent_5):
                return True, f"策略 {strategy_name} 连续 5 次亏损，暂停"

        # 近 10 次胜率低于 20%
        if len(recent_returns) >= 10:
            recent_10 = recent_returns[-10:]
            win_rate = sum(1 for r in recent_10 if r > 0) / len(recent_10)
            if win_rate < 0.2:
                return True, f"策略 {strategy_name} 近 10 次胜率 {win_rate:.0%}，暂停"

        return False, ""

    def set_daily_start(self, portfolio):
        """记录当日开盘时的市值"""
        self._daily_start_value = portfolio.get_total_value()

    def filter_orders(self, orders, portfolio, market_data=None):
        """批量过滤订单

        Args:
            orders: list of order dict
            portfolio: PositionManager
            market_data: 市场数据

        Returns:
            (approved_orders, rejected_orders)
        """
        # 先检查是否需要降仓
        should_reduce, reduce_reason = self.should_reduce_position(portfolio)

        approved_orders = []
        rejected_orders = []

        for order in orders:
            # 如果需要降仓，只允许卖出
            if should_reduce and order["action"] == "buy":
                rejected_orders.append({
                    "order": order,
                    "reason": reduce_reason,
                })
                continue

            # 检查是否需要暂停（回撤过大，不开新仓）
            exceeded, drawdown = self.check_max_drawdown(portfolio)
            if exceeded and order["action"] == "buy":
                rejected_orders.append({
                    "order": order,
                    "reason": f"回撤过大 {drawdown:.2%}，暂停开仓",
                })
                continue

            # 交易前风控
            ok, reason = self.pre_trade_check(order, portfolio, market_data)
            if ok:
                approved_orders.append(order)
            else:
                rejected_orders.append({
                    "order": order,
                    "reason": reason,
                })

        return approved_orders, rejected_orders
