@echo off
setlocal
cd /d "%~dp0"
rem Stop running GPU containers without undeploying the App.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $infoText=(& uv run modal app info modal-comfyui --json | Out-String); if($LASTEXITCODE -ne 0){exit 1}; $app=$infoText|ConvertFrom-Json; if($app.lifecycle.state -eq 'stopped'){Write-Host 'App already stopped; no GPU containers running.'; exit 0}; $json=(& uv run modal container list --app-id $app.app_id --json | Out-String); if($LASTEXITCODE -ne 0){exit 1}; $items=@($json|ConvertFrom-Json); $count=0; foreach($item in $items){$id=$item.container_id; if(-not $id){$id=$item.id}; if(-not $id){throw 'Container ID missing from CLI JSON'}; & uv run modal container stop $id --yes; if($LASTEXITCODE -ne 0){exit 1}; $count++}; Write-Host ('Stopped {0} container(s). App remains deployed; incoming requests can start new GPU containers.' -f $count)"
exit /b %ERRORLEVEL%
