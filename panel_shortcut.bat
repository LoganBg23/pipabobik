@echo off
chcp 65001 >nul
cd /d "%~dp0"
.venv\Scripts\python.exe tools\make_shortcut.py
if not "%~1"=="quiet" pause
