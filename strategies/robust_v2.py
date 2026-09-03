"""5 万元 A 股虚拟盘稳健策略 V2。

策略层只把收盘数据转换为目标权重，不直接操作账户。ETF 仅从宽基池选择中期趋势，
个股使用盈利收益率、适度规模和低波动，所有成交由统一分配器负责。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd

from config.settings import (
    DEFAULT_RPS_ETF_POOL,
    ROBUST_V2_ACCOUNT_ID,
    ROBUST_V2_BROAD_ETF_CODES,
    ROBUST_V2_ETF_STOP_PCT,
    ROBUST_V2_ETF_TARGET,
    ROBUST_V2_MAX_DATA_AGE_SECONDS,
    ROBUST_V2_MAX_SINGLE_ETF,
    ROBUST_V2_MAX_SINGLE_STOCK,
    ROBUST_V2_MAX_TOTAL_POSITION,
    ROBUST_V2_MIN_CASH,
    ROBUST_V2_REBALANCE_DAYS,
    ROBUST_V2_STOCK_STOP_PCT,
    ROBUST_V2_STOCK_TARGET,
    ROBUST_V2_STRATEGY_VERSION,
    get_stock_board,
    is_etf,
)
from trading.instruments import normalized_security_code
from trading.models import MarketSnapshot, OrderIntent, TargetPortfolio, TargetPosition

STOCK_FILTER_LABELS: dict[str, str] = {
    "non_mainboard": "非沪深主板",
    "invalid_market_data": "行情格式无效",
    "insufficient_history": "历史行情不足",
    "missing_signal_date": "缺少信号日行情",
    "missing_previous_trade_date": "缺少上一交易日行情",
    "price_out_of_range": "股价超出范围",
    "lot_too_expensive": "一手金额超过个股预算",
    "st_or_delisting": "ST 或退市风险",
    "suspended": "停牌或不可交易",
    "market_cap_out_of_range": "市值不在目标区间",
    "unprofitable": "盈利质量不合格",
    "earnings_yield_too_low": "盈利收益率无效或过低",
    "insufficient_liquidity": "近 20 日成交额不足",
    "excessive_20d_drop": "近 20 日跌幅超过风险阈值",
    "excessive_120d_volatility": "近 120 日年化波动率过高",
    "below_ma200": "股价低于 MA200",
    "overextended_ma20": "偏离 MA20 过高",
    "excessive_5d_gain": "近 5 日涨幅过高",
    "bottom_market_cap_30pct": "候选横截面市值最低 30%",
}


@dataclass(frozen=True)
class RobustV2Config:
    """稳健策略全部可选参数。"""

    account_id: str = ROBUST_V2_ACCOUNT_ID
    strategy_version: str = ROBUST_V2_STRATEGY_VERSION
    max_total_position: float = ROBUST_V2_MAX_TOTAL_POSITION
    etf_target: float = ROBUST_V2_ETF_TARGET
    stock_target: float = ROBUST_V2_STOCK_TARGET
    min_cash: float = ROBUST_V2_MIN_CASH
    max_single_etf: float = ROBUST_V2_MAX_SINGLE_ETF
    max_single_stock: float = ROBUST_V2_MAX_SINGLE_STOCK
    max_etf_count: int = 2
    max_stock_count: int | None = None
    etf_lookbacks: tuple[int, int, int] = (20, 60, 120)
    etf_score_weights: tuple[float, float, float] = (0.10, 0.45, 0.45)
    etf_min_avg_amount: float = 50_000_000.0
    etf_trend_ma_days: int = 200
    etf_min_20d_return: float = -0.05
    enable_etf_trend_filter: bool = True
    # 兼容旧回测参数；robust_v2 已不再把短期反转用于排序。
    stock_reversal_days: int = 20
    stock_min_history_days: int = 250
    stock_min_price: float = 3.0
    stock_max_price: float = 80.0
    # 兼容旧配置；PB 仅作为扫描审计字段，不参与过滤或评分。
    stock_min_pb: float = 0.5
    stock_max_pb: float = 8.0
    stock_min_market_cap: float = 3_000_000_000.0
    stock_max_market_cap: float = 50_000_000_000.0
    stock_market_cap_bottom_exclusion: float = 0.30
    stock_min_earnings_yield: float = 0.02
    stock_volatility_days: int = 120
    stock_max_annual_volatility: float = 0.60
    stock_min_20d_return: float = -0.15
    stock_trend_ma_days: int = 200
    stock_score_weights: tuple[float, float, float] = (0.45, 0.25, 0.30)
    stock_min_avg_amount: float = 100_000_000.0
    stock_max_5d_gain: float = 0.15
    stock_max_price_ma20: float = 1.15
    stock_stop_pct: float = ROBUST_V2_STOCK_STOP_PCT
    etf_stop_pct: float = ROBUST_V2_ETF_STOP_PCT
    rebalance_days: int = ROBUST_V2_REBALANCE_DAYS
    max_data_age_seconds: int = ROBUST_V2_MAX_DATA_AGE_SECONDS
    enable_stock_enhancement: bool = True

    def __post_init__(self) -> None:
        if not 0 < self.max_total_position <= 0.8:
            raise ValueError("robust_v2 总仓位必须位于 (0, 0.8]")
        if self.min_cash < 0.2:
            raise ValueError("robust_v2 现金下限不得低于 20%")
        if self.max_total_position + self.min_cash > 1 + 1e-9:
            raise ValueError("总仓位上限与现金下限之和不能超过 100%")
        if self.etf_target > 0.48 or self.stock_target > 0.50:
            raise ValueError("ETF/个股目标分别不得超过 48%/50%")
        if self.max_single_etf > 0.24 or self.max_single_stock > 0.16:
            raise ValueError("单只 ETF/股票上限分别不得超过 24%/16%")
        if not 1 <= self.max_etf_count <= 2:
            raise ValueError("ETF 数量上限必须位于 [1, 2]")
        if self.max_stock_count is not None and self.max_stock_count < 1:
            raise ValueError("个股数量上限必须为正数或 None")
        if self.etf_lookbacks != (20, 60, 120):
            raise ValueError("ETF 回看周期固定为 20/60/120 日")
        if abs(sum(self.etf_score_weights) - 1) > 1e-9:
            raise ValueError("ETF 评分权重之和必须为 1")
        if abs(sum(self.stock_score_weights) - 1) > 1e-9:
            raise ValueError("个股评分权重之和必须为 1")
        if self.stock_min_earnings_yield <= 0:
            raise ValueError("最低盈利收益率必须大于 0")
        if not 0 <= self.stock_market_cap_bottom_exclusion < 0.5:
            raise ValueError("微盘排除比例必须位于 [0, 0.5)")
        if self.stock_max_annual_volatility <= 0:
            raise ValueError("个股年化波动率上限必须大于 0")
        if self.stock_reversal_days not in {10, 20}:
            raise ValueError("兼容回测的旧反转周期只允许 10 或 20 个交易日")
        if self.rebalance_days not in {5, 10, 20}:
            raise ValueError("调仓周期只允许周度、双周或约月度")
        if self.stock_stop_pct not in {0.07, 0.09}:
            raise ValueError("股票灾难止损只允许 7% 或 9%")


@dataclass(frozen=True)
class AlignmentResult:
    """历史行情与实时行情口径校验结果。"""

    valid_codes: frozenset[str]
    rejected: dict[str, str]


@dataclass(frozen=True)
class StockScanResult:
    """一次主板个股因子扫描的结构化结果。"""

    input_count: int
    candidates: tuple[dict[str, Any], ...]
    filter_counts: dict[str, int]

    @property
    def eligible_count(self) -> int:
        """返回通过全部过滤条件的个股数量。"""
        return len(self.candidates)

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化字典。"""
        return {
            "input_count": self.input_count,
            "eligible_count": self.eligible_count,
            "candidates": [dict(candidate) for candidate in self.candidates],
            "filter_counts": dict(self.filter_counts),
        }


def _norm_date(value: Any) -> str:
    """归一化行情日期为 YYYYMMDD。"""
    return str(value).strip().replace("-", "")[:8]


def _slice_as_of(frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    """只保留信号日及以前数据，作为防未来函数的唯一入口。"""
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = frame.copy()
    if "date" in data.columns:
        normalized_dates = data["date"].map(_norm_date)
    elif isinstance(data.index, pd.DatetimeIndex):
        normalized_dates = pd.Series(data.index.strftime("%Y%m%d"), index=data.index)
    else:
        raise ValueError("行情数据必须包含 date 列或 DatetimeIndex")
    data = data.loc[normalized_dates <= trade_date].copy()
    if "date" in data.columns:
        data["date"] = data["date"].map(_norm_date)
        data = data.sort_values("date")
    else:
        data = data.sort_index()
    return data


def _numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    """读取数值列并过滤无效值。"""
    if column not in frame.columns:
        return pd.Series(dtype="float64")
    return pd.to_numeric(frame[column], errors="coerce").dropna()


def _latest_number(frame: pd.DataFrame, *columns: str) -> float | None:
    """按列优先级读取最后一个有效数值。"""
    for column in columns:
        values = _numeric_series(frame, column)
        if not values.empty:
            return float(values.iloc[-1])
    return None


def _average_amount(frame: pd.DataFrame, lookback: int = 20) -> float:
    """计算平均成交额，缺少 amount 时用收盘价乘成交量近似。"""
    amount = _numeric_series(frame, "amount")
    if not amount.empty:
        return float(amount.tail(lookback).mean())
    close = pd.to_numeric(frame.get("close"), errors="coerce")
    volume = pd.to_numeric(frame.get("volume"), errors="coerce")
    if close is None or volume is None:
        return 0.0
    proxy = (close * volume).dropna()
    return float(proxy.tail(lookback).mean()) if not proxy.empty else 0.0


def _contains_trade_date(frame: pd.DataFrame, trade_date: str | None) -> bool:
    """判断行情是否包含指定交易日。"""
    if trade_date is None:
        return True
    if "date" in frame.columns:
        return trade_date in set(frame["date"].map(_norm_date))
    if isinstance(frame.index, pd.DatetimeIndex):
        return trade_date in set(frame.index.strftime("%Y%m%d"))
    return False


def build_market_data_hash(
    history_map: Mapping[str, pd.DataFrame],
    trade_date: str,
) -> str:
    """对所有会影响策略决策的历史字段生成稳定数据哈希。"""
    payload: list[dict[str, Any]] = []
    decision_columns = (
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "turn",
        "peTTM",
        "pe_ttm",
        "pbMRQ",
        "pb",
        "isST",
        "is_st",
        "tradestatus",
        "is_suspended",
        "pctChg",
        "mktcap",
        "market_cap",
        "total_market_cap",
        "net_profit",
    )
    for code in sorted(history_map):
        frame = _slice_as_of(history_map[code], trade_date)
        close = _numeric_series(frame, "close")
        if frame.empty or close.empty:
            continue
        latest_date = (
            _norm_date(frame["date"].iloc[-1])
            if "date" in frame.columns
            else str(frame.index[-1])
        )
        selected_columns = [
            column for column in decision_columns if column in frame.columns
        ]
        decision_frame = frame.loc[:, selected_columns].copy()
        if "date" in decision_frame.columns:
            decision_frame["date"] = decision_frame["date"].map(_norm_date)
        row_hashes = pd.util.hash_pandas_object(
            decision_frame,
            index=False,
            categorize=True,
        )
        frame_digest = hashlib.sha256(row_hashes.values.tobytes()).hexdigest()
        payload.append(
            {
                "code": code,
                "date": latest_date,
                "close": round(float(close.iloc[-1]), 6),
                "rows": len(frame),
                "columns": selected_columns,
                "frame_hash": frame_digest,
            }
        )
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def validate_realtime_alignment(
    snapshot: MarketSnapshot,
    history_map: Mapping[str, pd.DataFrame],
    realtime_quotes: Mapping[str, Mapping[str, Any]],
    tolerance: float = 0.02,
) -> AlignmentResult:
    """校验前收盘、历史连续性与实时行情口径。

    偏差超过 2%、缺少上一交易日或快照陈旧的标的不会进入下单阶段。
    """
    if tolerance <= 0:
        raise ValueError("行情对齐容差必须大于 0")
    snapshot.require_fresh()
    normalized_history: dict[str, pd.DataFrame] = {}
    for history_code, frame in history_map.items():
        try:
            normalized_history[normalized_security_code(history_code)] = frame
        except ValueError:
            continue
    valid: set[str] = set()
    rejected: dict[str, str] = {}
    for code, quote in realtime_quotes.items():
        try:
            raw_code = normalized_security_code(code)
        except ValueError:
            rejected[code] = "证券代码格式无效"
            continue
        frame = normalized_history.get(raw_code)
        if frame is None or frame.empty:
            rejected[code] = "缺少历史行情"
            continue
        sliced = _slice_as_of(frame, snapshot.trade_date)
        if not _contains_trade_date(sliced, snapshot.trade_date):
            rejected[code] = f"缺少信号交易日 {snapshot.trade_date}"
            continue
        if not _contains_trade_date(sliced, snapshot.previous_trade_date):
            rejected[code] = f"缺少上一交易日 {snapshot.previous_trade_date}"
            continue
        historical_close = _latest_number(sliced, "close")
        realtime_prev_close = float(quote.get("prev_close", 0) or 0)
        if not historical_close or realtime_prev_close <= 0:
            rejected[code] = "前收盘价格无效"
            continue
        difference = abs(historical_close / realtime_prev_close - 1)
        if difference > tolerance:
            rejected[code] = f"前收盘口径偏差 {difference:.2%}"
            continue
        valid.add(code)
    return AlignmentResult(valid_codes=frozenset(valid), rejected=rejected)


def _percentile(values: list[float], *, lower_is_better: bool) -> list[float]:
    """把横截面数值转换为稳健分位得分。"""
    if not values:
        return []
    series = pd.Series(values, dtype="float64")
    return list(
        series.rank(method="average", ascending=not lower_is_better, pct=True).astype(
            float
        )
    )


class RobustV2Strategy:
    """ETF 为主、主板因子增强的低频目标组合策略。"""

    def __init__(self, config: RobustV2Config | None = None) -> None:
        self.config = config or RobustV2Config()
        self.name = "A股稳健策略V2"

    def score_etfs(
        self,
        history_map: Mapping[str, pd.DataFrame],
        snapshot: MarketSnapshot,
        name_map: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """从宽基池按 20/60/120 日风险调整收益筛选和排序。"""
        snapshot.require_fresh()
        names = name_map or {}
        max_lookback = max(self.config.etf_lookbacks)
        min_history = max(max_lookback + 1, self.config.etf_trend_ma_days)
        rows: list[dict[str, Any]] = []
        for code, original in history_map.items():
            try:
                raw_code = normalized_security_code(code)
            except ValueError:
                continue
            if not is_etf(code) or raw_code not in ROBUST_V2_BROAD_ETF_CODES:
                continue
            frame = _slice_as_of(original, snapshot.trade_date)
            close = _numeric_series(frame, "close")
            if len(close) < min_history:
                continue
            if not _contains_trade_date(frame, snapshot.trade_date):
                continue
            if not _contains_trade_date(frame, snapshot.previous_trade_date):
                continue
            if _average_amount(frame) < self.config.etf_min_avg_amount:
                continue

            returns: list[float] = []
            adjusted: list[float] = []
            for lookback in self.config.etf_lookbacks:
                total_return = float(close.iloc[-1] / close.iloc[-(lookback + 1)] - 1)
                daily_returns = close.pct_change().tail(lookback).dropna()
                annual_vol = float(daily_returns.std(ddof=0) * np.sqrt(252))
                returns.append(total_return)
                adjusted.append(total_return / max(annual_vol, 0.05))
            return_by_lookback = dict(zip(self.config.etf_lookbacks, returns))
            ma200 = float(close.tail(self.config.etf_trend_ma_days).mean())
            # 绝对趋势门槛始终生效；旧 enable_etf_trend_filter 参数仅保留配置兼容。
            if not (
                float(close.iloc[-1]) >= ma200
                and return_by_lookback[20] >= self.config.etf_min_20d_return
                and return_by_lookback[60] > 0
            ):
                continue
            score = float(
                sum(
                    value * weight
                    for value, weight in zip(
                        adjusted,
                        self.config.etf_score_weights,
                    )
                )
            )
            rows.append(
                {
                    "code": code,
                    "name": names.get(code)
                    or names.get(raw_code)
                    or DEFAULT_RPS_ETF_POOL.get(raw_code, {}).get("name", code),
                    "price": float(close.iloc[-1]),
                    "score": score,
                    "returns": tuple(returns),
                    "ma200": ma200,
                }
            )
        rows.sort(key=lambda row: (float(row["score"]), str(row["code"])), reverse=True)
        return rows

    def scan_stocks(
        self,
        history_map: Mapping[str, pd.DataFrame],
        snapshot: MarketSnapshot,
        account_value: float,
        name_map: Mapping[str, str] | None = None,
    ) -> StockScanResult:
        """筛选沪深主板并返回候选及每层过滤原因统计。"""
        snapshot.require_fresh()
        names = name_map or {}
        candidates: list[dict[str, Any]] = []
        filter_counts: Counter[str] = Counter()

        def reject(reason: str) -> None:
            """记录首个淘汰原因，确保统计总量可核对。"""
            filter_counts[reason] += 1

        for code, original in history_map.items():
            if get_stock_board(code) != "mainboard":
                reject("non_mainboard")
                continue
            try:
                frame = _slice_as_of(original, snapshot.trade_date)
            except (TypeError, ValueError):
                reject("invalid_market_data")
                continue
            if len(frame) < self.config.stock_min_history_days:
                reject("insufficient_history")
                continue
            if not _contains_trade_date(frame, snapshot.trade_date):
                reject("missing_signal_date")
                continue
            if not _contains_trade_date(frame, snapshot.previous_trade_date):
                reject("missing_previous_trade_date")
                continue
            close = _numeric_series(frame, "close")
            if len(close) < self.config.stock_min_history_days:
                reject("insufficient_history")
                continue
            price = float(close.iloc[-1])
            if not self.config.stock_min_price <= price <= self.config.stock_max_price:
                reject("price_out_of_range")
                continue
            if price * 100 > account_value * self.config.max_single_stock:
                reject("lot_too_expensive")
                continue

            last = frame.iloc[-1]
            name = str(
                names.get(code)
                or (last.get("name", code) if hasattr(last, "get") else code)
            )
            is_st_value = (
                bool(last.get("is_st", False)) if hasattr(last, "get") else False
            )
            if is_st_value or "ST" in name.upper():
                reject("st_or_delisting")
                continue
            trade_status = _latest_number(frame, "tradestatus")
            if trade_status is not None and trade_status == 0:
                reject("suspended")
                continue
            suspended = (
                bool(last.get("is_suspended", False)) if hasattr(last, "get") else False
            )
            if suspended:
                reject("suspended")
                continue

            pb = _latest_number(frame, "pb", "pbMRQ")
            market_cap = _latest_number(
                frame, "mktcap", "market_cap", "total_market_cap"
            )
            pe = _latest_number(frame, "peTTM", "pe_ttm", "pe")
            net_profit = _latest_number(frame, "net_profit")
            if (
                market_cap is None
                or not self.config.stock_min_market_cap
                <= market_cap
                <= self.config.stock_max_market_cap
            ):
                reject("market_cap_out_of_range")
                continue
            # 财报净利润存在时只把它作为盈利为正门槛，不用缺失的 ROE/现金流做代理。
            if net_profit is not None and net_profit <= 0:
                reject("unprofitable")
                continue
            if pe is None or pe <= 0:
                reject("earnings_yield_too_low")
                continue
            earnings_yield = 1 / pe
            if earnings_yield < self.config.stock_min_earnings_yield:
                reject("earnings_yield_too_low")
                continue
            if _average_amount(frame) < self.config.stock_min_avg_amount:
                reject("insufficient_liquidity")
                continue

            ma20 = float(close.tail(20).mean())
            ma200 = float(close.tail(self.config.stock_trend_ma_days).mean())
            gain_5d = float(close.iloc[-1] / close.iloc[-6] - 1)
            return_20d = float(close.iloc[-1] / close.iloc[-21] - 1)
            daily_returns = (
                close.pct_change().tail(self.config.stock_volatility_days).dropna()
            )
            annual_volatility = float(daily_returns.std(ddof=0) * np.sqrt(252))
            if return_20d < self.config.stock_min_20d_return:
                reject("excessive_20d_drop")
                continue
            if annual_volatility > self.config.stock_max_annual_volatility:
                reject("excessive_120d_volatility")
                continue
            if price < ma200:
                reject("below_ma200")
                continue
            if price / ma20 > self.config.stock_max_price_ma20:
                reject("overextended_ma20")
                continue
            if gain_5d > self.config.stock_max_5d_gain:
                reject("excessive_5d_gain")
                continue
            candidates.append(
                {
                    "code": code,
                    "name": name,
                    "price": price,
                    "pb": pb,
                    "market_cap": market_cap,
                    "earnings_yield": float(earnings_yield),
                    "annual_volatility_120d": annual_volatility,
                    "return_20d": return_20d,
                    "gain_5d": gain_5d,
                }
            )

        if candidates:
            # 先剔除横截面最小 30% 市值，避免把“偏小”误做成微盘暴露。
            exclusion_count = int(
                len(candidates) * self.config.stock_market_cap_bottom_exclusion
            )
            excluded_codes = {
                str(row["code"])
                for row in sorted(
                    candidates,
                    key=lambda row: (
                        float(row["market_cap"]),
                        str(row["code"]),
                    ),
                )[:exclusion_count]
            }
            if excluded_codes:
                candidates = [
                    row for row in candidates if str(row["code"]) not in excluded_codes
                ]
                filter_counts["bottom_market_cap_30pct"] += len(excluded_codes)

        if candidates:
            earnings_yield_scores = _percentile(
                [float(row["earnings_yield"]) for row in candidates],
                lower_is_better=False,
            )
            size_scores = _percentile(
                [float(row["market_cap"]) for row in candidates],
                lower_is_better=True,
            )
            low_volatility_scores = _percentile(
                [float(row["annual_volatility_120d"]) for row in candidates],
                lower_is_better=True,
            )
            earnings_weight, size_weight, volatility_weight = (
                self.config.stock_score_weights
            )
            for index, row in enumerate(candidates):
                row["score"] = (
                    earnings_weight * earnings_yield_scores[index]
                    + size_weight * size_scores[index]
                    + volatility_weight * low_volatility_scores[index]
                )
            candidates.sort(
                key=lambda row: (float(row["score"]), str(row["code"])),
                reverse=True,
            )
        ordered_counts = {
            reason: filter_counts[reason]
            for reason in STOCK_FILTER_LABELS
            if filter_counts[reason] > 0
        }
        return StockScanResult(
            input_count=len(history_map),
            candidates=tuple(candidates),
            filter_counts=ordered_counts,
        )

    def score_stocks(
        self,
        history_map: Mapping[str, pd.DataFrame],
        snapshot: MarketSnapshot,
        account_value: float,
        name_map: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """兼容原调用方，仅返回通过过滤后的个股排序。"""
        result = self.scan_stocks(history_map, snapshot, account_value, name_map)
        return [dict(candidate) for candidate in result.candidates]

    def generate_target(
        self,
        snapshot: MarketSnapshot,
        etf_history: Mapping[str, pd.DataFrame],
        stock_history: Mapping[str, pd.DataFrame],
        account_value: float,
        name_map: Mapping[str, str] | None = None,
        *,
        stock_scan_result: StockScanResult | None = None,
    ) -> TargetPortfolio:
        """从 T 日收盘数据生成目标仓位，成交必须留给 T+1 执行层。"""
        if account_value <= 0:
            raise ValueError("账户净值必须大于 0")
        snapshot.require_fresh()
        positions: list[TargetPosition] = []

        remaining_etf = min(self.config.etf_target, self.config.max_total_position)
        for row in self.score_etfs(etf_history, snapshot, name_map)[
            : self.config.max_etf_count
        ]:
            weight = min(self.config.max_single_etf, remaining_etf)
            if weight <= 0 or float(row["price"]) * 100 > account_value * weight:
                continue
            returns = row["returns"]
            positions.append(
                TargetPosition(
                    code=str(row["code"]),
                    name=str(row["name"]),
                    asset_type="etf",
                    target_weight=round(weight, 6),
                    reason=(
                        "ETF_RISK_ADJUSTED "
                        f"r20={returns[0]:+.2%} r60={returns[1]:+.2%} r120={returns[2]:+.2%}"
                    ),
                )
            )
            remaining_etf -= weight

        remaining_total = self.config.max_total_position - sum(
            position.target_weight for position in positions
        )
        remaining_stock = min(self.config.stock_target, remaining_total)
        stock_scan = stock_scan_result or self.scan_stocks(
            stock_history, snapshot, account_value, name_map
        )
        stock_rows = stock_scan.candidates
        if self.config.enable_stock_enhancement and remaining_stock > 0:
            selected_stock_count = 0
            for row in stock_rows:
                if (
                    self.config.max_stock_count is not None
                    and selected_stock_count >= self.config.max_stock_count
                ):
                    break
                weight = min(
                    self.config.max_single_stock,
                    remaining_stock,
                    remaining_total,
                )
                if weight <= 0:
                    break
                if float(row["price"]) * 100 > account_value * weight:
                    continue
                positions.append(
                    TargetPosition(
                        code=str(row["code"]),
                        name=str(row["name"]),
                        asset_type="stock",
                        target_weight=round(weight, 6),
                        reason=(
                            "MAINBOARD_EP_SIZE_LOW_VOL "
                            f"ep={row['earnings_yield']:.2%} "
                            f"vol120={row['annual_volatility_120d']:.2%} "
                            f"score={row['score']:.3f}"
                        ),
                    )
                )
                remaining_stock -= weight
                remaining_total -= weight
                selected_stock_count += 1

        exposure = sum(position.target_weight for position in positions)
        fallback = ""
        if self.config.enable_stock_enhancement and not any(
            position.asset_type == "stock" for position in positions
        ):
            fallback = "无合格主板增强标的，保留现金"
        if not positions:
            fallback = "无标的通过绝对趋势和风险过滤，保持全现金"
        return TargetPortfolio(
            account_id=self.config.account_id,
            strategy_version=self.config.strategy_version,
            signal_date=snapshot.trade_date,
            positions=tuple(positions),
            source_snapshot_hash=snapshot.data_hash,
            cash_weight=round(1 - exposure, 6),
            fallback_reason=fallback,
        )

    def catastrophic_stop_orders(
        self,
        positions: Mapping[str, Mapping[str, Any]],
        current_prices: Mapping[str, float],
        trade_date: str,
    ) -> list[OrderIntent]:
        """生成盘中唯一允许的灾难止损订单，不处理普通技术退出。"""
        normalized_prices: dict[str, float] = {}
        for price_code, price in current_prices.items():
            try:
                normalized_prices[normalized_security_code(price_code)] = float(price)
            except (TypeError, ValueError):
                continue
        orders: list[OrderIntent] = []
        for code, position in positions.items():
            current = float(
                normalized_prices.get(normalized_security_code(code), 0) or 0
            )
            average_cost = float(position.get("avg_cost", 0) or 0)
            if current <= 0 or average_cost <= 0:
                continue
            threshold = (
                self.config.etf_stop_pct if is_etf(code) else self.config.stock_stop_pct
            )
            loss = current / average_cost - 1
            sellable = int(position.get("sellable_qty", position.get("shares", 0)) or 0)
            if loss > -threshold or sellable <= 0:
                continue
            orders.append(
                OrderIntent(
                    account_id=self.config.account_id,
                    strategy_version=self.config.strategy_version,
                    signal_date=trade_date,
                    code=code,
                    action="sell",
                    price=current,
                    shares=sellable,
                    name=str(position.get("name", code)),
                    strategy=self.name,
                    strategy_tag="robust_v2",
                    reason="CATASTROPHIC_STOP_LOSS",
                    date=trade_date,
                    source="robust_v2_monitor",
                    metadata={"loss_pct": loss, "threshold": threshold},
                )
            )
        return orders
