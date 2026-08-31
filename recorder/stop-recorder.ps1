$ErrorActionPreference = 'Stop'
$RootPath = Split-Path -Parent $PSScriptRoot
$StatusPath = Join-Path $RootPath 'config\status.json'
$StopPath = Join-Path $RootPath 'config\stop.request'

if (-not (Test-Path -LiteralPath $StatusPath)) {
    Write-Host 'Recorder is not running (no status file).'
    exit 0
}

$Status = Get-Content -LiteralPath $StatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
$Process = Get-Process -Id $Status.SupervisorPid -ErrorAction SilentlyContinue
if (-not $Process) {
    Write-Host 'Recorder is not running (stale status file only).'
    exit 0
}

$Request = [ordered]@{
    RequestedKST = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz')
    RequestedBy = $env:USERNAME
} | ConvertTo-Json
Set-Content -LiteralPath $StopPath -Value $Request -Encoding UTF8
Write-Host ("Safe stop requested for recorder PID {0}." -f $Status.SupervisorPid)
Write-Host 'Waiting for the receiver (yt-dlp) to stop and FFmpeg to finalize the current MKV...'

for ($Second = 0; $Second -lt 45; $Second++) {
    Start-Sleep -Seconds 1
    if (-not (Get-Process -Id $Status.SupervisorPid -ErrorAction SilentlyContinue)) {
        Write-Host 'Recorder stopped safely.'
        exit 0
    }
}

Write-Warning 'Recorder is still finalizing after 45 seconds. Do not force-close it; check status/logs.'
exit 1
