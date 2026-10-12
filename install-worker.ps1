# 把这台 Windows 电脑接入 agent fleet，成为一台被管理的电脑（worker）。
# 在这台电脑上、用平时登录的 Windows 用户运行（不需要管理员）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\install-worker.ps1 -FullAccess
# 可以重复运行：令牌、主agent export 的说明文件和已有数据会保留。
param(
    [string]$Name = $env:COMPUTERNAME.ToLower(),
    [string]$Description = '',
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'agent-fleet'),
    [int]$Port = 8765,
    [int]$TailscalePort = 8766,
    [switch]$FullAccess,          # 子任务不设沙箱并自动同意权限请求，才能操作桌面、浏览器和其他程序
    [string]$CodexVersion = '0.162.1',
    [switch]$SkipPrerequisites,   # 不用 winget 安装 Node.js、Python、Tailscale
    [switch]$NoAutostart
)
$ErrorActionPreference = 'Stop'
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
    if (-not (Test-Path $tailscale)) { Winget-Install 'Tailscale.Tailscale' }
}
if (-not (Have 'node')) { throw '找不到 Node.js' }

# 2. Codex CLI
Step "安装 Codex 命令行 $CodexVersion"
$npmRoot = (& npm root -g).Trim()
$codexJs = Join-Path $npmRoot '@openai\codex\bin\codex.js'
$installed = $null
if (Test-Path $codexJs) { $installed = ((& node $codexJs --version) -replace '[^0-9.]', '').Trim() }
if ($installed -ne $CodexVersion) {
    & npm install -g "@openai/codex@$CodexVersion"
    if ($LASTEXITCODE -ne 0) { throw '安装 Codex 命令行失败' }
}
& node $codexJs login status *> $null
if ($LASTEXITCODE -ne 0) { $warnings.Add('Codex 还没有登录：运行 codex login 用你的 ChatGPT 账号登录，否则任务会失败。') }
$nodeExe = (Get-Command node).Source

# 3. Python environment for the service
Step '准备 Python 环境'
New-Item -ItemType Directory -Force -Path $DataDir, (Join-Path $DataDir 'state'), (Join-Path $DataDir 'tasks') | Out-Null
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
    $token = (Get-Content -LiteralPath $tokenFile -Raw).Trim()
} else {
    $bytes = New-Object byte[] 48
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
    Write-Utf8 $tokenFile $token
}

# 5. Tailscale address
$tsName = $null; $tsIp = $null
if (Test-Path $tailscale) {
    try {
        $status = & $tailscale status --json | ConvertFrom-Json
        if ($status.Self -and $status.BackendState -eq 'Running') {
            $tsName = $status.Self.DNSName.TrimEnd('.')
            $tsIp = @($status.Self.TailscaleIPs)[0]
        }
    } catch {}
}
if (-not $tsName) { $warnings.Add('Tailscale 还没有登录：登录后重新运行本脚本，其他电脑才能连到这台。') }

# 6. This computer's main agent
Step '创建主agent export'
$mainWorkspace = Join-Path $DataDir 'tasks\main-agent-export'
New-Item -ItemType Directory -Force -Path (Join-Path $mainWorkspace '.codex') | Out-Null
$agentsFile = Join-Path $mainWorkspace 'AGENTS.md'
if (-not (Test-Path $agentsFile)) {
    $template = [System.IO.File]::ReadAllText((Join-Path $worker 'templates\main-agent\AGENTS.md'))
    Write-Utf8 $agentsFile ($template.Replace('{{COMPUTER_NAME}}', $Name))
}
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
$codexConfig = Join-Path $env:USERPROFILE '.codex\config.toml'
New-Item -ItemType Directory -Force -Path (Split-Path $codexConfig) | Out-Null
$trustKey = $mainWorkspace.ToLower()
$existing = if (Test-Path $codexConfig) { [System.IO.File]::ReadAllText($codexConfig) } else { '' }
if (-not $existing.Contains("[projects.'$trustKey']")) {
    [System.IO.File]::AppendAllText($codexConfig, "`n[projects.'$trustKey']`ntrust_level = `"trusted`"`n", (New-Object System.Text.UTF8Encoding $false))
}

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
    tasks_dir = (Join-Path $DataDir 'tasks')
    allowed_roots = @((Join-Path $DataDir 'tasks'))
    allowed_hosts = $allowedHosts
    main_agent_workspaces = @($mainWorkspace)
    executor = 'app-server'
    sandbox = $(if ($FullAccess) { 'danger-full-access' } else { 'workspace-write' })
    auto_approve = [bool]$FullAccess
    max_timeout_seconds = 7200
    writer_wait_seconds = 10
    fork_when_busy = $false
}
Write-Utf8 $configPath ($config | ConvertTo-Json -Depth 5)

# 8. Start at logon, inside the desktop session (needed to control the screen)
if (-not $NoAutostart) {
    Step '设置登录后自动启动'
    $startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'agent-fleet-worker.cmd'
    Write-Utf8 $startup "@echo off`r`npowershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $worker 'start.ps1')`" -ConfigPath `"$configPath`"`r`n"
}

# 9. Start now
Step '启动服务'
$startArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $worker 'start.ps1'), '-ConfigPath', $configPath)
if (-not $tsName) { $startArgs += '-LocalOnly' }
& powershell @startArgs
if ($LASTEXITCODE -ne 0) { $warnings.Add('服务没有正常启动，看 state\server.stderr.log。') }

# 10. Connection info for the orchestrator
$connection = [ordered]@{
    name = $Name
    description = $Description
    local_url = "http://127.0.0.1:$Port/mcp"
    tailscale_url = $remoteUrl
    token = $token
    main_agent_workspace = $mainWorkspace
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
