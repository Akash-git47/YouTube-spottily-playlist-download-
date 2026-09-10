#!/bin/bash
# start-warp.sh — Start Cloudflare WARP proxy for YouTube traffic.
# Creates a SOCKS5 proxy on 127.0.0.1:1080 via wireproxy.
# YouTube traffic is routed through Cloudflare's residential-like IPs.
# Non-YouTube traffic goes through Render's normal network.
# If anything fails, the app still starts without WARP.

WARP_PORT=1080
WARP_HOST="127.0.0.1"
WARP_DIR="/app/warp"
WGCF="/usr/local/bin/wgcf"
WIREPROXY="/usr/local/bin/wireproxy"

log() { echo "[warp] $*"; }

start_gunicorn() {
    log "Starting TubeFetch..."
    exec gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 4 --timeout 300 --keep-alive 5 app:app
}

# ---------- check binaries ----------
if [ ! -x "$WIREPROXY" ] || [ ! -x "$WGCF" ]; then
    log "WARP binaries not found — starting without proxy"
    start_gunicorn
fi

# ---------- generate WARP credentials ----------
mkdir -p "$WARP_DIR" || { log "Cannot create $WARP_DIR"; start_gunicorn; }
cd "$WARP_DIR" || { log "Cannot cd to $WARP_DIR"; start_gunicorn; }

if [ ! -f "wgcf-account.toml" ] || [ ! -f "wgcf-profile.toml" ]; then
    log "Registering new WARP account..."
    if ! $WGCF register --accept-tos; then
        log "WARP registration failed — starting without proxy"
        start_gunicorn
    fi
    log "Generating WireGuard profile..."
    if ! $WGCF generate; then
        log "WireGuard profile generation failed — starting without proxy"
        start_gunicorn
    fi
    sed -i 's/^ConnectionType.*/ConnectionType = 0/' wgcf-profile.toml 2>/dev/null || true
    log "WARP credentials generated."
else
    log "Using existing WARP credentials."
fi

# ---------- start wireproxy ----------
log "Starting wireproxy on ${WARP_HOST}:${WARP_PORT}..."
if ! $WIREPROXY -c "$WARP_DIR/wgcf-profile.toml" -p "$WARP_PORT" &>/tmp/wireproxy.log &
then
    log "wireproxy failed to start — starting without proxy"
    start_gunicorn
fi
WARP_PID=$!
sleep 3

# Check if wireproxy is still running
if ! kill -0 $WARP_PID 2>/dev/null; then
    log "wireproxy crashed — starting without proxy"
    cat /tmp/wireproxy.log 2>/dev/null || true
    start_gunicorn
fi

log "Wireproxy process is running (PID $WARP_PID). Waiting for connectivity..."
i=0
while [ $i -lt 10 ]; do
    if curl -s --proxy "socks5h://${WARP_HOST}:${WARP_PORT}" --max-time 5 "https://ifconfig.me" >/dev/null 2>&1; then
        EXTERNAL_IP=$(curl -s --proxy "socks5h://${WARP_HOST}:${WARP_PORT}" --max-time 5 "https://ifconfig.me" 2>/dev/null || echo "unknown")
        log "WARP proxy ready! External IP: $EXTERNAL_IP"
        break
    fi
    sleep 2
    i=$((i + 1))
done

if [ $i -ge 10 ]; then
    log "WARNING: WARP proxy not reachable in time — YouTube may be blocked"
fi

# ---------- launch app ----------
start_gunicorn
