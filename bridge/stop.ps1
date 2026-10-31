$ErrorActionPreference = 'Stop'
$bridgeRoot = Split-Path -Parent $PSScriptRoot
[System.IO.File]::WriteAllText((Join-Path $bridgeRoot 'bridge.stop'), 'stop', [System.Text.UTF8Encoding]::new($false))
Write-Output 'Stop requested. The current long poll can take up to 45 seconds to finish.'
