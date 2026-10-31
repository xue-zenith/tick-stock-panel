#!/usr/bin/env bash
set -euo pipefail

# 独立 Futu Gateway 环境；不会把 OpenD 或 futu-api 注入主 FastAPI venv。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GATEWAY_DIR="${FUTU_GATEWAY_DIR:-$ROOT/gateways/futu}"
VENV_DIR="${FUTU_VENV_DIR:-$ROOT/backend/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
mkdir -p "$GATEWAY_DIR"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then "$PYTHON_BIN" -m venv "$VENV_DIR"; fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install "futu-api>=9.0" "keyring>=25.0"
"$VENV_DIR/bin/python" -c 'import futu, sys; print("futu-api:", getattr(futu, "__version__", "unknown")); print(sys.executable)'
echo "Futu SDK 已安装。请启动 OpenD、完成登录/设备验证，再在实盘页执行连接测试。"
