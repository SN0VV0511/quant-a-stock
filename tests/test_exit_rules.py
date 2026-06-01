"""持仓退出规则 evaluate_exit 测试:验证优先级与各类止损/止盈触发。

通过 monkeypatch 临时改写 exit_rules 模块内已绑定的阈值常量,验证各分支。
"""

import importlib

import strategies.exit_rules as er


def test_no_exit_when_flat():
    # 持平、无信号:不离场
    assert er.evaluate_exit(avg_cost=10, price=10.2, peak_price=10.2) is None


def test_hard_stop_loss():
    # 亏损超过 STOP_LOSS_PCT(默认 7%)
    out = er.evaluate_exit(avg_cost=10, price=9.0, peak_price=10)
    assert out is not None and out[0] == "止损"


def test_combo_sell_has_top_priority():
    # 即使盈利,Combo 卖出信号优先
    out = er.evaluate_exit(avg_cost=10, price=12, peak_price=12, combo_sell=True,
                           combo_reason="RSI超买")
    assert out[0] == "策略卖出"
    assert "RSI超买" in out[1]


def test_trailing_stop_triggers_after_activation():
    # 峰值 12(相对成本 +20% 已越过激活线 5%),当前 11 自峰值回撤 8.3% > 8%
    out = er.evaluate_exit(avg_cost=10, price=11.0, peak_price=12.0)
    assert out is not None and out[0] == "移动止损"


def test_trailing_stop_not_active_below_activation():
    # 峰值仅 10.2(相对成本 +2% < 激活线 5%),不启用移动止损;且未触发其他规则
    out = er.evaluate_exit(avg_cost=10, price=10.0, peak_price=10.2)
    assert out is None


def test_take_profit_requires_below_ma20():
    # 盈利 12% 达标但价格在 MA20 之上 → 不止盈
    assert er.evaluate_exit(avg_cost=10, price=11.2, peak_price=11.2, ma20=11.0) is None
    # 盈利达标且跌破 MA20 → 止盈(用未触发移动止损的峰值)
    out = er.evaluate_exit(avg_cost=10, price=11.2, peak_price=11.3, ma20=11.5)
    assert out is not None and out[0] == "止盈"


def test_time_stop_disabled_by_default():
    # 默认 TIME_STOP_DAYS=0,长期持有微亏也不触发时间止损
    out = er.evaluate_exit(avg_cost=10, price=9.8, peak_price=10.0, holding_days=999)
    assert out is None  # -2% 未达硬止损,时间止损禁用


def test_time_stop_when_enabled(monkeypatch):
    monkeypatch.setattr(er, "TIME_STOP_DAYS", 10)
    monkeypatch.setattr(er, "TIME_STOP_MIN_PROFIT", 0.0)
    out = er.evaluate_exit(avg_cost=10, price=9.8, peak_price=10.0, holding_days=15)
    assert out is not None and out[0] == "时间止损"


def test_invalid_inputs_return_none():
    assert er.evaluate_exit(avg_cost=0, price=10) is None
    assert er.evaluate_exit(avg_cost=10, price=0) is None
