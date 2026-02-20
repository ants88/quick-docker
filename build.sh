#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"

# Ensure Rust is in PATH
[ -f "$HOME/.cargo/env" ] && source "$HOME/.cargo/env"
TARGET_TRIPLE="$(rustc -vV | grep host | cut -d' ' -f2)"

echo "=== QuickDocker Build ==="
echo "Target: $TARGET_TRIPLE"
echo ""

# --- Step 1: Ensure venv & deps ---
if [ ! -d "$VENV" ]; then
    echo "[1/5] Creating Python venv..."
    python3 -m venv "$VENV"
fi

echo "[1/5] Installing Python dependencies..."
"$VENV/bin/pip" install -q -r "$ROOT/backend/requirements.txt" pyinstaller

# --- Step 2: Bundle backend with PyInstaller ---
echo "[2/5] Building backend binary with PyInstaller..."
cd "$ROOT/backend"

"$VENV/bin/pyinstaller" \
    --onefile \
    --name quickdocker-backend \
    --clean \
    --noconfirm \
    --log-level WARN \
    --add-data "docker_manager.py:." \
    --hidden-import docker_manager \
    --hidden-import uvicorn.logging \
    --hidden-import uvicorn.loops \
    --hidden-import uvicorn.loops.auto \
    --hidden-import uvicorn.protocols \
    --hidden-import uvicorn.protocols.http \
    --hidden-import uvicorn.protocols.http.auto \
    --hidden-import uvicorn.protocols.websockets \
    --hidden-import uvicorn.protocols.websockets.auto \
    --hidden-import uvicorn.lifespan \
    --hidden-import uvicorn.lifespan.on \
    --hidden-import uvicorn.lifespan.off \
    --collect-submodules uvicorn \
    --collect-submodules uvloop \
    --collect-submodules httptools \
    run_server.py

cd "$ROOT"

# --- Step 3: Place binary for Tauri sidecar ---
echo "[3/5] Placing sidecar binary..."
cp "$ROOT/backend/dist/quickdocker-backend" \
   "$ROOT/src-tauri/quickdocker-backend-${TARGET_TRIPLE}"

# Verify it works
echo -n "    Testing binary... "
timeout 5 "$ROOT/src-tauri/quickdocker-backend-${TARGET_TRIPLE}" &
BACKEND_PID=$!
sleep 3
if curl -sf http://127.0.0.1:8000/api/health > /dev/null 2>&1; then
    echo "OK"
else
    echo "WARNING: binary health check failed (Docker might not be running)"
fi
kill $BACKEND_PID 2>/dev/null; wait $BACKEND_PID 2>/dev/null || true

# --- Step 4: Install npm deps ---
echo "[4/5] Installing npm dependencies..."
cd "$ROOT"
npm install --silent 2>/dev/null

# --- Step 5: Build Tauri .deb ---
echo "[5/5] Building Tauri .deb package..."
npx tauri build 2>&1

echo ""
echo "=== Build complete ==="
echo ""
# Find the .deb
DEB=$(find "$ROOT/src-tauri/target/release/bundle/deb" -name "*.deb" 2>/dev/null | head -1)
if [ -n "$DEB" ]; then
    echo "Package: $DEB"
    echo "Size: $(du -h "$DEB" | cut -f1)"
    echo ""
    echo "Install with:"
    echo "  sudo dpkg -i $DEB"
else
    echo "WARNING: .deb not found. Check build output above for errors."
fi
