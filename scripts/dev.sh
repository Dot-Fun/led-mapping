#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -x "$ROOT_DIR/macos-cam/.build/release/macos-cam" ]]; then
  echo "Building macos-cam..."
  (cd "$ROOT_DIR/macos-cam" && swift build -c release)
fi

trap 'kill 0' EXIT

echo "Starting host backend on http://localhost:8780 ..."
(cd "$ROOT_DIR/host" && uv run uvicorn ledmap.api.server:app --host 0.0.0.0 --port 8780) &

echo "Starting web studio on http://localhost:5173 ..."
(cd "$ROOT_DIR/web" && npm run dev -- --host) &

wait
