"""
A 股交易规则引擎
处理涨跌停、佣金、印花税、过户费、T+1、整手等规则
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from config.settings import (
    COMMISSION_RATE,
    COMMISSION_MIN,
    STAMP_TAX_RATE,
    TRANSFER_FEE_RATE,
    SLIPPAGE_STOCK,
    SLIPPAGE_ETF,
    LOT_SIZE,
    LIMIT_MAINBOARD,
    LIMIT_ST,
    LIMIT_CHINEXT,
    is_etf,
    is_chinext,
    is_star_market,
    CASH_BUFFER,
)
from trading.instruments import get_instrument_profile


class TradingRules:
    """A 股交易规则引擎"""

    @staticmethod
    def get_price_limit_pct(code, name="", price_limit_override=None):
        """获取涨跌停幅度

        Args:
            code: 标的代码，如 sh601988, sz300750

        Returns:
            涨跌停比例，如 0.10 表示 10%
        """
        return get_instrument_profile(
            code,
            name=name,
            price_limit_override=price_limit_override,
        ).price_limit_pct

    @staticmethod
    def is_st(code, name=""):
        """判断是否为 ST 股票"""
        return "ST" in name.upper() or "*ST" in name.upper()

    def check_price_limit(self, code, current_price, prev_close, name=""):
        """检查涨跌停状态

        Args:
            code: 标的代码
            current_price: 当前价格
            prev_close: 昨日收盘价
            name: 股票名称（用于判断 ST）

        Returns:
            (can_buy, can_sell, limit_type) 元组
            can_buy: 是否可以买入（未涨停）
            can_sell: 是否可以卖出（未跌停）
            limit_type: "涨停" / "跌停" / "正常"
        """
        if prev_close <= 0:
            return True, True, "正常"

        # 获取涨跌停幅度
        if self.is_st(code, name):
            limit_pct = LIMIT_ST
        else:
            limit_pct = self.get_price_limit_pct(code, name=name)

        upper_limit = round(prev_close * (1 + limit_pct), 2)
        lower_limit = round(prev_close * (1 - limit_pct), 2)

        if current_price >= upper_limit:
            return False, True, "涨停"
        elif current_price <= lower_limit:
            return True, False, "跌停"
        return True, True, "正常"

    @staticmethod
    def calc_commission(amount, direction="buy"):
        """计算佣金：万三，最低 5 元

        Args:
            amount: 交易金额
            direction: buy/sell（佣金双向收取，direction 此处不影响金额）

        Returns:
            佣金金额
        """
        commission = amount * COMMISSION_RATE
        return max(commission, COMMISSION_MIN)

    @staticmethod
    def calc_stamp_tax(amount):
        """计算印花税：仅卖出收取，千分之五

        Args:
            amount: 卖出金额

        Returns:
            印花税金额
        """
        return amount * STAMP_TAX_RATE

    @staticmethod
    def calc_transfer_fee(amount):
        """计算过户费：双向收取，万分之0.1

        Args:
            amount: 交易金额

        Returns:
            过户费金额
        """
        return amount * TRANSFER_FEE_RATE

    def calc_total_cost(self, amount, direction="buy", is_etf_flag=False, code=None):
        """计算总交易成本（含滑点）

        Args:
            amount: 名义交易金额
            direction: buy/sell
            is_etf_flag: 是否为 ETF

        Returns:
            dict: {
                "slippage": 滑点成本,
                "commission": 佣金,
                "stamp_tax": 印花税（仅卖出）,
                "transfer_fee": 过户费,
                "total": 总成本,
                "actual_amount": 实际交易金额（含滑点）
            }
        """
        if amount < 0:
            raise ValueError(f"交易金额不能为负数: {amount}")
        if direction not in {"buy", "sell"}:
            raise ValueError(f"不支持的交易方向: {direction}")

        profile = get_instrument_profile(code) if code else None
        effective_is_etf = profile.is_etf if profile is not None else is_etf_flag

        # 滑点
        slippage_rate = SLIPPAGE_ETF if effective_is_etf else SLIPPAGE_STOCK
        if direction == "buy":
            slippage = amount * slippage_rate
            actual_amount = amount + slippage
        else:
            slippage = amount * slippage_rate
            actual_amount = amount - slippage

        # 佣金
        commission = self.calc_commission(actual_amount, direction)

        # 印花税（仅卖出）
        stamp_tax_rate = (
            profile.stamp_tax_rate
            if profile is not None
            else (0.0 if effective_is_etf else STAMP_TAX_RATE)
        )
        stamp_tax = actual_amount * stamp_tax_rate if direction == "sell" else 0.0

        # 过户费
        transfer_fee_rate = (
            profile.transfer_fee_rate
            if profile is not None
            else (0.0 if effective_is_etf else TRANSFER_FEE_RATE)
        )
        transfer_fee = actual_amount * transfer_fee_rate

        total = slippage + commission + stamp_tax + transfer_fee

        return {
            "slippage": round(slippage, 2),
            "commission": round(commission, 2),
            "stamp_tax": round(stamp_tax, 2),
            "transfer_fee": round(transfer_fee, 2),
            "total": round(total, 2),
            "actual_amount": round(actual_amount, 2),
        }

    @staticmethod
    def calc_lot_size(
        price: float,
        cash: float,
        max_ratio: float = 0.25,
        total_value: float | None = None,
        min_amount: float = 0.0,
    ) -> int:
        """计算可买股数（100 股整数倍）

        Args:
            price: 当前价格
            cash: 可用现金
            max_ratio: 单票最大仓位比例
            total_value: 组合总市值（用于计算仓位限制）
            min_amount: 最低建议成交额，低于该金额则不生成买入单

        Returns:
            可买股数（整手）
        """
        if price <= 0 or cash <= 0:
            return 0

        # 考虑现金缓冲
        usable_cash = cash * (1 - CASH_BUFFER)

        # 考虑单票上限（如果 total_value 提供）
        if total_value and total_value > 0:
            max_amount = total_value * max_ratio
            one_lot_cost = price * LOT_SIZE
            if one_lot_cost > max_amount:
                return 0
            usable_cash = min(usable_cash, max_amount)

        # 扣除预估交易成本（约千分之五用于佣金+过户费等）
        net_cash = usable_cash / 1.005

        # 计算可买股数，向下取整到 100 的整数倍
        max_shares = int(net_cash / price)
        lot_shares = (max_shares // LOT_SIZE) * LOT_SIZE
        if min_amount > 0 and lot_shares * price < min_amount:
            return 0

        return lot_shares

    @staticmethod
    def check_t1(buy_date_str, sell_date_str):
        """检查 T+1 限制

        Args:
            buy_date_str: 买入日期，格式 YYYYMMDD
            sell_date_str: 卖出日期，格式 YYYYMMDD

        Returns:
            True 表示可以卖出（T+1 已过），False 表示不可卖出
        """
        buy_dt = datetime.strptime(buy_date_str, "%Y%m%d")
        sell_dt = datetime.strptime(sell_date_str, "%Y%m%d")
        return sell_dt > buy_dt

    @staticmethod
    def get_slippage_price(price, direction, is_etf_flag=False):
        """计算含滑点的价格

        Args:
            price: 原始价格
            direction: buy/sell
            is_etf_flag: 是否为 ETF

        Returns:
            含滑点的价格
        """
        slippage_rate = SLIPPAGE_ETF if is_etf_flag else SLIPPAGE_STOCK
        if direction == "buy":
            return round(price * (1 + slippage_rate), 3)
        else:
            return round(price * (1 - slippage_rate), 3)
