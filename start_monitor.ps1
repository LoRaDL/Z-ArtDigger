# ============================================================
#  Z-ArtDigger Monitor Launcher
#  Usage: Run .\start_monitor.ps1 from the project root
# ============================================================

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=== Z-ArtDigger Monitor Launcher ===" -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot" -ForegroundColor Gray

# ---------- Start Redis ----------
Write-Host "`n[1/3] Starting Redis server ..." -ForegroundColor Yellow

$RedisArgs = @{
    FilePath     = "powershell"
    ArgumentList = "-NoExit", "-Command", "redis-server"
    WindowStyle  = "Normal"
}
Start-Process @RedisArgs

Start-Sleep -Seconds 1

# ---------- Start Backend ----------
Write-Host "[2/3] Starting backend (FastAPI @ http://localhost:8000) ..." -ForegroundColor Yellow

$BackendArgs = @{
    FilePath     = "powershell"
    ArgumentList = "-NoExit", "-Command",
                   "cd '$ProjectRoot'; python -m uvicorn monitor.api:app --host 0.0.0.0 --port 8000 --reload"
    WindowStyle  = "Normal"
}
Start-Process @BackendArgs

Start-Sleep -Seconds 1

# ---------- Start Frontend ----------
Write-Host "[3/3] Starting frontend (Vite @ http://localhost:5173) ..." -ForegroundColor Yellow

$FrontendArgs = @{
    FilePath     = "powershell"
    ArgumentList = "-NoExit", "-Command",
                   "cd '$ProjectRoot\monitor\web'; npm run dev"
    WindowStyle  = "Normal"
}
Start-Process @FrontendArgs

Write-Host "`n[OK] All 3 services launched in separate windows:" -ForegroundColor Green
Write-Host "   Redis      -> localhost:6379" -ForegroundColor White
Write-Host "   Backend    -> http://localhost:8000/docs" -ForegroundColor White
Write-Host "   Frontend   -> http://localhost:5173" -ForegroundColor White
Write-Host ""
