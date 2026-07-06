param(
  [string]$ProjectRoot = (Resolve-Path "$PSScriptRoot\..\..").Path,
  [switch]$StartQdrant,
  [switch]$QuickTunnel
)

$ErrorActionPreference = "Stop"

function Import-DotEnv {
  param([string]$Path)
  if (!(Test-Path $Path)) { return }
  Get-Content $Path | ForEach-Object {
    $line = $_.Trim()
    if (!$line -or $line.StartsWith("#") -or !$line.Contains("=")) { return }
    $name, $value = $line.Split("=", 2)
    [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), "Process")
  }
}

Import-DotEnv (Join-Path $ProjectRoot ".env.demo")
Import-DotEnv (Join-Path $ProjectRoot "app\.env")

if (!$env:PYTHONIOENCODING) {
  $env:PYTHONIOENCODING = "utf-8"
}
if (!$env:COMMONSOURCE_JWT_SECRET -and $env:COMMONSOURCE_REQUIRE_JWT_SECRET -eq "1") {
  throw "COMMONSOURCE_JWT_SECRET is required. Copy .env.demo.example to .env.demo and set a strong secret."
}

if ($StartQdrant) {
  Push-Location $ProjectRoot
  docker compose up -d qdrant
  Pop-Location
}

$python = Join-Path (Split-Path $ProjectRoot -Parent) ".venv\Scripts\python.exe"
if (!(Test-Path $python)) {
  $python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
if (!(Test-Path $python)) {
  $python = "python"
}

Start-Process -FilePath $python -ArgumentList "search_api.py" -WorkingDirectory (Join-Path $ProjectRoot "app") -WindowStyle Hidden
Start-Sleep -Seconds 5

Write-Host "CommonSource should now be available locally at http://127.0.0.1:5050"

if ($QuickTunnel) {
  Write-Host "Starting Cloudflare quick tunnel. Copy the trycloudflare.com URL from the cloudflared output."
  cloudflared tunnel --url http://localhost:5050
}
