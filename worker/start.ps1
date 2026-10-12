# Starts this computer's worker service. The per-computer config lives outside the project folder.
# (PowerShell names are case-insensitive, so the path parameter must not be called $Config: that is $config below.)
param([switch]$LocalOnly,
      [string]$ConfigPath = $(if ($env:AGENT_FLEET_CONFIG) { $env:AGENT_FLEET_CONFIG } else { Join-Path $env:LOCALAPPDATA 'agent-fleet\config.json' }))
$ErrorActionPreference = 'Stop'
$configPath = $ConfigPath
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$token = (Get-Content -LiteralPath $config.token_file -Raw).Trim()
$headers = @{ Authorization = "Bearer $token" }
$healthUrl = "http://127.0.0.1:$($config.port)/health"
$healthy = $false
try {
    $health = Invoke-RestMethod -Uri $healthUrl -Headers $headers -TimeoutSec 2
    $healthy = $health.service -eq 'codex-task-mcp'
} catch {}
if (-not $healthy) {
    $serverPath = Join-Path $PSScriptRoot 'server.py'
    $argsList = @('-u', ('"' + $serverPath + '"'), '--config', ('"' + $configPath + '"'))
    $process = Start-Process -FilePath $config.python_executable -ArgumentList $argsList -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $config.state_dir 'server.stdout.log') `
        -RedirectStandardError (Join-Path $config.state_dir 'server.stderr.log')
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 250
        if ($process.HasExited) { throw 'Service exited. Check server.stderr.log in state_dir.' }
        try {
            $health = Invoke-RestMethod -Uri $healthUrl -Headers $headers -TimeoutSec 1
            if ($health.service -eq 'codex-task-mcp') { $healthy = $true; break }
        } catch {}
    }
    if (-not $healthy) {
        Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
        throw 'Service did not become ready.'
    }
}
Write-Output "Local MCP: http://127.0.0.1:$($config.port)/mcp"
if (-not $LocalOnly) {
    if (-not (Test-Path -LiteralPath $config.tailscale_executable)) { throw 'Tailscale executable not found. Local service is ready.' }
    $serveStatus = & $config.tailscale_executable serve status --json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Could not read Tailscale Serve configuration.' }
    if ($serveStatus.TCP -and $serveStatus.TCP.PSObject.Properties.Name -contains "$($config.tailscale_port)") {
        $portHosts = @($serveStatus.Web.PSObject.Properties | Where-Object { $_.Name.EndsWith(":$($config.tailscale_port)") })
        $expectedProxy = "http://127.0.0.1:$($config.port)"
        if ($portHosts.Count -ne 1 -or $portHosts[0].Value.Handlers.'/'.Proxy -ne $expectedProxy) {
            throw 'Tailscale port is already used by another service; no changes made to it.'
        }
    }
    & $config.tailscale_executable serve --bg --http=$($config.tailscale_port) --yes "http://127.0.0.1:$($config.port)"
    if ($LASTEXITCODE -ne 0) { throw 'Tailscale Serve failed. Local service is ready.' }
    Write-Output "Tailscale MCP: $($config.remote_url)"
}
