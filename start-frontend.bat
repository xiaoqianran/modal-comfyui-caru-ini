@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

where uv >nul 2>&1
if errorlevel 1 (
    echo ERROR: uv is not installed or not in PATH.
    pause
    exit /b 1
)

echo Starting local workflow console at http://127.0.0.1:8768/
start "" "http://127.0.0.1:8768/"
uv run python frontend/server.py
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo Console exited with error %RESULT%.
pause
exit /b %RESULT%
