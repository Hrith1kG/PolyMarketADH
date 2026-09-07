@echo off
title PolyMarket ADH Launcher
echo ========================================================
echo       Starting PolyMarket ADH (Bot + Dashboard)
echo ========================================================

:: Check if virtual environment exists
if not exist ".venv\Scripts\activate.bat" (
    echo [!] Virtual environment (.venv) not found.
    echo [*] Creating virtual environment...
    python -m venv .venv
    call .venv\Scripts\activate.bat
    echo [*] Installing dependencies from requirements.txt...
    pip install --upgrade pip
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)

:: Launch Background Trading Bot in a separate console window
echo [*] Launching Background Trading Engine (main.py)...
start "PolyMarket Bot Engine" cmd /k ".venv\Scripts\python.exe main.py"

:: Launch Streamlit Dashboard on 0.0.0.0:8501
echo [*] Launching Dashboard on http://0.0.0.0:8501...
echo [*] Open your browser and navigate to http://localhost:8501 (or http://YOUR_VPS_IP:8501)
.venv\Scripts\streamlit.exe run dashboard.py --server.port 8501 --server.address 0.0.0.0
pause
