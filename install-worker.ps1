# 把这台 Windows 电脑接入 agent fleet，成为一台被管理的电脑（worker）。
# 在这台电脑上、用平时登录的 Windows 用户运行（不需要管理员）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\install-worker.ps1 -FullAccess
# 可以重复运行：令牌、主agent export 的说明文件和已有数据会保留。
param(
    [string]$Name = $env:COMPUTERNAME.ToLower(),
    [string]$MainAgentName = '主agent export',
    [string]$Description = '',
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'agent-fleet'),      # 内部数据：配置、令牌、任务记录
    [string]$Workspace = (Join-Path $env:USERPROFILE 'AgentFleet'),      # 工作区：main-agent\、tasks\、shared\
    [int]$Port = 8765,
    [int]$TailscalePort = 8766,
    [switch]$FullAccess,          # 子任务不设沙箱并自动同意权限请求，才能操作桌面、浏览器和其他程序
    [string]$CodexVersion = '0.162.1',
    [switch]$SkipPrerequisites,   # 不用 winget 安装 Node.js、Python、Tailscale
    [switch]$NoAutostart,
    [ValidateSet('codex','claude')][string]$Engine = 'codex',
    [switch]$LocalOnly,
    [switch]$NoStart,
    [string]$ClaudeExecutable = '',
    [string]$CodexHome = (Join-Path $env:USERPROFILE '.codex')
)
$ErrorActionPreference = 'Stop'
if (-not $PSBoundParameters.ContainsKey('MainAgentName') -and (Test-Path -LiteralPath (Join-Path $DataDir 'config.json'))) {
    $priorSettings = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $DataDir 'config.json') -Raw | ConvertFrom-Json
    if ($priorSettings.main_agent_name) { $MainAgentName = $priorSettings.main_agent_name }
}
$MainAgentName = $MainAgentName.Trim()
if ($MainAgentName.Length -lt 1 -or $MainAgentName.Length -gt 100 -or $MainAgentName -match '[\x00-\x1f\x7f]' -or $MainAgentName.StartsWith('--') -or $MainAgentName -in @('__proto__','constructor','prototype')) { throw 'Main agent name must contain 1 to 100 printable characters, without a leading --.' }
$repo = $PSScriptRoot
$worker = Join-Path $repo 'worker'

function Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Have($command) { [bool](Get-Command $command -ErrorAction SilentlyContinue) }
function Write-Utf8($path, $text) { [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding $false)) }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Winget-Install($id) {
    if (-not (Have 'winget')) { throw "缺少 $id，并且没有 winget。请手动安装后加 -SkipPrerequisites 重新运行。" }
    & winget install --id $id --exact --source winget --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "安装 $id 失败" }
    Refresh-Path
}
$warnings = New-Object System.Collections.Generic.List[string]

# 1. Node.js, Python, Tailscale
$tailscale = 'C:\Program Files\Tailscale\tailscale.exe'
if (-not $SkipPrerequisites) {
    Step '检查 Node.js、Python、Tailscale'
    if (-not (Have 'node')) { Winget-Install 'OpenJS.NodeJS.LTS' }
    $hasPython = (Have 'py') -or (Have 'python')
    if (-not $hasPython) { Winget-Install 'Python.Python.3.12' }
    if (-not $LocalOnly -and -not (Test-Path $tailscale)) { Winget-Install 'Tailscale.Tailscale' }
    if ($Engine -eq 'claude' -and -not (Have 'git')) { Winget-Install 'Git.Git' }
}
if (-not (Have 'node')) { throw '找不到 Node.js' }

# 2. Agent CLI
$nodeExe = (Get-Command node).Source
$codexJs = $null
if ($Engine -eq 'codex') {
Step "安装 Codex 命令行 $CodexVersion"
$npmRoot = (& npm root -g).Trim()
$codexJs = Join-Path $npmRoot '@openai\codex\bin\codex.js'
$installed = $null
if (Test-Path $codexJs) { $installed = ((& node $codexJs --version) -replace '[^0-9.]', '').Trim() }
if ($installed -ne $CodexVersion) {
    & npm install -g "@openai/codex@$CodexVersion"
    if ($LASTEXITCODE -ne 0) { throw '安装 Codex 命令行失败' }
}
# Windows PowerShell treats native stderr as errors even when login succeeds.
$loginErrorPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    & node $codexJs login status *> $null
    $loginExitCode = $LASTEXITCODE
} finally { $ErrorActionPreference = $loginErrorPreference }
if ($loginExitCode -ne 0) { $warnings.Add('Codex 还没有登录：运行 codex login 用你的 ChatGPT 账号登录，否则任务会失败。') }
} else {
    Step '准备 Claude Code 命令行'
    if (-not $ClaudeExecutable) {
        $nativeClaude = Get-Command claude.exe -ErrorAction SilentlyContinue
        if ($nativeClaude) { $ClaudeExecutable = $nativeClaude.Source }
        elseif (Test-Path (Join-Path $env:USERPROFILE '.local\bin\claude.exe')) {
            $ClaudeExecutable = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
        } else {
            New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
            $claudeInstaller = Join-Path $DataDir 'install-claude.ps1'
            Invoke-WebRequest -Uri 'https://claude.ai/install.ps1' -OutFile $claudeInstaller -UseBasicParsing
            & powershell -NoProfile -ExecutionPolicy Bypass -File $claudeInstaller
            if ($LASTEXITCODE -ne 0) { throw 'Claude Code 安装失败' }
            $ClaudeExecutable = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
        }
    }
    if (-not (Test-Path -LiteralPath $ClaudeExecutable)) { throw '找不到原生 claude.exe；可用 -ClaudeExecutable 指定路径' }
    $claudeVersionText = (& $ClaudeExecutable --version | Out-String).Trim()
    if ($claudeVersionText -notmatch '(\d+\.\d+\.\d+)') { throw '无法确认 Claude Code 版本' }
    $claudeVersion = $Matches[1]
    if ([version]$claudeVersion -lt [version]'2.1.248') { throw 'Claude Code 需要 2.1.248 或更新版本以支持 restricted 模式，请先升级' }
    $loginErrorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $ClaudeExecutable auth status *> $null
        $loginExitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $loginErrorPreference }
    if ($loginExitCode -ne 0) { $warnings.Add('Claude Code 还没有登录：请本人运行 claude auth login，然后重新运行安装。') }
}

# 3. Python environment for the service
Step '准备 Python 环境'
New-Item -ItemType Directory -Force -Path $DataDir, (Join-Path $DataDir 'state'), (Join-Path $Workspace 'tasks'), (Join-Path $Workspace 'shared') | Out-Null
$venv = Join-Path $DataDir 'venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    if (Have 'py') { & py -3 -m venv $venv } else { & python -m venv $venv }
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 环境失败（需要 Python 3.10 以上）' }
}
& $venvPython -m pip install --quiet --disable-pip-version-check -r (Join-Path $worker 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw '安装 Python 依赖失败' }

# 4. Token
$tokenFile = Join-Path $DataDir 'token.txt'
if (Test-Path $tokenFile) {
    $token = (Get-Content -Encoding UTF8 -LiteralPath $tokenFile -Raw).Trim()
} else {
    $bytes = New-Object byte[] 48
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
    Write-Utf8 $tokenFile $token
}

# 5. Tailscale address
$tsName = $null; $tsIp = $null
if (-not $LocalOnly -and (Test-Path $tailscale)) {
    try {
        $status = & $tailscale status --json | ConvertFrom-Json
        if ($status.Self -and $status.BackendState -eq 'Running') {
            $tsName = $status.Self.DNSName.TrimEnd('.')
            $tsIp = @($status.Self.TailscaleIPs)[0]
        }
    } catch {}
}
if (-not $LocalOnly -and -not $tsName) { $warnings.Add('Tailscale 还没有登录：登录后重新运行本脚本，其他电脑才能连到这台。') }

# 6. This computer's main agent
Step "创建主 Agent：$MainAgentName"
$mainWorkspace = Join-Path $Workspace 'main-agent'
New-Item -ItemType Directory -Force -Path (Join-Path $mainWorkspace '.codex') | Out-Null
$agentsFile = Join-Path $mainWorkspace 'AGENTS.md'
if (-not (Test-Path $agentsFile)) {
    $template = [System.IO.File]::ReadAllText((Join-Path $worker 'templates\main-agent\AGENTS.md'))
    $permissionNote = if ($FullAccess) { '子任务以完整权限运行，可操作桌面、浏览器和其他应用；所有权限请求自动批准。' } elseif ($Engine -eq 'claude') { '子任务使用 Claude Code restricted 模式：文件工具限于工作目录和 shared 文件夹，命令执行关闭；这不是操作系统级沙箱。' } else { '子任务使用 Codex workspace-write 沙箱，不自动批准额外权限。' }
    Write-Utf8 $agentsFile ($template.Replace('{{COMPUTER_NAME}}', $Name).Replace('{{WORKSPACE}}', $Workspace).Replace('{{AGENT_ENGINE}}', $Engine).Replace('{{MAIN_AGENT_NAME}}', $MainAgentName).Replace('{{PERMISSION_NOTE}}', $permissionNote))
}
if (-not (Test-Path (Join-Path $mainWorkspace 'CLAUDE.md'))) { Copy-Item -LiteralPath $agentsFile -Destination (Join-Path $mainWorkspace 'CLAUDE.md') }
if ($Engine -eq 'codex') {
Write-Utf8 (Join-Path $mainWorkspace '.codex\config.toml') @"
# 主agent export 专用：只在本项目里启用派任务服务，子任务不会加载它。文件里含有访问令牌，不要分享。
[mcp_servers.codex_tasks]
url = "http://127.0.0.1:$Port/mcp"
http_headers = { Authorization = "Bearer $token" }
tool_timeout_sec = 120
# 后台运行时没人点“批准”，预先批准这个服务的工具调用（只对本项目生效）
default_tools_approval_mode = "approve"
"@
# Codex only loads a project's own config when the project is trusted.
$codexConfig = Join-Path $CodexHome 'config.toml'
New-Item -ItemType Directory -Force -Path (Split-Path $codexConfig) | Out-Null
$trustKey = $mainWorkspace.ToLower()
$existing = if (Test-Path $codexConfig) { [System.IO.File]::ReadAllText($codexConfig) } else { '' }
if (-not $existing.Contains("[projects.'$trustKey']")) {
    [System.IO.File]::AppendAllText($codexConfig, "`n[projects.'$trustKey']`ntrust_level = `"trusted`"`n", (New-Object System.Text.UTF8Encoding $false))
}
}

# Fixed rules for every sub-task; the user can edit this file any time, no restart needed.
$rulesFile = Join-Path $Workspace 'worker-rules.md'
if (-not (Test-Path $rulesFile)) { Copy-Item (Join-Path $worker 'templates\worker-rules.md') $rulesFile }

# 7. Service config
Step '写入本机配置'
$configPath = Join-Path $DataDir 'config.json'
if (Test-Path $configPath) { Copy-Item $configPath "$configPath.bak" -Force }
$allowedHosts = @("127.0.0.1:$Port", "localhost:$Port")
if ($tsName) { $allowedHosts += "${tsName}:$TailscalePort" }
if ($tsIp) { $allowedHosts += "${tsIp}:$TailscalePort" }
$remoteUrl = if ($tsName) { "http://${tsName}:$TailscalePort/mcp" } else { '' }
$config = [ordered]@{
    computer_name = $Name
    main_agent_name = $MainAgentName
    port = $Port
    tailscale_port = $TailscalePort
    remote_url = $remoteUrl
    python_executable = $venvPython
    codex_executable = $codexJs
    codex_launcher_executable = $nodeExe
    codex_launcher_args = @($codexJs, '-c', 'windows.sandbox="mxc"')
    codex_version = $CodexVersion
    tailscale_executable = $tailscale
    state_dir = (Join-Path $DataDir 'state')
    token_file = $tokenFile
    worker_rules_file = (Join-Path $Workspace 'worker-rules.md')
    tasks_dir = (Join-Path $Workspace 'tasks')
    shared_dir = (Join-Path $Workspace 'shared')
    allowed_roots = @($Workspace)
    allowed_hosts = $allowedHosts
    main_agent_workspaces = @($mainWorkspace)
    executor = $(if ($Engine -eq 'claude') { 'claude' } else { 'app-server' })
    engine = $Engine
    claude_executable = $ClaudeExecutable
    local_only = [bool]$LocalOnly
    sandbox = $(if ($FullAccess) { 'danger-full-access' } else { 'workspace-write' })
    auto_approve = [bool]$FullAccess
    max_timeout_seconds = 7200
    writer_wait_seconds = 10
    fork_when_busy = $false
}
if ($Engine -eq 'claude') { $config['claude_version'] = $claudeVersion }
# Keep user-added options on repeated setup (including quota and display preferences).
if (Test-Path $configPath) {
    $oldConfig = Get-Content -Encoding UTF8 -LiteralPath $configPath -Raw | ConvertFrom-Json
    foreach ($property in $oldConfig.PSObject.Properties) {
        if (-not $config.Contains($property.Name)) { $config[$property.Name] = $property.Value }
    }
}
Write-Utf8 $configPath ($config | ConvertTo-Json -Depth 5)

# 8. Start at logon, inside the desktop session (needed to control the screen)
if (-not $NoAutostart) {
    Step '设置登录后自动启动'
    $startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'agent-fleet-worker.cmd'
    $localFlag = if ($LocalOnly) { ' -LocalOnly' } else { '' }
    Write-Utf8 $startup "@echo off`r`npowershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $worker 'start.ps1')`" -ConfigPath `"$configPath`"$localFlag`r`n"
}

# 9. Start now
if (-not $NoStart) {
Step '启动服务'
$startArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $worker 'start.ps1'), '-ConfigPath', $configPath)
if ($LocalOnly -or -not $tsName) { $startArgs += '-LocalOnly' }
& powershell @startArgs
if ($LASTEXITCODE -ne 0) { $warnings.Add('服务没有正常启动，看 state\server.stderr.log。') }
}

# 10. Connection info for the orchestrator
$connection = [ordered]@{
    name = $Name
    engine = $Engine
    description = $Description
    local_url = "http://127.0.0.1:$Port/mcp"
    tailscale_url = $remoteUrl
    token = $token
    main_agent_workspace = $mainWorkspace
    main_agent_name = $MainAgentName
    dashboard = $(if ($tsName) { "http://${tsName}:$TailscalePort/dashboard" } else { "http://127.0.0.1:$Port/dashboard" })
}
$connectionFile = Join-Path $DataDir 'connection.json'
Write-Utf8 $connectionFile ($connection | ConvertTo-Json)

Write-Host ''
Write-Host "完成：$Name 已接入。" -ForegroundColor Green
Write-Host "接入信息：$connectionFile（含令牌，只发给你自己的管理端电脑）"
Write-Host "在管理端电脑上接入：让 Claude 运行 add-computer，或 node <call-agent skill 文件夹>\fleet.mjs add-computer <connection.json 的路径>"
Write-Host "只读看板：$($connection.dashboard)"
if (-not $FullAccess) { Write-Host '当前子任务只能写自己的工作目录。要操作桌面和浏览器，用 -FullAccess 重新运行。' -ForegroundColor Yellow }
foreach ($w in $warnings) { Write-Warning $w }
