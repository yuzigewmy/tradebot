param([int]$Port = 8765)

$spotRoot = $PSScriptRoot
$spotPython = Join-Path $spotRoot '.venv\Scripts\python.exe'
$spotEnv = Join-Path $spotRoot '.env'
$spotArguments = @('-m', 'uvicorn', 'spot_agent.api:app', '--host', '127.0.0.1', '--port', $Port)
if (Test-Path -LiteralPath $spotEnv) {
    $spotArguments += @('--env-file', $spotEnv)
}
& $spotPython @spotArguments
exit $LASTEXITCODE
