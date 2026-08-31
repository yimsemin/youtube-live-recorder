param([switch]$NoStart)
$ErrorActionPreference = 'Stop'

$taskName = 'YouTubeRecorder Healthcheck'
$root = $PSScriptRoot
$pythonw = @(Join-Path $root 'recorder\python\pythonw.exe') + @(
    Get-ChildItem -LiteralPath (Join-Path $root 'recorder') -Directory -Filter 'streamlink-*-x86_64' |
        Sort-Object Name -Descending |
        ForEach-Object { Join-Path $_.FullName 'Python\pythonw.exe' }
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
$script = Join-Path $root 'recorder\healthcheck.py'

if (-not (Test-Path -LiteralPath $pythonw)) {
    throw "Python runtime was not found: $pythonw"
}
if (-not (Test-Path -LiteralPath $script)) {
    throw "Healthcheck script was not found: $script"
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument ('"{0}"' -f $script) -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 1)
$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentIdentity -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Checks YouTube recorder health every minute and reports to Healthchecks.io.' `
    -Force | Out-Null

if ($NoStart) {
    Disable-ScheduledTask -TaskName $taskName | Out-Null
    Write-Host "Installed (disabled): $taskName"
} else {
    Start-ScheduledTask -TaskName $taskName
    Write-Host "Installed and started: $taskName"
}
