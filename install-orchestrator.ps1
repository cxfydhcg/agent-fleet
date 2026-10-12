# 在管理端电脑上安装 /call-agent skill（不用 Claude Code 插件市场时用这个）。
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\install-orchestrator.ps1          装到 Claude Code
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\install-orchestrator.ps1 -Codex   同时装到 Codex
# 用插件市场的话，在 Claude Code 里运行：/plugin marketplace add cxfydhcg/agent-fleet
param([switch]$Codex)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw '需要 Node.js 18 以上' }
$source = Join-Path $PSScriptRoot 'plugin\skills\call-agent'
$fleetHome = if ($env:AGENT_FLEET_HOME) { $env:AGENT_FLEET_HOME } else { Join-Path $env:USERPROFILE '.agent-fleet' }
New-Item -ItemType Directory -Force -Path (Join-Path $fleetHome 'tokens') | Out-Null

$targets = @(Join-Path $env:USERPROFILE '.claude\skills\call-agent')
if ($Codex) { $targets += Join-Path $env:USERPROFILE '.codex\skills\call-agent' }
foreach ($target in $targets) {
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Copy-Item (Join-Path $source '*') $target -Recurse -Force
    Write-Host "已安装：$target"
}
$fleet = Join-Path $targets[0] 'fleet.mjs'
Write-Host '完成。' -ForegroundColor Green
Write-Host "登记表和令牌：$fleetHome"
Write-Host "接入电脑：node `"$fleet`" add-computer <那台电脑的 connection.json>"
Write-Host "查看总览：node `"$fleet`" overview"
