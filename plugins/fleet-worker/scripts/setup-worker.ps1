param(
    [ValidateSet('codex','claude')][string]$Engine = 'codex',
    [ValidateSet('Local','Tailscale')][string]$Connectivity = 'Local',
    [string]$Workspace = (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'AgentFleet'),
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'agent-fleet'),
    [string]$Name = $env:COMPUTERNAME.ToLower(),
    [string]$Description = '',
    [string]$ClaudeExecutable = '',
    [int]$Port = 8765, [int]$TailscalePort = 8766,
    [switch]$FullAccess, [switch]$NoAutostart, [switch]$SkipPrerequisites,
    [switch]$Reconfigure, [switch]$Plan
)
$ErrorActionPreference = 'Stop'
$configPath = Join-Path $DataDir 'config.json'
$previous = $null
if (Test-Path -LiteralPath $configPath) { $previous = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json }
if ($previous) {
    $oldEngine = if ($previous.executor -eq 'claude') { 'claude' } else { 'codex' }
    if (-not $PSBoundParameters.ContainsKey('Engine')) { $Engine = $oldEngine }
    if (-not $PSBoundParameters.ContainsKey('Connectivity')) { $Connectivity = $(if ($previous.remote_url -and -not $previous.local_only) { 'Tailscale' } else { 'Local' }) }
    if (-not $PSBoundParameters.ContainsKey('Workspace')) { $Workspace = Split-Path $previous.tasks_dir }
    if (-not $PSBoundParameters.ContainsKey('Name')) { $Name = $previous.computer_name }
    if (-not $PSBoundParameters.ContainsKey('FullAccess')) { $FullAccess = $previous.sandbox -eq 'danger-full-access' }
    if (-not $PSBoundParameters.ContainsKey('Port')) { $Port = $previous.port }
    if (-not $PSBoundParameters.ContainsKey('TailscalePort')) { $TailscalePort = $previous.tailscale_port }
}
if ($Plan) {
    [ordered]@{engine=$Engine;connectivity=$Connectivity;workspace=$Workspace;data_dir=$DataDir;full_access=[bool]$FullAccess;existing=[bool]$previous;reconfigure=[bool]$Reconfigure} | ConvertTo-Json
    return
}
if ($previous -and -not $Reconfigure) {
    if ($Engine -ne $oldEngine -or ($FullAccess -and $previous.sandbox -ne 'danger-full-access') -or
        (($Connectivity -eq 'Tailscale') -ne [bool]($previous.remote_url -and -not $previous.local_only)) -or
        $Workspace -ne (Split-Path $previous.tasks_dir) -or $Name -ne $previous.computer_name -or
        $Port -ne $previous.port -or $TailscalePort -ne $previous.tailscale_port) {
        throw 'Requested settings differ from the installation. Use -Reconfigure after confirming the change.'
    }
    Write-Output 'Existing worker reused. Use the main-agent skill to check its status. Use -Reconfigure for a repair or change.'
    return
}
$health = $null
if ($previous) {
    $headers = @{Authorization=('Bearer ' + (Get-Content -LiteralPath $previous.token_file -Raw).Trim())}
    try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:$($previous.port)/health" -Headers $headers -TimeoutSec 3 } catch {}
    if ($health -and $health.service -eq 'codex-task-mcp') {
        $tasks = Invoke-RestMethod -Uri "http://127.0.0.1:$($previous.port)/api/tasks" -Headers $headers -TimeoutSec 3
        if (@($tasks.tasks | Where-Object { $_.status -in @('queued','running') }).Count) { throw 'Worker is busy. Wait for its tasks before reconfiguration.' }
    }
}
$runtime = Join-Path (Split-Path $PSScriptRoot) 'runtime'
$deployment = Join-Path $Workspace 'service'
$absoluteDeployment = [System.IO.Path]::GetFullPath($deployment)
$absoluteWorkspace = [System.IO.Path]::GetFullPath($Workspace).TrimEnd('\') + '\'
if (-not $absoluteDeployment.StartsWith($absoluteWorkspace,[StringComparison]::OrdinalIgnoreCase)) { throw 'Deployment must be inside the chosen workspace.' }
New-Item -ItemType Directory -Force -Path $deployment | Out-Null
Copy-Item -LiteralPath (Join-Path $runtime 'worker') -Destination $deployment -Recurse -Force
Copy-Item -LiteralPath (Join-Path $runtime 'install-worker.ps1'),(Join-Path $runtime 'LICENSE') -Destination $deployment -Force
$installArgs = @('-NoProfile','-ExecutionPolicy','Bypass','-File',(Join-Path $deployment 'install-worker.ps1'),
    '-Engine',$Engine,'-Workspace',$Workspace,'-DataDir',$DataDir,'-Name',$Name,'-Port',"$Port",'-TailscalePort',"$TailscalePort",'-NoStart','-NoAutostart')
if ($Description) { $installArgs += @('-Description',$Description) }
if ($Connectivity -eq 'Local') { $installArgs += '-LocalOnly' }
if ($FullAccess) { $installArgs += '-FullAccess' }
if ($SkipPrerequisites) { $installArgs += '-SkipPrerequisites' }
if ($ClaudeExecutable) { $installArgs += @('-ClaudeExecutable',$ClaudeExecutable) }
& powershell @installArgs
if ($LASTEXITCODE -ne 0) { throw 'Worker installation failed; existing configuration is preserved in its backup.' }
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
# Desktop-app filesystem virtualization can redirect AppData. Resolve paths for autostart outside the app.
$resolveScript = @'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]).resolve(); c=json.loads(p.read_text(encoding='utf-8'))
for key in ('python_executable','codex_executable','codex_launcher_executable','claude_executable','state_dir','token_file','worker_rules_file','tasks_dir','shared_dir'):
    if c.get(key): c[key]=str(Path(c[key]).resolve())
if c.get('codex_launcher_args'): c['codex_launcher_args'][0]=c['codex_executable']
p.write_text(json.dumps(c,indent=2),encoding='utf-8'); print(p)
'@
$configPath = (& $config.python_executable -c $resolveScript $configPath | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve runtime paths.' }
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
if ($health -and $health.service -eq 'codex-task-mcp') {
    & taskkill.exe /PID $health.pid /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not restart the idle worker.' }
}
if ($Connectivity -eq 'Local' -and $previous -and $previous.remote_url -and (Test-Path -LiteralPath $previous.tailscale_executable)) {
    $serveStatus = & $previous.tailscale_executable serve status --json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Could not check the previous Tailscale route.' }
    if ($serveStatus.TCP -and $serveStatus.TCP.PSObject.Properties.Name -contains "$($previous.tailscale_port)") {
        $portHosts = @($serveStatus.Web.PSObject.Properties | Where-Object { $_.Name.EndsWith(":$($previous.tailscale_port)") })
        if ($portHosts.Count -ne 1 -or $portHosts[0].Value.Handlers.'/'.Proxy -ne "http://127.0.0.1:$($previous.port)") {
            throw 'The Tailscale route is owned by another service; leave it intact and choose a different worker port.'
        }
        & $previous.tailscale_executable serve --http=$($previous.tailscale_port) off
        if ($LASTEXITCODE -ne 0) { throw 'Could not disable the previous worker Tailscale route.' }
    }
}
$startScript = Join-Path $deployment 'worker\start.ps1'
$startArgs = @('-NoProfile','-ExecutionPolicy','Bypass','-File',$startScript,'-ConfigPath',$configPath)
if ($Connectivity -eq 'Local') { $startArgs += '-LocalOnly' }
elseif (-not $config.remote_url) { $startArgs += '-LocalOnly' }
& powershell @startArgs
if ($LASTEXITCODE -ne 0) { throw 'Worker startup failed; inspect the private state/server.stderr.log.' }
if (-not $NoAutostart) {
    $startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'agent-fleet-worker.cmd'
    $localFlag = if ($Connectivity -eq 'Local') { ' -LocalOnly' } else { '' }
    [System.IO.File]::WriteAllText($startup,"@echo off`r`npowershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startScript`" -ConfigPath `"$configPath`"$localFlag`r`n",(New-Object System.Text.UTF8Encoding $false))
}
$resolvedData = Split-Path $configPath
$clientHome = Join-Path $resolvedData 'local-client'
$savedFleetHome = $env:AGENT_FLEET_HOME
try {
    $env:AGENT_FLEET_HOME = $clientHome
    $client = Join-Path $PSScriptRoot 'fleet.mjs'
    & node $client add-computer (Join-Path $resolvedData 'connection.json') --local=1
    if ($LASTEXITCODE -ne 0) { throw 'Worker started, but local registration failed.' }
    & node $client describe-agent $Name '主agent export' '--role=Main agent responsible for this worker' '--type=main'
    if ($LASTEXITCODE -ne 0) { throw 'Worker started, but main-agent profile registration failed.' }
} finally { $env:AGENT_FLEET_HOME = $savedFleetHome }
[ordered]@{status='service-ready';engine=$Engine;computer=$Name;workspace=$Workspace;data_dir=$resolvedData;client_home=$clientHome;connection_file=(Join-Path $resolvedData 'connection.json');dashboard="http://127.0.0.1:$($config.port)/dashboard";remote_url=$config.remote_url;full_access=[bool]$FullAccess} | ConvertTo-Json
