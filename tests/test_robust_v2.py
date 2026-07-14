"""robust_v2 策略、证券制度和目标分配测试。"""

from __future__ import annotations

import pandas as pd
import pytest

from rules.engine import TradingRules
from strategies.robust_v2 import (
    RobustV2Config,
    RobustV2Strategy,
    build_market_data_hash,
    validate_realtime_alignment,
)
from trading.allocator import PortfolioAllocator
from trading.models import MarketSnapshot, TargetPortfolio, TargetPosition


def _history(
    code: str,
    *,
    slope: float = 0.02,
    pb: float = 1.2,
    market_cap: float = 10_000_000_000.0,
) -> pd.DataFrame:
    """生成包含 280 个交易日的确定性测试行情。"""
    dates = pd.bdate_range("2025-06-02", periods=280)
    closes = [10 + slope * index for index in range(len(dates))]
    return pd.DataFrame(
        {
            "date": dates.strftime("%Y%m%d"),
            "close": closes,
            "open": closes,
            "high": [value * 1.01 for value in closes],
            "low": [value * 0.99 for value in closes],
            "volume": [10_000_000] * len(dates),
            "amount": [150_000_000] * len(dates),
            "pb": [pb] * len(dates),
            "mktcap": [market_cap] * len(dates),
            "peTTM": [12.0] * len(dates),
            "roe": [12.0] * len(dates),
            "tradestatus": [1] * len(dates),
            "is_st": [False] * len(dates),
            "name": [code] * len(dates),
        }
    )


def _snapshot(frame: pd.DataFrame) -> MarketSnapshot:
    """生成与测试行情对齐的市场快照。"""
    return MarketSnapshot(
        trade_date=str(frame["date"].iloc[-1]),
        previous_trade_date=str(frame["date"].iloc[-2]),
        source="test",
        adjustment="qfq",
        data_hash="test-hash",
        freshness_seconds=0,
    )


def test_instrument_rules_apply_etf_specific_limits_and_fees() -> None:
    """科创/创业相关 ETF 为 20%，ETF 卖出不收股票印花税和过户费。"""
    rules = TradingRules()
    assert rules.get_price_limit_pct("510300") == 0.10
    assert rules.get_price_limit_pct("159915") == 0.20
    assert rules.get_price_limit_pct("588000") == 0.20

    etf_cost = rules.calc_total_cost(10_000, "sell", code="510300")
    stock_cost = rules.calc_total_cost(10_000, "sell", code="sh600000")
    assert etf_cost["stamp_tax"] == 0
    assert etf_cost["transfer_fee"] == 0
    assert stock_cost["stamp_tax"] > 0
    assert stock_cost["transfer_fee"] > 0


def test_market_data_hash_changes_when_factor_input_changes() -> None:
    """PB 等因子字段变化必须改变审计哈希，不能只记录最新收盘价。"""
    original = _history("600000", pb=1.2)
    changed = original.copy()
    changed.loc[changed.index[-1], "pb"] = 2.4
    trade_date = str(original["date"].iloc[-1])

    assert build_market_data_hash({"600000": original}, trade_date) != (
        build_market_data_hash({"600000": changed}, trade_date)
    )


def test_realtime_alignment_matches_prefixed_history_and_raw_quote_code() -> None:
    """实时前收盘对齐应按六位代码匹配，不能被市场前缀差异误拒绝。"""
    history = _history("600000")
    snapshot = _snapshot(history)
    latest_close = float(history["close"].iloc[-1])

    result = validate_realtime_alignment(
        snapshot,
        {"sh600000": history},
        {"600000": {"prev_close": latest_close}},
    )

    assert result.valid_codes == frozenset({"600000"})
    assert result.rejected == {}


def test_strategy_builds_two_etfs_one_mainboard_and_keeps_cash() -> None:
    """目标组合应遵守 60/20/20、数量和主板边界。"""
    frame = _history("base")
    snapshot = _snapshot(frame)
    strategy = RobustV2Strategy(
        RobustV2Config(
            etf_min_avg_amount=0,
            stock_min_avg_amount=0,
        )
    )
    target = strategy.generate_target(
        snapshot,
        etf_history={
            "510300": _history("510300", slope=0.03),
            "510500": _history("510500", slope=0.025),
            "159915": _history("159915", slope=-0.01),
        },
        stock_history={
            "sh600001": _history("sh600001", pb=0.9, market_cap=8_000_000_000),
            "sz300001": _history("sz300001", pb=0.7, market_cap=7_000_000_000),
            "sh688001": _history("sh688001", pb=0.6, market_cap=6_000_000_000),
        },
        account_value=50_000,
    )

    assert (
        len([position for position in target.positions if position.asset_type == "etf"])
        == 2
    )
    stocks = [
        position for position in target.positions if position.asset_type == "stock"
    ]
    assert [position.code for position in stocks] == ["sh600001"]
    assert target.exposure == pytest.approx(0.8)
    assert target.cash_weight == pytest.approx(0.2)


def test_strategy_ignores_rows_after_signal_date() -> None:
    """未来日期的极端价格不能影响 T 日目标。"""
    base = _history("510300", slope=0.03)
    snapshot = _snapshot(base)
    future = pd.concat(
        [
            base,
            pd.DataFrame(
                {
                    **{column: [base[column].iloc[-1]] for column in base.columns},
                    "date": ["20300101"],
                    "close": [0.01],
                }
            ),
        ],
        ignore_index=True,
    )
    strategy = RobustV2Strategy(
        RobustV2Config(etf_min_avg_amount=0, enable_stock_enhancement=False)
    )

    original = strategy.score_etfs({"510300": base}, snapshot)
    with_future = strategy.score_etfs({"510300": future}, snapshot)

    assert original[0]["score"] == pytest.approx(with_future[0]["score"])
    assert original[0]["price"] == with_future[0]["price"]


def test_stock_scan_reports_first_rejection_reason_for_every_input() -> None:
    """扫描结果应同时给出候选排序和可核对的首个淘汰原因。"""
    frame = _history("base")
    snapshot = _snapshot(frame)
    short_history = _history("short").tail(80).reset_index(drop=True)
    strategy = RobustV2Strategy(RobustV2Config(stock_min_avg_amount=0))

    result = strategy.scan_stocks(
        {
            "sh600001": _history("sh600001", pb=0.9),
            "sh600002": _history("sh600002", pb=12.0),
            "sz300001": _history("sz300001", pb=0.8),
            "sh600003": short_history,
        },
        snapshot,
        account_value=50_000,
    )

    assert [candidate["code"] for candidate in result.candidates] == ["sh600001"]
    assert result.filter_counts["pb_out_of_range"] == 1
    assert result.filter_counts["non_mainboard"] == 1
    assert result.filter_counts["insufficient_history"] == 1
    assert (
        result.eligible_count + sum(result.filter_counts.values()) == result.input_count
    )


def test_allocator_requires_next_day_and_respects_t1_sellable_quantity() -> None:
    """分配器不得在信号日成交，也不能卖出当日锁定批次。"""
    target = TargetPortfolio(
        account_id="paper_v2",
        strategy_version="robust_v2",
        signal_date="20260709",
        positions=(
            TargetPosition(
                code="510300",
                name="沪深300ETF",
                target_weight=0.3,
                reason="ETF_RISK_ADJUSTED",
                asset_type="etf",
            ),
        ),
        source_snapshot_hash="hash",
        cash_weight=0.7,
    )
    allocator = PortfolioAllocator(rebalance_band=0, min_etf_order=0)

    with pytest.raises(ValueError, match="下一交易日"):
        allocator.allocate(target, 50_000, {}, {"510300": 4.0}, "20260709")

    result = allocator.allocate(
        target,
        cash=30_000,
        positions={
            "sh600000": {
                "name": "浦发银行",
                "shares": 1000,
                "sellable_qty": 0,
                "avg_cost": 10.0,
                "current_price": 10.0,
            },
        },
        prices={"sh600000": 10.0, "510300": 4.0},
        execution_date="20260710",
    )

    assert all(order.action != "sell" for order in result.orders)
    assert "T+1" in result.skipped["sh600000"]
    assert result.projected_cash >= result.account_value * target.cash_weight


def test_catastrophic_stop_matches_prefixed_position_and_raw_quote_code() -> None:
    """灾难止损不能因腾讯行情省略市场前缀而漏掉持仓。"""
    strategy = RobustV2Strategy(RobustV2Config(stock_stop_pct=0.07))

    orders = strategy.catastrophic_stop_orders(
        {
            "sh600000": {
                "name": "浦发银行",
                "shares": 100,
                "sellable_qty": 100,
                "avg_cost": 10.0,
            }
        },
        {"600000": 9.2},
        "20260710",
    )

    assert len(orders) == 1
    assert orders[0].code == "sh600000"
    assert orders[0].reason == "CATASTROPHIC_STOP_LOSS"
