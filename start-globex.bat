@echo off
chcp 65001 >nul
title CrossBay - Start
"%~dp0.venv\Scripts\python.exe" "%~dp0globex_ctl.py" start
echo.
pause
