@echo off
title PolyMarket ADH Terminator
echo [*] Terminating PolyMarket Bot and Dashboard processes...
taskkill /F /FI "WINDOWTITLE eq PolyMarket Bot Engine*" /T 2>nul
taskkill /F /IM streamlit.exe /T 2>nul
echo [*] PolyMarket ADH processes stopped.
pause
