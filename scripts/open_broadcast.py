#!/usr/bin/env python3
"""开盘播报取数：从 paper_v2.db 读取真实持仓与资金（与面板 /api/portfolio 一致）。

历史说明：早期 combo_trend 系统把状态写进 data/portfolio_state.json，但当前
robust_v2 runner 已不再维护该文件（最后更新 2026-07-10，positions 为空）。
开盘播报若继续读 portfolio_state.json 会误报“无持仓”。本脚本改读权威账本
paper_v2.db，与面板保持一致。
"""
import json
import os
import sqlite3
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.abspath(os.path.join(ROOT, "data", "paper_v2.db"))
ACCOUNT = "paper_v2"


def main() -> int:
    if not os.path.exists(DB):
        print(json.dumps({"error": f"账本不存在: {DB}"}, ensure_ascii=False))
        return 1
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        print(json.dumps({"error": f"打开账本失败: {exc}"}, ensure_ascii=False))
        return 1

    try:
        acc = con.execute(
            "SELECT cash, initial_cash, updated_at FROM accounts WHERE account_id = ?",
            (ACCOUNT,),
        ).fetchone()
        if acc is None:
            print(json.dumps({"error": "账户 paper_v2 不存在"}, ensure_ascii=False))
            return 1
        cash = float(acc["cash"])
        initial = float(acc["initial_cash"])

        rows = con.execute(
            "SELECT code, name, shares, avg_cost, last_price "
            "FROM positions WHERE account_id = ? ORDER BY code",
            (ACCOUNT,),
        ).fetchall()

        # 优先采用官方收盘 NAV 快照做头条数字（现金/市值/总资产/净盈亏），
        # 与面板“昨日收盘总资产”一致；持仓清单仍逐笔取自 positions 表。
        nav = con.execute(
            "SELECT cash, market_value, total_value, net_pnl, snapshot_date "
            "FROM nav_snapshots WHERE account_id = ? "
            "ORDER BY snapshot_date DESC, captured_at DESC LIMIT 1",
            (ACCOUNT,),
        ).fetchone()
        if nav is not None:
            cash = float(nav["cash"])
            market_value = float(nav["market_value"])
            total_value = float(nav["total_value"])
            net_pnl = float(nav["net_pnl"])
            nav_date = str(nav["snapshot_date"])
        else:
            market_value = 0.0
            total_value = cash
            net_pnl = cash - initial
            nav_date = None

        positions = []
        for r in rows:
            shares = int(r["shares"])
            avg = float(r["avg_cost"])
            cur = float(r["last_price"] or avg)
            value = shares * cur
            if nav is None:  # 仅在无 NAV 快照时按逐笔累加市值
                market_value += value
            pnl_pct = (cur - avg) / avg * 100 if avg else 0.0
            positions.append(
                {
                    "code": str(r["code"]),
                    "name": str(r["name"]),
                    "shares": shares,
                    "avg_cost": round(avg, 4),
                    "current_price": round(cur, 4),
                    "market_value": round(value, 2),
                    "pnl_pct": round(pnl_pct, 2),
                }
            )

        if nav is None:  # 无快照时按逐笔累加重算头条
            total_value = cash + market_value
            net_pnl = total_value - initial
        net_pnl_pct = round(net_pnl / initial * 100, 2) if initial else 0.0
        out = {
            "date": date.today().strftime("%Y-%m-%d"),
            "cash": round(cash, 2),
            "market_value": round(market_value, 2),
            "total_value": round(total_value, 2),
            "initial_capital": round(initial, 2),
            "net_pnl": round(net_pnl, 2),
            "net_pnl_pct": net_pnl_pct,
            "position_count": len(positions),
            "positions": positions,
            "nav_date": nav_date,
            "updated_at": str(acc["updated_at"]),
            "source": "paper_v2",
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
