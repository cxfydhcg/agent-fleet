param(
    [ValidateSet('Codex','ClaudeCode','Both')][string]$Client = 'Codex'
)
$ErrorActionPreference = 'Stop'
$commands = @()
if ($Client -in @('Codex','Both')) {
    $codexCommand = Get-Command codex -ErrorAction SilentlyContinue
    if (-not $codexCommand) { throw 'Install the Codex CLI first, or select -Client ClaudeCode.' }
    $commands += [pscustomobject]@{Path=$codexCommand.Source;Name='Codex'}
}
if ($Client -in @('ClaudeCode','Both')) {
    $claudeCommand = Get-Command claude -ErrorAction SilentlyContinue
    if (-not $claudeCommand) { throw 'Install Claude Code first, or select -Client Codex.' }
    $commands += [pscustomobject]@{Path=$claudeCommand.Source;Name='ClaudeCode'}
}
function Invoke-PluginClient($executable, $arguments) {
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $executable @arguments
        $commandExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $savedPreference }
    if ($commandExit -ne 0) { throw 'Plugin installation command failed. See the client output above.' }
}
foreach ($command in $commands) {
    Invoke-PluginClient $command.Path @('plugin','marketplace','add',$PSScriptRoot)
    $verb = if ($command.Name -eq 'Codex') { 'add' } else { 'install' }
    Invoke-PluginClient $command.Path @('plugin',$verb,'agent-fleet@agent-fleet')
}
Write-Output 'Agent Fleet installed. Open a new chat, run setup, and choose orchestrator, worker, or both.'
