#!/usr/bin/env bash
# better-memory POSIX bootstrap: uv -> deps -> wiring. Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
    echo "[setup] uv not found — installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "[setup] Syncing dependencies..."
uv sync

echo "[setup] Installing Claude Code wiring..."
uv run better-memory setup
