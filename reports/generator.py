"""
报告生成器
生成日报、周报，适合终端/微信查看
"""

import os
from datetime import datetime, timedelta

from config.settings import REPORT_DIR, INITIAL_CAPITAL


class ReportGenerator:
    """生成日报、周报"""

    def __init__(self, report_dir=None):
        self.report_dir = report_dir or REPORT_DIR
        os.makedirs(self.report_dir, exist_ok=True)

    def _format_pnl(self, value, pct):
        """格式化盈亏显示"""
        sign = "+" if value >= 0 else ""
        return f"{sign}{value:,.2f} 元 ({sign}{pct:.2%})"

    def _format_position(self, pos):
        """格式化持仓显示"""
        pnl_sign = "+" if pos["profit"] >= 0 else ""
        return (
            f"  {pos['name']}({pos['code']}) "
            f"{pos['shares']}股 "
            f"成本{pos['avg_cost']:.3f} "
            f"现价{pos['current_price']:.3f} "
            f"{pnl_sign}{pos['profit_pct']:.2%} "
            f"市值{pos['market_value']:,.0f}元"
        )

    def daily_report(self, date, portfolio, trades, signals, risk_events):
        """生成日报（纯文本，适合微信推送）

        Args:
            date: 日期 YYYYMMDD
            portfolio: PositionManager
            trades: 当日交易列表
            signals: 当日信号列表
            risk_events: 当日风控事件

        Returns:
            str: 日报文本
        """
        date_fmt = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        summary = portfolio.summary()

        lines = []
        lines.append(f"📊 量化日报 {date_fmt}")
        lines.append("=" * 30)

        # 净值
        total_value = summary["total_value"]
        pnl = summary["pnl"]
        pnl_pct = summary["pnl_pct"]
        lines.append(f"💰 总市值: {total_value:,.2f} 元")
        lines.append(f"📈 累计盈亏: {self._format_pnl(pnl, pnl_pct)}")
        lines.append(f"💵 现金: {summary['cash']:,.2f} 元")
        lines.append(f"📊 仓位: {summary['position_ratio']:.1%} | 持仓 {summary['position_count']} 只")
        lines.append(f"📉 回撤: {summary['drawdown']:.2%}")

        # 持仓明细
        if summary["positions"]:
            lines.append("")
            lines.append("📋 持仓明细:")
            for pos in summary["positions"]:
                lines.append(self._format_position(pos))
        else:
            lines.append("")
            lines.append("📋 当前空仓")

        # 交易记录
        if trades:
            lines.append("")
            lines.append(f"🔄 今日交易 ({len(trades)} 笔):")
            for t in trades:
                direction = "买入" if t["direction"] == "buy" else "卖出"
                lines.append(
                    f"  {direction} {t.get('name', t['code'])} "
                    f"{t['shares']}股 @ {t['price']:.3f} "
                    f"金额{t['amount']:,.0f}元"
                )
                if t.get("profit") is not None:
                    pnl_sign = "+" if t["profit"] >= 0 else ""
                    lines.append(f"    盈亏: {pnl_sign}{t['profit']:,.2f} 元")

        # 风控事件
        if risk_events:
            lines.append("")
            lines.append(f"⚠️ 风控事件 ({len(risk_events)} 条):")
            for evt in risk_events:
                lines.append(f"  {evt.get('code', '')} {evt.get('action', '')} - {evt.get('reason', '')}")

        lines.append("")
        lines.append("-" * 30)
        lines.append(f"初始资金: {INITIAL_CAPITAL:,.0f} 元")

        report_text = "\n".join(lines)

        # 保存到文件
        self._save_report(f"daily_{date}.txt", report_text)

        return report_text

    def weekly_report(self, week_start, week_end, portfolio, trades):
        """生成周报

        Args:
            week_start: 周开始日期 YYYYMMDD
            week_end: 周结束日期 YYYYMMDD
            portfolio: PositionManager
            trades: 本周交易列表

        Returns:
            str: 周报文本
        """
        start_fmt = f"{week_start[:4]}-{week_start[4:6]}-{week_start[6:]}"
        end_fmt = f"{week_end[:4]}-{week_end[4:6]}-{week_end[6:]}"
        summary = portfolio.summary()

        # 计算周收益率
        snapshots = portfolio.state.get("daily_snapshots", {})
        week_start_value = None
        week_end_value = summary["total_value"]

        for d in sorted(snapshots.keys()):
            if d >= week_start:
                week_start_value = snapshots[d].get("total_value")
                break

        if week_start_value is None:
            week_start_value = INITIAL_CAPITAL

        week_return = (week_end_value - week_start_value) / week_start_value if week_start_value > 0 else 0
        week_pnl = week_end_value - week_start_value

        lines = []
        lines.append(f"📊 量化周报")
        lines.append(f"📅 {start_fmt} ~ {end_fmt}")
        lines.append("=" * 35)

        lines.append(f"💰 期初市值: {week_start_value:,.2f} 元")
        lines.append(f"💰 期末市值: {week_end_value:,.2f} 元")
        lines.append(f"📈 本周盈亏: {self._format_pnl(week_pnl, week_return)}")
        lines.append(f"📈 累计盈亏: {self._format_pnl(summary['pnl'], summary['pnl_pct'])}")
        lines.append(f"📉 最大回撤: {summary['drawdown']:.2%}")

        # 交易统计
        buy_count = len([t for t in trades if t["direction"] == "buy"])
        sell_count = len([t for t in trades if t["direction"] == "sell"])
        total_profit = sum(t.get("profit", 0) for t in trades if t.get("profit"))
        total_cost = sum(t.get("cost", 0) for t in trades)

        lines.append("")
        lines.append(f"🔄 本周交易:")
        lines.append(f"  买入 {buy_count} 笔 | 卖出 {sell_count} 笔")
        lines.append(f"  总成本: {total_cost:,.2f} 元")
        if sell_count > 0:
            lines.append(f"  已实现盈亏: {total_profit:,.2f} 元")

        # 当前持仓
        if summary["positions"]:
            lines.append("")
            lines.append("📋 当前持仓:")
            for pos in summary["positions"]:
                lines.append(self._format_position(pos))

        # 个股交易明细
        if trades:
            lines.append("")
            lines.append("📝 交易明细:")
            for t in trades:
                direction = "买入" if t["direction"] == "buy" else "卖出"
                line = f"  {t['date']} {direction} {t.get('name', t['code'])} {t['shares']}股@{t['price']:.3f}"
                if t.get("profit") is not None:
                    line += f" 盈亏{t['profit']:+,.2f}"
                lines.append(line)

        lines.append("")
        lines.append("-" * 35)

        report_text = "\n".join(lines)
        self._save_report(f"weekly_{week_start}_{week_end}.txt", report_text)

        return report_text

    def monthly_report(self, year, month, portfolio, all_trades, daily_values):
        """生成月报

        Args:
            year: 年份
            month: 月份
            portfolio: PositionManager
            all_trades: 月内所有交易
            daily_values: 月内每日净值

        Returns:
            str: 月报文本
        """
        summary = portfolio.summary()
        month_str = f"{year}-{month:02d}"

        lines = []
        lines.append(f"📊 量化月报 - {month_str}")
        lines.append("=" * 40)
        lines.append(f"💰 月末市值: {summary['total_value']:,.2f} 元")
        lines.append(f"📈 累计收益: {self._format_pnl(summary['pnl'], summary['pnl_pct'])}")
        lines.append(f"📉 最大回撤: {summary['drawdown']:.2%}")
        lines.append(f"🔄 交易次数: {len(all_trades)}")
        lines.append(f"📋 持仓数量: {summary['position_count']}")

        report_text = "\n".join(lines)
        self._save_report(f"monthly_{year}{month:02d}.txt", report_text)

        return report_text

    def _save_report(self, filename, content):
        """保存报告到文件"""
        filepath = os.path.join(self.report_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return filepath
