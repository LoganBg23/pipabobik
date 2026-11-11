@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   pipabobik - встановлення
echo   ========================
echo.

where py >nul 2>nul
if errorlevel 1 (
  echo [1/4] Встановлюю Python...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  echo.
  echo   Python встановлено. ЗАКРИЙТЕ це вікно і запустіть install.bat ще раз.
  pause
  exit /b
)

echo [1/4] Встановлюю mpv (програвач)...
winget install -e --id shinchiro.mpv --accept-package-agreements --accept-source-agreements

echo [2/4] Встановлюю Deno (потрібен YouTube для yt-dlp)...
winget install -e --id DenoLand.Deno --accept-package-agreements --accept-source-agreements

echo [3/4] Створюю середовище Python і ставлю бібліотеки...
if not exist .venv py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip >nul
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
  echo   Помилка встановлення бібліотек. Перевірте інтернет і запустіть ще раз.
  pause
  exit /b 1
)
echo   Вікно панелі (необов'язково)...
.venv\Scripts\python.exe -m pip install -r requirements-panel.txt
if errorlevel 1 echo   Не вдалося - панель відкриватиметься через Microsoft Edge, це нормально.

echo [4/4] Ярлик панелі керування на робочому столі...
call panel_shortcut.bat quiet
echo.
echo Майстер налаштування...
.venv\Scripts\python.exe bot.py --setup
pause
