@echo off
setlocal
cd /d "%~dp0"

set "WORKFLOW=%~1"
if not defined WORKFLOW set "WORKFLOW=comfyui-pixal3d-trellis2"

if not exist "workflows\%WORKFLOW%\" (
    echo ERROR: Workflow "%WORKFLOW%" not found.
    exit /b 1
)
if not exist "workflows\%WORKFLOW%\models.json" (
    echo ERROR: models.json missing for "%WORKFLOW%".
    exit /b 1
)
if not exist "workflows\%WORKFLOW%\plugins.json" (
    echo ERROR: plugins.json missing for "%WORKFLOW%".
    exit /b 1
)

set /a WORKFLOW_JSON_COUNT=0
for %%F in ("workflows\%WORKFLOW%\*.json") do (
    if /i not "%%~nxF"=="models.json" if /i not "%%~nxF"=="model.json" if /i not "%%~nxF"=="plugins.json" set /a WORKFLOW_JSON_COUNT+=1 >nul
)
if not "%WORKFLOW_JSON_COUNT%"=="1" (
    echo ERROR: Expected exactly one workflow JSON, found %WORKFLOW_JSON_COUNT%.
    exit /b 1
)
echo Deploying Modal ComfyUI using workflow: %WORKFLOW%
echo GPU comes from workflows\%WORKFLOW%\models.json unless MODAL_GPU overrides it.
uv run modal deploy comfyui.py
exit /b %ERRORLEVEL%
