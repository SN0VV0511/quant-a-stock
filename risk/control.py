"""
风控模块
五层风控：资金管理、交易前、交易中、交易后、合规
"""

import logging
from datetime import datetime

from config.settings import (
    INITIAL_CAPITAL, MAX_TOTAL_POSITION, MAX_SINGLE_ETF, MAX_SINGLE_STOCK,
    CASH_BUFFER, DAILY_LOSS_THRESHOLD, MAX_DRAWDOWN_THRESHOLD, LOT_SIZE,
    DRAWDOWN_RECOVERY_DAYS, DRAWDOWN_REDUCED_POSITION_LIMIT,
    is_etf, is_supported_trading_target, DEFAULT_UNIVERSE,
)
from trading.models import OrderIntent, RiskDecision

logger = logging.getLogger(__name__)


class RiskController:
    """五层风控"""

    def __init__(self):
        self.name = "风控引擎"
        self._daily_start_value = None
        self._strategy_returns = {}  # strategy -> [daily_returns]
        self._audit_log = []        # 参数变更审计
        self._drawdown_latched = False
        self._drawdown_recovery_days = 0
        self._last_drawdown_review_date = None
        # 触发熔断时的总市值快照。恢复判定改为"从熔断点起连续 N 个交易日
        # 市值不再跌破该快照",而非"回撤回落到阈值下方"——否则空仓后峰值
        # 只增不减,回撤永远 >= 阈值,熔断永远无法解除(策略永久空仓死锁)。
        self._drawdown_latch_value = None

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
            # 单票上限只按标的属性判定,不参考 strategy_tag:旧实现曾把
            # rps_rotation/etf_rotation 标签的股票也按 ETF 30% 放行,与
            # PositionManager.check_position_limit 的现价口径不一致,存在
            # 单票集中度失控风险。
            is_etf_flag = is_etf(order["code"])

            # 检查现金
            if order_amount > cash * (1 - CASH_BUFFER):
                return False, f"现金不足（需要 {order_amount:.0f}，可用 {cash * (1 - CASH_BUFFER):.0f}）"

            # 检查单票上限
            max_ratio = MAX_SINGLE_ETF if is_etf_flag else MAX_SINGLE_STOCK
            min_lot_value = order["price"] * LOT_SIZE
            max_position_value = total_value * max_ratio
            if min_lot_value > max_position_value:
                logger.warning(
                    "最小一手检查拒绝 %s: price=%.3f lot_value=%.2f total_asset=%.2f limit=%.2f",
                    order["code"],
                    order["price"],
                    min_lot_value,
                    total_value,
                    max_position_value,
                )
                return False, (
                    "MIN_LOT_EXCEEDS_POSITION_LIMIT: "
                    f"价格{order['price']:.3f},一手{min_lot_value:.2f},"
                    f"总资产{total_value:.2f},单票上限{max_position_value:.2f}"
                )
            within_limit, reason = portfolio.check_position_limit(
                order["code"], order_amount, total_value, max_ratio_override=max_ratio
            )
            if not within_limit:
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

        # 1. 标的范围检查：当前交易通道允许沪深 A 股股票和 ETF。
        if not is_supported_trading_target(code):
            return False, f"标的 {code} 不是支持的沪深 A 股股票或 ETF"

        # 2. 标的白名单检查（扫描策略跳过白名单限制；卖出不限制）
        strategy_name = order.get("strategy", "")
        if (
            action == "buy"
            and not is_etf(code)
            and "全市场扫描" not in strategy_name
            and code not in DEFAULT_UNIVERSE
        ):
            return False, f"标的 {code} 不在白名单中"

        # 3. 市场数据检查
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

        # 4. T+1 检查（卖出时）
        if action == "sell":
            can_sell, reason = portfolio.can_sell(code, order.get("date", datetime.now().strftime("%Y%m%d")))
            if not can_sell:
                return False, reason

        # 5. 持仓检查（卖出时）
        if action == "sell":
            pos = portfolio.get_position(code)
            if not pos:
                return False, f"标的 {code} 无持仓"
            sellable_qty = portfolio.get_sellable_qty(
                code, order.get("date", datetime.now().strftime("%Y%m%d"))
            )
            if order.get("shares", 0) > sellable_qty:
                order["shares"] = sellable_qty

        # 6. 价格合理性
        if order.get("price", 0) <= 0:
            return False, f"价格异常: {order.get('price')}"

        # 7. 资金检查
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
        # 仅在未锁存且回撤触及阈值时触发熔断,并记录触发点市值;已锁存时
        # 不在此处清零恢复计数(恢复推进统一由 set_daily_start 负责)。
        if not self._drawdown_latched and drawdown >= threshold:
            self._drawdown_latched = True
            self._drawdown_recovery_days = 0
            self._drawdown_latch_value = portfolio.get_total_value()
        exceeded = drawdown >= threshold or self._drawdown_latched

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
            return True, f"单日亏损 {loss_pct:.2%} 超过阈值 {DAILY_LOSS_THRESHOLD:.2%}，停止新买入"

        exceeded, drawdown = self.check_max_drawdown(portfolio)
        if exceeded:
            return True, (
                f"回撤 {drawdown:.2%} 超过阈值 {MAX_DRAWDOWN_THRESHOLD:.2%}，"
                "停止新买入且总仓位上限降至30%"
            )

        return False, ""

    def drawdown_deleverage_target_ratio(self, portfolio, current_prices=None):
        """回撤熔断激活且持仓超目标时,返回应削减的持仓比例(0~1);否则 0。

        与 should_reduce_position(仅拦截新买入)互补:本方法用于主动对**存量**
        减仓止血——这是 A4 修复点。例:当前仓位 60%、目标上限
        DRAWDOWN_REDUCED_POSITION_LIMIT=30%,返回 (0.60-0.30)/0.60 = 0.5,
        即每个持仓按 50% 同比例削减(同比例减仓中性,不引入选股偏好)。

        Args:
            portfolio: 持仓管理器。
            current_prices: 现价字典,按市值口径计算当前仓位。

        Returns:
            float: 需削减的持仓比例,0 表示无需减仓。
        """
        exceeded, _ = self.check_max_drawdown(portfolio)
        if not exceeded:
            return 0.0
        total_value = portfolio.get_total_value(current_prices)
        if total_value <= 0:
            return 0.0
        position_ratio = (total_value - portfolio.get_cash()) / total_value
        if position_ratio <= DRAWDOWN_REDUCED_POSITION_LIMIT:
            return 0.0
        return round((position_ratio - DRAWDOWN_REDUCED_POSITION_LIMIT) / position_ratio, 4)

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

    def set_daily_start(self, portfolio, date=None):
        """记录日初市值，并推进最大回撤熔断的恢复交易日计数。

        恢复判据:自触发熔断起,连续 ``DRAWDOWN_RECOVERY_DAYS`` 个交易日总市值
        **不再创新低**(即下跌已止住、企稳),即解除熔断。

        设计要点:
        - 回撤口径用历史峰值(只增不减),空仓后净值走平、峰值不降,回撤长期
          >= 阈值。旧实现用"回撤回落到阈值下方"作为恢复条件,导致熔断永远
          无法解除 → 策略永久空仓死锁。
        - 改用"不再创新低"而非"市值回到熔断点之上":熔断期间禁止开仓 → 空仓
          → 净值走平在低位,若要求回到熔断点之上才能恢复,会再次形成"要恢复
          得先开仓、要开仓得先恢复"的自我参照死锁。"不再创新低"只要求下跌
          停止,空仓走平即满足,可在 N 日后恢复开仓能力。
        - 恢复时把 ``peak_value`` 重置为当前总市值,使回撤归零、避免
          ``check_max_drawdown`` 因 drawdown 仍 >= 阈值而立即重新触发 latch。
        """
        self._daily_start_value = portfolio.get_total_value()
        review_date = str(date or datetime.now().strftime("%Y%m%d")).replace("-", "")[:8]
        if not self._drawdown_latched:
            # 未锁存:回撤触及阈值才触发熔断(实际 latch/记录在 check_max_drawdown 完成)。
            return
        # 已锁存:用 _drawdown_latch_value 跟踪熔断期间的"最近最低净值"。
        # 当日再创新低 → 下跌未止住,重置计数并更新基准;否则计为"企稳一日"。
        latch_low = self._drawdown_latch_value
        if latch_low is None or self._daily_start_value < latch_low:
            self._drawdown_latch_value = self._daily_start_value
            self._drawdown_recovery_days = 0
            return
        if review_date != self._last_drawdown_review_date:
            self._drawdown_recovery_days += 1
            self._last_drawdown_review_date = review_date
            if self._drawdown_recovery_days >= DRAWDOWN_RECOVERY_DAYS:
                self._drawdown_latched = False
                self._drawdown_recovery_days = 0
                self._drawdown_latch_value = None
                # 恢复时把回撤峰值重置为当前总市值,使回撤从当前水位重新计算。
                # 否则 drawdown 仍 >= 阈值,check_max_drawdown 会立即重新触发 latch。
                if self._daily_start_value > 0 and hasattr(portfolio, "state"):
                    portfolio.state["peak_value"] = self._daily_start_value
                logger.info(
                    "最大回撤熔断已连续 %d 个交易日企稳(不再创新低)，恢复正常买入",
                    DRAWDOWN_RECOVERY_DAYS,
                )

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

    def check_order_intent(self, intent: OrderIntent, portfolio, market_data=None) -> RiskDecision:
        """审批标准订单意图。

        Args:
            intent: 标准订单意图。
            portfolio: 持仓管理器。
            market_data: 行情风控数据。

        Returns:
            RiskDecision: 风控审批结果。
        """
        order = intent.to_order_dict()
        ok, reason = self.pre_trade_check(order, portfolio, market_data)
        return RiskDecision(order=intent, approved=ok, reason=reason)

    def filter_order_intents(self, intents, portfolio, market_data=None):
        """批量审批标准订单意图。

        Returns:
            tuple: (approved_intents, rejected_decisions)
        """
        should_reduce, reduce_reason = self.should_reduce_position(portfolio)
        rejected = []
        approved = []

        for intent in intents:
            if should_reduce and intent.action == "buy":
                rejected.append(RiskDecision(order=intent, approved=False, reason=reduce_reason))
                continue

            exceeded, drawdown = self.check_max_drawdown(portfolio)
            if exceeded and intent.action == "buy":
                rejected.append(RiskDecision(
                    order=intent,
                    approved=False,
                    reason=f"回撤过大 {drawdown:.2%}，暂停开仓",
                ))
                continue

            order = intent.to_order_dict()
            ok, reason = self.pre_trade_check(order, portfolio, market_data)
            if ok:
                approved.append(OrderIntent.from_order_dict(order))
            else:
                rejected.append(RiskDecision(order=intent, approved=False, reason=reason))

        return approved, rejected
