#!/usr/bin/env bash
# ===========================================================================
#  睡眠监测 (SleepMonitor) — macOS / Linux 启动脚本
#  run.bat 的等价物, 方便在开发机上联调。
#      ./run.sh list-ports
#      ./run.sh run --pressure-port /dev/cu.usbserial-xxx1
# ===========================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$HERE/.venv"
PY="$VENV_DIR/bin/python"
STAMP="$VENV_DIR/.requirements.stamp"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "[错误] 没有找到 $PYTHON_BIN，请先安装 Python 3.10+。" >&2
    exit 1
fi
echo "[1/4] 使用 Python: $("$PYTHON_BIN" --version)"

if [ ! -x "$PY" ]; then
    echo "[2/4] 首次运行，正在创建虚拟环境 .venv ..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
else
    echo "[2/4] 虚拟环境已存在，跳过创建。"
fi

REQ_HASH="$(shasum -a 256 "$HERE/requirements.txt" | awk '{print $1}')"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$REQ_HASH" ]; then
    echo "[3/4] 正在安装依赖，首次会慢一些 ..."
    "$PY" -m pip install --upgrade pip --quiet --disable-pip-version-check
    "$PY" -m pip install -r "$HERE/requirements.txt" --disable-pip-version-check
    echo "$REQ_HASH" > "$STAMP"
else
    echo "[3/4] 依赖已是最新，跳过安装。"
fi

echo "[4/4] 启动中 ..."
echo
cd "$HERE"
exec "$PY" -m sleepmonitor "$@"
