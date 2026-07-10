#!/bin/bash
# 量化早报生成脚本
# 读取持仓状态 + 今日扫描/择时信号，输出纯文本早报
# 用法: bash morning_brief.sh

WORK_DIR="/root/.openclaw/workspace/quant-a-stock"
PORTFOLIO="$WORK_DIR/data/portfolio_state.json"
LOG="$WORK_DIR/logs/live_today.log"

# 1. 持仓概况（python 生成纯文本）
cd "$WORK_DIR"
python3 -c "
import json, sys, io
sys.stdout = io.StringIO()  # 屏蔽 baostock print

p = json.load(open('data/portfolio_state.json'))
cash = p.get('cash', 0)
positions = p.get('positions', {})
INITIAL = 50000

total = cash
lines = []
for code, pos in positions.items():
    val = pos['shares'] * pos['current_price']
    total += val
    pnl = (pos['current_price'] - pos['avg_cost']) * pos['shares']
    pnl_pct = (pos['current_price'] - pos['avg_cost']) / pos['avg_cost'] if pos['avg_cost'] else 0
    emoji = '🔴' if pnl_pct < -0.05 else ('🟡' if pnl_pct < 0 else '🟢')
    lines.append(f'{emoji} {code} {pos[\"name\"]} {pos[\"shares\"]}股 现价{pos[\"current_price\"]:.3f} {pnl_pct:+.2%}')

sys.stdout = sys.__stdout__
print(f'💰 总资产: ¥{total:,.2f} | 收益率: {(total-INITIAL)/INITIAL:+.2%}')
print(f'💵 现金: ¥{cash:,.2f}')
print(f'📦 持仓 {len(positions)} 只:')
for l in lines:
    print(l)
" 2>/dev/null

echo "---"

# 2. 从日志提取今日扫描和择时信息
if [ -f "$LOG" ]; then
    # 大盘择时信号
    grep -o '大盘择时.*' "$LOG" | tail -1
    # 候选股数量
    grep '扫描完成.*候选股' "$LOG" | tail -1
    # ETF RPS 信号（如果有）
    grep -i 'ETF.*RPS\|RPS.*ETF' "$LOG" 2>/dev/null | tail -2
fi
