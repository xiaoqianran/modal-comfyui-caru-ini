$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$envFile = Join-Path $PSScriptRoot ".env"
$names = @{
    "cloudflare-r2" = @("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
    "huggingface-secret" = @("HF_TOKEN")
    "github" = @("GITHUB_TOKEN")
}

$existing = (& uv run modal secret list | Out-String)
if ($LASTEXITCODE -ne 0) { throw "Unable to list Modal secrets." }

$values = @{}
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            $values[$Matches[1]] = $Matches[2].Trim('"', "'")
        }
    }
}

foreach ($name in @("cloudflare-r2", "huggingface-secret", "github")) {
    if ($existing -match "(?m)^\|\s*$([regex]::Escape($name))\s*\|") {
        Write-Host "Using existing Modal secret: $name"
        continue
    }

    $keys = $names[$name]
    $missing = @($keys | Where-Object { [string]::IsNullOrWhiteSpace($values[$_]) })
    if ($missing.Count -gt 0) {
        if ($name -eq "cloudflare-r2") {
            throw "Missing R2 credentials in .env: $($missing -join ', '). Copy .env.example to .env and fill the credentials."
        }
        Write-Host "Skipping optional $name (missing: $($missing -join ', '))."
        continue
    }

    # modal CLI accepts a dotenv file; isolate each secret's keys.
    $temp = Join-Path $PSScriptRoot (".secrets/.modal-$name-$([guid]::NewGuid().ToString('N')).env")
    try {
        New-Item -ItemType Directory -Force (Split-Path $temp) | Out-Null
        $lines = @($keys | ForEach-Object { "$_=$($values[$_])" })
        [System.IO.File]::WriteAllLines($temp, $lines, [System.Text.UTF8Encoding]::new($false))
        Write-Host "Creating Modal secret: $name"
        & uv run modal secret create $name --from-dotenv $temp
        if ($LASTEXITCODE -ne 0) { throw "Failed to create $name" }
    }
    finally {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    }
}
