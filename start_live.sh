#!/bin/bash
# 量化盯盘启动脚本
# 用法: ./start_live.sh
# 功能: 清理旧进程 + 启动新实例 + 输出状态

WORK_DIR="/root/.openclaw/workspace/quant-a-stock"
PID_FILE="$WORK_DIR/data/live_runner.pid"

# 1. 杀旧进程（用 [l]ive_runner 技巧避免匹配到自己）
pkill -f "[l]ive_runner.py" 2>/dev/null
sleep 2

# 2. 清理 PID 文件
rm -f "$PID_FILE"

# 3. 启动新实例
cd "$WORK_DIR"
nohup python3 live_runner.py > /dev/null 2>&1 &
NEW_PID=$!

# 4. 等待确认
sleep 3
if ps -p "$NEW_PID" > /dev/null 2>&1; then
    echo "✅ 盯盘已启动 PID=$NEW_PID"
else
    echo "❌ 启动失败，进程已退出"
    exit 1
fi
