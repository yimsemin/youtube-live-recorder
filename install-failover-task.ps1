param([switch]$NoStart)
$ErrorActionPreference = 'Stop'

$taskName = 'YouTubeRecorder Failover Guardian'
$root = $PSScriptRoot
$script = Join-Path $root 'ensure-failover-guardian.pyw'
if (-not (Test-Path -LiteralPath $script)) {
    throw "Guardian launcher was not found: $script"
}

$pythonw = @(Join-Path $root 'recorder\python\pythonw.exe') + @(
    Get-ChildItem -LiteralPath (Join-Path $root 'recorder') -Directory -Filter 'streamlink-*-x86_64' |
        Sort-Object Name -Descending |
        ForEach-Object { Join-Path $_.FullName 'Python\pythonw.exe' }
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $pythonw) {
    throw 'Portable pythonw.exe was not found'
}
$action = New-ScheduledTaskAction -Execute $pythonw -Argument ('"{0}"' -f $script) -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 1)
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description 'Ensures the local failover guardian stays running.' -Force | Out-Null
if ($NoStart) {
    Disable-ScheduledTask -TaskName $taskName | Out-Null
    Write-Host "Installed (disabled): $taskName"
} else {
    Start-ScheduledTask -TaskName $taskName
    Write-Host "Installed and started: $taskName"
}
