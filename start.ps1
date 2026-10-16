# GROK FOREX AI — Windows startup
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path "$Root\.env")) {
    Copy-Item "$Root\.env.example" "$Root\.env"
    Write-Host "Created .env from .env.example (no secrets). Add API keys later if needed."
}

$Py = "python"
if (Get-Command py -ErrorAction SilentlyContinue) {
    $ver = & py -3 --version 2>$null
    if ($LASTEXITCODE -eq 0) { $Py = "py -3" }
}

if (-not (Test-Path "$Root\backend\.venv")) {
    Write-Host "Creating Python virtual environment..."
    Invoke-Expression "$Py -m venv `"$Root\backend\.venv`""
}

$VenvPy = "$Root\backend\.venv\Scripts\python.exe"
& $VenvPy -m pip install --upgrade pip
& $VenvPy -m pip install -r "$Root\backend\requirements.txt"

if (-not (Test-Path "$Root\frontend\node_modules")) {
    Write-Host "Installing frontend dependencies..."
    Push-Location "$Root\frontend"
    npm install
    Pop-Location
}

Write-Host "Starting API on http://127.0.0.1:8000"
Write-Host "Starting UI on http://127.0.0.1:5173"
Write-Host "LIVE trading is disabled unless explicitly authorized in .env"

function Test-PortInUse([int]$Port) {
    try {
        $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        return $null -ne $c
    } catch {
        return $false
    }
}

if (Test-PortInUse 8000) {
    Write-Host "Porta 8000 já em uso — a tentar libertar processos uvicorn..."
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'uvicorn app.main' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    if (Test-PortInUse 8000) {
        Write-Host "ERRO: porta 8000 continua ocupada. Pare o processo manualmente e volte a correr start.ps1."
        exit 1
    }
}

Start-Process -FilePath $VenvPy -ArgumentList "-m", "uvicorn", "app.main:app", "--app-dir", "backend", "--host", "127.0.0.1", "--port", "8000" -WorkingDirectory $Root
Start-Sleep -Seconds 2
if (-not (Test-PortInUse 5173)) {
    Start-Process -FilePath "npm" -ArgumentList "run", "dev" -WorkingDirectory "$Root\frontend"
} else {
    Write-Host "Vite já a escutar em 5173 — não foi iniciado outro processo."
}

Write-Host "Opened backend and frontend processes. Open http://127.0.0.1:5173"
