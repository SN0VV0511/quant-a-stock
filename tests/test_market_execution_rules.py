"""虚拟盘与未来 QMT 共用的实时交易规则测试。"""

from __future__ import annotations

from datetime import datetime

import pytest

from trading.market import (
    classify_trading_session,
    is_strategy_execution_session,
    order_is_tradable,
    validate_execution_quote,
)


def _quote(**overrides: object) -> dict[str, object]:
    """构造一条有效腾讯执行行情。"""
    quote: dict[str, object] = {
        "name": "浦发银行",
        "price": 10.0,
        "prev_close": 9.9,
        "quote_time": "20260714093600",
        "captured_at": "2026-07-14 09:36:01",
        "source": "tencent",
        "is_suspended": False,
        "trade_status": 1,
    }
    quote.update(overrides)
    return quote


def test_strategy_execution_session_excludes_lunch_break() -> None:
    """午间休市不得继续执行目标或灾难止损。"""
    assert is_strategy_execution_session(datetime(2026, 7, 14, 9, 36)) is True
    assert classify_trading_session(datetime(2026, 7, 14, 11, 45)) == "lunch_break"
    assert is_strategy_execution_session(datetime(2026, 7, 14, 11, 45)) is False
    assert is_strategy_execution_session(datetime(2026, 7, 14, 13, 1)) is True


def test_execution_quote_rejects_stale_and_suspended_data() -> None:
    """陈旧行情或停牌状态只能等待重试，不能虚拟成交。"""
    stale = validate_execution_quote(
        "600000",
        _quote(),
        execution_date="20260714",
        now=datetime(2026, 7, 14, 9, 40),
        max_age_seconds=120,
    )
    suspended = validate_execution_quote(
        "600000",
        _quote(price=0.0, is_suspended=True, trade_status=0),
        execution_date="20260714",
        now=datetime(2026, 7, 14, 9, 36, 30),
        max_age_seconds=120,
    )

    assert stale.accepted is False
    assert "陈旧" in stale.reason
    assert suspended.accepted is False
    assert "停牌" in suspended.reason


def test_execution_quote_applies_limit_up_and_limit_down_by_direction() -> None:
    """涨停拒买、跌停拒卖，反方向减仓或买入仍可继续。"""
    limit_up = validate_execution_quote(
        "600000",
        _quote(price=11.0, prev_close=10.0),
        execution_date="20260714",
        now=datetime(2026, 7, 14, 9, 36, 30),
        max_age_seconds=120,
    )
    limit_down = validate_execution_quote(
        "600000",
        _quote(price=9.0, prev_close=10.0),
        execution_date="20260714",
        now=datetime(2026, 7, 14, 9, 36, 30),
        max_age_seconds=120,
    )

    assert limit_up.context is not None
    assert order_is_tradable("buy", limit_up.context)[0] is False
    assert order_is_tradable("sell", limit_up.context)[0] is True
    assert limit_down.context is not None
    assert order_is_tradable("sell", limit_down.context)[0] is False
    assert order_is_tradable("buy", limit_down.context)[0] is True


@pytest.mark.parametrize("field", ["price", "prev_close", "trade_status"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), "bad"])
def test_execution_quote_rejects_invalid_numeric_fields(
    field: str, value: object
) -> None:
    """单条脏行情应被拒绝，不能通过交易校验或中断整批风控。"""
    result = validate_execution_quote(
        "600000",
        _quote(**{field: value}),
        execution_date="20260714",
        now=datetime(2026, 7, 14, 9, 36, 30),
        max_age_seconds=120,
    )
    assert not result.accepted
    assert result.retryable
    assert "无效" in result.reason
