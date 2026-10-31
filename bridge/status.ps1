$bridgeRoot = Split-Path -Parent $PSScriptRoot
$bridgeStatus = Join-Path $bridgeRoot 'bridge-status.json'
if (-not (Test-Path -LiteralPath $bridgeStatus)) {
    Write-Output 'WeChat bridge has not started.'
    exit 0
}
$snapshot = Get-Content -LiteralPath $bridgeStatus -Raw | ConvertFrom-Json
$running = Get-Process -Id $snapshot.pid -ErrorAction SilentlyContinue
[pscustomobject]@{
    Running = [bool]$running -and (([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $snapshot.updated_at) -lt 10)
    CodexConnected = $snapshot.codex_connected
    Model = $snapshot.model
    ReasoningEffort = $snapshot.reasoning_effort
    ActiveThread = $snapshot.active_thread
    Processing = $snapshot.processing
    QueuedMessages = $snapshot.queued
    WaitingRequests = ($snapshot.waiting -join ', ')
    ReceiveError = $snapshot.poll_error
}
