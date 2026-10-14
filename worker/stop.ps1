param([string]$ConfigPath = $(if ($env:AGENT_FLEET_CONFIG) { $env:AGENT_FLEET_CONFIG } else { Join-Path $env:LOCALAPPDATA 'agent-fleet\config.json' }))
$ErrorActionPreference = 'Stop'
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$token = (Get-Content -LiteralPath $config.token_file -Raw).Trim()
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$($config.port)/health" -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 2
} catch { $health = $null }
if ($health -and $health.service -eq 'codex-task-mcp') {
    # Stop only the PID authenticated by our service, including its Codex children.
    & taskkill.exe /PID $health.pid /T /F
    if ($LASTEXITCODE -ne 0) { throw 'Could not stop the service process tree.' }
}
if (-not $config.local_only -and (Test-Path -LiteralPath $config.tailscale_executable)) {
    & $config.tailscale_executable serve --http=$($config.tailscale_port) off
    if ($LASTEXITCODE -ne 0) { throw 'Could not disable the Tailscale Serve port.' }
}
Write-Output 'Codex task MCP stopped.'
