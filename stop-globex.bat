@echo off
chcp 65001 >nul
title CrossBay - Stop
"%~dp0.venv\Scripts\python.exe" "%~dp0globex_ctl.py" stop
echo.
pause
