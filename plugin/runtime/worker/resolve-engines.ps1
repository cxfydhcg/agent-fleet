# Shared by the Windows entry points; resolve identities before any install or service operation.

if ($Engines -and $setupBound.ContainsKey('Engine')) { throw '不要同时指定 Engine 和 Engines。 / Pass Engine or Engines, not both.' }
if (-not $Engines) {
    if (-not $setupBound.ContainsKey('Engine') -and $priorSettings.engines) { $Engines = $priorSettings.engines -join ',' }
    elseif (-not $setupBound.ContainsKey('Engine') -and $priorSettings.executor -eq 'claude') { $Engines = 'claude' }
    else { $Engines = $Engine }
}
$engineList = @($Engines.Split(',') | ForEach-Object { $_.Trim().ToLowerInvariant() })
if ($engineList.Count -gt 2 -or @($engineList | Select-Object -Unique).Count -ne $engineList.Count -or @($engineList | Where-Object { $_ -notin @('codex','claude') }).Count) { throw '引擎必须为 codex、claude 或两个不同引擎。 / Engines must be codex, claude or both distinct engines.' }
$Engine = $engineList[0]
if ($SecondMainAgentName -and $engineList.Count -lt 2) { throw '第二个名字需要两个引擎。 / A second main agent name requires two engines.' }
$savedMains = $null
$savedMainProfile = Join-Path $DataDir 'state\profile.json'
if (Test-Path -LiteralPath $savedMainProfile) { $savedMains = Get-Content -LiteralPath $savedMainProfile -Encoding UTF8 -Raw | ConvertFrom-Json }
$mainAgents = @()
foreach ($agentEngine in $engineList) {
    $oldMain = @($priorSettings.main_agents | Where-Object { $_.engine -eq $agentEngine }) | Select-Object -First 1
    $oldDefault = $priorSettings -and -not $priorSettings.main_agents -and $agentEngine -eq $(if ($priorSettings.executor -eq 'claude') { 'claude' } else { 'codex' })
    $chosenName = if ($mainAgents.Count -eq 0) { $MainAgentName } else { $SecondMainAgentName }
    if (-not $chosenName) {
        $chosenName = if ($oldMain) { $oldMain.name } elseif ($oldDefault) { $priorSettings.main_agent_name } elseif ($agentEngine -eq 'claude') { T 'Claude主agent' 'Claude main agent' } else { T '主agent export' 'Main agent' }
    }
    if (-not $chosenName) { $chosenName = if ($agentEngine -eq 'claude') { T 'Claude主agent' 'Claude main agent' } else { T '主agent export' 'Main agent' } }
    $chosenName = $chosenName.Trim()
    if ($chosenName.Length -lt 1 -or $chosenName.Length -gt 100 -or $chosenName -match '[\x00-\x1f\x7f]' -or $chosenName.StartsWith('--') -or $chosenName -in @('__proto__','constructor','prototype') -or $chosenName -cin @($mainAgents.name)) { throw '主 Agent 名字无效或重复。 / Invalid or duplicate main agent name.' }
    $chosenId = if ($oldMain) { $oldMain.id } elseif ($oldDefault) { $priorSettings.main_agent_id } else { '' }
    $savedMain = @($savedMains.main_agents | Where-Object { $_.engine -eq $agentEngine }) | Select-Object -First 1
    if ($savedMain.id) { $chosenId = $savedMain.id } elseif ($oldDefault -and $savedMains.main_agent_id) { $chosenId = $savedMains.main_agent_id }
    if (-not $chosenId) { $chosenId = [guid]::NewGuid().ToString() }
    $chosenWorkspace = if ($oldMain) { $oldMain.workspace } elseif ($oldDefault -and $priorSettings.main_agent_workspaces) { $priorSettings.main_agent_workspaces[0] } elseif ($engineList.Count -eq 1) { Join-Path $Workspace 'main-agent' } else { Join-Path $Workspace "main-$agentEngine" }
    $mainAgents += [ordered]@{name=$chosenName;engine=$agentEngine;id=$chosenId;workspace=$chosenWorkspace}
}
$MainAgentName = $mainAgents[0].name
$MainAgentId = $mainAgents[0].id
$mainWorkspace = $mainAgents[0].workspace
