# better-memory hook registration

better-memory ships three hooks that wire into Claude Code's hook framework:

| Hook | Purpose | Module |
|---|---|---|
| `SessionStart` | Open/reuse a background episode and inject project + general semantic memories and reflections as `additionalContext` | `better_memory.hooks.session_bootstrap` |
| `PostToolUse` | Capture every tool invocation as a spool event | `better_memory.hooks.observer` |
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
            "command": "uv run python -m better_memory.hooks.session_bootstrap"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write|Edit|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "uv run python -m better_memory.hooks.observer",
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
            "command": "uv run python -m better_memory.hooks.session_close",
            "async": true
          }
        ]
      }
    ]
  }
}
```

Adjust the `command` to match your environment — for example:

- If better-memory is installed as a system-wide package, drop the
  `uv run` prefix and use `python -m better_memory.hooks.session_bootstrap`.
- If your environment uses a different Python launcher (e.g. `py` on
  Windows with multiple Python versions), adjust accordingly.

## How sessions flow

1. **Claude Code starts a session.** It sets `CLAUDE_SESSION_ID` in the
   environment and fires the `SessionStart` hook with a JSON payload on
   stdin (`source`, `session_id`, `cwd`).
2. **Session-bootstrap hook runs in-process.** It opens `memory.db`
   directly (no MCP RPC on the hook critical path), calls
   `SessionBootstrapService.bootstrap`, which:
   - resolves the project name from `cwd` via the git-aware
     `project_name(cwd)` helper (uses `git rev-parse --git-common-dir` so
     worktrees share scope with their main repo);
   - opens a fresh background episode for this session, or reuses an
     existing open background episode if `source=resume`;
   - retrieves all project-scoped + general-scope semantic memories and
     all distilled reflections (`do` / `dont` / `neutral` buckets) — no
     per-bucket cap;
   - renders a markdown block with a `## better-memory: session
     bootstrap` header summarising project / source / episode action,
     followed by the memories and reflections.
   The hook prints a `hookSpecificOutput` JSON envelope with the rendered
   markdown as `additionalContext`. Claude Code injects this into the
   first turn's context. If anything fails, a fallback directive is
   emitted instructing Claude to call
   `mcp__better-memory__memory_session_bootstrap` manually, and the error
   is logged to the `hook_errors` table.
3. **Claude Code launches the better-memory MCP server.** The server reads
   `CLAUDE_SESSION_ID` for its own `session_id`, matching the hook's.
4. **Per-turn observations write to the background episode** via
   auto-binding in `ObservationService.create`. The episode opened by the
   bootstrap hook is the binding target.
5. **Session ends.** The `Stop` hook fires. If the session has any
   unrated memory exposures (reflections / semantic memories surfaced
   this session via the bootstrap hook or `memory.retrieve` /
   `memory.semantic_retrieve` but not yet credited by `memory.credit`),
   it emits a `decision:block` directive on stdout asking Claude to
   invoke the `rate-session-memories` skill. The skill calls
   `memory.list_session_exposures`, classifies each id as
   `cited` / `shaped` / `ignored` / `misled` / `overlooked`, and submits the batch via
   `memory.apply_session_ratings`. Claude Code then fires `Stop` again;
   on the second fire (no unrated exposures left) the hook writes a
   `session_end` marker to the spool.
6. **Marker drains.** On the next MCP retrieve drain, an unhardened
   (background) episode for that session is auto-closed as
   `outcome=no_outcome`, `close_reason=session_end_reconciled`. A
   hardened (goal-declared) episode stays open so the next session's
   reconciliation prompt can resolve it with a real outcome.
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

## Post-commit hook (opt-in episode close)

Unlike the session_bootstrap / observer / Stop hooks above (which are
Claude Code hooks registered in `settings.json`), the post-commit hook
is a **git-native hook** — a shell script at `.git/hooks/post-commit`
in each repository where you want episode close-on-commit behaviour.

### Why it's opt-in-per-commit

A git repo typically sees many commits per goal — phased work, review
fixes, WIP pushes. Auto-closing on every commit would churn episodes.
Instead, the hook only fires when a commit message carries a
`Closes-Episode: true` trailer. Normal commits are no-ops.

### Installing per repo

Create `.git/hooks/post-commit` in your project with executable
permissions:

```bash
#!/bin/sh
# Writes a commit_close marker to the better-memory spool iff the
# just-committed message contains `Closes-Episode: <truthy>`. Never
# raises; exits 0 regardless.
exec uv run python -m better_memory.hooks.post_commit
```

Make it executable:

```bash
chmod +x .git/hooks/post-commit
```

Verify it runs without side-effects (no trailer → no marker written):

```bash
git commit --allow-empty -m "test: no trailer"
ls ~/.better-memory/spool/
```

Expected: no new `*commit_close*.json` file.

Now test the opt-in path:

```bash
git commit --allow-empty -m "test: close it

Closes-Episode: true"
ls ~/.better-memory/spool/
```

Expected: one new `*commit_close*.json` file. The next MCP retrieve
(or a direct `uv run python -c 'from better_memory.services.spool import SpoolService; ...'`
drain) will consume it.

### Cross-platform notes

- **Windows + git-bash / git-cmd:** the shebang `#!/bin/sh` is handled
  by git's bundled bash. The `uv run` command must be on PATH for the
  hook to find it.
- **Windows + PowerShell:** no action needed; git always uses its
  bundled bash for hook execution, not the parent shell.

### Integrating plan-complete close

The post-commit hook covers "I made a commit that closes the episode".
The complementary path — "I just finished a multi-step plan run and
want to close the episode cleanly" — stays LLM-invoked:

```
memory.close_episode(outcome="success", close_reason="plan_complete")
```

See the "Closing episodes on git commit + plan completion" section of
the CLAUDE snippet for the LLM-side guidance.

## Verifying the hooks work

After registering, start a Claude Code session. The
`session_bootstrap` hook runs in-process and opens (or reuses) a
background episode directly in `memory.db`. Confirm via:

```bash
sqlite3 ~/.better-memory/memory.db \
  "SELECT id, project, status, started_at FROM episodes \
   WHERE status='open' ORDER BY started_at DESC LIMIT 5;"
```

You should see an open background episode for the current project.
You should also see the bootstrap markdown block (header
`## better-memory: session bootstrap`, followed by `Project: <name>  •
Source: <startup|resume|...>  •  Episode: <opened|reused> id=<short>`)
appear in Claude's first-turn context.

Make a tool call (any Write/Edit/Bash) so the observer fires, then
verify the spool received a snapshot:

```bash
ls ~/.better-memory/spool/
```

You should see one or more snapshot JSON files. After the next
`memory.retrieve` call (or session end + drain), they migrate into the
`hook_events` and `observations` tables:

```bash
sqlite3 ~/.better-memory/memory.db \
  "SELECT event_type, session_id FROM hook_events ORDER BY id DESC LIMIT 5;"
```

If bootstrap fails for any reason, the hook injects a fallback
directive instead and records the failure:

```bash
sqlite3 ~/.better-memory/memory.db \
  "SELECT hook_name, exc_type, occurred_at FROM hook_errors \
   ORDER BY occurred_at DESC LIMIT 5;"
```
