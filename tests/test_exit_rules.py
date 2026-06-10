"""分层退出规则测试。"""

import strategies.exit_rules as er


def test_t1_locked_records_hold() -> None:
    """T+1 不可卖时必须返回锁定状态。"""
    out = er.evaluate_position_exit(avg_cost=10, price=9, sellable_qty=0)
    assert out.should_sell is False
    assert out.sell_reason == "T1_LOCKED"


def test_catastrophic_and_atr_stop_priority(monkeypatch) -> None:
    """灾难止损优先于 ATR，ATR 优先于弱势信号。"""
    disaster = er.evaluate_position_exit(avg_cost=10, price=9, atr=0.2, combo_sell=True)
    assert disaster.sell_reason == "CATASTROPHIC_STOP_LOSS"

    monkeypatch.setattr(er, "ENABLE_ATR_STOP", True)
    atr_stop = er.evaluate_position_exit(avg_cost=10, price=9.7, atr=0.2)
    assert atr_stop.sell_reason == "ATR_STOP_LOSS"


def test_combo_only_defends_loss_or_tiny_profit() -> None:
    """Combo 只防守浮亏或微利，盈利趋势票不能被直接卖出。"""
    weak = er.evaluate_position_exit(
        avg_cost=10,
        price=10.05,
        combo_sell=True,
        combo_reason="MA5/20死叉",
    )
    assert weak.sell_reason == "COMBO_DEFENSIVE_EXIT"

    profitable = er.evaluate_position_exit(
        avg_cost=10,
        price=10.2,
        ma20=10.1,
        combo_sell=True,
        combo_reason="MA5/20死叉 + RSI超买78",
    )
    assert profitable.should_sell is False


def test_rsi_overbought_never_directly_sells() -> None:
    """仅 RSI 超买不能触发 Combo 防守卖出。"""
    out = er.evaluate_position_exit(
        avg_cost=10,
        price=9.98,
        combo_sell=True,
        combo_reason="RSI超买82",
        rsi=82,
    )
    assert out.should_sell is False


def test_rsi_tightens_trailing_take_profit() -> None:
    """RSI 越高，允许回吐比例越低。"""
    normal = er.evaluate_position_exit(avg_cost=10, price=11.3, highest_price=12, rsi=70)
    tightened = er.evaluate_position_exit(avg_cost=10, price=11.3, highest_price=12, rsi=81)
    assert normal.should_sell is False
    assert tightened.sell_reason == "TRAILING_TAKE_PROFIT"
    assert tightened.indicators["trailing_giveback"] == 0.25


def test_limitup_follow_skips_fixed_profit_and_uses_intraday_drawdown() -> None:
    """涨停延续票只在日内高点明显回撤后移动止盈。"""
    hold = er.evaluate_position_exit(
        avg_cost=10,
        price=10.8,
        strategy_tag="limitup_follow",
        intraday_high_price=10.9,
        previous_close=10,
    )
    assert hold.should_sell is False

    sell = er.evaluate_position_exit(
        avg_cost=10,
        price=10.45,
        strategy_tag="limitup_follow",
        intraday_high_price=10.9,
        previous_close=10,
    )
    assert sell.sell_reason == "LIMITUP_FOLLOW_TRAILING_STOP"


def test_limitup_follow_vwap_break() -> None:
    """涨停延续票持续跌破 VWAP 时退出。"""
    out = er.evaluate_position_exit(
        avg_cost=10,
        price=10.2,
        strategy_tag="limitup_follow",
        vwap=10.3,
        below_vwap_minutes=3,
    )
    assert out.sell_reason == "VWAP_BREAK"


def test_etf_ignores_stock_atr_combo_and_trailing() -> None:
    """ETF 不使用股票 ATR、Combo、RSI 和移动止盈。"""
    hold = er.evaluate_position_exit(
        avg_cost=10,
        price=10.5,
        highest_price=12,
        strategy_tag="etf_rotation",
        atr=2,
        combo_sell=True,
        combo_reason="MA5/20死叉",
        rsi=90,
        ma20=10.4,
        ma60=10.0,
    )
    assert hold.should_sell is False

    trend_exit = er.evaluate_position_exit(
        avg_cost=10,
        price=10.2,
        strategy_tag="etf_rotation",
        ma20=10.3,
        ma60=10.1,
    )
    assert trend_exit.sell_reason == "ETF_ROTATION_EXIT"


def test_smallcap_value_ignores_short_term_combo_and_trailing() -> None:
    """价值持仓不被短线 Combo 和移动止盈洗掉。"""
    out = er.evaluate_position_exit(
        avg_cost=10,
        price=10.5,
        highest_price=12,
        strategy_tag="smallcap_value",
        combo_sell=True,
        combo_reason="MA5/20死叉",
        ma20=11,
    )
    assert out.should_sell is False


def test_time_stop_when_enabled(monkeypatch) -> None:
    """时间止损在配置启用后生效。"""
    monkeypatch.setattr(er, "TIME_STOP_DAYS", 10)
    monkeypatch.setattr(er, "TIME_STOP_MIN_PROFIT", 0.0)
    out = er.evaluate_position_exit(avg_cost=10, price=9.8, holding_days=15)
    assert out.sell_reason == "TIME_STOP"


def test_invalid_inputs_hold() -> None:
    """非法价格不生成卖单。"""
    assert er.evaluate_position_exit(avg_cost=0, price=10).should_sell is False
    assert er.evaluate_position_exit(avg_cost=10, price=0).should_sell is False
