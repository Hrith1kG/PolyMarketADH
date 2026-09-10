#!/usr/bin/env bash
# start_mac.sh - PolyMarket ADH Launcher for macOS
set -e

cd "$(dirname "$0")"

echo "========================================================"
echo "      Starting PolyMarket ADH (macOS Local Test)       "
echo "========================================================"

# Check if .venv exists
if [ ! -d ".venv" ]; then
    echo "[*] Creating virtual environment with Python 3.12..."
    if command -v uv >/dev/null 2>&1; then
        uv venv --python 3.12 .venv
        uv pip install -r requirements.txt
    else
        python3 -m venv .venv
        .venv/bin/pip install --upgrade pip
        .venv/bin/pip install -r requirements.txt
    fi
fi

echo "[*] Launching Streamlit Dashboard on http://localhost:8501..."
.venv/bin/streamlit run dashboard.py --server.port 8501
