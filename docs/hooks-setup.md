# better-memory hook registration

better-memory ships three hooks that wire into Claude Code's hook framework:

| Hook | Purpose | Module |
|---|---|---|
| `PostToolUse` | Capture every tool invocation as a spool event | `better_memory.hooks.observer` |
| `SessionStart` | Open a background episode for this session | `better_memory.hooks.session_start` |
| `Stop` | Mark session end for consolidation boundary detection | `better_memory.hooks.session_close` |

## Registering the hooks

Add the following to your Claude Code `settings.json` (typically
`~/.claude/settings.json` for global, or `.claude/settings.json` for
project-scoped):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "uv run python -m better_memory.hooks.session_start"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "uv run python -m better_memory.hooks.observer"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "uv run python -m better_memory.hooks.session_close"
          }
        ]
      }
    ]
  }
}
```

Adjust the `command` to match your environment — for example:

- If better-memory is installed as a system-wide package, drop the
  `uv run` prefix and use `python -m better_memory.hooks.session_start`.
- If your environment uses a different Python launcher (e.g. `py` on
  Windows with multiple Python versions), adjust accordingly.

## How sessions flow

1. **Claude Code starts a session.** It sets `CLAUDE_SESSION_ID` in the
   environment and fires the `SessionStart` hook.
2. **Session-start hook runs.** It writes a `session_start` marker to
   `$BETTER_MEMORY_HOME/spool` (defaulting to `~/.better-memory/spool`).
   The hook never touches the database — it stays fast and cannot fail.
3. **Claude Code launches the better-memory MCP server.** The server reads
   `CLAUDE_SESSION_ID` for its own `session_id`, matching the hook's.
4. **First `memory.retrieve` call drains the spool.** `SpoolService` sees
   the `session_start` marker, inserts the `hook_events` row, and calls
   `EpisodeService.open_background` — creating the background episode that
   subsequent `memory.observe` calls bind to.
5. **Per-turn observations write to the background episode** via
   auto-binding in `ObservationService.create`.
6. **Session ends.** The `Stop` hook writes a `session_end` marker. The
   open episode is NOT auto-closed — it stays open so the next session's
   reconciliation prompt can resolve it.
7. **Next session starts.** Claude calls `memory.reconcile_episodes()`,
   sees the prior unclosed episode, and prompts the user in chat per the
   guidance in the CLAUDE.md snippet.

## Fallback behaviour

If hooks are NOT installed (e.g. you're using better-memory outside
Claude Code), the system still works:

- `ObservationService.create` lazy-opens a background episode on first
  observation if none exists for the current session.
- The session_id is either from `CLAUDE_SESSION_ID` if set, or a fresh
  `uuid4().hex` per MCP server process.

No data is lost — only the reconciliation prompt becomes unreliable
because session ids change every process.

## Verifying the hooks work

After registering, start a Claude Code session and run:

```bash
ls ~/.better-memory/spool/
```

You should see a `*_session_start_*.json` file appear within ~1 second
of the session starting. Make a tool call (any tool), wait, and run:

```bash
ls ~/.better-memory/spool/
```

The `session_start` marker should be gone (drained into `hook_events`).
Query the DB to confirm:

```bash
sqlite3 ~/.better-memory/memory.db \
  "SELECT event_type, session_id FROM hook_events ORDER BY id DESC LIMIT 5;"
```

You should see the `session_start` event with your `CLAUDE_SESSION_ID`.
