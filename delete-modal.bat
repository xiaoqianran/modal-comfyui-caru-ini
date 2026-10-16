@echo off
setlocal
cd /d "%~dp0"
echo WARNING: Modal CLI has no separate app delete command.
echo This permanently stops the deployment; it does NOT delete Volumes or Images.
echo App: modal-comfyui
uv run modal app stop modal-comfyui --yes
if errorlevel 1 (
  echo Failed to stop Modal App.
  exit /b 1
)
echo Deployment stopped. Cached Volumes and stored Images were NOT deleted.
endlocal
