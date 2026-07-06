param(
  [Parameter(Mandatory = $true)]
  [string]$TunnelToken
)

$ErrorActionPreference = "Stop"

Write-Host "Installing cloudflared as a Windows service..."
Write-Host "Run this from an Administrator PowerShell after creating a tunnel in Cloudflare Zero Trust."

cloudflared.exe service install $TunnelToken

Write-Host "cloudflared service installed. Check status with:"
Write-Host "  Get-Service cloudflared"
