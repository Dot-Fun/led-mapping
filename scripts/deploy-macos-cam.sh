#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <ssh-host> [port]"
  echo "Example: $0 sb1"
  exit 1
fi

HOST="$1"
PORT="${2:-8781}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$ROOT_DIR/macos-cam/.build/release/macos-cam"

if [[ ! -x "$BIN" ]]; then
  echo "Building macos-cam release binary..."
  (cd "$ROOT_DIR/macos-cam" && swift build -c release)
fi

echo "Copying macos-cam to $HOST:~/ledmap-macos-cam ..."
scp -q "$BIN" "$HOST:~/ledmap-macos-cam"
ssh "$HOST" "chmod +x ~/ledmap-macos-cam"

AUTH=$(ssh "$HOST" 'swift -e "import AVFoundation; print(AVCaptureDevice.authorizationStatus(for: .video).rawValue)"' 2>/dev/null || echo "0")
TS_IP=$(ssh "$HOST" 'tailscale ip -4 2>/dev/null || ipconfig getifaddr en0 2>/dev/null || echo ""')

echo "Deployed ~/ledmap-macos-cam to $HOST (IP: ${TS_IP:-unknown}, camera auth rawValue=$AUTH)."
if [[ "$AUTH" != "3" ]]; then
  echo ""
  echo "NOTE: Camera permission on $HOST is not yet granted (rawValue=$AUTH)."
  echo "Open Terminal once on $HOST's desktop and run:"
  echo "  ~/ledmap-macos-cam --port $PORT"
  echo "Click 'Allow' on the macOS Camera permission prompt. After that, it can run headless and will auto-discover!"
  if [[ -n "$TS_IP" ]]; then
    echo "Or add directly in the Studio Camera Fleet tab: http://$TS_IP:$PORT"
  fi
else
  echo "Starting ~/ledmap-macos-cam on $HOST:$PORT ..."
  ssh "$HOST" "pkill -f ledmap-macos-cam 2>/dev/null || true; nohup ~/ledmap-macos-cam --port $PORT >/tmp/ledmap-macos-cam.log 2>&1 &"
  sleep 1
  echo "Live devices on $HOST:"
  curl -s "http://${TS_IP:-$HOST}:$PORT/devices" | head -c 400; echo ""
fi
