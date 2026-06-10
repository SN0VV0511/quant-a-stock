"""组合策略回测器:复现线上"全市场扫描动量选股 + ComboSignal 择时"决策链。

线上主链路(``live_runner.py``)此前没有任何历史回测路径,导致实盘策略的夏普、
回撤、胜率不可知、无法调参。本模块用与线上**完全相同的组件**(选股打分
``score_candidates``、择时 ``ComboSignalStrategy``、风控 ``RiskController``、
撮合与成本 ``PositionManager`` + ``TradingRules``)复现逐日决策,使该策略可被回测。

关键设计:消除未来函数(look-ahead bias)
    - 信号在 **T 日收盘** 基于"截至 T 日"的历史切片生成;
    - 成交在 **T+1 日开盘价** 撮合(实盘无法用当日收盘信号当日收盘价成交)。

核心 ``run()`` 是纯函数(只依赖传入的历史数据与交易日列表,无网络/IO),便于单测;
``run_with_loader()`` 负责通过 ``DataLoader`` 拉取历史后调用 ``run()``。

回测近似说明:线上是对全市场约 5000 只逐日实时扫描,回测无法重放历史全市场实时
行情,因此用一个固定的较大股票池(如指数成分股)作为 universe,并统一使用 BaoStock
前复权历史口径。这是有意的近似,不影响对"选股+择时"逻辑有效性的验证。
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from config.settings import (
    INITIAL_CAPITAL,
    MAX_SINGLE_STOCK,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
)
from backtest.metrics import compute_performance_metrics, format_summary
from risk.control import RiskController
from rules.position import PositionManager
from strategies.combo_signal import ComboSignalStrategy
from strategies.exit_rules import evaluate_position_exit
from strategies.indicators import calculate_atr
from strategies.market_regime import is_risk_on
from strategies.market_scanner import score_candidates

logger = logging.getLogger(__name__)

# 买入策略名:含"全市场扫描"以绕过固定白名单,与线上保持一致
_BUY_STRATEGY = "全市场扫描+组合策略"


def _norm_date(value: Any) -> str:
    """归一化日期为 YYYYMMDD 字符串。"""
    s = str(value).strip().replace("-", "")
    return s[:8]


def _holding_days(buy_date: Any, today: str) -> int:
    """计算持仓自然日数(用于时间止损),解析失败返回 0。"""
    if not buy_date:
        return 0
    try:
        d0 = datetime.strptime(_norm_date(buy_date), "%Y%m%d")
        d1 = datetime.strptime(_norm_date(today), "%Y%m%d")
        return max(0, (d1 - d0).days)
    except (ValueError, TypeError):
        return 0


class PortfolioBacktester:
    """组合策略回测器。"""

    def __init__(
        self,
        initial_capital: float = INITIAL_CAPITAL,
        top_n: int = 30,
        momentum_period: int = 60,
        rebalance_every: int = 1,
        max_single_stock: float = MAX_SINGLE_STOCK,
        stop_loss_pct: float = STOP_LOSS_PCT,
        take_profit_pct: float = TAKE_PROFIT_PCT,
    ) -> None:
        """
        Args:
            initial_capital: 初始资金。
            top_n: 候选池规模。
            momentum_period: 动量周期(交易日)。
            rebalance_every: 每隔多少个交易日重算候选池(1=每日)。
            max_single_stock: 单票仓位上限。
            stop_loss_pct: 固定止损线。
            take_profit_pct: 止盈触发线(配合跌破 MA20)。
        """
        self.initial_capital = initial_capital
        self.top_n = top_n
        self.momentum_period = momentum_period
        self.rebalance_every = max(1, rebalance_every)
        self.max_single_stock = max_single_stock
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.combo = ComboSignalStrategy()

    # ==================== 纯函数回测核心 ====================

    def run(
        self,
        history_map: dict[str, pd.DataFrame],
        trading_days: list[str],
        index_history: pd.DataFrame | None = None,
    ) -> dict[str, Any]:
        """对给定历史数据与交易日列表运行回测(纯函数,无 IO)。

        Args:
            history_map: ``{code: DataFrame}``,DataFrame 需含
                ``date, open, high, low, close, volume`` 列(前复权口径,date 为 YYYYMMDD 或带横线)。
            trading_days: 升序交易日列表(YYYYMMDD)。
            index_history: 基准指数历史(含 date/close),用于大盘择时;
                None 或未启用择时时全程视为 risk-on。

        Returns:
            dict: 绩效指标 + ``daily_values`` / ``trades`` 明细。
        """
        if not trading_days:
            return {}

        frames = self._index_frames(history_map)
        trading_days = [_norm_date(d) for d in trading_days]
        index_frame = self._index_frames({"_INDEX_": index_history}).get("_INDEX_") if index_history is not None else None

        portfolio = self._new_portfolio()
        risk = RiskController()

        daily_values: list[dict[str, Any]] = []
        all_trades: list[dict[str, Any]] = []
        risk_events: list[dict[str, Any]] = []

        pending_orders: list[dict[str, Any]] = []
        self._last_candidates = []  # 实例级候选缓存,_build_orders 维护

        for i, date in enumerate(trading_days):
            close_today = self._prices_on(frames, date, "close")
            open_today = self._prices_on(frames, date, "open")
            prev_date = trading_days[i - 1] if i > 0 else None
            prev_close = self._prices_on(frames, prev_date, "close") if prev_date else {}

            # 当日开盘前记录日初市值(供单日亏损风控)
            risk.set_daily_start(portfolio, date=date)

            # 1) 执行昨日生成的订单:T+1 开盘价撮合
            if pending_orders:
                self._execute_orders(
                    pending_orders, portfolio, risk, open_today, close_today,
                    prev_close, date, all_trades, risk_events,
                )
                pending_orders = []

            # 2) 按收盘价盯市并记录净值
            portfolio.update_prices(close_today, date=date)
            portfolio.save_snapshot(date, close_today)  # 更新峰值,供回撤风控
            total_value = portfolio.get_total_value(close_today)
            daily_values.append({
                "date": date,
                "total_value": total_value,
                "cash": portfolio.get_cash(),
                "position_count": len(portfolio.get_all_positions()),
            })

            # 3) T 日收盘生成信号,排队到 T+1 执行(最后一天无需生成)
            if i < len(trading_days) - 1:
                pending_orders = self._build_orders(
                    frames, date, close_today, portfolio, i, index_frame,
                )

        self._enrich_post_exit_prices(all_trades, frames)
        results = compute_performance_metrics(
            daily_values, all_trades, risk_events, self.initial_capital
        )
        results["daily_values"] = daily_values
        results["trades"] = all_trades
        results["risk_events_detail"] = risk_events
        self._cleanup(portfolio)
        return results

    # ==================== 信号生成 ====================

    def _build_orders(
        self, frames: dict, date: str, close_today: dict[str, float],
        portfolio: PositionManager, day_index: int, index_frame: dict | None,
    ) -> list[dict[str, Any]]:
        """生成 T+1 待执行订单(默认:动量选股 + Combo 择时)。

        子类可重写此方法实现其他策略(如周度因子调仓),其余执行/风控/计价
        逻辑完全复用,保证 A/B 对比公平。
        """
        if day_index % self.rebalance_every == 0 or not self._last_candidates:
            self._last_candidates = self._rank_candidates(frames, date)
        risk_on = self._regime_on(index_frame, date)
        return self._generate_signals(
            frames, date, close_today, portfolio, self._last_candidates, risk_on,
        )

    def _regime_on(self, index_frame: dict | None, date: str) -> bool:
        """根据截至 ``date`` 的指数切片判断大盘择时状态。

        未启用择时或无指数数据时返回 True(全程放行)。
        """
        from config.settings import ENABLE_MARKET_REGIME, MARKET_REGIME_MA

        if not ENABLE_MARKET_REGIME or index_frame is None:
            return True
        sl = self._slice(index_frame, date)
        if sl is None:
            return True
        return is_risk_on(sl, ma_period=MARKET_REGIME_MA)

    def _rank_candidates(self, frames: dict, date: str) -> list[str]:
        """用截至 ``date`` 的历史切片做横截面动量排序,返回候选代码。"""
        sliced = {}
        name_map = {}
        for code, fr in frames.items():
            sl = self._slice(fr, date)
            if sl is not None and len(sl) >= self.momentum_period + 1:
                sliced[code] = sl
                name_map[code] = fr["name"]
        ranked = score_candidates(
            sliced, top_n=self.top_n, momentum_period=self.momentum_period, name_map=name_map
        )
        return [r["code"] for r in ranked]

    def _generate_signals(
        self,
        frames: dict,
        date: str,
        close_today: dict[str, float],
        portfolio: PositionManager,
        candidates: list[str],
        risk_on: bool = True,
    ) -> list[dict[str, Any]]:
        """T 日收盘生成买卖意图(不含价格/股数,T+1 执行时再定)。

        Args:
            risk_on: 大盘择时状态。False 时只生成卖出,不开新仓。
        """
        orders: list[dict[str, Any]] = []
        positions = portfolio.get_all_positions()
        held = set(positions.keys())

        # 持仓退出:与线上 _handle_position_exits 共用 evaluate_exit 同优先级
        for code, pos in positions.items():
            price = close_today.get(code)
            avg_cost = float(pos.get("avg_cost", 0) or 0)
            if not price or price <= 0 or avg_cost <= 0:
                continue
            peak_price = float(pos.get("peak_price", avg_cost) or avg_cost)
            holding_days = _holding_days(pos.get("buy_date"), date)
            exit_reason = self._exit_reason(
                frames,
                code,
                date,
                price,
                avg_cost,
                peak_price,
                holding_days,
                str(pos.get("strategy_tag", "combo_trend")),
                portfolio.get_sellable_qty(code, date),
            )
            if exit_reason:
                orders.append({
                    "code": code, "action": "sell", "name": pos.get("name", code),
                    "strategy": "分层退出策略",
                    "strategy_tag": pos.get("strategy_tag", "combo_trend"),
                    "reason": exit_reason.sell_reason,
                    "exit_detail": exit_reason.detail,
                })

        sell_codes = {o["code"] for o in orders}

        # 大盘择时:风险关闭时只卖不买,过滤系统性下跌期的开仓
        if not risk_on:
            return orders

        # 候选买入:未持仓且通过 Combo 买入信号
        for code in candidates:
            if code in held or code in sell_codes:
                continue
            sl = self._slice(frames[code], date)
            if sl is None or len(sl) < 25:
                continue
            sig = self.combo.check_realtime(sl)
            if sig.get("signal") == "buy":
                strategy_tag = "momentum_breakout" if "动量追涨" in str(sig.get("reason", "")) else "combo_trend"
                close = pd.to_numeric(sl["close"], errors="coerce").dropna()
                if len(close) >= 2 and float(close.iloc[-2]) > 0 and float(close.iloc[-1]) / float(close.iloc[-2]) - 1 >= 0.095:
                    strategy_tag = "limitup_follow"
                orders.append({
                    "code": code, "action": "buy", "name": frames[code]["name"],
                    "strategy": _BUY_STRATEGY,
                    "strategy_tag": strategy_tag,
                    "reason": str(sig.get("reason", "")),
                })
        return orders

    def _exit_reason(
        self, frames: dict, code: str, date: str, price: float, avg_cost: float,
        peak_price: float,
        holding_days: int,
        strategy_tag: str,
        sellable_qty: int,
    ):
        """判断持仓是否触发退出，复用共享分层退出规则。"""
        sl = self._slice(frames[code], date)
        combo_sell = False
        combo_reason = ""
        ma20 = None
        ma60 = None
        atr = None
        rsi = None
        if sl is not None and len(sl) >= 25:
            sig = self.combo.check_realtime(sl)
            combo_sell = sig.get("signal") == "sell"
            combo_reason = str(sig.get("reason", "策略信号"))
            rsi = float(sig["rsi"]) if sig.get("rsi") is not None else None
        if sl is not None and len(sl) >= 20:
            ma20 = float(pd.to_numeric(sl["close"], errors="coerce").tail(20).mean())
        if sl is not None and len(sl) >= 60:
            ma60 = float(pd.to_numeric(sl["close"], errors="coerce").tail(60).mean())
        if sl is not None and {"high", "low", "close"} <= set(sl.columns):
            from config.settings import ATR_PERIOD

            atr_value = calculate_atr(sl, period=ATR_PERIOD).iloc[-1]
            atr = float(atr_value) if pd.notna(atr_value) else None

        decision = evaluate_position_exit(
            avg_cost=avg_cost,
            price=price,
            strategy_tag=strategy_tag,
            sellable_qty=sellable_qty,
            highest_price=peak_price,
            intraday_high_price=float(sl["high"].iloc[-1]) if sl is not None and "high" in sl.columns else price,
            ma20=ma20,
            ma60=ma60,
            atr=atr,
            rsi=rsi,
            holding_days=holding_days,
            combo_sell=combo_sell,
            combo_reason=combo_reason,
        )
        return decision if decision.should_sell else None

    # ==================== 订单执行 ====================

    def _execute_orders(
        self,
        orders: list[dict[str, Any]],
        portfolio: PositionManager,
        risk: RiskController,
        open_today: dict[str, float],
        close_today: dict[str, float],
        prev_close: dict[str, float],
        date: str,
        all_trades: list,
        risk_events: list,
    ) -> None:
        """以 T+1 开盘价撮合订单(经风控过滤)。

        先处理卖出再处理买入:卖出释放现金后再按可用现金为买入计算股数,
        避免调仓日"卖出未结算导致买入资金不足"的失真(对周度全调仓尤其重要)。
        """
        sells = [o for o in orders if o["action"] == "sell"]
        buys = [o for o in orders if o["action"] == "buy"]

        self._execute_batch(sells, portfolio, risk, open_today, close_today,
                            prev_close, date, all_trades, risk_events)
        self._execute_batch(buys, portfolio, risk, open_today, close_today,
                            prev_close, date, all_trades, risk_events)

    def _execute_batch(
        self,
        orders: list[dict[str, Any]],
        portfolio: PositionManager,
        risk: RiskController,
        open_today: dict[str, float],
        close_today: dict[str, float],
        prev_close: dict[str, float],
        date: str,
        all_trades: list,
        risk_events: list,
    ) -> None:
        """撮合同方向一批订单(买/卖)。"""
        if not orders:
            return
        priced: list[dict[str, Any]] = []
        for o in orders:
            code = o["code"]
            exec_price = open_today.get(code) or close_today.get(code)
            if not exec_price or exec_price <= 0:
                continue  # 当日无行情,放弃该单
            order = dict(o)
            order["price"] = float(exec_price)
            order["date"] = date
            if order["action"] == "sell":
                pos = portfolio.get_position(code)
                if not pos:
                    continue
                order["shares"] = portfolio.get_sellable_qty(code, date)
            else:
                total_value = portfolio.get_total_value(open_today)
                order["shares"] = portfolio.rules.calc_lot_size(
                    exec_price, portfolio.get_cash(),
                    max_ratio=self.max_single_stock, total_value=total_value,
                )
                if order["shares"] <= 0:
                    continue
            priced.append(order)

        market_data = {
            o["code"]: {
                "current_price": o["price"],
                "prev_close": prev_close.get(o["code"]),
                "is_st": False,
                "is_suspended": False,
            }
            for o in priced
        }

        approved, rejected = risk.filter_orders(priced, portfolio, market_data)
        for rej in rejected:
            risk_events.append({
                "date": date, "code": rej["order"]["code"],
                "action": rej["order"]["action"], "reason": rej["reason"],
            })

        for order in approved:
            code, price = order["code"], order["price"]
            if order["action"] == "buy":
                res = portfolio.buy(code, order.get("name", code), price,
                                    order["shares"], date, order.get("strategy", "backtest"),
                                    strategy_tag=order.get("strategy_tag", "combo_trend"),
                                    trigger_reason=order.get("reason", ""))
                if res.get("success"):
                    all_trades.append({
                        "date": date, "code": code, "action": "buy",
                        "price": price, "shares": res["shares"],
                        "cost": res["cost_detail"]["total"],
                        "amount": round(price * res["shares"], 2),
                        "strategy_tag": order.get("strategy_tag", "combo_trend"),
                    })
            else:
                position_before_sell = dict(portfolio.get_position(code) or {})
                res = portfolio.sell(code, price, order["shares"], date,
                                     order.get("strategy", "backtest"),
                                     sell_reason=order.get("reason", ""),
                                     indicators={"detail": order.get("exit_detail", "")})
                if res.get("success"):
                    all_trades.append({
                        "date": date, "code": code, "action": "sell",
                        "price": price, "shares": res["shares"],
                        "profit": res["profit"], "cost": res["cost_detail"]["total"],
                        "amount": round(price * res["shares"], 2),
                        "strategy_tag": position_before_sell.get("strategy_tag", "combo_trend"),
                        "sell_reason": order.get("reason", ""),
                        "holding_days": _holding_days(position_before_sell.get("buy_date"), date),
                    })

    # ==================== 数据辅助 ====================

    @staticmethod
    def _index_frames(history_map: dict[str, pd.DataFrame]) -> dict[str, dict]:
        """预处理:每个代码归一化日期、排序,并建立 date->行 的索引以便切片。"""
        frames = {}
        for code, df in history_map.items():
            if df is None or df.empty or "close" not in df.columns:
                continue
            d = df.copy()
            d["date"] = d["date"].map(_norm_date)
            d = d.sort_values("date").reset_index(drop=True)
            pos = {dt: i for i, dt in enumerate(d["date"].tolist())}
            frames[code] = {
                "df": d,
                "pos": pos,
                "name": str(df["name"].iloc[0]) if "name" in df.columns and len(df) else code,
            }
        return frames

    @staticmethod
    def _slice(frame: dict, date: str) -> pd.DataFrame | None:
        """返回截至 ``date``(含)的历史切片;``date`` 当日无数据则返回 None。"""
        idx = frame["pos"].get(date)
        if idx is None:
            return None
        return frame["df"].iloc[: idx + 1]

    @staticmethod
    def _prices_on(frames: dict, date: str | None, col: str) -> dict[str, float]:
        """取某交易日所有代码的指定价格列。"""
        if date is None:
            return {}
        out: dict[str, float] = {}
        for code, fr in frames.items():
            idx = fr["pos"].get(date)
            if idx is None:
                continue
            val = fr["df"].iloc[idx].get(col)
            if pd.notna(val) and float(val) > 0:
                out[code] = float(val)
        return out

    @staticmethod
    def _enrich_post_exit_prices(all_trades: list[dict[str, Any]], frames: dict) -> None:
        """补充卖出后三个交易日行情，供卖飞率和止损有效率统计。"""
        for trade in all_trades:
            if trade.get("action") != "sell" or trade.get("code") not in frames:
                continue
            frame = frames[trade["code"]]
            index = frame["pos"].get(_norm_date(trade.get("date", "")))
            if index is None:
                continue
            future = frame["df"].iloc[index + 1:index + 4]
            if future.empty:
                continue
            high_col = "high" if "high" in future.columns else "close"
            trade["post_sell_3d_high"] = float(pd.to_numeric(future[high_col], errors="coerce").max())
            trade["post_sell_3d_close"] = float(pd.to_numeric(future["close"], errors="coerce").iloc[-1])

    def _new_portfolio(self) -> PositionManager:
        """用临时状态文件创建持仓管理器,避免污染真实虚拟盘状态。"""
        tmp = tempfile.mktemp(suffix=".json")
        pm = PositionManager(
            state_file=tmp,
            trade_log_file=tempfile.mktemp(suffix=".jsonl"),
            snapshot_log_file=tempfile.mktemp(suffix=".jsonl"),
        )
        pm.state["cash"] = self.initial_capital
        pm.state["peak_value"] = self.initial_capital
        pm.save()
        return pm

    @staticmethod
    def _cleanup(portfolio: PositionManager) -> None:
        """清理临时文件。"""
        for path in (portfolio.state_file, portfolio.trade_log_file, portfolio.snapshot_log_file):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass

    # ==================== IO 包装 ====================

    def run_with_loader(
        self,
        universe_codes: list[str],
        start_date: str,
        end_date: str,
        data_loader=None,
    ) -> dict[str, Any]:
        """通过 DataLoader 拉取前复权历史后运行回测。"""
        from data.loader import DataLoader

        owns = data_loader is None
        loader = data_loader or DataLoader()
        try:
            start_date = start_date.replace("-", "")
            end_date = end_date.replace("-", "")

            # 提前 180 天用于计算动量/均线指标
            preload_start = (
                datetime.strptime(start_date, "%Y%m%d") - timedelta(days=180)
            ).strftime("%Y%m%d")

            history_map: dict[str, pd.DataFrame] = {}
            for code in universe_codes:
                try:
                    df = loader.get_daily_data(code, preload_start, end_date, adjust_flag="2")
                    if df is not None and not df.empty:
                        df = df.copy()
                        df["name"] = code
                        history_map[code] = df
                except Exception as exc:  # noqa: BLE001 - 单只失败不应中断回测
                    logger.warning("加载历史失败 %s: %s", code, exc)

            # 交易日历:优先用 BaoStock,失败则由已加载历史的日期并集兜底(更稳健)
            trading_days = loader.get_trading_calendar(start_date, end_date)
            if not trading_days:
                logger.warning("交易日历获取失败,改用历史数据日期并集")
                all_dates = set()
                for df in history_map.values():
                    all_dates.update(str(d).replace("-", "")[:8] for d in df["date"])
                trading_days = sorted(d for d in all_dates if start_date <= d <= end_date)
            if not trading_days:
                logger.error("无法确定交易日历且历史为空")
                return {}

            logger.info("回测数据就绪: %d/%d 只,%d 个交易日",
                        len(history_map), len(universe_codes), len(trading_days))

            # 拉取基准指数用于大盘择时(失败则全程 risk-on)
            index_history = None
            from config.settings import ENABLE_MARKET_REGIME, MARKET_INDEX_CODE
            if ENABLE_MARKET_REGIME:
                try:
                    from data.ak_loader import AKDataLoader
                    ak = AKDataLoader()
                    try:
                        index_history = ak.get_index_history(
                            MARKET_INDEX_CODE, start_date=preload_start, end_date=end_date
                        )
                    finally:
                        ak.close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("获取基准指数失败,大盘择时将全程放行: %s", exc)

            return self.run(history_map, trading_days, index_history=index_history)
        finally:
            if owns:
                loader.close()


def save_backtest_result(result, name, window, path=None):
    """将回测结果写入 reports/backtest_latest.json,供 Web 仪表盘展示。

    结构支持多策略 series,便于 A/B 脚本追加对比。
    """
    import os as _os
    import json as _json
    from datetime import datetime as _dt
    from config.settings import REPORT_DIR

    path = path or _os.path.join(REPORT_DIR, "backtest_latest.json")
    metric_keys = ("total_return", "annual_return", "max_drawdown",
                   "sharpe_ratio", "calmar_ratio", "win_rate",
                   "profit_factor", "total_trades")
    series = {
        "name": name,
        "metrics": {k: result.get(k) for k in metric_keys},
        "equity": [{"date": d["date"], "value": d["total_value"]}
                   for d in result.get("daily_values", [])],
    }
    payload = {
        "generated_at": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": window,
        "series": [series],
    }
    _os.makedirs(REPORT_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        _json.dump(payload, f, ensure_ascii=False)


if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # 默认用 config 中的标的池作为回测 universe(可按需替换为指数成分股)
    from config.settings import DEFAULT_UNIVERSE

    universe = [v["raw_code"] for v in DEFAULT_UNIVERSE.values()]
    start = sys.argv[1] if len(sys.argv) > 1 else "20240101"
    end = sys.argv[2] if len(sys.argv) > 2 else "20250101"

    bt = PortfolioBacktester(initial_capital=INITIAL_CAPITAL, top_n=10)
    result = bt.run_with_loader(universe, start, end)
    print(format_summary(result))

    # 落地结果供 Web 仪表盘读取
    if result:
        save_backtest_result(result, name="动量Combo", window=f"{start}~{end}")
