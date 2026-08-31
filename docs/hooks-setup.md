# better-memory hook registration

better-memory ships eight managed hook entries across seven hook modules
(`contextual_inject` registers on two events) that wire into Claude Code's
hook framework. `better-memory setup` (and `doctor --fix`) install and
repair all eight automatically — see [Registering the hooks](#registering-the-hooks)
below.

| Event | Matcher / `if` | Purpose | Module |
|---|---|---|---|
| `SessionStart` | — | Open/reuse a background episode; inject project + general semantic memories and reflections as `additionalContext`; run the wiring autocheck | `better_memory.hooks.session_bootstrap` |
| `PostToolUse` | `Write\|Edit\|Bash`, async | Capture every matched tool invocation as a spool event | `better_memory.hooks.observer` |
| `Stop` | — | Mark session end for consolidation boundary detection; emit the end-of-session rating-sweep directive when unrated exposures remain | `better_memory.hooks.session_close` |
| `Stop` | — (second, independent registration) | Remind Claude to record non-obvious observations before stopping | `better_memory.hooks.stop_sweep` |
| `UserPromptSubmit` | — | Inject curated memories (semantic + reflections) relevant to the current prompt as `additionalContext` | `better_memory.hooks.contextual_inject` |
| `PreToolUse` | unscoped, latched to one firing/session | Same as above, keyed to the tool name + input | `better_memory.hooks.contextual_inject` |
| `PreToolUse` | `Bash`, `if: "Bash(git commit*)"` | Remind Claude to record an observation before a `git commit` that fixes a bug, addresses review feedback, or closes a phase | `better_memory.hooks.commit_checkpoint` |
| `PreCompact` | — | Remind Claude to persist the in-flight task, decisions, and open questions before context compaction | `better_memory.hooks.pre_compact` |

The `contextual_inject` hook is gated by `BETTER_MEMORY_CONTEXT_INJECT_MODE`
(`userprompt` \| `pretool` \| `both` (default) \| `off`). It runs in-process
against `memory.db` and gates each candidate through a three-leg evidence
check (BM25 match against `reflection_fts`, or vector cosine similarity
>= `BETTER_MEMORY_CONTEXT_VEC_FLOOR` (default 0.55), or — only when both
those legs are structurally unavailable — a keyword-hit fallback), then
ranks qualifiers by reciprocal rank fusion over a Wilson-score usefulness
prior; it injects the top matches (capped at `BETTER_MEMORY_CONTEXT_MAX_ITEMS`)
and never blocks a turn. `PreToolUse` hosts two independent registrations —
`contextual_inject`, unscoped (matches every tool) but latched to one real
firing per session (later tool calls short-circuit on a state file before
touching the DB), and `commit_checkpoint`, scoped to `Bash` and gated to
fire only immediately before a `git commit`. `stop_sweep` and `pre_compact`
are simple, ungated reminder-injectors — no evidence gate, no DB access.

## Registering the hooks

**Managed automatically — reference only.** Run `better-memory setup` (or
either bootstrap script, `scripts/setup.sh` / `scripts/setup.ps1`) — it
writes and idempotently repairs all eight entries below in your Claude
Code `settings.json` (`~/.claude/settings.json` for global). `better-memory
doctor --fix` repairs drift the same way later. You should not need to
hand-edit this file; the JSON below just documents the exact shape
`better_memory/setup/manifest.py` renders, for inspection or recovery.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"/absolute/path/to/.venv/bin/python\" -m better_memory.hooks.session_bootstrap"
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
            "command": "\"/absolute/path/to/.venv/bin/pythonw\" -m better_memory.hooks.observer",
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
            "command": "\"/absolute/path/to/.venv/bin/python\" -m better_memory.hooks.session_close"
          }
        ]
      },
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"/absolute/path/to/.venv/bin/python\" -m better_memory.hooks.stop_sweep"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"/absolute/path/to/.venv/bin/python\" -m better_memory.hooks.contextual_inject"
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"/absolute/path/to/.venv/bin/python\" -m better_memory.hooks.contextual_inject"
          }
        ]
      },
      {
        "matcher": "Bash",
        "if": "Bash(git commit*)",
        "hooks": [
          {
            "type": "command",
            "command": "\"/absolute/path/to/.venv/bin/python\" -m better_memory.hooks.commit_checkpoint"
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"/absolute/path/to/.venv/bin/python\" -m better_memory.hooks.pre_compact"
          }
        ]
      }
    ]
  }
}
```

Notes on the shape above (`better_memory/setup/manifest.py::hook_entry`):

- `async: true` appears **only** on `observer` — every other hook needs
  Claude Code to read its stdout synchronously (`additionalContext` /
  block directives / `systemMessage`), so it is registered without the
  `async` key.
- Windows interpreter selection: `better-memory setup` writes
  `pythonw.exe` (no console flash) only for `observer`, since it's the
  one hook that needs no stdout back; every other hook is written with
  `python.exe` because pythonw.exe silently nulls `sys.stdout` and the
  hook's output would never reach Claude. If you hand-edit this file on
  Windows, keep that split — don't blanket-switch every hook to
  `pythonw.exe`.
- The `if` filter on `commit_checkpoint`'s `PreToolUse` group scopes it to
  commands matching `git commit*`; `contextual_inject`'s `PreToolUse`
  group has no matcher (fires on every tool, latched to one real firing
  per session — see above) and is a separate group, not merged with
  `commit_checkpoint`'s.
- If better-memory is installed as a system-wide package rather than a
  repo-local `.venv`, the `command` is whatever `python -m
  better_memory.hooks.<name>` resolves to on your `PATH` — this is exactly
  what `better-memory setup` computes for you from `sys.platform`.

## How sessions flow

1. **Claude Code starts a session.** It sets `CLAUDE_SESSION_ID` in the
   environment and fires the `SessionStart` hook with a JSON payload on
   stdin (`source`, `session_id`, `cwd`).
2. **Session-bootstrap hook runs in-process.** It opens `memory.db`
   directly (no MCP RPC on the hook critical path), calls
   `SessionBootstrapService.bootstrap`, which:
   - resolves the project name from `cwd` via the git-aware
     `project_name(cwd)` helper (walks up looking for `.git`, handling
     worktrees, so worktrees share scope with their main repo);
   - opens a fresh background episode for this session, or reuses an
     existing open background episode if `source=resume`;
   - retrieves project-scoped + general-scope semantic memories and
     distilled reflections (`do` / `dont` / `neutral` buckets, capped at
     20 per bucket), then renders them per `BETTER_MEMORY_INJECT_MODE`:
     - `deferred` (the mode actually deployed live) — renders only
       general-scope semantic memories in full, plus a one-line index
       ("better-memory knows N reflections + M semantic memories...")
       telling Claude to pull specifics via `memory_retrieve` with a
       task query. No reflections are rendered in full at bootstrap.
     - `legacy` (the config default when the env var is unset) — renders
       up to `BETTER_MEMORY_BOOTSTRAP_TOP_N` (default 5) semantic
       memories and reflections in full, with the remainder listed in a
       compact "Index (not expanded)" section.
   - renders a markdown block with a `## better-memory: session
     bootstrap` header summarising project / source / episode action,
     followed by the memories and reflections;
   - runs the wiring autocheck (`better_memory.setup.autocheck.maybe_repair`)
     after bootstrap renders: a near-zero-cost per-session drift check,
     cached against a fingerprint of the desired wiring state plus the
     target files' mtimes, that self-repairs `~/.claude.json`,
     `~/.claude/settings.json`, the CLAUDE.md managed block, and skill
     links if anything drifted, and installs the per-repo `post-commit`
     hook (see [below](#post-commit-hook-opt-in-episode-close)) the first
     time it sees a git repo without one; at most one summary line is
     appended to `additionalContext`. Disable with
     `BETTER_MEMORY_WIRING_AUTOCHECK=off`.
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

### Installed automatically

You don't create `.git/hooks/post-commit` by hand anymore. The
session-start wiring autocheck (`better_memory.setup.autocheck.maybe_repair`,
see [How sessions flow](#how-sessions-flow) above) calls
`better_memory.setup.repo_hook.ensure_post_commit(cwd, params)` on every
Claude Code session start, installing the hook into whatever repo `cwd`
resolves to the first time it's missing there — worktree-safe, and
honoring a custom `core.hooksPath`. `better-memory setup` and
`doctor --fix` do **not** install it themselves (they only manage the
machine-level wiring in `~/.claude*`); only the per-session autocheck
does, so it's the first Claude Code session opened inside a given repo
(with `BETTER_MEMORY_WIRING_AUTOCHECK` not set to `off`) that installs it
there.

The installed hook looks like:

```sh
#!/bin/sh
# better-memory post-commit (managed)
"/absolute/path/to/.venv/bin/python" -m better_memory.hooks.post_commit || true
```

### Skip rules (concern 5)

`ensure_post_commit` never overwrites a hook it doesn't recognize:

- No `.git` directory at `cwd` → skip silently (not a git repo).
- A custom `core.hooksPath` is configured (`git config --get
  core.hooksPath`) → install there instead of the default
  `.git/hooks`, resolving a relative path against the repo root.
- The resolved hooks directory doesn't exist → skip silently.
- No `post-commit` file exists yet → create one (mode `0755`) with the
  sentinel comment (`# better-memory post-commit (managed)`) followed by
  the invocation line above.
- A `post-commit` file already exists:
  - It already contains the sentinel or `better_memory.hooks.post_commit`
    → no-op, already installed.
  - It isn't readable as UTF-8 text (binary / undecodable) → skip with a
    warning; never touched.
  - Its first non-blank line isn't a `#!` shebang containing `sh` → skip
    with a warning; never touched (won't append to a non-shell script).
  - Otherwise → **chain**: append the sentinel comment + invocation line
    after the existing script's content, so the pre-existing hook keeps
    running first.

Verify it installed by starting a Claude Code session inside a git repo
that doesn't have `.git/hooks/post-commit` yet (or the `core.hooksPath`
directory, if set), then:

```bash
cat .git/hooks/post-commit
```

Expected: a `# better-memory post-commit (managed)` line followed by the
invocation line — appended after any pre-existing script content if the
hook already existed.

Then verify the opt-in trailer behaviour itself:

```bash
git commit --allow-empty -m "test: no trailer"
ls ~/.better-memory/spool/
```

Expected: no new `*commit_close*.json` file.

```bash
git commit --allow-empty -m "test: close it

Closes-Episode: true"
ls ~/.better-memory/spool/
```

Expected: one new `*commit_close*.json` file. The next MCP retrieve
(or a direct `uv run python -c 'from better_memory.services.spool import SpoolService; ...'`
drain) will consume it.

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
