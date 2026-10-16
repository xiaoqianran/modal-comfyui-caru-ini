@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
set "RESULT=0"
if not exist ".env" (
  echo ERROR: .env not found. Copy .env.example to .env and fill in credentials.
  set "RESULT=1"
  goto finish
)
where uv >nul 2>&1
if errorlevel 1 (
  echo ERROR: uv is not installed or not on PATH.
  set "RESULT=1"
  goto finish
)
set "LIST=%TEMP%\modal-secrets-%RANDOM%%RANDOM%.txt"
uv run modal secret list > "%LIST%"
if errorlevel 1 (
  echo ERROR: Cannot list Modal Secrets.
  set "RESULT=1"
  goto finish
)
call :ensure cloudflare-r2 AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
if errorlevel 1 set "RESULT=1"
if "%RESULT%"=="0" call :ensure huggingface-secret HF_TOKEN
if "%RESULT%"=="0" call :ensure github GITHUB_TOKEN
goto finish

:ensure
set "NAME=%~1"
set "KEY1=%~2"
set "KEY2=%~3"
findstr /L /C:"%NAME%" "%LIST%" >nul 2>&1
if not errorlevel 1 (
  echo Existing Modal Secret: %NAME%
  exit /b 0
)
set "TMP=%TEMP%\modal-secret-%RANDOM%%RANDOM%.env"
findstr /B /L /C:"%KEY1%=" .env > "%TMP%"
if defined KEY2 findstr /B /L /C:"%KEY2%=" .env >> "%TMP%"
findstr /B /R /C:"%KEY1%=..*" "%TMP%" >nul
if errorlevel 1 goto missing
if defined KEY2 (
  findstr /B /R /C:"%KEY2%=..*" "%TMP%" >nul
  if errorlevel 1 goto missing
)
echo Creating Modal Secret: %NAME%
uv run modal secret create "%NAME%" --from-dotenv "%TMP%"
set "RC=%ERRORLEVEL%"
del /q "%TMP%" >nul 2>&1
exit /b %RC%
:missing
del /q "%TMP%" >nul 2>&1
if /i "%NAME%"=="cloudflare-r2" (
  echo ERROR: R2 credentials missing in .env.
  exit /b 1
)
echo Skipping optional Modal Secret: %NAME%
exit /b 0

:finish
if defined LIST if exist "%LIST%" del /q "%LIST%" >nul 2>&1
if "%RESULT%"=="0" (echo Secret setup completed.) else (echo Secret setup failed.)
echo.
pause
exit /b %RESULT%
