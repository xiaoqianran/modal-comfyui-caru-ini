$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$names = @("cloudflare-r2", "huggingface-secret", "github")
$existing = (& uv run modal secret list | Out-String)
if ($LASTEXITCODE -ne 0) { throw "Unable to list Modal secrets." }

foreach ($name in $names) {
    $file = Join-Path $PSScriptRoot ".secrets/$name.env"
    $alreadyExists = $existing -match "(?m)^\|\s*$([regex]::Escape($name))\s*\|"

    if ($alreadyExists) {
        Write-Host "Using existing Modal secret: $name"
        continue
    }
    if (-not (Test-Path -LiteralPath $file)) {
        if ($name -eq "cloudflare-r2") {
            throw "Missing required R2 secret. Copy .secrets/cloudflare-r2.env.example to .secrets/cloudflare-r2.env, fill AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, then deploy again."
        }
        Write-Host "Optional secret $name not found; create .secrets/$name.env to configure it."
        continue
    }

    $values = @{}
    foreach ($line in Get-Content -LiteralPath $file) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            $values[$Matches[1]] = $Matches[2].Trim('"', "'")
        }
    }
    $required = switch ($name) {
        "cloudflare-r2" { @("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY") }
        "huggingface-secret" { @("HF_TOKEN") }
        "github" { @("GITHUB_TOKEN") }
    }
    foreach ($key in $required) {
        if ([string]::IsNullOrWhiteSpace($values[$key])) {
            throw "Missing $key in $file"
        }
    }

    Write-Host "Creating Modal secret: $name"
    & uv run modal secret create $name --from-dotenv $file
    if ($LASTEXITCODE -ne 0) { throw "Failed to create $name" }
}
