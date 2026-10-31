$ErrorActionPreference = 'Stop'
$bridgeRoot = Split-Path -Parent $PSScriptRoot
$bridgePython = Join-Path $bridgeRoot '.venv\Scripts\python.exe'
$bridgeEntry = Join-Path $PSScriptRoot 'wechat_bridge.py'

# Match the actual entry path, including before the first heartbeat or after a delayed heartbeat.
$entryPattern = '(?i)(?:^|[\s"])' + [regex]::Escape($bridgeEntry) + '(?:[\s"]|$)'
$running = Get-CimInstance -ClassName Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -match $entryPattern } |
    Select-Object -First 1
if ($running) {
    Write-Output 'WeChat bridge is already running.'
    exit 0
}

$bridgeStop = Join-Path $bridgeRoot 'bridge.stop'
if (Test-Path -LiteralPath $bridgeStop) {
    Remove-Item -LiteralPath $bridgeStop
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$bridgeProcess = Start-Process -FilePath $bridgePython -ArgumentList @('-u', ('"' + $bridgeEntry + '"')) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $bridgeRoot 'bridge-stdout.log') -RedirectStandardError (Join-Path $bridgeRoot 'bridge-stderr.log')
Write-Output "WeChat bridge started. PID: $($bridgeProcess.Id)"
