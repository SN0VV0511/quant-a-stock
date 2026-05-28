"""
可视化模块 - 生成回测图表
"""
import matplotlib
matplotlib.use("Agg")  # 无 GUI 模式
import matplotlib.pyplot as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import os

# 中文字体
plt.rcParams["font.sans-serif"] = ["SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def plot_backtest(result_df, summary, symbol="000001", save_path=None):
    """
    生成回测结果图表

    参数:
        result_df: 回测结果 DataFrame
        summary: 回测摘要 dict
        symbol: 股票代码
        save_path: 保存路径，None 则自动命名
    """
    if save_path is None:
        reports_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
        os.makedirs(reports_dir, exist_ok=True)
        save_path = os.path.join(reports_dir, f"backtest_{symbol}.png")

    fig, axes = plt.subplots(3, 1, figsize=(14, 12), gridspec_kw={"height_ratios": [3, 1.5, 1]})
    fig.suptitle(f"A Stock Quantitative Backtest - {symbol}", fontsize=16, fontweight="bold")

    # ===== 图1: 价格和净值 =====
    ax1 = axes[0]
    ax1.plot(result_df["date"], result_df["close"], label="Stock Price", alpha=0.6, color="gray")
    ax1.set_ylabel("Price", color="gray")
    ax1.tick_params(axis="y", labelcolor="gray")

    # 右轴：净值
    ax1_r = ax1.twinx()
    initial = result_df["total_value"].iloc[0]
    ax1_r.plot(result_df["date"], result_df["total_value"] / initial, label="Strategy NAV", color="blue", linewidth=2)
    ax1_r.axhline(y=1.0, color="red", linestyle="--", alpha=0.5, label="Breakeven")
    ax1_r.set_ylabel("Net Asset Value")
    ax1_r.legend(loc="upper left")
    ax1.set_title("Price vs Strategy NAV")

    # 买卖点标注
    buys = result_df[result_df["signal"] == 1]
    sells = result_df[result_df["signal"] == -1]
    ax1.scatter(buys["date"], buys["close"], marker="^", color="green", s=80, zorder=5, label="Buy")
    ax1.scatter(sells["date"], sells["close"], marker="v", color="red", s=80, zorder=5, label="Sell")
    ax1.legend(loc="upper right")

    # ===== 图2: 回撤 =====
    ax2 = axes[1]
    ax2.fill_between(result_df["date"], result_df["drawdown"], 0, color="red", alpha=0.3)
    ax2.plot(result_df["date"], result_df["drawdown"], color="red", linewidth=1)
    ax2.set_ylabel("Drawdown (%)")
    ax2.set_title("Max Drawdown")
    ax2.axhline(y=0, color="gray", linestyle="-", alpha=0.3)

    # ===== 图3: 每日收益 =====
    ax3 = axes[2]
    colors = ["green" if r >= 0 else "red" for r in result_df["daily_return"].fillna(0)]
    ax3.bar(result_df["date"], result_df["daily_return"].fillna(0) * 100, color=colors, alpha=0.6, width=1)
    ax3.set_ylabel("Daily Return (%)")
    ax3.set_title("Daily Return Distribution")

    # 格式化 x 轴
    for ax in axes:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=45)
        ax.grid(True, alpha=0.3)

    # 添加摘要文字
    summary_text = "\n".join([f"{k}: {v}" for k, v in summary.items()])
    fig.text(0.02, 0.02, summary_text, fontsize=8, family="monospace",
             verticalalignment="bottom", bbox=dict(boxstyle="round", facecolor="wheat", alpha=0.5))

    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"📊 图表已保存: {save_path}")
    return save_path


def plot_kline(df, symbol="000001", save_path=None):
    """
    生成 K 线图

    参数:
        df: 包含 date/open/close/high/low/volume 的 DataFrame
        symbol: 股票代码
        save_path: 保存路径
    """
    import mplfinance as mpf

    if save_path is None:
        reports_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
        os.makedirs(reports_dir, exist_ok=True)
        save_path = os.path.join(reports_dir, f"kline_{symbol}.png")

    kline_df = df[["date", "open", "close", "high", "low", "volume"]].copy()
    kline_df.columns = ["Date", "Open", "Close", "High", "Low", "Volume"]
    kline_df = kline_df.set_index("Date")

    mpf.plot(
        kline_df,
        type="candle",
        mav=(5, 10, 20),
        volume=True,
        title=f"K Line - {symbol}",
        style="charles",
        savefig=dict(fname=save_path, dpi=150)
    )
    print(f"📊 K线图已保存: {save_path}")
    return save_path
