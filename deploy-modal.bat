@echo off
setlocal
cd /d "%~dp0"

set "WORKFLOW=%~1"
if not defined WORKFLOW set "WORKFLOW=pixal3d"

if not exist "workflows\%WORKFLOW%\workflow.json" (
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

echo Deploying Modal ComfyUI using workflow: %WORKFLOW%
echo GPU comes from workflows\%WORKFLOW%\models.json unless MODAL_GPU overrides it.
uv run modal deploy comfyui.py
exit /b %ERRORLEVEL%
