#!/bin/zsh
# Tick Stock Panel backend launcher (for launchd)
# launchd 启动的进程环境极简, 需显式设置 PATH 和 PYTHONPATH

export PATH="/Users/ziyixue/Desktop/ziyixue/tick-stock-panel/backend/.venv/bin:/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"
export PYTHONPATH="/Users/ziyixue/Desktop/ziyixue/tick-stock-panel/backend"
export PYTHONUNBUFFERED=1
export HOME="/Users/ziyixue"

cd /Users/ziyixue/Desktop/ziyixue/tick-stock-panel/backend
exec /Users/ziyixue/Desktop/ziyixue/tick-stock-panel/backend/.venv/bin/python -m uvicorn app.main:app --env-file /Users/ziyixue/Desktop/ziyixue/tick-stock-panel/.env --host 0.0.0.0 --port 3018