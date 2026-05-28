"""
虚拟盘批量回测 - 1万本金，多只股票
"""
import sys, os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from data.data_loader import get_stock_data
from strategies.rsi import RSIStrategy
from strategies.ma_cross import MACrossStrategy
from backtest.engine import BacktestEngine
from tabulate import tabulate

PICKS = [
    ("000001", "平安银行"),
    ("601166", "兴业银行"),
    ("601988", "中国银行"),
    ("000725", "京东方A"),
]

STRATEGIES = {
    "RSI14": RSIStrategy(period=14, oversold=30, overbought=70),
    "RSI10": RSIStrategy(period=10, oversold=25, overbought=75),
    "MA5/20": MACrossStrategy(short_window=5, long_window=20),
}

START = "20250101"
END = "20260526"


def main():
    print("=" * 70)
    print("📊 虚拟盘回测 · 1万本金 · 2025.1 至今")
    print("=" * 70)

    all_results = []

    for code, name in PICKS:
        print(f"\n{'='*50}")
        print(f"📈 {name} ({code})")
        print(f"{'='*50}")

        try:
            df = get_stock_data(code, START, END, adjust="3")
        except Exception as e:
            print(f"  ⚠️ 数据获取失败: {e}")
            continue

        for strat_name, strategy in STRATEGIES.items():
            try:
                sig_df = strategy.calculate_signals(df)
                engine = BacktestEngine(initial_capital=10000.0)
                result_df, summary = engine.run(sig_df)

                final = float(summary["期末资金"].replace("¥","").replace(",",""))
                ret = float(summary["策略收益"].replace("%",""))
                bench = float(summary["基准收益"].replace("%",""))
                dd = float(summary["最大回撤"].replace("%",""))

                all_results.append({
                    "股票": f"{name}({code})",
                    "策略": strat_name,
                    "期末资金": f"¥{final:,.2f}",
                    "收益": f"{ret:+.2f}%",
                    "基准": f"{bench:+.2f}%",
                    "超额": f"{ret-bench:+.2f}%",
                    "回撤": f"{dd:.2f}%",
                })

                buy_cnt = int(summary["买入次数"].replace(" 次",""))
                sell_cnt = int(summary["卖出次数"].replace(" 次",""))
                print(f"\n  [{strat_name}] 买{buy_cnt}次 卖{sell_cnt}次")
                print(f"    期末: ¥{final:,.2f}  收益: {ret:+.2f}%  超额: {ret-bench:+.2f}%")

            except Exception as e:
                print(f"  [{strat_name}] ⚠️ 回测失败: {e}")

    print(f"\n{'='*70}")
    print("📊 全部结果汇总（按收益排序）")
    print(f"{'='*70}")

    all_results.sort(key=lambda x: float(x["收益"].replace("%","").replace("+","")), reverse=True)
    table = [[r["股票"], r["策略"], r["期末资金"], r["收益"], r["超额"], r["回撤"]] for r in all_results]
    print(tabulate(table, headers=["股票", "策略", "期末资金", "收益", "超额收益", "最大回撤"], tablefmt="grid"))

    best = all_results[0]
    print(f"\n🏆 最佳: {best['股票']} + {best['策略']}  收益: {best['收益']}")


if __name__ == "__main__":
    main()
