#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"
BACKEND_PID=""

cleanup() {
    echo ""
    echo "Shutting down..."
    if [ -n "$BACKEND_PID" ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
        kill "$BACKEND_PID" 2>/dev/null
        wait "$BACKEND_PID" 2>/dev/null || true
    fi
    exit 0
}

trap cleanup SIGINT SIGTERM

# Auto-create venv if missing
if [ ! -d "$VENV" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q -r "$ROOT/backend/requirements.txt"
fi

# Check deps installed
if ! "$VENV/bin/python" -c "import fastapi, docker" 2>/dev/null; then
    echo "Installing Python dependencies..."
    "$VENV/bin/pip" install -q -r "$ROOT/backend/requirements.txt"
fi

# Check Docker is running
if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker daemon is not running."
    exit 1
fi

echo "Starting QuickDocker backend..."
"$VENV/bin/uvicorn" backend.main:app \
    --host 127.0.0.1 --port 8000 --reload \
    --app-dir "$ROOT" &
BACKEND_PID=$!

# Wait for backend to be ready
echo -n "Waiting for backend"
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
        echo " ready!"
        break
    fi
    echo -n "."
    sleep 1
done

if ! curl -sf http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
    echo " FAILED"
    echo "Backend did not start. Check logs above."
    cleanup
    exit 1
fi

if [ "${1:-}" = "--tauri" ]; then
    echo "Starting Tauri desktop app..."
    cd "$ROOT"
    npx tauri dev 2>&1 &
    TAURI_PID=$!
    wait "$TAURI_PID" 2>/dev/null || true
else
    echo ""
    echo "QuickDocker running at: http://localhost:8000"
    echo "Press Ctrl+C to stop."
    echo ""
    wait "$BACKEND_PID"
fi
