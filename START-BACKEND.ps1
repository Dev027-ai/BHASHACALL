$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot\backend
if (!(Test-Path '.env')) { Copy-Item '.env.example' '.env'; Write-Host 'Created backend/.env. Add your private Gnani API key before using speech features.' -ForegroundColor Yellow }
if (!(Test-Path '.venv\Scripts\python.exe')) { python -m venv .venv }
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
& '.\.venv\Scripts\python.exe' -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
