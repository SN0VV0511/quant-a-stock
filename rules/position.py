"""
持仓状态管理器
JSON 持久化，支持买入、卖出、T+1 检查、仓位限制等
"""

import contextlib
import json
import logging
import os
import tempfile
from datetime import datetime

from config.settings import (
    STATE_FILE, INITIAL_CAPITAL, MAX_SINGLE_ETF, MAX_SINGLE_STOCK,
    MAX_TOTAL_POSITION, LOT_SIZE, is_etf, TRADE_LOG_FILE, SNAPSHOT_LOG_FILE,
    ENFORCE_T1,
)
from rules.engine import TradingRules

logger = logging.getLogger(__name__)


class PositionManager:
    """持仓状态管理，JSON 持久化"""

    def __init__(self, state_file=None, trade_log_file=None, snapshot_log_file=None):
        self.state_file = state_file or STATE_FILE
        self.trade_log_file = trade_log_file or TRADE_LOG_FILE
        self.snapshot_log_file = snapshot_log_file or SNAPSHOT_LOG_FILE
        self.rules = TradingRules()
        self.state = self._default_state()
        self.load()

    @staticmethod
    def _default_state():
        """默认状态"""
        return {
            "cash": INITIAL_CAPITAL,
            "positions": {},       # code -> {name, shares, avg_cost, buy_date, strategy, current_price}
            "trades": [],          # 交易记录
            "daily_snapshots": {}, # date -> {cash, positions_value, total_value}
            "created_at": datetime.now().strftime("%Y%m%d %H:%M:%S"),
            "updated_at": None,
            "peak_value": INITIAL_CAPITAL,
        }

    def buy(self, code, name, price, shares, date, strategy="manual"):
        """执行买入，更新状态

        Args:
            code: 标的代码
            name: 标的名称
            price: 买入价格
            shares: 买入数量（必须为整手）
            date: 交易日期 YYYYMMDD
            strategy: 策略名称

        Returns:
            dict: 交易结果 {success, cost_detail, actual_price, shares}
        """
        is_etf_flag = is_etf(code)
        amount = price * shares

        # 计算交易成本
        cost_detail = self.rules.calc_total_cost(amount, "buy", is_etf_flag)
        total_cost = amount + cost_detail["total"]

        # 检查现金
        if total_cost > self.state["cash"]:
            # 尝试减少到可买数量
            affordable_shares = self.rules.calc_lot_size(
                price, self.state["cash"],
                max_ratio=MAX_SINGLE_ETF if is_etf_flag else MAX_SINGLE_STOCK,
                total_value=self.get_total_value({"cash": self.state["cash"]})
            )
            if affordable_shares <= 0:
                return {"success": False, "reason": "现金不足", "shares": 0}
            shares = affordable_shares
            amount = price * shares
            cost_detail = self.rules.calc_total_cost(amount, "buy", is_etf_flag)
            total_cost = amount + cost_detail["total"]

        # 确保整手
        shares = (shares // LOT_SIZE) * LOT_SIZE
        if shares <= 0:
            return {"success": False, "reason": "不足一手", "shares": 0}

        actual_price = cost_detail["actual_amount"] / shares

        # 更新持仓
        if code in self.state["positions"]:
            pos = self.state["positions"][code]
            total_shares = pos["shares"] + shares
            pos["avg_cost"] = round(
                (pos["avg_cost"] * pos["shares"] + actual_price * shares) / total_shares, 4
            )
            pos["shares"] = total_shares
            # 保留最早的买入日期（用于 T+1）
            pos["buy_date"] = min(pos["buy_date"], date)
        else:
            self.state["positions"][code] = {
                "name": name,
                "shares": shares,
                "avg_cost": round(actual_price, 4),
                "buy_date": date,
                "strategy": strategy,
                "peak_price": round(actual_price, 4),
            }

        # 扣除现金
        self.state["cash"] = round(self.state["cash"] - total_cost, 2)

        trade = {
            "date": date,
            "time": datetime.now().strftime("%H:%M:%S"),
            "code": code,
            "name": name,
            "action": "buy",
            "direction": "buy",
            "price": price,
            "shares": shares,
            "amount": round(amount, 2),
            "cost": cost_detail["total"],
            "actual_price": round(actual_price, 4),
            "strategy": strategy,
        }
        self.state["trades"].append(trade)

        self.state["updated_at"] = datetime.now().strftime("%Y%m%d %H:%M:%S")
        self.save()
        self._append_trade_log(trade)

        return {
            "success": True,
            "cost_detail": cost_detail,
            "actual_price": round(actual_price, 4),
            "shares": shares,
        }

    def sell(self, code, price, shares, date, strategy="manual"):
        """执行卖出，更新状态

        Args:
            code: 标的代码
            price: 卖出价格
            shares: 卖出数量
            date: 交易日期 YYYYMMDD
            strategy: 策略名称

        Returns:
            dict: 交易结果 {success, cost_detail, actual_price, shares, profit}
        """
        if code not in self.state["positions"]:
            return {"success": False, "reason": "无持仓", "shares": 0}

        pos = self.state["positions"][code]

        # T+1 检查(可通过 config.ENFORCE_T1 关闭以便虚拟盘当日联调)
        if ENFORCE_T1 and not self.rules.check_t1(pos["buy_date"], date):
            return {"success": False, "reason": "T+1 限制，不可卖出", "shares": 0}

        # 检查可卖数量
        if shares > pos["shares"]:
            shares = pos["shares"]
        if shares <= 0:
            return {"success": False, "reason": "可卖数量为零", "shares": 0}

        is_etf_flag = is_etf(code)
        amount = price * shares

        # 计算交易成本
        cost_detail = self.rules.calc_total_cost(amount, "sell", is_etf_flag)
        net_proceeds = cost_detail["actual_amount"] - cost_detail["commission"] - cost_detail["stamp_tax"] - cost_detail["transfer_fee"]

        actual_price = net_proceeds / shares if shares > 0 else 0
        profit = (actual_price - pos["avg_cost"]) * shares

        # 更新持仓
        pos["shares"] -= shares
        if pos["shares"] <= 0:
            del self.state["positions"][code]

        # 增加现金
        self.state["cash"] = round(self.state["cash"] + net_proceeds, 2)

        trade = {
            "date": date,
            "time": datetime.now().strftime("%H:%M:%S"),
            "code": code,
            "name": pos.get("name", code),
            "action": "sell",
            "direction": "sell",
            "price": price,
            "shares": shares,
            "amount": round(amount, 2),
            "cost": cost_detail["total"],
            "actual_price": round(actual_price, 4),
            "profit": round(profit, 2),
            "strategy": strategy,
        }
        self.state["trades"].append(trade)

        self.state["updated_at"] = datetime.now().strftime("%Y%m%d %H:%M:%S")
        self.save()
        self._append_trade_log(trade)

        return {
            "success": True,
            "cost_detail": cost_detail,
            "actual_price": round(actual_price, 4),
            "shares": shares,
            "profit": round(profit, 2),
        }

    def get_position(self, code):
        """获取某只股票的持仓

        Returns:
            dict or None
        """
        return self.state["positions"].get(code)

    def get_all_positions(self):
        """获取所有持仓"""
        return dict(self.state["positions"])

    def get_position_codes(self):
        """获取全部持仓代码。"""
        return list(self.state["positions"].keys())

    def get_positions_snapshot(self):
        """获取持仓快照副本。"""
        return json.loads(json.dumps(self.state["positions"], ensure_ascii=False))

    def get_cash(self):
        """获取可用现金"""
        return self.state["cash"]

    def update_prices(self, current_prices):
        """更新持仓当前价格,并维护持仓期间最高价(供移动止损)。

        Args:
            current_prices: dict {code: price}
        """
        if not current_prices:
            return
        for code, pos in self.state["positions"].items():
            if code in current_prices:
                price = current_prices[code]
                pos["current_price"] = price
                # 维护峰值价:取历史峰值、成本、当前价三者最大,兼容旧状态文件
                prev_peak = pos.get("peak_price", pos.get("avg_cost", price))
                if price and price > 0:
                    pos["peak_price"] = max(prev_peak, price)
        self.state["updated_at"] = datetime.now().strftime("%Y%m%d %H:%M:%S")

    def get_total_value(self, current_prices=None):
        """计算总市值

        Args:
            current_prices: dict {code: price}，如果为 None，使用持仓均价

        Returns:
            float: 总市值
        """
        positions_value = 0.0
        for code, pos in self.state["positions"].items():
            if current_prices and code in current_prices:
                price = current_prices[code]
            else:
                price = pos.get("current_price", pos["avg_cost"])
            positions_value += price * pos["shares"]
        return round(self.state["cash"] + positions_value, 2)

    def can_sell(self, code, date):
        """检查是否可卖（T+1）

        Args:
            code: 标的代码
            date: 当前日期 YYYYMMDD

        Returns:
            (can_sell, reason)
        """
        if code not in self.state["positions"]:
            return False, "无持仓"
        pos = self.state["positions"][code]
        if not self.rules.check_t1(pos["buy_date"], date):
            return False, f"T+1 限制（买入日: {pos['buy_date']}）"
        return True, "可卖出"

    def check_position_limit(self, code, amount, total_value=None, max_ratio_override=None):
        """检查持仓比例限制

        Args:
            code: 标的代码
            amount: 拟交易金额
            total_value: 当前总市值
            max_ratio_override: 覆盖默认仓位限制

        Returns:
            (within_limit, reason)
        """
        if total_value is None:
            total_value = self.get_total_value()
        if total_value <= 0:
            return False, "总市值为零"

        if max_ratio_override:
            max_ratio = max_ratio_override
        elif is_etf(code):
            max_ratio = MAX_SINGLE_ETF
        else:
            max_ratio = MAX_SINGLE_STOCK

        # 当前该标的持仓市值
        current_pos_value = 0.0
        if code in self.state["positions"]:
            pos = self.state["positions"][code]
            current_pos_value = pos["avg_cost"] * pos["shares"]

        new_ratio = (current_pos_value + amount) / total_value
        if new_ratio > max_ratio:
            return False, f"超过单票上限 {max_ratio:.0%}（当前比例 {new_ratio:.1%}）"

        return True, "符合限制"

    def check_total_position_limit(self, extra_amount=0):
        """检查总仓位限制

        Args:
            extra_amount: 拟额外买入金额

        Returns:
            (within_limit, current_ratio)
        """
        total_value = self.get_total_value()
        if total_value <= 0:
            return False, 0.0

        current_position_value = total_value - self.state["cash"]
        new_position_value = current_position_value + extra_amount
        ratio = new_position_value / total_value

        return ratio <= MAX_TOTAL_POSITION, ratio

    def save_snapshot(self, date, current_prices=None):
        """保存每日快照

        Args:
            date: 日期 YYYYMMDD
            current_prices: 当前价格字典
        """
        total_value = self.get_total_value(current_prices)

        positions_value = 0.0
        for code, pos in self.state["positions"].items():
            if current_prices and code in current_prices:
                price = current_prices[code]
            else:
                price = pos.get("current_price", pos["avg_cost"])
            positions_value += price * pos["shares"]

        self.state["daily_snapshots"][date] = {
            "cash": self.state["cash"],
            "positions_value": round(positions_value, 2),
            "total_value": total_value,
            "position_count": len(self.state["positions"]),
        }

        # 更新峰值
        if total_value > self.state.get("peak_value", INITIAL_CAPITAL):
            self.state["peak_value"] = total_value

        self.save()
        self._append_snapshot_log(date, current_prices)

    def get_drawdown(self, current_value=None):
        """计算当前回撤

        Returns:
            float: 回撤比例（正数）
        """
        if current_value is None:
            current_value = self.get_total_value()
        peak = self.state.get("peak_value", INITIAL_CAPITAL)
        if peak <= 0:
            return 0.0
        return round((peak - current_value) / peak, 4)

    def get_daily_return(self, date, current_prices=None):
        """计算日收益率"""
        snapshots = self.state.get("daily_snapshots", {})
        if date not in snapshots:
            return None

        today_value = self.get_total_value(current_prices)

        # 找前一个交易日的快照
        sorted_dates = sorted(snapshots.keys())
        idx = sorted_dates.index(date) if date in sorted_dates else -1
        if idx > 0:
            prev_value = snapshots[sorted_dates[idx - 1]]["total_value"]
        else:
            prev_value = INITIAL_CAPITAL

        if prev_value <= 0:
            return 0.0
        return round((today_value - prev_value) / prev_value, 4)

    def save(self):
        """原子保存状态到文件。

        先写入同目录临时文件再 ``os.replace`` 原子替换,避免进程在写入
        ``portfolio_state.json`` 中途崩溃时损坏关键持仓状态文件,也避免
        Web 仪表盘读取到写了一半的半截 JSON。``os.replace`` 在 POSIX 和
        Windows 上均保证同卷内的原子重命名。
        """
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        # 临时文件与目标同目录,确保 os.replace 在同一文件系统内原子生效
        dir_name = os.path.dirname(self.state_file)
        fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=dir_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())  # 落盘后再替换,防止替换后内容仍在缓存丢失
            os.replace(tmp_path, self.state_file)
        except Exception:
            # 写入失败时清理临时文件,避免残留 *.tmp
            with contextlib.suppress(OSError):
                os.remove(tmp_path)
            raise

    def load(self):
        """从文件加载状态"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # 合并到默认状态（兼容新增字段）
                for key in self._default_state():
                    if key not in loaded:
                        loaded[key] = self._default_state()[key]
                self._normalize_positions(loaded)
                self.state = loaded
            except (json.JSONDecodeError, IOError) as exc:
                logger.warning("加载持仓状态失败，将使用默认状态: %s", exc)
                self.state = self._default_state()

    def reset(self):
        """重置为初始状态"""
        self.state = self._default_state()
        self.save()

    def get_recent_trades(self, n=10):
        """获取最近 N 条交易记录"""
        return self.state["trades"][-n:] if self.state["trades"] else []

    def get_trades_by_date(self, date):
        """获取指定日期的交易记录"""
        return [t for t in self.state["trades"] if t["date"] == date]

    def summary(self, current_prices=None):
        """持仓摘要"""
        total_value = self.get_total_value(current_prices)
        cash = self.state["cash"]
        position_ratio = (total_value - cash) / total_value if total_value > 0 else 0
        drawdown = self.get_drawdown(total_value)
        pnl = total_value - INITIAL_CAPITAL
        pnl_pct = pnl / INITIAL_CAPITAL if INITIAL_CAPITAL > 0 else 0

        positions_summary = []
        for code, pos in self.state["positions"].items():
            if current_prices and code in current_prices:
                cur_price = current_prices[code]
            else:
                cur_price = pos.get("current_price", pos["avg_cost"])
            market_val = cur_price * pos["shares"]
            profit = (cur_price - pos["avg_cost"]) * pos["shares"]
            profit_pct = (cur_price - pos["avg_cost"]) / pos["avg_cost"] if pos["avg_cost"] > 0 else 0
            positions_summary.append({
                "code": code,
                "name": pos.get("name", code),
                "shares": pos["shares"],
                "avg_cost": pos["avg_cost"],
                "current_price": cur_price,
                "market_value": round(market_val, 2),
                "profit": round(profit, 2),
                "profit_pct": round(profit_pct, 4),
                "weight": round(market_val / total_value, 4) if total_value > 0 else 0,
            })

        return {
            "total_value": round(total_value, 2),
            "cash": round(cash, 2),
            "position_ratio": round(position_ratio, 4),
            "drawdown": drawdown,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
            "position_count": len(self.state["positions"]),
            "positions": positions_summary,
        }

    def _normalize_positions(self, state):
        """兼容旧版实时脚本写出的持仓结构。"""
        for code, pos in state.get("positions", {}).items():
            pos.setdefault("name", code)
            pos.setdefault("strategy", "legacy")
            pos.setdefault("buy_date", datetime.now().strftime("%Y%m%d"))
            pos.setdefault("current_price", pos.get("avg_cost", 0))
            pos.setdefault("peak_price", pos.get("avg_cost", 0))

    def _append_trade_log(self, trade):
        """同步追加 Web 仪表盘读取的交易流水。"""
        try:
            os.makedirs(os.path.dirname(self.trade_log_file), exist_ok=True)
            with open(self.trade_log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(trade, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            logger.warning("追加交易流水失败: %s", exc)

    def _append_snapshot_log(self, date, current_prices=None):
        """追加账户快照流水，便于一个月观察期复盘。"""
        snapshot = {
            "date": date,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": self.summary(current_prices),
        }
        try:
            os.makedirs(os.path.dirname(self.snapshot_log_file), exist_ok=True)
            with open(self.snapshot_log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(snapshot, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            logger.warning("追加账户快照失败: %s", exc)
