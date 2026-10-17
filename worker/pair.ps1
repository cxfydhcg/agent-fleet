# 在这台 worker 上打开配对窗口，显示一次性配对码（10 分钟内有效）。管理端电脑用它接入，不用拷 connection.json。
# Opens a pairing window on this worker and shows a one-time code (valid 10 minutes) for the orchestrator.
param([string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'agent-fleet\config.json'))
$ErrorActionPreference = 'Stop'
$config = Get-Content -Encoding UTF8 -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
& $config.python_executable (Join-Path $PSScriptRoot 'tools\pairing.py') --config $ConfigPath
exit $LASTEXITCODE
