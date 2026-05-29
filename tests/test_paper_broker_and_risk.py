"""虚拟 Broker 与风控集成测试。"""

import pytest

from live_runner import SharedState, _handle_position_exits
from risk.control import RiskController

from trading.brokers import PaperBrokerAdapter, QmtBrokerAdapter
from trading.models import OrderIntent
from trading.observability import EventRecorder


def _paper_broker(tmp_path) -> PaperBrokerAdapter:
    """创建使用临时文件的虚拟 Broker。"""
    broker = PaperBrokerAdapter(
        state_file=str(tmp_path / "state.json"),
        trade_log_file=str(tmp_path / "trade_log.json"),
        snapshot_log_file=str(tmp_path / "snapshots.jsonl"),
    )
    broker.connect()
    return broker


def test_risk_rejects_non_whitelist_order(tmp_path) -> None:
    """非白名单标的应被风控拒绝。"""
    broker = _paper_broker(tmp_path)
    risk = RiskController()
    order = OrderIntent(
        code="sh600000",
        action="buy",
        price=10.0,
        shares=100,
        strategy="普通策略",
        date="20260528",
    )

    approved, rejected = risk.filter_order_intents(
        [order],
        broker.portfolio,
        {"sh600000": {"current_price": 10.0, "prev_close": 9.9, "is_st": False, "is_suspended": False}},
    )

    assert approved == []
    assert len(rejected) == 1
    assert "不在白名单" in rejected[0].reason


def test_full_market_order_can_pass_risk_and_fill(tmp_path) -> None:
    """全市场扫描订单可绕过固定白名单并在虚拟盘成交。"""
    broker = _paper_broker(tmp_path)
    risk = RiskController()
    order = OrderIntent(
        code="600000",
        action="buy",
        price=10.0,
        shares=100,
        name="浦发银行",
        strategy="全市场扫描+组合策略",
        reason="固定样本信号",
        date="20260528",
    )
    market_data = {
        "600000": {"current_price": 10.0, "prev_close": 9.9, "is_st": False, "is_suspended": False}
    }

    approved, rejected = risk.filter_order_intents([order], broker.portfolio, market_data)
    report = broker.place_order(approved[0])

    assert rejected == []
    assert len(approved) == 1
    assert report.is_success is True
    assert broker.query_positions()["600000"]["shares"] == 100


def test_risk_allows_non_whitelist_position_sell(tmp_path) -> None:
    """非白名单持仓卖出不应被买入白名单拦截。"""
    broker = _paper_broker(tmp_path)
    risk = RiskController()
    buy_report = broker.place_order(
        OrderIntent(
            code="603773",
            action="buy",
            price=100.0,
            shares=100,
            name="沃格光电",
            strategy="全市场扫描+组合策略",
            date="20260528",
        )
    )
    sell_order = OrderIntent(
        code="603773",
        action="sell",
        price=101.0,
        shares=100,
        name="沃格光电",
        strategy="止损",
        date="20260529",
    )

    approved, rejected = risk.filter_order_intents(
        [sell_order],
        broker.portfolio,
        {
            "603773": {
                "current_price": 101.0,
                "prev_close": 100.0,
                "is_st": False,
                "is_suspended": False,
            }
        },
    )

    assert buy_report.is_success is True
    assert rejected == []
    assert len(approved) == 1
    assert approved[0].code == "603773"
    assert approved[0].action == "sell"


def test_same_day_exit_rejection_enters_cooldown(tmp_path) -> None:
    """同日卖出因 T+1 被拒后，不应在盯盘循环里重复刷风控日志。"""
    broker = _paper_broker(tmp_path)
    risk = RiskController()
    shared = SharedState()
    recorder = EventRecorder(path=str(tmp_path / "events.jsonl"))
    buy_report = broker.place_order(
        OrderIntent(
            code="603773",
            action="buy",
            price=100.0,
            shares=100,
            name="沃格光电",
            strategy="全市场扫描+组合策略",
            date="20260529",
        )
    )
    positions = broker.query_positions()
    prices = {"603773": 90.0}
    market_data = {
        "603773": {
            "current_price": 90.0,
            "prev_close": 100.0,
            "is_st": False,
            "is_suspended": False,
        }
    }

    _handle_position_exits(
        broker, positions, prices, object(), risk, market_data, recorder, shared=shared
    )
    first_event_count = len(
        (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    )
    _handle_position_exits(
        broker, positions, prices, object(), risk, market_data, recorder, shared=shared
    )
    second_event_count = len(
        (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    )

    assert buy_report.is_success is True
    assert shared.is_exit_cooling_down("603773") is True
    assert first_event_count == 2
    assert second_event_count == first_event_count


def test_paper_broker_buy_and_next_day_sell(tmp_path) -> None:
    """虚拟 Broker 应能完成买入和次日卖出，并保留订单回报。"""
    broker = _paper_broker(tmp_path)
    buy_order = OrderIntent(
        code="sh601988",
        action="buy",
        price=5.0,
        shares=100,
        name="中国银行",
        strategy="测试策略",
        date="20260527",
    )
    sell_order = OrderIntent(
        code="sh601988",
        action="sell",
        price=5.2,
        shares=100,
        name="中国银行",
        strategy="测试策略",
        date="20260528",
    )

    buy_report = broker.place_order(buy_order)
    sell_report = broker.place_order(sell_order)

    assert buy_report.status == "filled"
    assert sell_report.status == "filled"
    assert sell_report.profit is not None
    assert broker.query_positions() == {}
    assert len(broker.query_orders()) == 2


def test_risk_adjusts_sell_shares_to_position_size(tmp_path) -> None:
    """风控应把超出持仓的卖出数量下调到可卖数量。"""
    broker = _paper_broker(tmp_path)
    risk = RiskController()
    broker.place_order(OrderIntent(
        code="sh601988",
        action="buy",
        price=5.0,
        shares=100,
        name="中国银行",
        strategy="测试策略",
        date="20260527",
    ))
    sell_order = OrderIntent(
        code="sh601988",
        action="sell",
        price=5.2,
        shares=300,
        name="中国银行",
        strategy="测试策略",
        date="20260528",
    )

    approved, rejected = risk.filter_order_intents(
        [sell_order],
        broker.portfolio,
        {"sh601988": {"current_price": 5.2, "prev_close": 5.1, "is_st": False, "is_suspended": False}},
    )

    assert rejected == []
    assert len(approved) == 1
    assert approved[0].shares == 100


def test_qmt_adapter_defaults_to_dry_run_and_blocks_live() -> None:
    """QMT 适配器默认只做 dry-run，真实通道必须被显式阻断。"""
    dry_run = QmtBrokerAdapter(live_enabled=False)
    dry_run.connect()
    report = dry_run.place_order(OrderIntent(
        code="sh601988",
        action="buy",
        price=5.0,
        shares=100,
        strategy="QMT联调",
        date="20260528",
    ))

    assert report.status == "submitted"
    assert "dry-run" in report.message

    live = QmtBrokerAdapter(live_enabled=True)
    with pytest.raises(NotImplementedError):
        live.connect()
