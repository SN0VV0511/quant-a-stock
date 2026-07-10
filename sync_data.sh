#!/bin/bash
# 同步 quant-live 容器 logs 到宿主机（data 目录已挂载，无需同步）
docker cp quant-live:/app/logs/live.log /root/.openclaw/workspace/quant-a-stock/logs/live.log 2>/dev/null
docker cp quant-live:/app/logs/live_today.log /root/.openclaw/workspace/quant-a-stock/logs/live_today.log 2>/dev/null
