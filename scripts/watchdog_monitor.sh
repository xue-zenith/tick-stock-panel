#!/bin/bash
# Tick Stock Panel 崩溃自愈监控
# 由 launchd 每 30s 调用一次: 检查 3018 端口, 没监听就拉起后端

PORT=3018
LOG=/Users/ziyixue/Desktop/ziyixue/tick-stock-panel/data/backend.log
STARTUP_LOG=/Users/ziyixue/Desktop/ziyixue/tick-stock-panel/data/watchdog_monitor.log

# 端口在监听 = 服务正常, 无需动作
if lsof -nP -iTCP:${PORT} -sTCP:LISTEN > /dev/null 2>&1; then
    exit 0
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] port ${PORT} not listening, restarting..." >> "${STARTUP_LOG}"

# 端口未监听 = 进程已退出或僵死, 拉起后端
export PATH="/Users/ziyixue/Desktop/ziyixue/tick-stock-panel/backend/.venv/bin:/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"
export PYTHONPATH="/Users/ziyixue/Desktop/ziyixue/tick-stock-panel/backend"
export PYTHONUNBUFFERED=1
export HOME="/Users/ziyixue"

cd /Users/ziyixue/Desktop/ziyixue/tick-stock-panel/backend
nohup /Users/ziyixue/Desktop/ziyixue/tick-stock-panel/backend/.venv/bin/python -m uvicorn app.main:app --env-file /Users/ziyixue/Desktop/ziyixue/tick-stock-panel/.env --host 0.0.0.0 --port 3018 >> "${LOG}" 2>&1 &

echo "[$(date '+%Y-%m-%d %H:%M:%S')] restart initiated, PID=$!" >> "${STARTUP_LOG}"
