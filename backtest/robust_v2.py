"""robust_v2 无未来函数组合回测与受限参数选择。"""

from __future__ import annotations

import itertools
import statistics
from dataclasses import asdict, dataclass
from typing import Any, Mapping

import pandas as pd

from backtest.metrics import compute_performance_metrics
from rules.engine import TradingRules
from strategies.robust_v2 import (
    RobustV2Config,
    RobustV2Strategy,
    build_market_data_hash,
)
from trading.allocator import PortfolioAllocator
from trading.models import MarketSnapshot, TargetPortfolio
from trading.schedule import backtest_rebalance_due


@dataclass(frozen=True)
class RobustParameterSet:
    """受限搜索空间中的一组参数。"""

    enable_etf_trend_filter: bool
    stock_reversal_days: int
    rebalance_days: int
    stock_stop_pct: float
    max_total_position: float

    def to_config(self, *, enable_stock_enhancement: bool = True) -> RobustV2Config:
        """转换为策略配置。"""
        return RobustV2Config(
            enable_etf_trend_filter=self.enable_etf_trend_filter,
            stock_reversal_days=self.stock_reversal_days,
            rebalance_days=self.rebalance_days,
            stock_stop_pct=self.stock_stop_pct,
            max_total_position=self.max_total_position,
            enable_stock_enhancement=enable_stock_enhancement,
        )


@dataclass(frozen=True)
class BacktestResult:
    """单次稳健组合回测结果。"""

    params: RobustParameterSet
    start_date: str
    end_date: str
    metrics: dict[str, Any]
    daily_values: tuple[dict[str, Any], ...]
    trades: tuple[dict[str, Any], ...]
    benchmark_return: float | None


@dataclass(frozen=True)
class ValidationWindow:
    """24 个月训练之后的 6 个月样本外验证窗。"""

    train_start: str
    train_end: str
    validation_start: str
    validation_end: str


@dataclass(frozen=True)
class CandidateEvaluation:
    """一组参数在全部样本外窗口的门槛指标。"""

    params: RobustParameterSet
    median_calmar: float
    median_sharpe: float
    max_drawdown: float
    annual_turnover: float
    fee_to_gross_profit: float
    stress_underperformance_windows: int
    passed: bool
    rejection_reasons: tuple[str, ...]


@dataclass(frozen=True)
class WalkForwardSelection:
    """滚动样本外选择及最终锁定测试结果。"""

    selected_params: RobustParameterSet
    fallback_to_etf: bool
    validation_windows: tuple[ValidationWindow, ...]
    evaluations: tuple[CandidateEvaluation, ...]
    locked_test: BacktestResult
    stress_test: BacktestResult
    pure_etf_test: BacktestResult


def parameter_grid() -> tuple[RobustParameterSet, ...]:
    """返回计划限定的恰好 32 组参数。"""
    rows = tuple(
        RobustParameterSet(*values)
        for values in itertools.product(
            (True, False),
            (10, 20),
            (5, 10),
            (0.07, 0.09),
            (0.70, 0.80),
        )
    )
    if len(rows) != 32:
        raise RuntimeError(f"参数空间应为 32 组，实际为 {len(rows)}")
    return rows


class StressTradingRules(TradingRules):
    """支持滑点倍数压力测试的交易规则。"""

    def __init__(self, slippage_multiplier: float = 1.0) -> None:
        if slippage_multiplier < 1:
            raise ValueError("滑点压力倍数不能低于 1")
        self.slippage_multiplier = slippage_multiplier

    def calc_total_cost(
        self,
        amount: float,
        direction: str = "buy",
        is_etf_flag: bool = False,
        code: str | None = None,
    ) -> dict[str, float]:
        """按指定倍数放大滑点，其余费率保持不变。"""
        base = super().calc_total_cost(amount, direction, is_etf_flag, code)
        slippage = float(base["slippage"]) * self.slippage_multiplier
        extra = slippage - float(base["slippage"])
        actual_amount = float(base["actual_amount"]) + (
            extra if direction == "buy" else -extra
        )
        return {
            **base,
            "slippage": round(slippage, 2),
            "total": round(float(base["total"]) + extra, 2),
            "actual_amount": round(actual_amount, 2),
        }


def _normalize_history(history: Mapping[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """复制并归一化历史日期，避免回测修改调用方数据。"""
    normalized: dict[str, pd.DataFrame] = {}
    for code, frame in history.items():
        if frame is None or frame.empty or "date" not in frame.columns:
            continue
        data = frame.copy()
        data["date"] = (
            data["date"].astype(str).str.replace("-", "", regex=False).str[:8]
        )
        data = data.sort_values("date").drop_duplicates("date", keep="last")
        normalized[code] = data
    return normalized


def _row_for(frame: pd.DataFrame, date: str) -> pd.Series | None:
    """读取指定交易日行情行。"""
    rows = frame.loc[frame["date"] == date]
    return rows.iloc[-1] if not rows.empty else None


def _price(
    history: Mapping[str, pd.DataFrame],
    code: str,
    date: str,
    column: str,
) -> float:
    """安全读取指定证券的 OHLC 价格。"""
    frame = history.get(code)
    if frame is None:
        return 0.0
    row = _row_for(frame, date)
    if row is None:
        return 0.0
    value = row.get(column, row.get("close", 0))
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _point_in_time_codes(
    snapshots: Mapping[str, set[str] | frozenset[str]],
    signal_date: str,
) -> set[str]:
    """读取不晚于信号日的最近可交易池版本。"""
    eligible_dates = [date for date in snapshots if date <= signal_date]
    if not eligible_dates:
        return set()
    return set(snapshots[max(eligible_dates)])


class RobustPortfolioBacktester:
    """T 日收盘信号、T+1 开盘成交的低频组合回测器。"""

    def __init__(
        self,
        etf_history: Mapping[str, pd.DataFrame],
        stock_history: Mapping[str, pd.DataFrame],
        universe_snapshots: Mapping[str, set[str] | frozenset[str]],
        *,
        initial_capital: float = 50_000.0,
        benchmark_history: pd.DataFrame | None = None,
        name_map: Mapping[str, str] | None = None,
    ) -> None:
        if initial_capital <= 0:
            raise ValueError("回测初始资金必须大于 0")
        self.etf_history = _normalize_history(etf_history)
        self.stock_history = _normalize_history(stock_history)
        self.all_history = {**self.etf_history, **self.stock_history}
        self.universe_snapshots = universe_snapshots
        self.initial_capital = float(initial_capital)
        self.benchmark_history = (
            _normalize_history({"benchmark": benchmark_history}).get("benchmark")
            if benchmark_history is not None
            else None
        )
        self.name_map = dict(name_map or {})
        dates: set[str] = set()
        for frame in self.etf_history.values():
            dates.update(frame["date"].tolist())
        self.trading_dates = sorted(dates)

    def _benchmark_return(self, start_date: str, end_date: str) -> float | None:
        """计算锁定区间基准收益。"""
        if self.benchmark_history is None:
            return None
        data = self.benchmark_history.loc[
            (self.benchmark_history["date"] >= start_date)
            & (self.benchmark_history["date"] <= end_date)
        ]
        close = pd.to_numeric(data["close"], errors="coerce").dropna()
        if len(close) < 2 or float(close.iloc[0]) <= 0:
            return None
        return float(close.iloc[-1] / close.iloc[0] - 1)

    def run(
        self,
        params: RobustParameterSet,
        start_date: str,
        end_date: str,
        *,
        enable_stock_enhancement: bool = True,
        slippage_multiplier: float = 1.0,
    ) -> BacktestResult:
        """执行一个显式日期区间，所有信号只读取当日及以前数据。"""
        dates = [date for date in self.trading_dates if start_date <= date <= end_date]
        if len(dates) < 2:
            raise ValueError(f"回测区间交易日不足: {start_date}-{end_date}")
        config = params.to_config(enable_stock_enhancement=enable_stock_enhancement)
        strategy = RobustV2Strategy(config)
        rules = StressTradingRules(slippage_multiplier)
        allocator = PortfolioAllocator(rules=rules)
        cash = self.initial_capital
        positions: dict[str, dict[str, Any]] = {}
        pending: TargetPortfolio | None = None
        last_signal_date: str | None = None
        daily_values: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []

        for index, date in enumerate(dates):
            global_index = self.trading_dates.index(date)
            previous_date = (
                self.trading_dates[global_index - 1] if global_index > 0 else None
            )
            if pending is not None:
                open_prices = {
                    code: _price(self.all_history, code, date, "open")
                    for code in (
                        {position.code for position in pending.positions}
                        | set(positions)
                    )
                }
                portfolio_positions = {
                    code: {
                        **position,
                        "sellable_qty": sum(
                            int(lot["shares"])
                            for lot in position["lots"]
                            if str(lot["date"]) < date
                        ),
                        "current_price": open_prices.get(code, 0),
                    }
                    for code, position in positions.items()
                }
                allocation = allocator.allocate(
                    pending,
                    cash,
                    portfolio_positions,
                    open_prices,
                    date,
                    tradable_codes={
                        code for code, price in open_prices.items() if price > 0
                    },
                )
                for order in allocation.orders:
                    previous_close = (
                        _price(self.all_history, order.code, previous_date, "close")
                        if previous_date
                        else 0.0
                    )
                    can_buy, can_sell, _ = rules.check_price_limit(
                        order.code,
                        order.price,
                        previous_close,
                        order.name,
                    )
                    if (order.action == "buy" and not can_buy) or (
                        order.action == "sell" and not can_sell
                    ):
                        continue
                    amount = order.price * order.shares
                    costs = rules.calc_total_cost(amount, order.action, code=order.code)
                    if order.action == "sell":
                        position = positions.get(order.code)
                        if position is None:
                            continue
                        sellable = sum(
                            int(lot["shares"])
                            for lot in position["lots"]
                            if str(lot["date"]) < date
                        )
                        shares = min(order.shares, int(position["shares"]), sellable)
                        if shares <= 0:
                            continue
                        amount = order.price * shares
                        costs = rules.calc_total_cost(amount, "sell", code=order.code)
                        net = amount - float(costs["total"])
                        cost_basis = self._consume_lots(position, shares, date)
                        profit = net - cost_basis
                        cash += net
                        position["shares"] -= shares
                        if position["shares"] <= 0:
                            del positions[order.code]
                        else:
                            remaining_cost = sum(
                                float(lot["cost_per_share"]) * int(lot["shares"])
                                for lot in position["lots"]
                            )
                            position["avg_cost"] = remaining_cost / int(
                                position["shares"]
                            )
                        trades.append(
                            self._trade_row(
                                date,
                                order.code,
                                "sell",
                                order.price,
                                shares,
                                costs,
                                profit,
                                order.reason,
                            )
                        )
                    else:
                        debit = amount + float(costs["total"])
                        if debit > cash + 1e-9:
                            continue
                        existing = positions.get(order.code)
                        old_shares = int(existing["shares"]) if existing else 0
                        old_cost = (
                            float(existing["avg_cost"]) * old_shares
                            if existing
                            else 0.0
                        )
                        total_shares = old_shares + order.shares
                        lots = list(existing["lots"]) if existing else []
                        lots.append(
                            {
                                "date": date,
                                "shares": order.shares,
                                "cost_per_share": debit / order.shares,
                            }
                        )
                        positions[order.code] = {
                            "code": order.code,
                            "name": order.name or order.code,
                            "shares": total_shares,
                            "avg_cost": (old_cost + debit) / total_shares,
                            "buy_date": (
                                min(str(existing["buy_date"]), date)
                                if existing
                                else date
                            ),
                            "asset_type": (
                                "etf" if order.code in self.etf_history else "stock"
                            ),
                            "lots": lots,
                        }
                        cash -= debit
                        trades.append(
                            self._trade_row(
                                date,
                                order.code,
                                "buy",
                                order.price,
                                order.shares,
                                costs,
                                None,
                                order.reason,
                            )
                        )
                pending = None

            # 灾难止损使用日内低点触发，成交价按跳空开盘或止损线中更差者处理。
            for code in list(positions):
                position = positions[code]
                sellable = sum(
                    int(lot["shares"])
                    for lot in position["lots"]
                    if str(lot["date"]) < date
                )
                if sellable <= 0:
                    continue
                low = _price(self.all_history, code, date, "low")
                open_price = _price(self.all_history, code, date, "open")
                threshold = (
                    config.etf_stop_pct
                    if position["asset_type"] == "etf"
                    else config.stock_stop_pct
                )
                stop_price = float(position["avg_cost"]) * (1 - threshold)
                if low <= 0 or low > stop_price:
                    continue
                execution_price = (
                    min(open_price, stop_price) if open_price > 0 else stop_price
                )
                shares = sellable
                amount = execution_price * shares
                costs = rules.calc_total_cost(amount, "sell", code=code)
                net = amount - float(costs["total"])
                cost_basis = self._consume_lots(position, shares, date)
                profit = net - cost_basis
                cash += net
                position["shares"] -= shares
                if position["shares"] <= 0:
                    del positions[code]
                else:
                    remaining_cost = sum(
                        float(lot["cost_per_share"]) * int(lot["shares"])
                        for lot in position["lots"]
                    )
                    position["avg_cost"] = remaining_cost / int(position["shares"])
                trades.append(
                    self._trade_row(
                        date,
                        code,
                        "sell",
                        execution_price,
                        shares,
                        costs,
                        profit,
                        "CATASTROPHIC_STOP_LOSS",
                    )
                )

            close_prices = {
                code: _price(self.all_history, code, date, "close")
                for code in positions
            }
            total_value = cash + sum(
                close_prices.get(code, 0) * int(position["shares"])
                for code, position in positions.items()
            )
            daily_values.append(
                {
                    "date": date,
                    "total_value": round(total_value, 2),
                    "cash": round(cash, 2),
                    "position_count": len(positions),
                }
            )

            should_signal = index < len(dates) - 1 and backtest_rebalance_due(
                dates,
                index,
                last_signal_date,
                config.rebalance_days,
            )
            if not should_signal:
                continue
            universe = _point_in_time_codes(self.universe_snapshots, date)
            stock_history = {
                code: frame
                for code, frame in self.stock_history.items()
                if code in universe
            }
            selected_history = {**self.etf_history, **stock_history}
            snapshot = MarketSnapshot(
                trade_date=date,
                previous_trade_date=previous_date,
                source="point-in-time-backtest",
                adjustment="qfq",
                data_hash=build_market_data_hash(selected_history, date),
                freshness_seconds=0,
            )
            pending = strategy.generate_target(
                snapshot,
                self.etf_history,
                stock_history,
                total_value,
                self.name_map,
            )
            last_signal_date = date

        metrics = compute_performance_metrics(
            daily_values, trades, [], self.initial_capital
        )
        if (
            float(metrics.get("gross_profit", 0) or 0) <= 0
            and float(metrics.get("total_cost", 0) or 0) > 0
        ):
            metrics["fee_to_gross_profit"] = 999.0
        trading_days = int(metrics.get("trading_days", len(dates)) or len(dates))
        metrics["annual_turnover"] = round(
            float(metrics.get("turnover_rate", 0)) * 252 / max(trading_days, 1),
            4,
        )
        benchmark_return = self._benchmark_return(start_date, end_date)
        metrics["benchmark_return"] = benchmark_return
        return BacktestResult(
            params=params,
            start_date=start_date,
            end_date=end_date,
            metrics=metrics,
            daily_values=tuple(daily_values),
            trades=tuple(trades),
            benchmark_return=benchmark_return,
        )

    @staticmethod
    def _consume_lots(position: dict[str, Any], shares: int, sell_date: str) -> float:
        """按 FIFO 消耗卖出日前批次并返回含买入成本的成本基础。"""
        remaining = shares
        cost_basis = 0.0
        for lot in position["lots"]:
            if str(lot["date"]) >= sell_date or remaining <= 0:
                continue
            take = min(remaining, int(lot["shares"]))
            cost_basis += take * float(lot["cost_per_share"])
            lot["shares"] = int(lot["shares"]) - take
            remaining -= take
        position["lots"] = [lot for lot in position["lots"] if int(lot["shares"]) > 0]
        if remaining != 0:
            raise RuntimeError("回测 T+1 批次不足，拒绝产生不可能成交")
        return cost_basis

    @staticmethod
    def _trade_row(
        date: str,
        code: str,
        action: str,
        price: float,
        shares: int,
        costs: Mapping[str, float],
        profit: float | None,
        reason: str,
    ) -> dict[str, Any]:
        """构建公共指标可读取的成交记录。"""
        row: dict[str, Any] = {
            "date": date,
            "code": code,
            "action": action,
            "direction": action,
            "price": round(price, 4),
            "shares": shares,
            "amount": round(price * shares, 2),
            "cost": float(costs["total"]),
            "commission": float(costs["commission"]),
            "stamp_tax": float(costs["stamp_tax"]),
            "slippage": float(costs["slippage"]),
            "sell_reason": reason if action == "sell" else "",
        }
        if profit is not None:
            row["profit"] = round(profit, 2)
        return row


def build_validation_windows(
    trading_dates: list[str],
    *,
    locked_test_months: int = 12,
) -> tuple[tuple[ValidationWindow, ...], str, str]:
    """构建 24 月训练 + 6 月验证窗口，并锁定最后 12 月。"""
    if len(trading_dates) < 2:
        raise ValueError("交易日期不足")
    dates = pd.DatetimeIndex(pd.to_datetime(sorted(trading_dates), format="%Y%m%d"))
    test_start_target = (
        dates[-1] - pd.DateOffset(months=locked_test_months) + pd.Timedelta(days=1)
    )
    test_candidates = dates[dates >= test_start_target]
    if test_candidates.empty:
        raise ValueError("无法构建锁定测试集")
    test_start = test_candidates[0]
    test_end = dates[-1]
    windows: list[ValidationWindow] = []
    validation_start_target = dates[0] + pd.DateOffset(months=24)
    while validation_start_target < test_start:
        validation_candidates = dates[
            (dates >= validation_start_target) & (dates < test_start)
        ]
        if validation_candidates.empty:
            break
        validation_start = validation_candidates[0]
        validation_end_target = (
            validation_start + pd.DateOffset(months=6) - pd.Timedelta(days=1)
        )
        validation_end_candidates = dates[
            (dates <= validation_end_target) & (dates < test_start)
        ]
        if validation_end_candidates.empty:
            break
        validation_end = validation_end_candidates[-1]
        train_end_candidates = dates[dates < validation_start]
        train_start_candidates = dates[
            dates >= validation_start - pd.DateOffset(months=24)
        ]
        if train_end_candidates.empty or train_start_candidates.empty:
            break
        train_end = train_end_candidates[-1]
        train_start = train_start_candidates[0]
        windows.append(
            ValidationWindow(
                train_start=train_start.strftime("%Y%m%d"),
                train_end=train_end.strftime("%Y%m%d"),
                validation_start=validation_start.strftime("%Y%m%d"),
                validation_end=validation_end.strftime("%Y%m%d"),
            )
        )
        validation_start_target = validation_start + pd.DateOffset(months=6)
    if not windows:
        raise ValueError("历史长度不足以构建 24+6 月滚动窗口和 12 月测试集")
    return tuple(windows), test_start.strftime("%Y%m%d"), test_end.strftime("%Y%m%d")


class RobustWalkForwardSelector:
    """按样本外门槛和 Calmar 中位数选择稳健参数。"""

    def __init__(self, backtester: RobustPortfolioBacktester) -> None:
        self.backtester = backtester

    @staticmethod
    def _evaluate(
        params: RobustParameterSet,
        results: list[BacktestResult],
        stress_results: list[BacktestResult],
    ) -> CandidateEvaluation:
        """聚合多窗口指标并执行硬门槛。"""
        calmars = [float(result.metrics.get("calmar_ratio", 0)) for result in results]
        sharpes = [float(result.metrics.get("sharpe_ratio", 0)) for result in results]
        drawdown = max(
            float(result.metrics.get("max_drawdown", 1)) for result in results
        )
        turnover = max(
            float(result.metrics.get("annual_turnover", 999)) for result in results
        )
        fee_ratio = max(
            float(result.metrics.get("fee_to_gross_profit", 999)) for result in results
        )
        median_calmar = statistics.median(calmars)
        median_sharpe = statistics.median(sharpes)
        stress_underperformance = sum(
            1
            for result in stress_results
            if result.benchmark_return is not None
            and float(result.metrics.get("total_return", -1)) < result.benchmark_return
        )
        reasons: list[str] = []
        if drawdown > 0.12:
            reasons.append("样本外最大回撤超过 12%")
        if min(sharpes) < 0.6:
            reasons.append("至少一个样本外窗口夏普低于 0.6")
        if min(calmars) < 0.6:
            reasons.append("至少一个样本外窗口 Calmar 低于 0.6")
        if turnover > 12:
            reasons.append("年换手超过 12 倍")
        if fee_ratio > 0.25:
            reasons.append("成本占毛利润超过 25%")
        if stress_underperformance:
            reasons.append(f"双倍滑点下有 {stress_underperformance} 个窗口落后基准")
        return CandidateEvaluation(
            params=params,
            median_calmar=round(median_calmar, 4),
            median_sharpe=round(median_sharpe, 4),
            max_drawdown=round(drawdown, 4),
            annual_turnover=round(turnover, 4),
            fee_to_gross_profit=round(fee_ratio, 4),
            stress_underperformance_windows=stress_underperformance,
            passed=not reasons,
            rejection_reasons=tuple(reasons),
        )

    def select(self) -> WalkForwardSelection:
        """执行 32 组滚动验证、双倍滑点压力测试和最终锁定测试。"""
        if self.backtester.benchmark_history is None:
            raise ValueError("参数选择必须提供沪深 300 基准，不能跳过双倍滑点比较")
        windows, test_start, test_end = build_validation_windows(
            self.backtester.trading_dates
        )
        evaluations: list[CandidateEvaluation] = []
        passed: list[CandidateEvaluation] = []
        for params in parameter_grid():
            results = [
                self.backtester.run(
                    params, window.validation_start, window.validation_end
                )
                for window in windows
            ]
            stress_results = [
                self.backtester.run(
                    params,
                    window.validation_start,
                    window.validation_end,
                    slippage_multiplier=2.0,
                )
                for window in windows
            ]
            evaluation = self._evaluate(params, results, stress_results)
            evaluations.append(evaluation)
            if evaluation.passed:
                passed.append(evaluation)
        passed.sort(key=lambda item: item.median_calmar, reverse=True)

        fallback = not passed
        selected = (
            passed[0].params
            if passed
            else RobustParameterSet(
                enable_etf_trend_filter=True,
                stock_reversal_days=20,
                rebalance_days=5,
                stock_stop_pct=0.09,
                max_total_position=0.60,
            )
        )
        locked = self.backtester.run(
            selected,
            test_start,
            test_end,
            enable_stock_enhancement=not fallback,
        )
        stress = self.backtester.run(
            selected,
            test_start,
            test_end,
            enable_stock_enhancement=not fallback,
            slippage_multiplier=2.0,
        )
        pure_etf = self.backtester.run(
            RobustParameterSet(
                enable_etf_trend_filter=True,
                stock_reversal_days=20,
                rebalance_days=selected.rebalance_days,
                stock_stop_pct=selected.stock_stop_pct,
                max_total_position=0.60,
            ),
            test_start,
            test_end,
            enable_stock_enhancement=False,
        )
        return WalkForwardSelection(
            selected_params=selected,
            fallback_to_etf=fallback,
            validation_windows=windows,
            evaluations=tuple(evaluations),
            locked_test=locked,
            stress_test=stress,
            pure_etf_test=pure_etf,
        )


def selection_to_dict(selection: WalkForwardSelection) -> dict[str, Any]:
    """把选择结果转换为 JSON 可序列化结构。"""
    return {
        "selected_params": asdict(selection.selected_params),
        "fallback_to_etf": selection.fallback_to_etf,
        "validation_windows": [
            asdict(window) for window in selection.validation_windows
        ],
        "evaluations": [
            {**asdict(evaluation), "params": asdict(evaluation.params)}
            for evaluation in selection.evaluations
        ],
        "locked_test": {
            "start_date": selection.locked_test.start_date,
            "end_date": selection.locked_test.end_date,
            "metrics": selection.locked_test.metrics,
        },
        "stress_test": {
            "metrics": selection.stress_test.metrics,
        },
        "pure_etf_test": {
            "metrics": selection.pure_etf_test.metrics,
        },
    }
