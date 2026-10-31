#!/usr/bin/env bash
set -euo pipefail

# IBKR 官方 API 需要从官网下载安装包后本地 pip install .；不使用来源不明的
# 公共 PyPI ibapi 包。IBKR_API_SOURCE 指向官方 TWS API 解压目录下的 pythonclient。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GATEWAY_DIR="${IBKR_GATEWAY_DIR:-$ROOT/gateways/ibkr}"
VENV_DIR="${IBKR_VENV_DIR:-$ROOT/backend/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
API_SOURCE="${IBKR_API_SOURCE:-}"
if [[ -z "$API_SOURCE" || ! -f "$API_SOURCE/setup.py" && ! -f "$API_SOURCE/pyproject.toml" ]]; then
  echo "请先从 IBKR 官方下载 TWS API，并设置 IBKR_API_SOURCE=/path/to/TWS API/source/pythonclient" >&2
  exit 2
fi
mkdir -p "$GATEWAY_DIR"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then "$PYTHON_BIN" -m venv "$VENV_DIR"; fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip keyring
"$VENV_DIR/bin/python" -m pip install "$API_SOURCE"
"$VENV_DIR/bin/python" -c 'import ibapi, sys; print("ibapi:", ibapi.__file__); print(sys.executable)'
echo "IBKR ibapi 已安装。请启动 TWS/IB Gateway，开启 Socket API 后执行 Paper 连接测试。"
