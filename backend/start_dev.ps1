$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $root '.env'
if (-not (Test-Path $envPath)) {
    $envPath = Join-Path $PSScriptRoot '.env'
}
if (-not (Test-Path $envPath)) {
    throw "No .env file was found in $root or $PSScriptRoot. Copy .env.example and configure it before starting the backend."
}

foreach ($line in Get-Content $envPath) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
        $name = $matches[1]
        $value = $matches[2].Trim()
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}

$listen = Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue
if (-not $listen) {
    $outLog = Join-Path $PSScriptRoot 'backend.out.log'
    $errLog = Join-Path $PSScriptRoot 'backend.err.log'
    $process = Start-Process -FilePath 'python' -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8000' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
    Write-Output "backend_pid=$($process.Id)"
    Write-Output "stdout_log=$outLog"
    Write-Output "stderr_log=$errLog"
    Write-Output "tail -f:  Get-Content -Wait $errLog"
} else {
    Write-Output "backend_already_running=$($listen.OwningProcess)"
    Write-Output "already-running process holds old code; stop it first:  Stop-Process -Id $($listen.OwningProcess) -Force"
}
