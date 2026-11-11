@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Створюю ярлик pipabobik на робочому столі...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'pipabobik.lnk')); $s.TargetPath='%CD%\.venv\Scripts\pythonw.exe'; $s.Arguments='panel.pyw'; $s.WorkingDirectory='%CD%'; $s.IconLocation='%CD%\web\static\pipabobik.ico'; $s.Description='pipabobik — панель керування музикою'; $s.Save()"
if errorlevel 1 (echo Не вдалося створити ярлик.) else (echo Готово! На робочому столі з'явився ярлик pipabobik.)
if not "%~1"=="quiet" pause
