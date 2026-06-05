#!/bin/bash

# ============================================================
#  Z-ArtDigger Monitor Launcher (macOS/Linux)
#  Usage: ./start_monitor.sh from the project root
# ============================================================

# Get project root directory
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "\033[36m=== Z-ArtDigger Monitor Launcher ===\033[0m"
echo -e "\033[90mProject root: $PROJECT_ROOT\033[0m"

# Array to keep track of spawned process PIDs
PIDS=()

# Cleanup function to terminate all spawned background processes
cleanup() {
    echo -e "\n\033[31m[STOP] Shutting down all services...\033[0m"
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
        fi
    done
    exit 0
}

# Trap Ctrl+C (SIGINT) and SIGTERM to call cleanup
trap cleanup SIGINT SIGTERM

# ---------- Start Redis ----------
echo -e "\n\033[33m[1/3] Starting Redis server ...\033[0m"
redis-server > /dev/null 2>&1 &
REDIS_PID=$!
PIDS+=($REDIS_PID)
sleep 1

# ---------- Start Backend ----------
echo -e "\033[33m[2/3] Starting backend (FastAPI @ http://localhost:8000) ...\033[0m"
cd "$PROJECT_ROOT"
python3 -m uvicorn monitor.api:app --host 0.0.0.0 --port 8000 --reload &
BACKEND_PID=$!
PIDS+=($BACKEND_PID)
sleep 1

# ---------- Start Frontend ----------
echo -e "\033[33m[3/3] Starting frontend (Vite @ http://localhost:5173) ...\033[0m"
cd "$PROJECT_ROOT/monitor/web"
npm run dev &
FRONTEND_PID=$!
PIDS+=($FRONTEND_PID)

echo -e "\n\033[32m[OK] All services launched successfully!\033[0m"
echo -e "   Redis      -> localhost:6379"
echo -e "   Backend    -> http://localhost:8000/docs"
echo -e "   Frontend   -> http://localhost:5173"
echo -e "\nPress [Ctrl+C] to stop all services."

# Keep the script running to keep trapping Ctrl+C
wait
