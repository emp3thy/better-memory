---
name: start-better-memory-ui
description: Use when user asks to start, launch, open, or run the better-memory UI / management UI / review UI. Triggers on "start ui", "launch ui", "open the better-memory ui", "start the memory ui". The MCP tool memory_start_ui is a stub — the UI must be launched directly via python.
---

# Start better-memory UI

The MCP tool `mcp__better-memory__memory_start_ui` is a stub that returns "UI not yet implemented" — but the UI itself is fully built. Launch it directly.

## Project

Working directory: the better-memory checkout's repo root (this skill is vendored inside it, under `.claude/skills/`).
Default home: `~/.better-memory` (used unless `BETTER_MEMORY_HOME` is set).

## Steps

### 1. Check if already running

If `~/.better-memory/ui.url` exists and the URL responds, reuse it:

```bash
url=$(cat ~/.better-memory/ui.url 2>/dev/null) && curl -sfo /dev/null --max-time 1 "$url" && echo "already running at $url"
```

If that prints a URL, skip step 2.

### 2. Start the UI in the background

```bash
cd <the better-memory checkout's repo root> && uv run python -m better_memory.ui
```

Run with `run_in_background: true`. The server binds to `127.0.0.1:0` (random port) and writes the bound URL to `~/.better-memory/ui.url`.

### 3. Read the URL and open it

```bash
url=$(cat ~/.better-memory/ui.url) && start "$url"
```

Report the URL to the user.

## Notes

- The UI exits after 30 minutes of inactivity, or when the user clicks "Close UI" in the header.
- Each launch picks a new random port and rewrites `ui.url`. Do not start a second instance if one is already alive — the old port file will be overwritten and the original process will be orphaned.
