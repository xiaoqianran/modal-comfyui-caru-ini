@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
cd /d "%~dp0"

if not "%~1"=="" set "WORKFLOW=%~1"
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

rem Validate recursively; only models.json and plugins.json are configuration.
powershell -NoProfile -Command "$files = @(Get-ChildItem -LiteralPath 'workflows\%WORKFLOW%' -Recurse -File -Filter '*.json' | Where-Object { $_.Name -notin @('models.json','plugins.json') }); if ($files.Count -eq 0) { exit 1 }; Write-Host ('Found ' + $files.Count + ' workflow JSON files.')"
if errorlevel 1 (
    echo ERROR: At least one workflow JSON is required.
    exit /b 1
)
echo Deploying Modal ComfyUI using workflow: %WORKFLOW%
echo GPU comes from workflows\%WORKFLOW%\models.json unless MODAL_GPU overrides it.
set "WORKFLOW=%WORKFLOW%"
uv run modal run download-models.py --ensure
if errorlevel 1 exit /b 1
uv run modal deploy comfyui.py
exit /b %ERRORLEVEL%
