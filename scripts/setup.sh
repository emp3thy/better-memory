#!/usr/bin/env bash
# better-memory setup script. Cross-platform bash (Linux, macOS, Git Bash on
# Windows). Detects prerequisites, installs what's missing, creates the
# runtime filesystem layout, and prints the JSON snippets you paste into
# ~/.claude.json and ~/.claude/settings.json.
#
# Does NOT auto-edit your Claude config — that's too high a blast radius
# for a setup script. You review the printed snippets and paste them yourself.

set -euo pipefail

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

case "$(uname -s)" in
    Darwin*)                  OS=macos ;;
    Linux*)                   OS=linux ;;
    MINGW*|MSYS*|CYGWIN*)     OS=windows ;;
    *)
        echo "Unsupported OS: $(uname -s)" >&2
        exit 1
        ;;
esac

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BETTER_MEMORY_HOME_DEFAULT="${HOME}/.better-memory"
BETTER_MEMORY_HOME="${BETTER_MEMORY_HOME:-$BETTER_MEMORY_HOME_DEFAULT}"

# Windows path conversion for the printed JSON (uppercase drive letter,
# backslash-escaped for JSON string literals).
win_path() {
    if [[ "$OS" == "windows" ]]; then
        local p="$1"
        if [[ "$p" =~ ^/([a-zA-Z])/(.*) ]]; then
            local drive="${BASH_REMATCH[1]^^}"
            p="${drive}:/${BASH_REMATCH[2]}"
        fi
        # Replace / with \\ for JSON string literals.
        echo "${p//\//\\\\}"
    else
        echo "$1"
    fi
}

log()   { printf '\033[1;36m[setup]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[setup]\033[0m %s\n' "$*" >&2; }
error() { printf '\033[1;31m[setup]\033[0m %s\n' "$*" >&2; }

log "Platform: $OS"
log "Project:  $PROJECT_DIR"
log "Home:     $BETTER_MEMORY_HOME"

# ---------------------------------------------------------------------------
# 1. Python 3.12+
# ---------------------------------------------------------------------------

log "Checking Python..."
if ! command -v python >/dev/null 2>&1 && ! command -v python3 >/dev/null 2>&1; then
    error "Python not found on PATH. Install Python 3.12 or newer from https://www.python.org/"
    exit 1
fi

PYTHON_BIN="$(command -v python3 || command -v python)"
PY_VERSION="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
log "Found Python $PY_VERSION at $PYTHON_BIN"

"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ required"' || {
    error "Python 3.12 or newer is required (found $PY_VERSION)."
    exit 1
}

# ---------------------------------------------------------------------------
# 2. uv
# ---------------------------------------------------------------------------

log "Checking uv..."
if ! command -v uv >/dev/null 2>&1; then
    error "uv not found on PATH."
    error "Install from https://docs.astral.sh/uv/getting-started/installation/"
    error "macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh"
    error "Windows:     irm https://astral.sh/uv/install.ps1 | iex  (PowerShell)"
    exit 1
fi
log "Found uv $(uv --version 2>&1 | head -1)"

# ---------------------------------------------------------------------------
# 3. uv sync — prime the project venv
# ---------------------------------------------------------------------------

log "Syncing project dependencies (uv sync)..."
(cd "$PROJECT_DIR" && uv sync)
log "Dependencies installed."

# Detect the venv Python path so the printed JSON is accurate.
if [[ "$OS" == "windows" ]]; then
    VENV_PY="$PROJECT_DIR/.venv/Scripts/python.exe"
    VENV_PYW="$PROJECT_DIR/.venv/Scripts/pythonw.exe"
else
    VENV_PY="$PROJECT_DIR/.venv/bin/python"
    VENV_PYW="$PROJECT_DIR/.venv/bin/python"   # no pythonw on Unix; same binary
fi

if [[ ! -x "$VENV_PY" && ! -f "$VENV_PY" ]]; then
    warn "Expected venv Python not found at $VENV_PY — uv sync may have landed elsewhere."
fi

# ---------------------------------------------------------------------------
# 4. Ollama
# ---------------------------------------------------------------------------

log "Checking Ollama..."
OLLAMA_BIN=""
if command -v ollama >/dev/null 2>&1; then
    OLLAMA_BIN="$(command -v ollama)"
elif [[ "$OS" == "windows" && -x "/c/Users/$(whoami)/AppData/Local/Programs/Ollama/ollama.exe" ]]; then
    OLLAMA_BIN="/c/Users/$(whoami)/AppData/Local/Programs/Ollama/ollama.exe"
fi

if [[ -z "$OLLAMA_BIN" ]]; then
    warn "Ollama not found."
    case "$OS" in
        macos)
            read -rp "Install via Homebrew? (brew install ollama) [y/N]: " yn
            if [[ "$yn" =~ ^[Yy]$ ]]; then
                brew install ollama
                OLLAMA_BIN="$(command -v ollama)"
            fi
            ;;
        linux)
            read -rp "Install via official script? (curl https://ollama.com/install.sh | sh) [y/N]: " yn
            if [[ "$yn" =~ ^[Yy]$ ]]; then
                curl -fsSL https://ollama.com/install.sh | sh
                OLLAMA_BIN="$(command -v ollama)"
            fi
            ;;
        windows)
            read -rp "Install via winget? (winget install Ollama.Ollama) [y/N]: " yn
            if [[ "$yn" =~ ^[Yy]$ ]]; then
                winget install --id=Ollama.Ollama -e --accept-package-agreements --accept-source-agreements --silent
                OLLAMA_BIN="/c/Users/$(whoami)/AppData/Local/Programs/Ollama/ollama.exe"
            fi
            ;;
    esac
fi

if [[ -z "$OLLAMA_BIN" ]]; then
    warn "Ollama still missing. Install manually from https://ollama.com/ and re-run this script."
    warn "Skipping model pull and reachability check."
else
    log "Ollama at $OLLAMA_BIN"

    # Reachability — start the service on macOS/Linux if it isn't up.
    # Windows installs as a tray app and usually auto-starts on login.
    if ! curl -fsS "http://localhost:11434/api/tags" >/dev/null 2>&1; then
        warn "Ollama daemon not reachable at localhost:11434."
        if [[ "$OS" != "windows" ]]; then
            warn "Start it in another terminal with: ollama serve"
        else
            warn "Start Ollama from the Start Menu (it runs in the system tray)."
        fi
        warn "After Ollama is running, re-run this script to complete the model pull."
    else
        log "Ollama is reachable. Pulling nomic-embed-text..."
        "$OLLAMA_BIN" pull nomic-embed-text
        log "Embedding model pulled."

        # Synthesis (Reflection consolidation) needs a chat model.
        # Default matches better_memory.config._DEFAULT_CONSOLIDATE_MODEL.
        # Respect CONSOLIDATE_MODEL if the user already exported one.
        CHAT_MODEL="${CONSOLIDATE_MODEL:-llama3}"
        read -rp "Pull chat model for reflection synthesis ($CHAT_MODEL, ~4.7 GB for llama3)? [Y/n]: " yn
        if [[ -z "$yn" || "$yn" =~ ^[Yy]$ ]]; then
            "$OLLAMA_BIN" pull "$CHAT_MODEL"
            log "Chat model pulled."
        else
            warn "Skipping chat model pull. Set CONSOLIDATE_MODEL env var"
            warn "to a chat model you already have, or run 'ollama pull <model>'"
            warn "manually. The synthesis UI will fail with 'model not found'"
            warn "until a chat model is installed."
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 5. Filesystem layout under $BETTER_MEMORY_HOME
# ---------------------------------------------------------------------------

log "Creating runtime directories under $BETTER_MEMORY_HOME..."
mkdir -p "$BETTER_MEMORY_HOME/spool"
mkdir -p "$BETTER_MEMORY_HOME/knowledge-base/standards"
mkdir -p "$BETTER_MEMORY_HOME/knowledge-base/languages"
mkdir -p "$BETTER_MEMORY_HOME/knowledge-base/projects"
log "Runtime layout ready."

# ---------------------------------------------------------------------------
# 6. Install MCP server + hooks into Claude Code config files
# ---------------------------------------------------------------------------

log "Installing into ~/.claude.json and ~/.claude/settings.json..."
(cd "$PROJECT_DIR" && uv run python -m better_memory.cli.install_hooks \
    --venv-py "$(win_path "$VENV_PY")" \
    --venv-pyw "$(win_path "$VENV_PYW")" \
    --home "$BETTER_MEMORY_HOME") || {
    error "install_hooks failed (see message above)."
    error "scripts/setup.sh aborting; fix the issue and re-run."
    exit 1
}

log "Done."
