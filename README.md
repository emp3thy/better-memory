# better-memory

A local-first semantic + episodic memory manager for Claude Code. All state lives on your machine — SQLite databases for observations and the knowledge base, and a local Ollama instance for embeddings.

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
- **Claude Code** installed

SQLite ships with Python; `sqlite-vec` is installed as a pip dependency.

## Quick start

```bash
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

## Configuration

One env var roots the runtime filesystem layout:

| Variable | Default | Purpose |
|---|---|---|
| `BETTER_MEMORY_HOME` | `~/.better-memory` | Root dir for `memory.db`, `knowledge.db`, `spool/`, `knowledge-base/` |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint |
| `EMBED_MODEL` | `nomic-embed-text` | Embedding model (must produce 768-dim vectors) |
| `AUDIT_LOG_RETRIEVED` | `true` | Whether `memory.retrieve` writes per-result audit rows |

## MCP tools

| Tool | Purpose |
|---|---|
| `memory.observe(content, component?, theme?, trigger_type?, outcome?)` | Create a new observation. Returns `{"id": ...}`. |
| `memory.retrieve(query?, component?, window?='30d', scope_path?)` | Three outcome buckets + insights + knowledge. Drains spool first. |
| `memory.record_use(id, outcome?)` | Stamp reinforcement outcome on a memory after validation. |
| `knowledge.search(query, project?)` | BM25 search against the knowledge base. |
| `knowledge.list(project?)` | List indexed knowledge docs. |
| `memory.start_ui()` | Plan 2 stub. |

## Skills

The `better_memory/skills/` directory contains four markdown skills the AI should load at the appropriate moment:

- `memory-retrieve.md` — before starting any task
- `memory-write.md` — at every decision point
- `memory-feedback.md` — when validation evidence arrives
- `session-close.md` — before wrapping up

Plus `CLAUDE.snippet.md` — paste into your project's `CLAUDE.md` to teach the AI about better-memory.

## Development

```bash
uv sync              # install deps
uv run pytest         # full suite (requires Ollama running for integration marker)
uv run pytest -m "not integration"   # unit tests only
uv run ruff check .   # lint
```

Run the MCP server standalone for manual poking:

```bash
uv run python -m better_memory.mcp
```

It speaks JSON-RPC over stdio — pipe `initialize` / `tools/list` / `tools/call` payloads in.

## Troubleshooting

**"Ollama unreachable" on startup.**
Make sure Ollama is running (`ollama serve` on macOS/Linux; the tray app on Windows) and that `nomic-embed-text` is pulled (`ollama pull nomic-embed-text`). The MCP server continues booting and serves `knowledge.*` tools, but `memory.observe` / `memory.retrieve` will error until Ollama is up.

**MCP server not appearing in Claude Code after editing `~/.claude.json`.**
MCP servers don't hot-reload. Restart Claude Code.

**Hooks not firing.**
Open `/hooks` once in Claude Code to reload the hook config — the settings watcher only watches directories that had a settings file when the session started. If that fails, restart.

**Spool files piling up.**
The spool drains on every `memory.retrieve` call. If you haven't retrieved in a long time, files accumulate — they're tiny JSON, not a concern. Bad files are moved to `spool/.quarantine/` so one corrupt file never blocks the drain.

**Windows console flashes on every tool call.**
Your hook command is using `python.exe`; switch to `.venv\Scripts\pythonw.exe`. The no-console variant still reads stdin pipes fine and won't flash a window.

## Architecture

See `docs/superpowers/specs/2026-04-06-better-memory-design.md` for the full design spec — four-layer epistemic hierarchy, hybrid search via FTS5 + sqlite-vec + RRF, reinforcement-weighted ranking, and the consolidation pipeline that lives in Plan 2.

## License

See [LICENSE](LICENSE).

## Management UI

Spawn the UI on demand (Phase 10 of Plan 2 will expose this as the
`memory.start_ui()` MCP tool). Until then, start it manually:

```bash
BETTER_MEMORY_HOME=~/.better-memory uv run python -m better_memory.ui
cat ~/.better-memory/ui.url   # print the bound URL
```

The UI exits after 30 minutes of inactivity, or when you click
**Close UI** in the header.
