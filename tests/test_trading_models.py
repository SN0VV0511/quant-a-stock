"""交易模型测试。"""

import pytest

from trading.models import OrderIntent


def test_order_intent_to_order_dict() -> None:
    """订单意图应能转换为旧版风控可读字典。"""
    intent = OrderIntent(
        code="sh601988",
        action="buy",
        price=5.0,
        shares=100,
        name="中国银行",
        strategy="测试策略",
        strategy_tag="momentum_breakout",
        reason="固定样本信号",
        date="20260528",
    )

    order = intent.to_order_dict()

    assert order["code"] == "sh601988"
    assert order["action"] == "buy"
    assert order["shares"] == 100
    assert order["strategy"] == "测试策略"
    assert order["strategy_tag"] == "momentum_breakout"


def test_order_intent_rejects_invalid_values() -> None:
    """订单意图应拒绝非法方向、负价格和负股数。"""
    with pytest.raises(ValueError):
        OrderIntent(code="sh601988", action="hold", price=5.0, shares=100)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        OrderIntent(code="sh601988", action="buy", price=-1.0, shares=100)

    with pytest.raises(ValueError):
        OrderIntent(code="sh601988", action="buy", price=5.0, shares=-100)
