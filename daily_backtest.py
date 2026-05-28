"""
每日模拟盘回测 - 自包含版本
绕过不匹配的模块接口，直接用 BaoStock + 简单策略回测
"""
import baostock as bs
import pandas as pd
import numpy as np
import json
import os
from datetime import datetime

# ==================== 配置 ====================
INITIAL_CAPITAL = 10000.0
COMMISSION_RATE = 0.0003
COMMISSION_MIN = 5.0
STAMP_TAX_RATE = 0.0005  # 仅卖出
LOT_SIZE = 100

PICKS = [
    ("601988", "中国银行"),
    ("600519", "贵州茅台"),
    ("000858", "五粮液"),
    ("601318", "中国平安"),
    ("000651", "格力电器"),
    ("000725", "京东方A"),
    ("002415", "海康威视"),
    ("300750", "宁德时代"),
]

START = "2025-01-01"
END = "2026-05-26"

STATE_FILE = os.path.join(os.path.dirname(__file__), "data", "daily_backtest_state.json")


def get_data(code, start, end):
    """从 BaoStock 获取日线数据"""
    prefix = "sh" if code.startswith("6") else "sz"
    bs_code = f"{prefix}.{code}"
    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,open,high,low,close,volume,amount",
        start_date=start, end_date=end,
        frequency="d", adjustflag="3"
    )
    data = []
    while rs.next():
        data.append(rs.get_row_data())
    if not data:
        return None
    df = pd.DataFrame(data, columns=["date","open","high","low","close","volume","amount"])
    for c in ["open","high","low","close","volume","amount"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def calc_commission(amount, is_sell=False):
    """计算交易成本"""
    comm = max(amount * COMMISSION_RATE, COMMISSION_MIN)
    stamp = amount * STAMP_TAX_RATE if is_sell else 0
    return round(comm + stamp, 2)


def rsi_signal(df, period=14, oversold=30, overbought=70):
    """RSI 策略信号"""
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    df = df.copy()
    df["rsi"] = rsi
    df["signal"] = "hold"
    df.loc[df["rsi"] < oversold, "signal"] = "buy"
    df.loc[df["rsi"] > overbought, "signal"] = "sell"
    return df


def ma_cross_signal(df, short=5, long=20):
    """均线交叉策略信号"""
    df = df.copy()
    df["ma_short"] = df["close"].rolling(short).mean()
    df["ma_long"] = df["close"].rolling(long).mean()
    df["signal"] = "hold"
    # 金叉买入
    df.loc[(df["ma_short"] > df["ma_long"]) & (df["ma_short"].shift(1) <= df["ma_long"].shift(1)), "signal"] = "buy"
    # 死叉卖出
    df.loc[(df["ma_short"] < df["ma_long"]) & (df["ma_short"].shift(1) >= df["ma_long"].shift(1)), "signal"] = "sell"
    return df


def backtest_single(df, signal_func, name="", code=""):
    """单只股票回测"""
    df = signal_func(df)
    df = df.dropna().reset_index(drop=True)

    cash = INITIAL_CAPITAL
    shares = 0
    avg_cost = 0
    trades = []
    daily_values = []

    for _, row in df.iterrows():
        price = row["close"]
        date_str = row["date"].strftime("%Y-%m-%d")
        signal = row["signal"]

        if signal == "buy" and shares == 0:
            # 全仓买入（整手）
            max_shares = int(cash / (price * (1 + COMMISSION_RATE) + price * STAMP_TAX_RATE)) // LOT_SIZE * LOT_SIZE
            if max_shares >= LOT_SIZE:
                cost = calc_commission(price * max_shares, is_sell=False)
                total = price * max_shares + cost
                if total <= cash:
                    cash -= total
                    shares = max_shares
                    avg_cost = price
                    trades.append({"date": date_str, "action": "买入", "price": price, "shares": shares, "cost": cost})

        elif signal == "sell" and shares > 0:
            # 全仓卖出
            revenue = price * shares
            cost = calc_commission(revenue, is_sell=True)
            cash += revenue - cost
            profit = (price - avg_cost) * shares - cost
            trades.append({"date": date_str, "action": "卖出", "price": price, "shares": shares, "cost": cost, "profit": profit})
            shares = 0
            avg_cost = 0

        total_value = cash + shares * price
        daily_values.append({"date": date_str, "value": total_value})

    # 最终指标
    final_value = daily_values[-1]["value"] if daily_values else INITIAL_CAPITAL
    total_return = (final_value - INITIAL_CAPITAL) / INITIAL_CAPITAL
    values = [d["value"] for d in daily_values]
    peak = values[0]
    max_dd = 0
    for v in values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # 基准收益（买入持有）
    first_price = df["close"].iloc[0]
    last_price = df["close"].iloc[-1]
    benchmark_return = (last_price - first_price) / first_price

    # 最近信号
    recent = df.tail(5)
    last_signal = "无"
    last_signal_date = ""
    for _, row in recent.iterrows():
        if row["signal"] != "hold":
            last_signal = row["signal"]
            last_signal_date = row["date"].strftime("%Y-%m-%d")

    return {
        "name": name,
        "code": code,
        "final_value": round(final_value, 2),
        "return_pct": round(total_return * 100, 2),
        "benchmark_pct": round(benchmark_return * 100, 2),
        "excess_pct": round((total_return - benchmark_return) * 100, 2),
        "max_drawdown": round(max_dd * 100, 2),
        "trade_count": len(trades),
        "last_signal": last_signal,
        "last_signal_date": last_signal_date,
        "trades": trades,
        "last_5_days": recent[["date","close","signal"]].to_dict("records"),
    }


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    print("=" * 60)
    print("📊 A股量化模拟盘 · 每日回测")
    print(f"📅 日期: {END}  初始资金: ¥{INITIAL_CAPITAL:,.2f}")
    print("=" * 60)

    bs.login()
    all_results = []

    for code, name in PICKS:
        try:
            df = get_data(code, START, END)
            if df is None or len(df) < 30:
                print(f"  ⚠️ {name}({code}) 数据不足，跳过")
                continue
        except Exception as e:
            print(f"  ⚠️ {name}({code}) 获取失败: {e}")
            continue

        # RSI 策略
        r = backtest_single(df, lambda d: rsi_signal(d, 14, 30, 70), name, code)
        r["strategy"] = "RSI(14,30,70)"
        all_results.append(r)

        # MA 交叉策略
        r2 = backtest_single(df, lambda d: ma_cross_signal(d, 5, 20), name, code)
        r2["strategy"] = "MA(5/20)"
        all_results.append(r2)

    bs.logout()

    # 排序
    all_results.sort(key=lambda x: x["return_pct"], reverse=True)

    # 打印汇总
    print(f"\n{'='*60}")
    print("📊 全部结果汇总（按收益排序）")
    print(f"{'='*60}")
    print(f"{'股票':<16} {'策略':<14} {'期末资金':>10} {'收益':>8} {'超额':>8} {'回撤':>8} {'信号':>6}")
    print("-" * 76)

    for r in all_results:
        sig = f"{r['last_signal']}" if r['last_signal'] != '无' else '-'
        print(f"{r['name']:<14} {r['strategy']:<14} ¥{r['final_value']:>8,.2f} {r['return_pct']:>+7.2f}% {r['excess_pct']:>+7.2f}% {r['max_drawdown']:>7.2f}%  {sig:>5}")

    # 最佳
    best = all_results[0]
    print(f"\n🏆 最佳组合: {best['name']} + {best['strategy']}  收益: {best['return_pct']:+.2f}%")

    # 今日信号
    print(f"\n{'='*60}")
    print("📡 最近策略信号")
    print(f"{'='*60}")
    has_signal = False
    for r in all_results:
        if r["last_signal"] != "无":
            has_signal = True
            emoji = "🟢" if r["last_signal"] == "buy" else "🔴"
            print(f"  {emoji} {r['name']}({r['code']}) [{r['strategy']}] {r['last_signal'].upper()} @ {r['last_signal_date']}")

    if not has_signal:
        print("  无近期信号")

    # 保存状态
    state = load_state()
    state[END] = {
        "timestamp": datetime.now().isoformat(),
        "results": [{k: v for k, v in r.items() if k not in ("trades", "last_5_days")} for r in all_results],
    }
    save_state(state)

    return all_results


if __name__ == "__main__":
    results = main()
