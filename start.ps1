param(
    [int]$Port = 8765,
    [switch]$Live,
    [switch]$Background
)

$spotRoot = $PSScriptRoot
$spotPython = Join-Path $spotRoot '.venv\Scripts\python.exe'
$spotArguments = @('-m', 'uvicorn', 'spot_agent.api:app', '--host', '127.0.0.1', '--port', $Port, '--no-proxy-headers')
if (Test-Path -LiteralPath (Join-Path $spotRoot '.env')) {
    $spotArguments += @('--env-file', '.env')
}
if ($Live) {
    if (-not (Test-Path -LiteralPath (Join-Path $spotRoot '.env.binance-live') -PathType Leaf)) {
        throw '缺少私有实盘配置 .env.binance-live'
    }
    $spotArguments = @('-m', 'dotenv', '-f', '.env.binance-live', 'run', '--override', $spotPython) + $spotArguments
}

if ($Background) {
    $logDirectory = Join-Path $spotRoot 'logs'
    $logPrefix = Join-Path $logDirectory "tradebot-$Port-$(Get-Date -Format 'yyyyMMdd-HHmmss-fff')"
    $spotCommandLine = foreach ($argument in $spotArguments) {
        if ($argument -match '\s') { '"' + $argument + '"' } else { [string]$argument }
    }
    Start-Process -FilePath $spotPython -ArgumentList $spotCommandLine -WorkingDirectory $spotRoot -WindowStyle Hidden `
        -RedirectStandardOutput "$logPrefix.stdout.log" -RedirectStandardError "$logPrefix.stderr.log" -PassThru
    return
}

Push-Location -LiteralPath $spotRoot
try {
    & $spotPython @spotArguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
