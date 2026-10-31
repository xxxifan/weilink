$ErrorActionPreference = 'Stop'
$bridgeRoot = Split-Path -Parent $PSScriptRoot
$bridgePython = Join-Path $bridgeRoot '.venv\Scripts\python.exe'
$bridgeEntry = Join-Path $PSScriptRoot 'wechat_bridge.py'
$bridgeStatus = Join-Path $bridgeRoot 'bridge-status.json'
if (Test-Path -LiteralPath $bridgeStatus) {
    $snapshot = Get-Content -LiteralPath $bridgeStatus -Raw | ConvertFrom-Json
    $running = Get-Process -Id $snapshot.pid -ErrorAction SilentlyContinue
    if ($running -and ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $snapshot.updated_at) -lt 10) {
        Write-Output 'WeChat bridge is already running.'
        exit 0
    }
}
$bridgeStop = Join-Path $bridgeRoot 'bridge.stop'
if (Test-Path -LiteralPath $bridgeStop) {
    Remove-Item -LiteralPath $bridgeStop
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$bridgeProcess = Start-Process -FilePath $bridgePython -ArgumentList @('-u', ('"' + $bridgeEntry + '"')) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $bridgeRoot 'bridge-stdout.log') -RedirectStandardError (Join-Path $bridgeRoot 'bridge-stderr.log')
Write-Output "WeChat bridge started. PID: $($bridgeProcess.Id)"
