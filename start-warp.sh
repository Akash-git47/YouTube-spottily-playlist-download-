#!/bin/bash
# start-warp.sh — Start Cloudflare WARP proxy for YouTube traffic.
# Creates a SOCKS5 proxy on 127.0.0.1:1080 via wireproxy.
# YouTube traffic is routed through Cloudflare's residential-like IPs.
# Non-YouTube traffic goes through Render's normal network.

set -e

WARP_PORT=1080
WARP_HOST="127.0.0.1"
WARP_DIR="/app/warp"
WGCF="/usr/local/bin/wgcf"
WIREPROXY="/usr/local/bin/wireproxy"

# ---------- helper ----------
log() { echo "[warp] $*"; }

wait_for_proxy() {
    local i=0
    while [ $i -lt 30 ]; do
        if curl -s --proxy "socks5h://${WARP_HOST}:${WARP_PORT}" "https://ifconfig.me" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    return 1
}

# ---------- check binaries ----------
if [ ! -x "$WIREPROXY" ]; then
    log "ERROR: wireproxy binary not found at $WIREPROXY"
    exec gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 4 --timeout 300 --keep-alive 5 app:app
fi

if [ ! -x "$WGCF" ]; then
    log "ERROR: wgcf binary not found at $WGCF"
    exec gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 4 --timeout 300 --keep-alive 5 app:app
fi

# ---------- generate WARP credentials (if not already done) ----------
mkdir -p "$WARP_DIR"
cd "$WARP_DIR"

if [ ! -f "wgcf-account.toml" ] || [ ! -f "wgcf-profile.toml" ]; then
    log "Registering new WARP account..."
    $WGCF register --accept-tos 2>&1 || {
        log "WARP registration failed — starting without proxy"
        exec gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 4 --timeout 300 --keep-alive 5 app:app
    }
    log "Generating WireGuard profile..."
    $WGCF generate 2>&1 || {
        log "WireGuard profile generation failed — starting without proxy"
        exec gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 4 --timeout 300 --keep-alive 5 app:app
    }
    # Patch for wireproxy: set ConnectionType = 0 (TCP + UDP)
    sed -i 's/^ConnectionType.*/ConnectionType = 0/' wgcf-profile.toml 2>/dev/null || true
    log "WARP credentials generated."
else
    log "Using existing WARP credentials."
fi

# ---------- start wireproxy ----------
log "Starting wireproxy on ${WARP_HOST}:${WARP_PORT}..."
$WIREPROXY -c "$WARP_DIR/wgcf-profile.toml" -p "$WARP_PORT" &
WARP_PID=$!

# Wait for proxy to become ready
log "Waiting for WARP proxy..."
if wait_for_proxy; then
    EXTERNAL_IP=$(curl -s --proxy "socks5h://${WARP_HOST}:${WARP_PORT}" "https://ifconfig.me" 2>/dev/null || echo "unknown")
    log "WARP proxy ready! External IP: $EXTERNAL_IP"
else
    log "WARNING: WARP proxy did not become ready in time — YouTube may be blocked"
fi

# ---------- launch app ----------
log "Starting TubeFetch..."
exec gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 4 --timeout 300 --keep-alive 5 app:app
