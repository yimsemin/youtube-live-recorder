$ErrorActionPreference = 'SilentlyContinue'
$taskName = 'YouTubeRecorder Healthcheck'
$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName

if ($null -eq $task) {
    Write-Host 'Healthcheck task is not installed.'
    exit 1
}

Write-Host ('Task state      : {0}' -f $task.State)
Write-Host ('Last run        : {0}' -f $info.LastRunTime)
Write-Host ('Last result     : {0}' -f $info.LastTaskResult)
Write-Host ('Next run        : {0}' -f $info.NextRunTime)

$statePath = Join-Path $PSScriptRoot 'config\healthcheck-state.json'
if (Test-Path -LiteralPath $statePath) {
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    Write-Host ('Watchdog health : {0}' -f $state.LastHealth)
    Write-Host ('Last ping       : {0}' -f $state.LastPingKST)
    Write-Host ('Ping result     : {0}' -f $state.LastPingResult)
    if ($state.LastIssues.Count -gt 0) {
        Write-Host 'Issues:'
        $state.LastIssues | ForEach-Object { Write-Host ('  - {0}' -f $_) }
    }
}
