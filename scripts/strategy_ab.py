"""动量策略 vs 小市值价值策略 vs ETF/RPS 轮动 A/B/C 回测对比。

同一份历史数据、同一执行/风控/成本内核,对比三种选股逻辑的表现:
    - 动量:全市场扫描动量选股 + Combo 择时(现有线上策略)
    - 小市值价值:小市值 + 低PB + 短期反转 + 国九条风控,周度调仓
    - ETF/RPS轮动:ETF动量 + 行业动量加权 RPS,日频轮动 top 2

universe 用 BaoStock 全市场股票按步长抽样近似,数据一次性加载并附带 pb / 流通市值 /
ST / 停牌字段,供因子策略使用。

用法: python -m scripts.strategy_ab [universe_size]
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import config.settings as cfg
from backtest.portfolio_backtest import PortfolioBacktester
from backtest.factor_backtest import FactorBacktester
from data.ak_loader import AKDataLoader
from data.loader import DataLoader

logging.basicConfig(level=logging.ERROR, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("strategy_ab")

_EXT_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg,"
    "peTTM,pbMRQ,isST,tradestatus"
)


def _build_universe(size: int) -> list[str]:
    """从 BaoStock 全市场股票按步长抽样,得到一个分散的回测 universe。"""
    ak = AKDataLoader()
    try:
        stocks = ak.get_all_stocks()
    finally:
        ak.close()
    codes = [s["code"] for s in stocks]
    if len(codes) <= size:
        return codes
    step = len(codes) // size
    return codes[::step][:size]


def _load_history(codes, preload_start, end):
    """加载扩展字段历史并附加 pb / 流通市值 / ST / 停牌列。"""
    loader = DataLoader()
    history = {}
    try:
        for code in codes:
            try:
                df = loader.get_daily_data(code, preload_start, end,
                                           adjust_flag="2", fields=_EXT_FIELDS)
                if df is None or df.empty:
                    continue
                df = df.copy()
                for col in ("pbMRQ", "isST", "tradestatus", "turn", "volume", "close"):
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                df["pb"] = df.get("pbMRQ")
                df["is_st"] = df.get("isST", 0)
                # 流通市值(元)= close * 流通股本 = close * volume / (turn/100)
                turn = df["turn"].where(df["turn"] > 0)
                df["mktcap"] = df["close"] * df["volume"] / (turn / 100.0)
                df["name"] = code
                history[code] = df
            except Exception as exc:  # noqa: BLE001
                logger.warning("加载失败 %s: %s", code, exc)
    finally:
        loader.close()
    return history


def _trading_days(history, lo, hi):
    days = set()
    for df in history.values():
        days.update(str(d).replace("-", "")[:8] for d in df["date"])
    return sorted(d for d in days if lo <= d <= hi)


# ==================== ETF/RPS 轮动回测 ====================

_RPS_ETF_CODES = ["510300", "510500", "159915", "588000", "512100",
                  "512880", "512170", "512690", "512660", "512800"]
_RPS_INDUSTRY_NAMES = ["证券", "光伏设备", "电池", "半导体",
                       "软件开发", "通信设备", "医疗服务", "白酒"]
_RPS_LOOKBACK = 60
_RPS_TOP_N = 3
_RPS_COST = 0.0003  # 单边 0.03%


def _load_etf_data(start: str, end: str) -> tuple[dict, dict]:
    """加载 ETF 和行业指数历史日线。返回 (etf_hist, industry_hist)。"""
    loader = AKDataLoader()
    try:
        etf_hist = loader.get_batch_etf_history(_RPS_ETF_CODES, days=9999,
                                                 max_batch=len(_RPS_ETF_CODES))
        # 过滤到窗口内
        for code, df in list(etf_hist.items()):
            df = df[df["date"] >= start]
            df = df[df["date"] <= end]
            if df.empty:
                del etf_hist[code]
            else:
                etf_hist[code] = df.reset_index(drop=True)

        industry_hist = loader.get_batch_industry_index_history(
            _RPS_INDUSTRY_NAMES, days=9999, max_batch=len(_RPS_INDUSTRY_NAMES))
        # AKShare 数据不足时降级到腾讯接口
        if len(industry_hist) < len(_RPS_INDUSTRY_NAMES) * 0.5:
            print(f"  AKShare 行业数据不足 ({len(industry_hist)}/{len(_RPS_INDUSTRY_NAMES)}), 降级到腾讯接口...")
            tencent = loader.get_industry_index_history_tencent(
                _RPS_INDUSTRY_NAMES, days=9999)
            for name, df in tencent.items():
                if name not in industry_hist:
                    industry_hist[name] = df
        for name, df in list(industry_hist.items()):
            df = df[df["date"] >= start]
            df = df[df["date"] <= end]
            if df.empty:
                del industry_hist[name]
            else:
                industry_hist[name] = df.reset_index(drop=True)
    finally:
        loader.close()
    return etf_hist, industry_hist


def _calc_rps(values: list[float]) -> float:
    """计算 RPS: 当前动量在 N 日窗口中的分位(0~100)。"""
    if not values or len(values) < 2:
        return 50.0
    current = values[-1]
    rank = sum(1 for v in values if v <= current)
    return (rank / len(values)) * 100.0


def _run_rps_backtest(etf_hist: dict, industry_hist: dict,
                      days: list[str]) -> dict:
    """ETF/RPS 日频轮动回测。

    每天: 计算 ETF 动量 RPS + 行业动量 RPS 加权,选 top 2 ETF 等权持有。
    按 close 价成交,扣 0.03% 成本。
    """
    import numpy as np

    initial = cfg.INITIAL_CAPITAL
    cash = initial
    holdings: dict[str, dict] = {}  # code -> {shares, cost}
    daily_values: list[dict] = []
    trades: list[dict] = []
    peak = initial
    max_dd = 0.0

    # 收集所有 ETF 日期并建立索引
    all_dates = set()
    for df in etf_hist.values():
        all_dates.update(str(d).replace("-", "")[:8] for d in df["date"])
    sorted_dates = sorted(d for d in all_dates if days[0] <= d <= days[-1])

    for i, date in enumerate(sorted_dates):
        # 获取当天 close
        close_etf = {}
        for code, df in etf_hist.items():
            row = df[df["date"] == date]
            if not row.empty:
                close_etf[code] = float(row.iloc[-1]["close"])

        close_ind = {}
        for name, df in industry_hist.items():
            row = df[df["date"] == date]
            if not row.empty:
                close_ind[name] = float(row.iloc[-1]["close"])

        if len(close_etf) < 3:
            # 数据不足,记录净值
            total = cash + sum(
                h["shares"] * close_etf.get(c, h["cost"])
                for c, h in holdings.items()
            )
            daily_values.append({"date": date, "total_value": total})
            continue

        # 计算 ETF RPS(过去 60 日动量)
        etf_rps = {}
        for code, df in etf_hist.items():
            sub = df[df["date"] <= date].tail(_RPS_LOOKBACK + 1)
            if len(sub) < 20:
                continue
            closes = sub["close"].tolist()
            mom = (closes[-1] / closes[0] - 1) if closes[0] > 0 else 0
            etf_rps[code] = mom

        # 计算行业 RPS
        ind_rps = {}
        for name, df in industry_hist.items():
            sub = df[df["date"] <= date].tail(_RPS_LOOKBACK + 1)
            if len(sub) < 20:
                continue
            closes = sub["close"].tolist()
            mom = (closes[-1] / closes[0] - 1) if closes[0] > 0 else 0
            ind_rps[name] = mom

        # 综合 RPS: ETF 动量 0.6 + 行业动量 0.4
        ind_avg = np.mean(list(ind_rps.values())) if ind_rps else 0
        scores = {}
        for code, mom in etf_rps.items():
            scores[code] = mom * 0.6 + ind_avg * 0.4

        # 选 top 2
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:_RPS_TOP_N]
        target = {code for code, _ in ranked}

        # 调仓
        held = set(holdings)
        # 卖出不在目标的
        for code in held - target:
            if code in close_etf:
                h = holdings.pop(code)
                cash += h["shares"] * close_etf[code] * (1 - _RPS_COST)
                trades.append({"date": date, "code": code, "action": "sell",
                               "price": close_etf[code], "shares": h["shares"]})

        # 买入目标但未持有的
        if target:
            per_etf_cash = (cash + sum(
                h["shares"] * close_etf.get(c, h["cost"])
                for c, h in holdings.items()
            )) / len(target)
            for code in target - held:
                if code in close_etf and close_etf[code] > 0:
                    shares = int(per_etf_cash / close_etf[code] / 100) * 100
                    if shares >= 100:
                        cost = shares * close_etf[code] * (1 + _RPS_COST)
                        if cost <= cash:
                            cash -= cost
                            holdings[code] = {"shares": shares, "cost": close_etf[code]}
                            trades.append({"date": date, "code": code, "action": "buy",
                                           "price": close_etf[code], "shares": shares})

        # 计算当日净值
        total = cash + sum(
            h["shares"] * close_etf.get(c, h["cost"])
            for c, h in holdings.items()
        )
        peak = max(peak, total)
        dd = (peak - total) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
        daily_values.append({"date": date, "total_value": total})

    # 计算指标
    total_return = (daily_values[-1]["total_value"] / initial - 1) if daily_values else 0
    n_days = len(daily_values)
    annual_return = (1 + total_return) ** (252 / max(n_days, 1)) - 1 if n_days > 0 else 0

    returns = []
    for j in range(1, len(daily_values)):
        r = daily_values[j]["total_value"] / daily_values[j - 1]["total_value"] - 1
        returns.append(r)
    avg_ret = np.mean(returns) if returns else 0
    std_ret = np.std(returns, ddof=1) if len(returns) > 1 else 0.0001
    sharpe = (avg_ret / std_ret * np.sqrt(252)) if std_ret > 0 else 0
    calmar = abs(annual_return / max_dd) if max_dd > 0 else 0

    wins = sum(1 for r in returns if r > 0)
    win_rate = wins / len(returns) if returns else 0

    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "max_drawdown": max_dd,
        "sharpe_ratio": sharpe,
        "calmar_ratio": calmar,
        "win_rate": win_rate,
        "profit_factor": 0,
        "total_trades": len(trades),
        "daily_values": daily_values,
    }


# ==================== 输出 ====================


def _row(label, r):
    cols = [
        (label, "{}", None),
        (r.get("total_return", 0), "{:+.2%}", None),
        (r.get("max_drawdown", 0), "{:.2%}", None),
        (r.get("sharpe_ratio", 0), "{:.2f}", None),
        (r.get("calmar_ratio", 0), "{:.2f}", None),
        (r.get("win_rate", 0), "{:.1%}", None),
        (r.get("total_trades", 0), "{}", None),
    ]
    return " | ".join(f"{fmt.format(v):>10}" for v, fmt, _ in cols)


def _print_window(title, history, days, etf_hist=None, industry_hist=None):
    print(f"\n=== {title}（{len(days)} 个交易日） ===")
    print(" | ".join(f"{h:>10}" for h in
                      ["策略", "总收益", "最大回撤", "夏普", "卡尔玛", "胜率", "交易数"]))
    print("-" * 80)
    mom = PortfolioBacktester(initial_capital=cfg.INITIAL_CAPITAL, top_n=10).run(history, days)
    fac = FactorBacktester(initial_capital=cfg.INITIAL_CAPITAL).run(history, days)
    rps = None
    if etf_hist and industry_hist:
        rps = _run_rps_backtest(etf_hist, industry_hist, days)
    print(_row("动量Combo", mom))
    print(_row("小市值价值", fac))
    if rps:
        print(_row("ETF/RPS轮动", rps))
    return mom, fac, rps


def _save_ab(title, mom, fac, rps=None):
    """将三条曲线写入 reports/backtest_latest.json 供仪表盘展示。"""
    import json
    from datetime import datetime as _dt

    def series(name, r):
        keys = ("total_return", "annual_return", "max_drawdown", "sharpe_ratio",
                "calmar_ratio", "win_rate", "profit_factor", "total_trades")
        return {
            "name": name,
            "metrics": {k: r.get(k) for k in keys},
            "equity": [{"date": d["date"], "value": d["total_value"]}
                       for d in r.get("daily_values", [])],
        }

    all_series = [series("动量Combo", mom), series("小市值价值", fac)]
    if rps:
        all_series.append(series("ETF/RPS轮动", rps))

    payload = {
        "generated_at": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": title,
        "series": all_series,
    }
    path = os.path.join(cfg.REPORT_DIR, "backtest_latest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"\n已写入仪表盘回测数据: {path}")


def main():
    size = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    cfg.ENABLE_MARKET_REGIME = False  # A/B 基线统一关闭择时

    # 回测窗口滚动到当前日期
    today = datetime.now().strftime("%Y%m%d")
    full_lo = (datetime.now() - timedelta(days=730)).strftime("%Y%m%d")  # 近2年
    full_hi = today
    # 近期窗口：最近 6 个月
    recent_lo = (datetime.now() - timedelta(days=180)).strftime("%Y%m%d")

    print(f"抽样 universe (目标 {size} 只)...")
    codes = _build_universe(size)
    print(f"universe: {len(codes)} 只,加载扩展历史(含估值/市值/ST)...")

    preload = (datetime.strptime(full_lo, "%Y%m%d") - timedelta(days=200)).strftime("%Y%m%d")
    history = _load_history(codes, preload, full_hi)
    print(f"数据就绪: {len(history)}/{len(codes)} 只")

    # 加载 ETF/行业数据
    print("加载 ETF/行业指数数据...")
    etf_hist, industry_hist = _load_etf_data(full_lo, full_hi)
    print(f"ETF: {len(etf_hist)}只 行业: {len(industry_hist)}个")

    full_title = f"全窗口 {full_lo[:4]}-{full_lo[4:6]}~{full_hi[:4]}-{full_hi[4:6]}"
    _print_window(full_title, history,
                  _trading_days(history, full_lo, full_hi),
                  etf_hist, industry_hist)
    recent_days = _trading_days(history, recent_lo, full_hi)
    recent_title = f"近期窗口 {recent_lo[:4]}-{recent_lo[4:6]}~{full_hi[:4]}-{full_hi[4:6]}"
    mom, fac, rps = _print_window(recent_title, history, recent_days,
                                   etf_hist, industry_hist)
    _save_ab(recent_title, mom, fac, rps)


if __name__ == "__main__":
    main()
