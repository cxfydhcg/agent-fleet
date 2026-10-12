# 在管理端电脑（运行 Claude Code 的那台）上安装 orchestrator：/call-agent skill 和 fleet 客户端的数据目录。
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\install-orchestrator.ps1
$ErrorActionPreference = 'Stop'
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw '需要 Node.js 18 以上' }
$fleet = (Join-Path $PSScriptRoot 'orchestrator\fleet.mjs').Replace('\', '/')
$home_ = if ($env:AGENT_FLEET_HOME) { $env:AGENT_FLEET_HOME } else { Join-Path $env:USERPROFILE '.agent-fleet' }
New-Item -ItemType Directory -Force -Path (Join-Path $home_ 'tokens') | Out-Null

$skillDir = Join-Path $env:USERPROFILE '.claude\skills\call-agent'
New-Item -ItemType Directory -Force -Path $skillDir | Out-Null
$skill = Join-Path $skillDir 'SKILL.md'
if (Test-Path $skill) { Copy-Item $skill "$skill.bak" -Force }
$template = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot 'orchestrator\skills\call-agent\SKILL.md'))
[System.IO.File]::WriteAllText($skill, $template.Replace('{{FLEET}}', $fleet), (New-Object System.Text.UTF8Encoding $false))

Write-Host '完成。' -ForegroundColor Green
Write-Host "skill：$skill（在 Claude Code 里用 /call-agent）"
Write-Host "登记表和令牌：$home_"
Write-Host "接入电脑：node `"$fleet`" add-computer <那台电脑的 connection.json>"
Write-Host "查看总览：node `"$fleet`" overview"
