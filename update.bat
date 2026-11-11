@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Оновлюю yt-dlp і бібліотеки (робіть це, якщо YouTube перестав грати)...
.venv\Scripts\python.exe -m pip install -U -r requirements.txt
pause
