param(
    [ValidateSet('codex','claude')][string]$Engine = 'codex',
    [ValidateSet('Local','Tailscale','Lan')][string]$Connectivity = 'Local',
    [string]$LanAddress = '',
    [string]$Workspace = (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'AgentFleet'),
    [string]$DataDir = (Join-Path $env:USERPROFILE 'AppData\Local\agent-fleet'),
    [string]$Name = $env:COMPUTERNAME.ToLower(),
    [string]$MainAgentName = '',
    [ValidateSet('', 'zh', 'en')][string]$Language = '',
    [string]$ManagerName = '',
    [string]$Description = '',
    [string]$ClaudeExecutable = '',
    [int]$Port = 8765, [int]$TailscalePort = 8766,
    [switch]$FullAccess, [switch]$Restricted, [switch]$NoAutostart, [switch]$SkipPrerequisites,
    [switch]$Reconfigure, [switch]$Plan, [switch]$NoDesktop
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
if ($Restricted -and $FullAccess) { throw 'Pass either -FullAccess or -Restricted, not both.' }
$configPath = Join-Path $DataDir 'config.json'
function Ensure-DesktopProject {
    $desktopArgs = @((Join-Path $PSScriptRoot 'setup-desktop.mjs'),'--workspace',$Workspace,'--engine',$Engine)
    if ($NoDesktop) { $desktopArgs += '--no-desktop' }
    $desktopOutput = & node @desktopArgs
    if ($LASTEXITCODE -ne 0) { throw 'Worker is configured, but desktop project registration failed.' }
    return ($desktopOutput | Out-String | ConvertFrom-Json)
}
$previous = $null
if (Test-Path -LiteralPath $configPath) { $previous = Get-Content -Encoding UTF8 -LiteralPath $configPath -Raw | ConvertFrom-Json }
$oldConnectivity = $null
if ($previous) {
    $oldEngine = if ($previous.executor -eq 'claude') { 'claude' } else { 'codex' }
    $oldConnectivity = if ($previous.lan_url) { 'Lan' } elseif ($previous.remote_url -and -not $previous.local_only) { 'Tailscale' } else { 'Local' }
    if (-not $PSBoundParameters.ContainsKey('Engine')) { $Engine = $oldEngine }
    if (-not $PSBoundParameters.ContainsKey('Connectivity')) { $Connectivity = $oldConnectivity }
    if (-not $PSBoundParameters.ContainsKey('LanAddress') -and $Connectivity -eq 'Lan' -and $previous.lan_address) { $LanAddress = $previous.lan_address }
    if (-not $PSBoundParameters.ContainsKey('Workspace')) { $Workspace = Split-Path $previous.tasks_dir }
    if (-not $PSBoundParameters.ContainsKey('Name')) { $Name = $previous.computer_name }
    if (-not $PSBoundParameters.ContainsKey('MainAgentName') -and $previous.main_agent_name) { $MainAgentName = $previous.main_agent_name }
    if (-not $PSBoundParameters.ContainsKey('FullAccess')) { $FullAccess = $previous.sandbox -eq 'danger-full-access' }
    if (-not $PSBoundParameters.ContainsKey('Port')) { $Port = $previous.port }
    if (-not $PSBoundParameters.ContainsKey('TailscalePort')) { $TailscalePort = $previous.tailscale_port }
}
if ($Restricted) { $FullAccess = $false }
if (-not $Language) {
    if ($previous) { $Language = $(if ($previous.language -eq 'en') { 'en' } else { 'zh' }) }
    elseif ((Get-Culture).Name -like 'zh*') { $Language = 'zh' } else { $Language = 'en' }
}
if (-not $MainAgentName) { $MainAgentName = $(if ($Language -eq 'en') { 'Main agent' } else { '主agent export' }) }
if ($LanAddress -and $Connectivity -ne 'Lan') { throw '-LanAddress requires -Connectivity Lan.' }
if ($Connectivity -eq 'Lan' -and -not $LanAddress) {
    # The adapter with a default gateway is the one other computers on the network can reach.
    $LanAddress = @(Get-NetIPConfiguration -ErrorAction SilentlyContinue | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' } |
        ForEach-Object { @($_.IPv4Address)[0].IPAddress } | Where-Object { $_ -and $_ -notlike '169.254.*' })[0]
    if (-not $LanAddress) { throw 'Could not detect this computer''s local network address; pass -LanAddress.' }
}
$MainAgentName = $MainAgentName.Trim()
if ($MainAgentName.Length -lt 1 -or $MainAgentName.Length -gt 100 -or $MainAgentName -match '[\x00-\x1f\x7f]' -or $MainAgentName.StartsWith('--') -or $MainAgentName -in @('__proto__','constructor','prototype')) { throw 'Main agent name must contain 1 to 100 printable characters, without a leading --.' }
$oldMainName = if ($previous -and $previous.main_agent_name) { $previous.main_agent_name } else { '主agent export' }
$existingRegistry = Join-Path $DataDir 'local-client\computers.json'
if ($MainAgentName -cne $oldMainName -and (Test-Path -LiteralPath $existingRegistry)) {
    $registered = Get-Content -Encoding UTF8 -LiteralPath $existingRegistry -Raw | ConvertFrom-Json
    $registeredComputer = $registered.computers.PSObject.Properties[$Name]
    if ($registeredComputer -and @($registeredComputer.Value.agents.PSObject.Properties | Where-Object { $_.Name -ceq $MainAgentName }).Count) { throw 'An agent already uses the requested main agent name; choose another name.' }
}
if ($Plan) {
    $tasksLeaf = if ($previous -and $previous.codex_app_folders.other) { $previous.codex_app_folders.other } elseif ($Language -eq 'en') { 'AgentFleet-Other' } else { 'AgentFleet-其他' }
    [ordered]@{tasks_dir=(Join-Path $Workspace $tasksLeaf);engine=$Engine;connectivity=$Connectivity;lan_address=$LanAddress;main_agent_name=$MainAgentName;language=$Language;workspace=$Workspace;data_dir=$DataDir;full_access=[bool]$FullAccess;existing=[bool]$previous;reconfigure=[bool]$Reconfigure} | ConvertTo-Json
    return
}
if ($previous -and -not $Reconfigure) {
    if ($Engine -ne $oldEngine -or ([bool]$FullAccess -ne ($previous.sandbox -eq 'danger-full-access')) -or
        $Connectivity -ne $oldConnectivity -or ($Connectivity -eq 'Lan' -and $LanAddress -ne $previous.lan_address) -or
        $Workspace -ne (Split-Path $previous.tasks_dir) -or $Name -ne $previous.computer_name -or $MainAgentName -cne $oldMainName -or
        $Port -ne $previous.port -or $TailscalePort -ne $previous.tailscale_port -or
        ($PSBoundParameters.ContainsKey('Language') -and $Language -ne $(if ($previous.language -eq 'en') { 'en' } else { 'zh' }))) {
        throw 'Requested settings differ from the installation. Use -Reconfigure after confirming the change.'
    }
    Write-Output 'Existing worker reused. Use the main-agent skill to check its status. Use -Reconfigure for a repair or change.'
    $existingData = Split-Path $previous.state_dir
    $existingHeaders = @{Authorization=('Bearer ' + (Get-Content -Encoding UTF8 -LiteralPath $previous.token_file -Raw).Trim())}
    $existingHealth = $null
    try { $existingHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$($previous.port)/health" -Headers $existingHeaders -TimeoutSec 3 } catch {}
    if (-not $existingHealth -or $existingHealth.service -ne 'codex-task-mcp') {
        $existingStartScript = Join-Path $Workspace 'service\worker\start.ps1'
        if (-not (Test-Path -LiteralPath $existingStartScript)) { throw 'Existing worker runtime is missing. Use -Reconfigure to redeploy it.' }
        $existingStartArgs = @('-NoProfile','-ExecutionPolicy','Bypass','-File',$existingStartScript,'-ConfigPath',$configPath)
        if ($Connectivity -ne 'Tailscale' -or -not $previous.remote_url) { $existingStartArgs += '-LocalOnly' }
        & powershell @existingStartArgs
        if ($LASTEXITCODE -ne 0) { throw 'Existing worker could not start; inspect its private service logs.' }
        $existingHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$($previous.port)/health" -Headers $existingHeaders -TimeoutSec 5
        if ($existingHealth.service -ne 'codex-task-mcp') { throw 'The configured port is not the expected worker.' }
    }
    $desktop = Ensure-DesktopProject
    [ordered]@{status=$(if ($desktop.status -eq 'pending') { 'desktop-setup-pending' } else { 'existing-worker' });engine=$Engine;computer=$Name;main_agent_name=$MainAgentName;workspace=$Workspace;
        data_dir=$existingData;client_home=(Join-Path $existingData 'local-client');
        connection_file=(Join-Path $existingData 'connection.json');
        dashboard="http://127.0.0.1:$($previous.port)/dashboard";remote_url=$previous.remote_url;lan_url=$previous.lan_url;
        full_access=[bool]$FullAccess;desktop=$desktop;service_status=$existingHealth.status} | ConvertTo-Json -Depth 5
    return
}
$health = $null
if ($previous) {
    $headers = @{Authorization=('Bearer ' + (Get-Content -Encoding UTF8 -LiteralPath $previous.token_file -Raw).Trim())}
    try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:$($previous.port)/health" -Headers $headers -TimeoutSec 3 } catch {}
    if ($health -and $health.service -eq 'codex-task-mcp') {
        $tasks = Invoke-RestMethod -Uri "http://127.0.0.1:$($previous.port)/api/tasks" -Headers $headers -TimeoutSec 3
        if (@($tasks.tasks | Where-Object { $_.status -in @('queued','running') }).Count) { throw 'Worker is busy. Wait for its tasks before reconfiguration.' }
    }
    $profilePath = Join-Path $previous.state_dir 'profile.json'
    if ($MainAgentName -cne $oldMainName -and (Test-Path -LiteralPath $profilePath)) {
        $savedProfile = Get-Content -Encoding UTF8 -LiteralPath $profilePath -Raw | ConvertFrom-Json
        if (@($savedProfile.agents.PSObject.Properties | Where-Object { $_.Name -ceq $MainAgentName }).Count) { throw 'An agent already uses the requested main agent name; choose another name.' }
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
    '-Engine',$Engine,'-Workspace',$Workspace,'-DataDir',$DataDir,'-Name',$Name,'-MainAgentName',$MainAgentName,'-Port',"$Port",'-TailscalePort',"$TailscalePort",'-NoStart','-NoAutostart')
$installArgs += @('-Language',$Language)
if ($ManagerName) { $installArgs += @('-ManagerName',$ManagerName) }
if ($Description) { $installArgs += @('-Description',$Description) }
if ($Connectivity -ne 'Tailscale') { $installArgs += '-LocalOnly' }
if ($Connectivity -eq 'Lan') { $installArgs += @('-LanAddress',$LanAddress) }
if ($FullAccess) { $installArgs += '-FullAccess' }
if ($SkipPrerequisites) { $installArgs += '-SkipPrerequisites' }
if ($ClaudeExecutable) { $installArgs += @('-ClaudeExecutable',$ClaudeExecutable) }
& powershell @installArgs
if ($LASTEXITCODE -ne 0) { throw 'Worker installation failed; existing configuration is preserved in its backup.' }
$config = Get-Content -Encoding UTF8 -LiteralPath $configPath -Raw | ConvertFrom-Json
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
$config = Get-Content -Encoding UTF8 -LiteralPath $configPath -Raw | ConvertFrom-Json
if ($health -and $health.service -eq 'codex-task-mcp') {
    & taskkill.exe /PID $health.pid /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not restart the idle worker.' }
}
if ($Connectivity -ne 'Tailscale' -and $previous -and $previous.remote_url -and (Test-Path -LiteralPath $previous.tailscale_executable)) {
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
if ($Connectivity -ne 'Tailscale') { $startArgs += '-LocalOnly' }
elseif (-not $config.remote_url) { $startArgs += '-LocalOnly' }
& powershell @startArgs
if ($LASTEXITCODE -ne 0) { throw 'Worker startup failed; inspect the private state/server.stderr.log.' }
if (-not $NoAutostart) {
    $startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'agent-fleet-worker.cmd'
    $localFlag = if ($Connectivity -ne 'Tailscale') { ' -LocalOnly' } else { '' }
    [System.IO.File]::WriteAllText($startup,"@echo off`r`npowershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startScript`" -ConfigPath `"$configPath`"$localFlag`r`n",(New-Object System.Text.UTF8Encoding $false))
}
# A pairing code lets a remote orchestrator connect without copying connection.json.
$pairing = $null
if ($Connectivity -ne 'Local') {
    $pairingOutput = & $config.python_executable (Join-Path $deployment 'worker\tools\pairing.py') --config $configPath --json
    if ($LASTEXITCODE -eq 0) { $pairing = $pairingOutput | Out-String | ConvertFrom-Json }
}
$resolvedData = Split-Path $configPath
$clientHome = Join-Path $resolvedData 'local-client'
$savedFleetHome = $env:AGENT_FLEET_HOME
try {
    $env:AGENT_FLEET_HOME = $clientHome
    $client = Join-Path $PSScriptRoot 'fleet.mjs'
    & node $client add-computer (Join-Path $resolvedData 'connection.json') --local=1
    if ($LASTEXITCODE -ne 0) { throw 'Worker started, but local registration failed.' }
    & node $client describe-agent $Name $MainAgentName $(if ($Language -eq 'en') { '--role=Main agent responsible for this worker' } else { '--role=负责这台电脑的主 Agent' }) '--type=main'
    if ($LASTEXITCODE -ne 0) { throw 'Worker started, but main-agent profile registration failed.' }
    if ($MainAgentName -cne $oldMainName) {
        $updatedProfile = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $config.state_dir 'profile.json') -Raw | ConvertFrom-Json
        if (@($updatedProfile.agents.PSObject.Properties | Where-Object { $_.Name -ceq $oldMainName }).Count) {
            & node $client remove-agent $Name $oldMainName
            if ($LASTEXITCODE -ne 0) { throw 'Main agent renamed, but old profile cleanup failed.' }
        }
    }
} finally { $env:AGENT_FLEET_HOME = $savedFleetHome }
$desktop = Ensure-DesktopProject
[ordered]@{status=$(if ($desktop.status -eq 'pending') { 'desktop-setup-pending' } else { 'service-ready' });engine=$Engine;computer=$Name;main_agent_name=$MainAgentName;workspace=$Workspace;data_dir=$resolvedData;client_home=$clientHome;connection_file=(Join-Path $resolvedData 'connection.json');dashboard="http://127.0.0.1:$($config.port)/dashboard";remote_url=$config.remote_url;lan_url=$config.lan_url;
    firewall_note=$(if ($config.lan_url) { "Allow inbound TCP $($config.port) on Private networks in Windows Firewall (administrator)." } else { '' });
    full_access=[bool]$FullAccess;desktop=$desktop;pairing=$pairing;
    pair_script=(Join-Path $deployment 'worker\pair.ps1')} | ConvertTo-Json -Depth 5
