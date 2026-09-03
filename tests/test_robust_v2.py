"""robust_v2 策略、证券制度和目标分配测试。"""

from __future__ import annotations

import pandas as pd
import pytest

from rules.engine import TradingRules
from strategies.robust_v2 import (
    RobustV2Config,
    RobustV2Strategy,
    StockScanResult,
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
    pe_ttm: float = 12.0,
    volatility: float = 0.0,
) -> pd.DataFrame:
    """生成包含 280 个交易日的确定性测试行情。"""
    dates = pd.bdate_range("2025-06-02", periods=280)
    closes = [
        (10 + slope * index) * (1 + volatility * (1 if index % 2 else -1))
        for index in range(len(dates))
    ]
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
            "peTTM": [pe_ttm] * len(dates),
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


def test_strategy_builds_two_etfs_two_mainboards_and_keeps_20pct_cash() -> None:
    """默认目标应为 2 只宽基 ETF、2 只主板股和 20% 现金。"""
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
            "sh600001": _history(
                "sh600001",
                pb=0.9,
                market_cap=8_000_000_000,
                pe_ttm=8.0,
            ),
            "sh600002": _history(
                "sh600002",
                pb=3.0,
                market_cap=12_000_000_000,
                pe_ttm=15.0,
            ),
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
    assert [position.code for position in stocks] == ["sh600001", "sh600002"]
    assert [position.target_weight for position in target.positions] == pytest.approx(
        [0.24, 0.24, 0.16, 0.16]
    )
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
            "sh600002": _history("sh600002", pb=12.0, pe_ttm=80.0),
            "sz300001": _history("sz300001", pb=0.8),
            "sh600003": short_history,
        },
        snapshot,
        account_value=50_000,
    )

    assert [candidate["code"] for candidate in result.candidates] == ["sh600001"]
    assert result.filter_counts["earnings_yield_too_low"] == 1
    assert result.filter_counts["non_mainboard"] == 1
    assert result.filter_counts["insufficient_history"] == 1
    assert (
        result.eligible_count + sum(result.filter_counts.values()) == result.input_count
    )


def test_stock_scan_rejects_47pct_crash() -> None:
    """近 20 日急跌 47% 的股票不得因短期反转预期进入候选。"""
    frame = _history("sh600001")
    crash_price = float(frame.loc[frame.index[-21], "close"]) * 0.53
    frame.loc[frame.index[-1], "close"] = crash_price
    strategy = RobustV2Strategy(RobustV2Config(stock_min_avg_amount=0))

    result = strategy.scan_stocks(
        {"sh600001": frame},
        _snapshot(frame),
        account_value=50_000,
    )

    assert result.candidates == ()
    assert result.filter_counts == {"excessive_20d_drop": 1}


def test_stock_scan_rejects_negative_and_low_earnings_yield() -> None:
    """负 PE 和低于 2% 的 EP 都不能通过盈利收益率门槛。"""
    frame = _history("base")
    strategy = RobustV2Strategy(RobustV2Config(stock_min_avg_amount=0))

    result = strategy.scan_stocks(
        {
            "sh600001": _history("sh600001", pe_ttm=-10.0),
            "sh600002": _history("sh600002", pe_ttm=80.0),
            "sh600003": _history("sh600003", pe_ttm=20.0),
        },
        _snapshot(frame),
        account_value=50_000,
    )

    assert [row["code"] for row in result.candidates] == ["sh600003"]
    assert result.filter_counts["earnings_yield_too_low"] == 2


def test_stock_scan_excludes_bottom_30pct_microcaps() -> None:
    """横截面市值最低 30% 应先剔除，再在剩余股票中偏向较小市值。"""
    frame = _history("base")
    histories = {
        f"sh60000{index}": _history(
            f"sh60000{index}",
            market_cap=(4 + index) * 1_000_000_000.0,
        )
        for index in range(10)
    }
    strategy = RobustV2Strategy(RobustV2Config(stock_min_avg_amount=0))

    result = strategy.scan_stocks(
        histories,
        _snapshot(frame),
        account_value=50_000,
    )

    selected_codes = {str(row["code"]) for row in result.candidates}
    assert {"sh600000", "sh600001", "sh600002"}.isdisjoint(selected_codes)
    assert result.filter_counts["bottom_market_cap_30pct"] == 3
    assert result.eligible_count == 7


def test_low_volatility_stock_scores_above_high_volatility_peer() -> None:
    """EP 和市值相同时，120 日低波股票应获得更高综合得分。"""
    frame = _history("base")
    strategy = RobustV2Strategy(RobustV2Config(stock_min_avg_amount=0))

    rows = strategy.score_stocks(
        {
            "sh600001": _history("sh600001", volatility=0.003),
            "sh600002": _history("sh600002", volatility=0.015),
        },
        _snapshot(frame),
        account_value=50_000,
    )

    by_code = {str(row["code"]): row for row in rows}
    assert (
        by_code["sh600001"]["annual_volatility_120d"]
        < (by_code["sh600002"]["annual_volatility_120d"])
    )
    assert by_code["sh600001"]["score"] > by_code["sh600002"]["score"]


def test_industry_etf_is_never_selected_or_allocated() -> None:
    """行业 ETF 即使动量更强，也不能进入 robust_v2 目标或执行订单。"""
    frame = _history("base")
    snapshot = _snapshot(frame)
    strategy = RobustV2Strategy(
        RobustV2Config(etf_min_avg_amount=0, enable_stock_enhancement=False)
    )

    target = strategy.generate_target(
        snapshot,
        etf_history={
            "512760": _history("512760", slope=0.08),
            "510300": _history("510300", slope=0.03),
            "510500": _history("510500", slope=0.025),
        },
        stock_history={},
        account_value=50_000,
    )

    assert "512760" not in {position.code for position in target.positions}
    invalid_target = TargetPortfolio(
        account_id="paper_v2",
        strategy_version="robust_v2",
        signal_date=snapshot.trade_date,
        positions=(
            TargetPosition(
                code="512760",
                name="半导体ETF",
                target_weight=0.24,
                reason="ETF_RISK_ADJUSTED",
                asset_type="etf",
            ),
        ),
        source_snapshot_hash="hash",
        cash_weight=0.76,
    )
    with pytest.raises(ValueError, match="非宽基 ETF"):
        PortfolioAllocator(rebalance_band=0).allocate(
            invalid_target,
            cash=50_000,
            positions={},
            prices={"512760": 1.0},
            execution_date="20260711",
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
                target_weight=0.24,
                reason="ETF_RISK_ADJUSTED",
                asset_type="etf",
            ),
        ),
        source_snapshot_hash="hash",
        cash_weight=0.76,
    )
    allocator = PortfolioAllocator(rebalance_band=0, min_etf_order=0)

    with pytest.raises(ValueError, match="下一交易日"):
        allocator.allocate(target, 50_000, {}, {"510300": 4.0}, "20260709")

    result = allocator.allocate(
        target,
        cash=32_000,
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


def test_default_50k_allocator_can_buy_current_two_stock_targets() -> None:
    """V2 的整手和最低订单约束不能再次把已选个股全部过滤。"""
    target = TargetPortfolio(
        account_id="paper_v2",
        strategy_version="robust_v2",
        signal_date="20260723",
        positions=(
            TargetPosition(
                code="601677",
                name="明泰铝业",
                target_weight=0.16,
                reason="MAINBOARD_EP_SIZE_LOW_VOL",
                asset_type="stock",
            ),
            TargetPosition(
                code="600166",
                name="福田汽车",
                target_weight=0.16,
                reason="MAINBOARD_EP_SIZE_LOW_VOL",
                asset_type="stock",
            ),
        ),
        source_snapshot_hash="hash",
        cash_weight=0.68,
    )

    result = PortfolioAllocator(rebalance_band=0).allocate(
        target,
        cash=50_000,
        positions={},
        prices={"601677": 16.50, "600166": 3.14},
        execution_date="20260724",
    )

    buys = {order.code: order.shares for order in result.orders}
    assert buys == {"601677": 400, "600166": 2500}
    assert result.projected_cash >= 50_000 * target.cash_weight


def test_stock_selection_continues_after_an_unaffordable_ranked_candidate() -> None:
    """前两名中的高价股不可买时，应继续尝试后续可成交候选。"""
    etf_history = {
        "510300": _history("510300", slope=0.03),
        "510500": _history("510500", slope=0.025),
    }
    snapshot = _snapshot(etf_history["510300"])
    scan = StockScanResult(
        input_count=3,
        candidates=(
            {
                "code": "600001",
                "name": "第一名",
                "price": 10.0,
                "earnings_yield": 0.10,
                "annual_volatility_120d": 0.20,
                "score": 0.9,
            },
            {
                "code": "600002",
                "name": "高价第二名",
                "price": 40.0,
                "earnings_yield": 0.09,
                "annual_volatility_120d": 0.21,
                "score": 0.8,
            },
            {
                "code": "600003",
                "name": "可买第三名",
                "price": 10.0,
                "earnings_yield": 0.08,
                "annual_volatility_120d": 0.22,
                "score": 0.7,
            },
        ),
        filter_counts={},
    )
    strategy = RobustV2Strategy(
        RobustV2Config(max_total_position=0.70, etf_min_avg_amount=0)
    )

    target = strategy.generate_target(
        snapshot,
        etf_history=etf_history,
        stock_history={},
        account_value=50_000,
        stock_scan_result=scan,
    )

    stock_codes = [
        position.code for position in target.positions if position.asset_type == "stock"
    ]
    assert stock_codes == ["600001", "600003"]


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


def _three_stock_target() -> TargetPortfolio:
    """构造 1 只 ETF + 3 只股票、权重合计 72% 的目标组合。"""
    return TargetPortfolio(
        account_id="paper_v2",
        strategy_version="robust_v2",
        signal_date="20260709",
        positions=(
            TargetPosition(
                code="510300",
                target_weight=0.24,
                reason="ETF_RISK_ADJUSTED",
                asset_type="etf",
            ),
            TargetPosition(
                code="600000",
                target_weight=0.16,
                reason="MAINBOARD_EP_SIZE_LOW_VOL",
                asset_type="stock",
            ),
            TargetPosition(
                code="600036",
                target_weight=0.16,
                reason="MAINBOARD_EP_SIZE_LOW_VOL",
                asset_type="stock",
            ),
            TargetPosition(
                code="601398",
                target_weight=0.16,
                reason="MAINBOARD_EP_SIZE_LOW_VOL",
                asset_type="stock",
            ),
        ),
        source_snapshot_hash="hash",
        cash_weight=0.28,
    )


def test_allocator_count_limits_align_with_strategy_config() -> None:
    """执行边界数量上限应从策略配置读取，max_stock_count=None 时不再限制。"""
    target = _three_stock_target()
    prices = {"510300": 4.0, "600000": 10.0, "600036": 35.0, "601398": 5.0}

    # 默认上限(2 只)保持原校验语义。
    with pytest.raises(ValueError, match=r"最多允许 2 只股票"):
        PortfolioAllocator(rebalance_band=0).allocate(
            target,
            cash=50_000,
            positions={},
            prices=prices,
            execution_date="20260710",
        )

    # 与策略侧 max_stock_count=None 对齐后，3 只股票的目标应可正常分配。
    result = PortfolioAllocator(rebalance_band=0, max_stock_count=None).allocate(
        target,
        cash=50_000,
        positions={},
        prices=prices,
        execution_date="20260710",
    )

    bought = {order.code for order in result.orders}
    assert {"510300", "600000", "600036", "601398"} <= bought
    assert result.projected_cash >= result.account_value * target.cash_weight - 1e-6

    # ETF 数量上限同样可配置收紧。
    two_etf_target = TargetPortfolio(
        account_id="paper_v2",
        strategy_version="robust_v2",
        signal_date="20260709",
        positions=(
            TargetPosition(
                code="510300",
                target_weight=0.24,
                reason="ETF_RISK_ADJUSTED",
                asset_type="etf",
            ),
            TargetPosition(
                code="510500",
                target_weight=0.24,
                reason="ETF_RISK_ADJUSTED",
                asset_type="etf",
            ),
        ),
        source_snapshot_hash="hash",
        cash_weight=0.52,
    )
    with pytest.raises(ValueError, match=r"最多允许 1 只 ETF"):
        PortfolioAllocator(rebalance_band=0, max_etf_count=1).allocate(
            two_etf_target,
            cash=50_000,
            positions={},
            prices={"510300": 4.0, "510500": 5.0},
            execution_date="20260710",
        )
