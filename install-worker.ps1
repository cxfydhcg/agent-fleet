# 把这台 Windows 电脑接入 agent fleet，成为一台被管理的电脑（worker）。
# 在这台电脑上、用平时登录的 Windows 用户运行（不需要管理员）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\install-worker.ps1 -FullAccess
# 可以重复运行：令牌、主agent export 的说明文件和已有数据会保留。
param(
    [string]$Name = $env:COMPUTERNAME.ToLower(),
    [string]$MainAgentName = '',  # 默认：中文 主agent export，英文 Main agent
    [ValidateSet('', 'zh', 'en')][string]$Language = '',  # 用户能看到的文字用哪种语言；默认沿用上次的设置，第一次按系统语言
    [string]$ManagerName = '',    # 可选：管理端调用这台电脑主 Agent 时用的名字（比如 经理cris），写进 connection.json
    [string]$Description = '',
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'agent-fleet'),      # 内部数据：配置、令牌、任务记录
    [string]$Workspace = (Join-Path $env:USERPROFILE 'AgentFleet'),      # 工作区：main-agent\、AgentFleet-其他\（英文 AgentFleet-Other\）、shared\
    [int]$Port = 8765,
    [int]$TailscalePort = 8766,
    [switch]$FullAccess,          # 子任务不设沙箱并自动同意权限请求，才能操作桌面、浏览器和其他程序
    [string]$CodexVersion = '0.162.1',
    [switch]$SkipPrerequisites,   # 不用 winget 安装 Node.js、Python、Tailscale
    [switch]$NoAutostart,
    [ValidateSet('codex','claude')][string]$Engine = 'codex',
    [switch]$LocalOnly,
    [string]$LanAddress = '',     # 局域网 IPv4 地址：服务对局域网开放（明文 HTTP，只在可信网络使用）
    [switch]$NoStart,
    [string]$ClaudeExecutable = '',
    [string]$CodexHome = (Join-Path $env:USERPROFILE '.codex')
)
$ErrorActionPreference = 'Stop'
$priorSettings = $null
if (Test-Path -LiteralPath (Join-Path $DataDir 'config.json')) {
    $priorSettings = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $DataDir 'config.json') -Raw | ConvertFrom-Json
}
if (-not $Language) {
    if ($priorSettings) { $Language = $priorSettings.language }
    elseif ((Get-Culture).Name -like 'zh*') { $Language = 'zh' } else { $Language = 'en' }
}
function T($zh, $en) { if ($Language -eq 'en') { $en } else { $zh } }
$tasksLeaf = if ($priorSettings -and $priorSettings.codex_app_folders.other) { $priorSettings.codex_app_folders.other } else { T 'AgentFleet-其他' 'AgentFleet-Other' }
if ($tasksLeaf -in @('.', '..') -or $tasksLeaf.IndexOfAny([System.IO.Path]::GetInvalidFileNameChars()) -ge 0) { throw 'The task project name must be a valid directory name.' }
if ($priorSettings -and $priorSettings.tasks_dir -and (Split-Path -Leaf $priorSettings.tasks_dir) -cne $tasksLeaf) { throw 'Task directory name must match the configured project name; automatic migration is not supported.' }
$tasksDir = Join-Path $Workspace $tasksLeaf
if (-not $PSBoundParameters.ContainsKey('MainAgentName')) {
    $MainAgentName = $(if ($priorSettings -and $priorSettings.main_agent_name) { $priorSettings.main_agent_name } else { T '主agent export' 'Main agent' })
}
$ManagerName = $ManagerName.Trim()
# The main agent's permanent id: the orchestrator maps to it, so renaming the main agent or changing its
# management name never breaks the link. Reused from the service's own record, then from the old config.
$MainAgentId = ''
$savedProfile = Join-Path $DataDir 'state\profile.json'
if (Test-Path -LiteralPath $savedProfile) { $MainAgentId = (Get-Content -Encoding UTF8 -LiteralPath $savedProfile -Raw | ConvertFrom-Json).main_agent_id }
if (-not $MainAgentId -and $priorSettings) { $MainAgentId = $priorSettings.main_agent_id }
if (-not $MainAgentId) { $MainAgentId = [guid]::NewGuid().ToString() }
if ($ManagerName.Length -gt 100 -or $ManagerName -match '[\x00-\x1f\x7f]') { throw 'ManagerName must contain at most 100 printable characters.' }
$MainAgentName = $MainAgentName.Trim()
if ($MainAgentName.Length -lt 1 -or $MainAgentName.Length -gt 100 -or $MainAgentName -match '[\x00-\x1f\x7f]' -or $MainAgentName.StartsWith('--') -or $MainAgentName -in @('__proto__','constructor','prototype')) { throw 'Main agent name must contain 1 to 100 printable characters, without a leading --.' }
if ($LanAddress) {
    $parsedLan = $null
    if (-not [System.Net.IPAddress]::TryParse($LanAddress, [ref]$parsedLan) -or $parsedLan.AddressFamily -ne 'InterNetwork' -or [System.Net.IPAddress]::IsLoopback($parsedLan)) { throw 'LanAddress must be this computer''s IPv4 address on the local network.' }
    $LanAddress = $parsedLan.ToString()
}
$repo = $PSScriptRoot
$worker = Join-Path $repo 'worker'

function Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Have($command) { [bool](Get-Command $command -ErrorAction SilentlyContinue) }
function Write-Utf8($path, $text) { [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding $false)) }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Winget-Install($id) {
    if (-not (Have 'winget')) { throw (T "缺少 $id，并且没有 winget。请手动安装后加 -SkipPrerequisites 重新运行。" "$id is missing and winget is not available. Install it yourself, then rerun with -SkipPrerequisites.") }
    & winget install --id $id --exact --source winget --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw (T "安装 $id 失败" "Installing $id failed") }
    Refresh-Path
}
$warnings = New-Object System.Collections.Generic.List[string]

# 1. Node.js, Python, Tailscale
$tailscale = 'C:\Program Files\Tailscale\tailscale.exe'
if (-not $SkipPrerequisites) {
    Step (T '检查 Node.js、Python、Tailscale' 'Checking Node.js, Python and Tailscale')
    if (-not (Have 'node')) { Winget-Install 'OpenJS.NodeJS.LTS' }
    $hasPython = (Have 'py') -or (Have 'python')
    if (-not $hasPython) { Winget-Install 'Python.Python.3.12' }
    if (-not $LocalOnly -and -not (Test-Path $tailscale)) { Winget-Install 'Tailscale.Tailscale' }
    if ($Engine -eq 'claude' -and -not (Have 'git')) { Winget-Install 'Git.Git' }
}
if (-not (Have 'node')) { throw (T '找不到 Node.js' 'Node.js not found') }

# 2. Agent CLI
$nodeExe = (Get-Command node).Source
$codexJs = $null
if ($Engine -eq 'codex') {
Step (T "安装 Codex 命令行 $CodexVersion" "Installing the Codex CLI $CodexVersion")
$npmRoot = (& npm root -g).Trim()
$codexJs = Join-Path $npmRoot '@openai\codex\bin\codex.js'
$installed = $null
if (Test-Path $codexJs) { $installed = ((& node $codexJs --version) -replace '[^0-9.]', '').Trim() }
if ($installed -ne $CodexVersion) {
    & npm install -g "@openai/codex@$CodexVersion"
    if ($LASTEXITCODE -ne 0) { throw (T '安装 Codex 命令行失败' 'Installing the Codex CLI failed') }
}
# Windows PowerShell treats native stderr as errors even when login succeeds.
$loginErrorPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    & node $codexJs login status *> $null
    $loginExitCode = $LASTEXITCODE
} finally { $ErrorActionPreference = $loginErrorPreference }
if ($loginExitCode -ne 0) { $warnings.Add((T 'Codex 还没有登录：运行 codex login 用你的 ChatGPT 账号登录，否则任务会失败。' 'Codex is not signed in: run codex login with your ChatGPT account, or tasks will fail.')) }
} else {
    Step (T '准备 Claude Code 命令行' 'Preparing the Claude Code CLI')
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
            if ($LASTEXITCODE -ne 0) { throw (T 'Claude Code 安装失败' 'Installing Claude Code failed') }
            $ClaudeExecutable = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
        }
    }
    if (-not (Test-Path -LiteralPath $ClaudeExecutable)) { throw (T '找不到原生 claude.exe；可用 -ClaudeExecutable 指定路径' 'The native claude.exe was not found; pass its path with -ClaudeExecutable') }
    $claudeVersionText = (& $ClaudeExecutable --version | Out-String).Trim()
    if ($claudeVersionText -notmatch '(\d+\.\d+\.\d+)') { throw (T '无法确认 Claude Code 版本' 'Could not determine the Claude Code version') }
    $claudeVersion = $Matches[1]
    if ([version]$claudeVersion -lt [version]'2.1.248') { throw (T 'Claude Code 需要 2.1.248 或更新版本以支持 restricted 模式，请先升级' 'Claude Code 2.1.248 or newer is needed for restricted mode; upgrade it first') }
    $loginErrorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $ClaudeExecutable auth status *> $null
        $loginExitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $loginErrorPreference }
    if ($loginExitCode -ne 0) { $warnings.Add((T 'Claude Code 还没有登录：请本人运行 claude auth login，然后重新运行安装。' 'Claude Code is not signed in: the owner must run claude auth login, then rerun this setup.')) }
}

# 3. Python environment for the service
Step (T '准备 Python 环境' 'Preparing the Python environment')
New-Item -ItemType Directory -Force -Path $DataDir, (Join-Path $DataDir 'state'), $tasksDir, (Join-Path $Workspace 'shared') | Out-Null
$venv = Join-Path $DataDir 'venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    if (Have 'py') { & py -3 -m venv $venv } else { & python -m venv $venv }
    if ($LASTEXITCODE -ne 0) { throw (T '创建 Python 环境失败（需要 Python 3.10 以上）' 'Creating the Python environment failed (Python 3.10 or newer is needed)') }
}
& $venvPython -m pip install --quiet --disable-pip-version-check -r (Join-Path $worker 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw (T '安装 Python 依赖失败' 'Installing the Python packages failed') }

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
if (-not $LocalOnly -and -not $tsName) { $warnings.Add((T 'Tailscale 还没有登录：登录后重新运行本脚本，其他电脑才能连到这台。' 'Tailscale is not signed in: sign in, then rerun this script so other computers can reach this one.')) }

# 6. This computer's main agent
Step (T "创建主 Agent：$MainAgentName" "Creating the main agent: $MainAgentName")
$mainWorkspace = Join-Path $Workspace 'main-agent'
New-Item -ItemType Directory -Force -Path (Join-Path $mainWorkspace '.codex') | Out-Null
$agentsFile = Join-Path $mainWorkspace 'AGENTS.md'
if (-not (Test-Path $agentsFile)) {
    $template = [System.IO.File]::ReadAllText((Join-Path $worker (T 'templates\main-agent\AGENTS.md' 'templates\main-agent\AGENTS.en.md')))
    $permissionNote = if ($FullAccess) { T '子任务以完整权限运行，可操作桌面、浏览器和其他应用；所有权限请求自动批准。' 'Sub-tasks run with full access and can operate the desktop, browsers and other apps; every permission request is approved automatically.' } elseif ($Engine -eq 'claude') { T '子任务使用 Claude Code restricted 模式：文件工具限于工作目录和 shared 文件夹，命令执行关闭；这不是操作系统级沙箱。' 'Sub-tasks use Claude Code restricted mode: file tools are limited to the working folder and the shared folder, and command execution is off. This is not an OS-level sandbox.' } else { T '子任务使用 Codex workspace-write 沙箱，不自动批准额外权限。' 'Sub-tasks use the Codex workspace-write sandbox; extra permissions are not approved automatically.' }
    Write-Utf8 $agentsFile ($template.Replace('{{COMPUTER_NAME}}', $Name).Replace('{{WORKSPACE}}', $Workspace).Replace('{{TASKS_DIR_NAME}}', $tasksLeaf).Replace('{{AGENT_ENGINE}}', $Engine).Replace('{{MAIN_AGENT_NAME}}', $MainAgentName).Replace('{{PERMISSION_NOTE}}', $permissionNote))
}
if (-not (Test-Path (Join-Path $mainWorkspace 'CLAUDE.md'))) { Copy-Item -LiteralPath $agentsFile -Destination (Join-Path $mainWorkspace 'CLAUDE.md') }
if ($Engine -eq 'codex') {
$tomlNote = T '# 主 Agent 专用：只在本项目里启用派任务服务，子任务不会加载它。文件里含有访问令牌，不要分享。' '# For the main agent only: the dispatcher is enabled in this project alone, and sub-tasks do not load it. This file holds an access token; do not share it.'
$approveNote = T '# 后台运行时没人点“批准”，预先批准这个服务的工具调用（只对本项目生效）' '# Nobody is there to click "approve" in the background, so this service''s tool calls are pre-approved (this project only)'
Write-Utf8 (Join-Path $mainWorkspace '.codex\config.toml') @"
$tomlNote
[mcp_servers.codex_tasks]
url = "http://127.0.0.1:$Port/mcp"
http_headers = { Authorization = "Bearer $token" }
tool_timeout_sec = 120
$approveNote
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
if (-not (Test-Path $rulesFile)) { Copy-Item (Join-Path $worker (T 'templates\worker-rules.md' 'templates\worker-rules.en.md')) $rulesFile }

# 7. Service config
Step (T '写入本机配置' 'Writing this computer''s configuration')
$configPath = Join-Path $DataDir 'config.json'
if (Test-Path $configPath) { Copy-Item $configPath "$configPath.bak" -Force }
$allowedHosts = @("127.0.0.1:$Port", "localhost:$Port")
if ($tsName) { $allowedHosts += "${tsName}:$TailscalePort" }
if ($tsIp) { $allowedHosts += "${tsIp}:$TailscalePort" }
$remoteUrl = if ($tsName) { "http://${tsName}:$TailscalePort/mcp" } else { '' }
$lanUrl = if ($LanAddress) { "http://${LanAddress}:$Port/mcp" } else { '' }
if ($LanAddress) { $allowedHosts += "${LanAddress}:$Port" }
$config = [ordered]@{
    computer_name = $Name
    main_agent_name = $MainAgentName
    main_agent_id = $MainAgentId
    language = $Language
    port = $Port
    tailscale_port = $TailscalePort
    remote_url = $remoteUrl
    bind_host = $(if ($LanAddress) { '0.0.0.0' } else { '127.0.0.1' })
    lan_address = $LanAddress
    lan_url = $lanUrl
    python_executable = $venvPython
    codex_executable = $codexJs
    codex_launcher_executable = $nodeExe
    codex_launcher_args = @($codexJs, '-c', 'windows.sandbox="mxc"')
    codex_version = $CodexVersion
    tailscale_executable = $tailscale
    state_dir = (Join-Path $DataDir 'state')
    token_file = $tokenFile
    worker_rules_file = (Join-Path $Workspace 'worker-rules.md')
    tasks_dir = $tasksDir
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
# Codex sessions go into two desktop-app sidebar folders: what the main agent opened, and what other agents
# opened directly. Added once, so a user who changed or turned it off keeps that choice.
if ($Engine -eq 'codex' -and -not $config.Contains('codex_app_folders')) {
    $config['codex_app_folders'] = [ordered]@{ main = 'AgentFleet'; other = (T 'AgentFleet-其他' 'AgentFleet-Other') }
}
Write-Utf8 $configPath ($config | ConvertTo-Json -Depth 5)

# 8. Start at logon, inside the desktop session (needed to control the screen)
if (-not $NoAutostart) {
    Step (T '设置登录后自动启动' 'Adding autostart at logon')
    $startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'agent-fleet-worker.cmd'
    $localFlag = if ($LocalOnly) { ' -LocalOnly' } else { '' }
    Write-Utf8 $startup "@echo off`r`npowershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $worker 'start.ps1')`" -ConfigPath `"$configPath`"$localFlag`r`n"
}

# 9. Start now
if (-not $NoStart) {
Step (T '启动服务' 'Starting the service')
$startArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $worker 'start.ps1'), '-ConfigPath', $configPath)
if ($LocalOnly -or -not $tsName) { $startArgs += '-LocalOnly' }
& powershell @startArgs
if ($LASTEXITCODE -ne 0) { $warnings.Add((T '服务没有正常启动，看 state\server.stderr.log。' 'The service did not start properly; see state\server.stderr.log.')) }
}

# 10. Connection info for the orchestrator
$connection = [ordered]@{
    name = $Name
    engine = $Engine
    description = $Description
    local_url = "http://127.0.0.1:$Port/mcp"
    tailscale_url = $remoteUrl
    lan_url = $lanUrl
    token = $token
    main_agent_workspace = $mainWorkspace
    main_agent_name = $MainAgentName
    main_agent_id = $MainAgentId
    manager_name = $ManagerName
    language = $Language
    dashboard = $(if ($tsName) { "http://${tsName}:$TailscalePort/dashboard" } elseif ($LanAddress) { "http://${LanAddress}:$Port/dashboard" } else { "http://127.0.0.1:$Port/dashboard" })
}
$connectionFile = Join-Path $DataDir 'connection.json'
Write-Utf8 $connectionFile ($connection | ConvertTo-Json)

Write-Host ''
Write-Host (T "完成：$Name 已接入。" "Done: $Name is connected.") -ForegroundColor Green
Write-Host (T "接入信息：$connectionFile（含令牌，只发给你自己的管理端电脑）" "Connection info: $connectionFile (contains the token; send it only to your own orchestrator computer)")
Write-Host (T "在管理端电脑上接入：让 Claude 运行 add-computer，或 node <call-agent skill 文件夹>\fleet.mjs add-computer <connection.json 的路径>" "To connect it on the orchestrator: ask Claude to run add-computer, or node <call-agent skill folder>\fleet.mjs add-computer <path to connection.json>")
Write-Host (T "只读看板：$($connection.dashboard)" "Read-only dashboard: $($connection.dashboard)")
if ($LanAddress) { Write-Host (T "局域网地址：$lanUrl（明文 HTTP，只在可信网络使用；需要在 Windows 防火墙放行 TCP $Port 端口）" "LAN address: $lanUrl (plain HTTP, trusted networks only; allow TCP port $Port in Windows Firewall)") -ForegroundColor Yellow }
Write-Host $(if ($FullAccess) { T '子任务权限：完全访问' 'Sub-task permissions: full access' } else { T '子任务权限：受限' 'Sub-task permissions: restricted' })
if (-not $FullAccess) { Write-Host (T '当前子任务只能写自己的工作目录。要操作桌面和浏览器，用 -FullAccess 重新运行。' 'Sub-tasks can currently write only their own working folder. To let them operate the desktop and browsers, rerun with -FullAccess.') -ForegroundColor Yellow }
# A pairing code lets the orchestrator connect without copying connection.json (only when others can reach this computer).
if (-not $NoStart -and ($LanAddress -or ($tsName -and -not $LocalOnly))) {
    Write-Host ''
    & $venvPython (Join-Path $worker 'tools\pairing.py') --config $configPath
    if ($LASTEXITCODE -ne 0) { $warnings.Add((T '没能打开配对窗口；稍后运行 worker\pair.ps1 再试。' 'Could not open a pairing window; run worker\pair.ps1 later.')) }
    else { Write-Host (T '配对码过期了就运行 worker\pair.ps1 重新生成。' 'If the code expires, run worker\pair.ps1 for a new one.') }
}
foreach ($w in $warnings) { Write-Warning $w }
