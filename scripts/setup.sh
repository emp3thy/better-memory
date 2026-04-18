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

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -W 2>/dev/null || cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BETTER_MEMORY_HOME_DEFAULT="${HOME}/.better-memory"
BETTER_MEMORY_HOME="${BETTER_MEMORY_HOME:-$BETTER_MEMORY_HOME_DEFAULT}"

# Windows path conversion for the printed JSON.
win_path() {
    if [[ "$OS" == "windows" ]]; then
        # /c/Users/... -> C:\Users\...
        echo "$1" | sed -E 's|^/([a-zA-Z])/|\1:/|' | sed 's|/|\\\\|g'
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
        log "Model pulled."
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
# 6. Print the Claude config snippets
# ---------------------------------------------------------------------------

PY_FOR_JSON="$(win_path "$VENV_PY")"
PYW_FOR_JSON="$(win_path "$VENV_PYW")"
HOME_FOR_JSON="$(win_path "$BETTER_MEMORY_HOME")"

cat <<EOF

================================================================================
 better-memory is installed. To enable it in Claude Code, paste the two
 snippets below into the listed files. Do NOT replace existing contents;
 merge the \`mcpServers\` and \`hooks\` keys.
================================================================================

 1) Add to ~/.claude.json under the top-level \`mcpServers\` key:

    "better-memory": {
      "type": "stdio",
      "command": "$PY_FOR_JSON",
      "args": ["-m", "better_memory.mcp"],
      "env": {
        "BETTER_MEMORY_HOME": "$HOME_FOR_JSON"
      }
    }

    (If \`OLLAMA_HOST\` or \`EMBED_MODEL\` aren't the defaults —
    http://localhost:11434 and nomic-embed-text — add them under \`env\`.)

 2) Add to ~/.claude/settings.json under the top-level \`hooks\` key:

    "PostToolUse": [
      {
        "matcher": "Write|Edit|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "\"$PYW_FOR_JSON\" -m better_memory.hooks.observer",
            "async": true
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"$PYW_FOR_JSON\" -m better_memory.hooks.session_close",
            "async": true
          }
        ]
      }
    ]

    If you already have PostToolUse or Stop arrays, append these objects —
    don't replace the arrays.

 3) Restart Claude Code. MCP servers don't hot-reload.
    Hooks reload when you open the \`/hooks\` slash command once; restart
    is simpler if anything else changed.

 4) Verify: in a Claude Code session, ask it to call \`memory.observe\`
    with a test content. Then \`memory.retrieve(query="...")\` should
    return the \`do\`/\`dont\`/\`neutral\` buckets.

================================================================================
EOF

log "Done."
