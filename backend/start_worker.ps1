# Start the Celery worker for local Windows development.
#
# Why this script exists: paper generation runs inside the worker process, and the
# documented manual start
#     cd backend
#     celery -A app.infrastructure.tasks.celery_app worker --loglevel=INFO
# never loads the repository-root .env in any visible way. app/config.py now looks
# for .env by walking up from __file__, but a worker started from a shell that also
# happens to define DEEPSEEK_* would still silently override it, and a hand-started
# worker prints nothing about which endpoint it ended up using.
#
# This mirrors start_dev.ps1: export the repo-root .env into the Process
# environment first, then start the worker, then print the effective LLM endpoint so
# the "which base_url am I actually using" question is answerable at a glance.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 decodes a .ps1 without a
# UTF-8 BOM using the machine ANSI codepage, and non-ASCII comments get mangled hard
# enough to swallow adjacent statements.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $root '.env'
if (-not (Test-Path $envPath)) {
    $envPath = Join-Path $PSScriptRoot '.env'
}
if (-not (Test-Path $envPath)) {
    throw "No .env file was found in $root or $PSScriptRoot. Copy .env.example and configure it before starting the worker."
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

# Windows has no prefork pool; celery refuses to start without an explicit one.
# solo always works; override with the CELERY_POOL env var (e.g. threads).
$pool = ([string]$env:CELERY_POOL).Trim()
if ([string]::IsNullOrWhiteSpace($pool)) { $pool = 'solo' }
$loglevel = ([string]$env:CELERY_LOGLEVEL).Trim()
if ([string]::IsNullOrWhiteSpace($loglevel)) { $loglevel = 'INFO' }

$existing = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'celery.exe'" |
    Where-Object { $_.CommandLine -like '*celery_app*' -or $_.CommandLine -like '*-A app.infrastructure.tasks.celery_app*' }
if ($existing) {
    Write-Output "worker_already_running=$($existing.ProcessId -join ',')"
    Write-Output "An already-running worker keeps the settings it was started with."
    Write-Output "Stop it first so the new .env takes effect:"
    Write-Output "  Stop-Process -Id $($existing.ProcessId -join ',') -Force"
    exit 0
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

$outLog = Join-Path $PSScriptRoot 'worker.out.log'
$errLog = Join-Path $PSScriptRoot 'worker.err.log'
$process = Start-Process -FilePath $python `
    -ArgumentList '-m', 'celery', '-A', 'app.infrastructure.tasks.celery_app', 'worker',
                  "--pool=$pool", "--loglevel=$loglevel" `
    -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru

Start-Sleep -Seconds 4
if ($process.HasExited) {
    Write-Output "ERROR: worker failed to start (pid $($process.Id)); see $errLog"
    Get-Content $errLog -Tail 20
    exit 1
}

Write-Output "worker_pid=$($process.Id)  pool=$pool  loglevel=$loglevel"
Write-Output "stdout_log=$outLog"
Write-Output "stderr_log=$errLog"
Write-Output "tail -f:  Get-Content -Wait $errLog"
Write-Output "--- effective LLM config (from $envPath) ---"
Select-String -Path $envPath -Pattern '^(DEEPSEEK_BASE_URL|DEEPSEEK_MODEL)=' | ForEach-Object { "  $($_.Line)" }
Write-Output "During generation the log must show: DeepSeekGateway ... base_url=<the value above>"
