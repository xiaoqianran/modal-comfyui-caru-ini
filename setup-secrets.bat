@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0setup-secrets.ps1" (
  echo ERROR: setup-secrets.ps1 is missing.
  set "RESULT=1"
  goto done
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-secrets.ps1"
set "RESULT=%ERRORLEVEL%"
:done
if not "%RESULT%"=="0" echo Secret setup failed. Review the message above.
echo.
pause
exit /b %RESULT%