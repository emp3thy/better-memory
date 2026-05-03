---
template: home.html
title: better-memory
description: Memory that sticks between sessions.
hide:
  - navigation
  - toc
---

# better-memory

A local-first semantic + episodic memory manager for [Claude Code](https://claude.com/claude-code). All state lives on your machine — SQLite databases for observations and the knowledge base, and a local [Ollama](https://ollama.com/) instance for embeddings.

## What it gives you

- **Observations** the AI writes at decision points (`memory.observe`), tagged with an `outcome` of `success` / `failure` / `neutral`.
- **Retrieval in three buckets** (`memory.retrieve`): `do` (prior successes), `dont` (approaches to avoid), `neutral` (context). Reinforcement-weighted.
- **Knowledge base** (`~/.better-memory/knowledge-base/`) — human-authored markdown indexed via FTS5. Standards, language conventions, per-project docs.
- **Fire-and-forget hooks** that snapshot Claude Code's tool calls into a spool, drained lazily on the next retrieve.
- **Full audit trail** in `audit_log` — every state change is an immutable append.

## Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** for environment management
- **[Ollama](https://ollama.com/)** running locally with the `nomic-embed-text` model pulled
- **[Claude Code](https://claude.com/claude-code)** installed

SQLite ships with Python; `sqlite-vec` is installed as a pip dependency.

## Quick start

```bash
git clone https://github.com/emp3thy/better-memory
cd better-memory
./scripts/setup.sh
```

The script:

1. Verifies Python ≥ 3.12 and uv.
2. Runs `uv sync` to build the venv.
3. Checks for Ollama; offers to install via `brew` / `apt` / `winget` if missing.
4. Pulls `nomic-embed-text`.
5. Creates `~/.better-memory/{spool,knowledge-base/...}`.
6. Prints the JSON snippets you paste into `~/.claude.json` and `~/.claude/settings.json`.

It does **not** auto-edit your Claude config — too high-blast-radius for a setup script. Review and paste the snippets yourself.

## Manual setup

If you'd rather do it by hand:

```bash
uv sync
mkdir -p ~/.better-memory/{spool,knowledge-base/{standards,languages,projects}}
ollama pull nomic-embed-text
```

Then add to `~/.claude.json` (user-scope MCP — create the file if it doesn't exist):

```json
{
  "mcpServers": {
    "better-memory": {
      "type": "stdio",
      "command": "/absolute/path/to/better-memory/.venv/bin/python",
      "args": ["-m", "better_memory.mcp"],
      "env": {
        "BETTER_MEMORY_HOME": "/absolute/path/to/your/home/.better-memory"
      }
    }
  }
}
```

And add hooks to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write|Edit|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/.venv/bin/python -m better_memory.hooks.observer",
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
            "command": "/absolute/path/to/.venv/bin/python -m better_memory.hooks.session_close",
            "async": true
          }
        ]
      }
    ]
  }
}
```

On Windows, point hooks at `.venv\Scripts\pythonw.exe` (no console flash) instead of `python.exe`.

Restart Claude Code. MCP servers don't hot-reload.

## Skills

The `better_memory/skills/` directory contains four markdown skills the AI should load at the appropriate moment:

- `memory-retrieve.md` — before starting any task
- `memory-write.md` — at every decision point
- `memory-feedback.md` — when validation evidence arrives
- `session-close.md` — before wrapping up

Plus `CLAUDE.snippet.md` — paste into your project's `CLAUDE.md` to teach the AI about better-memory.

## Troubleshooting

!!! warning "Ollama unreachable on startup"
    Make sure Ollama is running (`ollama serve` on macOS/Linux; the tray app on Windows) and that `nomic-embed-text` is pulled (`ollama pull nomic-embed-text`). The MCP server continues booting and serves `knowledge.*` tools, but `memory.observe` / `memory.retrieve` will error until Ollama is up.

!!! warning "MCP server not appearing in Claude Code after editing `~/.claude.json`"
    MCP servers don't hot-reload. Restart Claude Code.

!!! warning "Hooks not firing"
    Open `/hooks` once in Claude Code to reload the hook config — the settings watcher only watches directories that had a settings file when the session started. If that fails, restart.

!!! warning "Spool files piling up"
    The spool drains on every `memory.retrieve` call. If you haven't retrieved in a long time, files accumulate — they're tiny JSON, not a concern. Bad files are moved to `spool/.quarantine/` so one corrupt file never blocks the drain.

!!! warning "Windows console flashes on every tool call"
    Your hook command is using `python.exe`; switch to `.venv\Scripts\pythonw.exe`. The no-console variant still reads stdin pipes fine and won't flash a window.
