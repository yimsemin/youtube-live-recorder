$ErrorActionPreference = 'Stop'
$RootPath = Split-Path -Parent $PSScriptRoot
$StatusPath = Join-Path $RootPath 'config\status.json'
$LogPath = Join-Path $RootPath 'logs\recorder.log'

if (-not (Test-Path -LiteralPath $StatusPath)) {
    Write-Host 'State              : not started (no status file)'
    Write-Host "Recordings folder  : $RecordingsPath"
    Write-Host "Log file           : $LogPath"
    exit 1
}

$Status = Get-Content -LiteralPath $StatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
$RecordingsPath = if ($Status.RecordingDirectory) { [string]$Status.RecordingDirectory } else { Join-Path $RootPath 'recordings' }
$Alive = $false
if ($Status.SupervisorPid) {
    $Alive = $null -ne (Get-Process -Id $Status.SupervisorPid -ErrorAction SilentlyContinue)
}
$Drive = [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($RecordingsPath))
$ActiveFilter = if ($Status.OutputPattern) { [System.IO.Path]::GetFileName([string]$Status.OutputPattern).Replace('%03d','*') } else { '*.mkv' }
$Newest = Get-ChildItem -LiteralPath $RecordingsPath -Filter $ActiveFilter -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

Write-Host ("State              : {0}" -f $Status.State)
Write-Host ("Supervisor alive   : {0}" -f $Alive)
Write-Host ("Supervisor PID     : {0}" -f $Status.SupervisorPid)
$ReceiverKind = if ($Status.ReceiverKind) { [string]$Status.ReceiverKind } else { 'yt-dlp' }
Write-Host ("Receiver           : {0} (PID {1})" -f $ReceiverKind, $Status.ReceiverPid)
Write-Host ("FFmpeg PID         : {0}" -f $Status.FfmpegPid)
Write-Host ("Last status (KST)  : {0}" -f $Status.UpdatedKST)
Write-Host ("HLS sequences      : {0} .. {1}" -f $Status.FirstSequence, $Status.LastSequence)
Write-Host ("Recordings folder  : {0}" -f $RecordingsPath)
Write-Host ("Segment duration   : {0} hours" -f $Status.SegmentHours)
Write-Host ("Free disk          : {0:N2} GB" -f ($Drive.AvailableFreeSpace / 1GB))
Write-Host ("Protected minimum  : {0} GB" -f $Status.MinimumFreeSpaceGB)
if ($Newest) {
    Write-Host ("Newest recording   : {0}" -f $Newest.FullName)
    Write-Host ("Newest size        : {0:N2} GB" -f ($Newest.Length / 1GB))
    Write-Host ("Newest modified    : {0:yyyy-MM-dd HH:mm:ss}" -f $Newest.LastWriteTime)
} else {
    Write-Host 'Newest recording   : none yet'
}
Write-Host ("Message            : {0}" -f $Status.Message)
Write-Host ("Log file           : {0}" -f $LogPath)

if (-not $Alive) { exit 1 }
