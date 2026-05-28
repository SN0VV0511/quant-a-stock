"""
A 股量化回测系统 - 主入口
小白友好的量化项目，开箱即用

使用方法:
    python main.py                    # 默认回测平安银行
    python main.py 000001             # 回测指定股票
    python main.py 000001 20230101 20240101  # 指定时间范围
"""
import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from data.data_loader import get_stock_data, get_stock_info
from strategies.ma_cross import MACrossStrategy
from backtest.engine import BacktestEngine
from backtest.visualizer import plot_backtest, plot_kline
from tabulate import tabulate


def print_banner():
    print("""
╔══════════════════════════════════════════╗
║        📈 A 股量化回测系统 v1.0          ║
║        小白友好 · 开箱即用               ║
╚══════════════════════════════════════════╝
    """)


def main():
    print_banner()

    # 解析参数
    symbol = sys.argv[1] if len(sys.argv) > 1 else "000001"
    start_date = sys.argv[2] if len(sys.argv) > 2 else "20240101"
    end_date = sys.argv[3] if len(sys.argv) > 3 else "20250101"

    # 1. 获取数据
    print("\n📥 步骤 1/4: 获取数据")
    print("-" * 40)
    df = get_stock_data(symbol, start_date, end_date, adjust="3")

    # 打印股票信息
    info = get_stock_info(symbol)
    if info:
        print(f"\n📋 股票信息:")
        for k, v in info.items():
            print(f"  {k}: {v}")

    # 2. 计算策略信号
    print("\n🧠 步骤 2/4: 计算策略信号")
    print("-" * 40)
    strategy = MACrossStrategy(short_window=5, long_window=20)
    print(strategy.describe())
    df = strategy.calculate_signals(df)

    buy_signals = (df["signal"] == 1).sum()
    sell_signals = (df["signal"] == -1).sum()
    print(f"\n  📊 信号统计: 买入 {buy_signals} 次，卖出 {sell_signals} 次")

    # 3. 执行回测
    print("\n⚡ 步骤 3/4: 执行回测")
    print("-" * 40)
    engine = BacktestEngine()
    result_df, summary = engine.run(df)

    # 打印回测结果
    print("\n" + "=" * 50)
    print("📈 回测结果")
    print("=" * 50)
    table_data = [[k, v] for k, v in summary.items()]
    print(tabulate(table_data, headers=["指标", "数值"], tablefmt="grid"))

    # 打印最近几笔交易
    trades = result_df[result_df["signal"] != 0][["date", "close", "signal", "shares", "total_value"]].copy()
    if not trades.empty:
        trades["操作"] = trades["signal"].map({1: "🟢 买入", -1: "🔴 卖出"})
        trades = trades.rename(columns={"date": "日期", "close": "价格", "shares": "持仓", "total_value": "总资产"})
        print(f"\n📋 交易记录 (最近 10 笔):")
        print(trades[["日期", "操作", "价格", "持仓", "总资产"]].tail(10).to_string(index=False))

    # 4. 生成图表
    print("\n🎨 步骤 4/4: 生成图表")
    print("-" * 40)
    try:
        chart_path = plot_backtest(result_df, summary, symbol)
        print(f"  ✅ 回测图表: {chart_path}")
    except Exception as e:
        print(f"  ⚠️ 回测图表生成失败: {e}")
        chart_path = None

    try:
        kline_path = plot_kline(df, symbol)
        print(f"  ✅ K线图: {kline_path}")
    except Exception as e:
        print(f"  ⚠️ K线图生成失败: {e}")
        kline_path = None

    # 保存结果
    reports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    csv_path = os.path.join(reports_dir, f"result_{symbol}.csv")
    result_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"  ✅ 详细数据: {csv_path}")

    print("\n" + "=" * 50)
    print("🎉 回测完成！")
    print("=" * 50)
    print(f"""
  使用方法:
    python main.py {symbol}              # 再次回测
    python main.py 600519                # 回测贵州茅台
    python main.py 000858 20230101 20240101  # 指定时间
    """)


if __name__ == "__main__":
    main()
