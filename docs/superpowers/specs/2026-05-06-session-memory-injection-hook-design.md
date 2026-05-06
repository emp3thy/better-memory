# Session-start memory injection hook — design

**Status:** Approved 2026-05-06
**Branch target:** new feature branch off `main` (e.g. `session-retrieve-hook`)
**Predecessor:** none — additive new module under `better_memory/hooks/`

## Goal

Add a new SessionStart hook, `better_memory.hooks.session_retrieve`, that fetches the project's distilled reflections at the start of every Claude Code session and injects them into Claude's context as a system reminder via `additionalContext`. Eliminates the failure mode where Claude skips the mandatory `memory_retrieve` call on first turn and proceeds memory-blind.

## Why now

The 2026-05-06 session opened with this exact failure: Claude jumped straight to a UI-launch task without invoking `memory_retrieve`, missing all 22 distilled `do`-bucket reflections (including project-specific Python/SQLite gotchas, MkDocs pitfalls, htmx quirks, test-discipline rules) and 12 `dont`-bucket reflections. CLAUDE.md already mandates the startup retrieve — it isn't enough. The user's pushback: *"What can I do to encourage you not to skip it?"*

The right answer is to remove this from Claude's judgment surface entirely. A SessionStart hook injects the memory deterministically, before the first model turn, and Claude Code concatenates it with CLAUDE.md as a system reminder. Hook-based injection is upstream of Claude's decision to invoke a tool.

## Decisions log

| Decision | Choice | Why |
|---|---|---|
| Hook architecture | New sibling module `better_memory/hooks/session_retrieve.py`, registered as a second SessionStart entry. Existing `session_start.py` (spool-marker) is unchanged. | Single responsibility per file. Failure-isolated — if injection breaks, episode lazy-open still works. Claude Code concatenates `additionalContext` from multiple SessionStart hooks (per `code.claude.com/docs/en/hooks-guide.md`). |
| Content scope | Reflections only (do / dont / neutral). No semantic memories, no episode reconciliation, no knowledge listing in v1. | Reflections are the highest-leverage memory class — they encode *lessons* that should shape behavior. Semantic memories and episode state are useful but lower priority for unblocking the failure mode. Adding them later is additive. |
| Detail level | Title + use_cases + full hints, top 10 per bucket, per-hint truncation to ~600 chars. | Self-contained injection: Claude can act on hints without round-tripping. Top 10 caps total at ~3-5K tokens (≈ doubling the per-session preamble vs CLAUDE.md alone). Truncation bounds worst-case if a single hint blows up. |
| `resume` / `clear` handling | Inject on every SessionStart event regardless of source. | YAGNI — Claude Code resume already includes prior context, but re-injecting is cheap (~100ms SQLite read) and the redundancy doesn't cost correctness. Skipping on `resume` adds a stdin-source-inspection branch that is platform-version-fragile. |
| First-install / empty-DB behavior | Inject `"better-memory: no memory yet for this project — use memory_observe to record observations"` rather than silent skip. | Teaches Claude what to do next. Silent skip recreates the exact failure mode we're fixing (no signal that memory exists / is expected). |
| Failure handling on retrieval errors | All-of-the-above: log to `hook_errors` table, write a `[better-memory] session_retrieve: <ExcClass>: <msg>` line to stderr (Claude Code surfaces stderr to user), inject a fallback directive (`"Memory injection failed (...). Call mcp__better-memory__memory_retrieve manually before any task."`), exit 0. | Hook contract: must never `exit ≠ 0`. User sees the error AND Claude gets a directive AND `/diagnostics` has the row. No silent regression to memory-blind state. |
| Configurability | None for v1 — no env-var override, no per-project disable. | YAGNI. If the injection causes problems for a specific user / project we add `BETTER_MEMORY_SESSION_INJECT=0` later. |
| Connection pattern | Open a fresh `sqlite3.Connection` per hook invocation via `better_memory.db.connection.connect(config.memory_db)`. No migrations call (the MCP server applies them at boot; if migrations haven't run yet, the SELECT fails and goes through the exception path). | Hooks are short-lived subprocesses. Reusing a long-lived connection is meaningless. Skipping `apply_migrations()` saves ~50ms and avoids holding a write lock if migrations would race the MCP server. |
| Service call | `ReflectionSynthesisService(conn).retrieve_reflections(project=project_name(), limit_per_bucket=10)` | Confirmed signature at `better_memory/services/reflection.py:1106`. Returns `{"do": [...], "dont": [...], "neutral": [...]}` with each item containing `id, title, phase, polarity, use_cases, hints (list), confidence, tech, evidence_count` — exactly the shape we need. |
| Setup-script integration | Out of scope for this design — covered by **Track B** (auto-install). For v1, document the new hook entry in README.md alongside the existing `Stop` and `PostToolUse` hooks. | Track A (this design) only ships the hook module + tests. Track B will collapse manual hook registration into `setup.sh`. |

## Approach

Single branch off `main`, two logical commits:

| # | Commit | Files | Type |
|---|---|---|---|
| 1 | `feat(hooks): session_retrieve injects reflections at session start` | `better_memory/hooks/session_retrieve.py` (new), `tests/hooks/test_session_retrieve.py` (new) | Feature |
| 2 | `docs(readme): document new SessionStart memory-injection hook` | `README.md` (extend Manual setup section), `website/configuration.md` (cross-reference) | Docs |

CI green at each commit boundary.

## Commit 1 — `session_retrieve.py`

### Module shape

`better_memory/hooks/session_retrieve.py`:

```python
"""Session-start hook: inject persisted reflections as additionalContext.

Companion to ``session_start.py`` (which writes a spool marker for episode
lazy-open). This module is purely about surfacing prior memory at the
start of every Claude Code session so Claude does not have to remember
to call ``memory_retrieve`` on first turn.

Reads stdin payload (Claude Code SessionStart event JSON), opens
memory.db, calls ReflectionSynthesisService.retrieve_reflections, renders
the three buckets to Markdown, prints a hookSpecificOutput JSON envelope
to stdout, and exits 0. Never raises; on any error, logs to hook_errors
and injects a fallback directive.
"""
```

### Public entry point

```python
def main() -> None:
    try:
        cfg = get_config()
        proj = project_name()
        with closing(connect(cfg.memory_db)) as conn:
            service = ReflectionSynthesisService(conn)
            buckets = service.retrieve_reflections(
                project=proj, limit_per_bucket=10,
            )
        rendered = _render_or_empty_message(buckets)
    except BaseException as exc:  # noqa: BLE001
        _record_failure(exc)
        rendered = _fallback_directive(exc)
    _print_hook_output(rendered)
    sys.exit(0)
```

### Render function

`_render_or_empty_message(buckets)`:

- If all three buckets empty (first install / new project) → return `_EMPTY_PROJECT_MESSAGE`.
- Otherwise render Markdown:
  - Title: `## Persisted reflections for this project (better-memory)`
  - Three sections: `### do (prior wins)`, `### dont (approaches to avoid)`, `### neutral (context)`. Skip a section if its bucket is empty.
  - Per reflection: `**{title}**` line, italic `_{use_cases}_` line, bulleted `hints` list (each hint truncated to 600 chars with trailing `…` if cut), trailing `id: {id}` line in italic small text.
  - Footer: `Use mcp__better-memory__memory_record_use(id, success|failure) when a memory materially helps or misleads. Use mcp__better-memory__memory_observe to write new ones.`

`_EMPTY_PROJECT_MESSAGE`:
```
better-memory: no reflections recorded yet for this project. Use mcp__better-memory__memory_observe to record observations as you work; reflections will be distilled from them on episode close.
```

`_fallback_directive(exc)`:
```
better-memory: memory injection failed ({exc.__class__.__name__}: {short_msg}). Call mcp__better-memory__memory_retrieve manually before any task in this session.
```

`short_msg` = first line of `str(exc)` truncated to 200 chars.

### Stdout envelope

```python
def _print_hook_output(text: str) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }
    print(json.dumps(payload), flush=True)
```

### Failure recording

`_record_failure(exc)`:
- `record_hook_error(hook_name="session_retrieve", exc=exc)` (existing helper at `better_memory.hooks._error_log`). Wrap in `try/except BaseException: pass` so a hook_errors write failure can't mask the original exception or break the hook.
- Write `[better-memory] session_retrieve: {exc.__class__.__name__}: {short_msg}` to stderr.

### Tests

`tests/hooks/test_session_retrieve.py`:

| # | Test | Setup | Assertion |
|---|---|---|---|
| 1 | populated DB → injects rendered Markdown | seed two `do` + one `dont` reflection in a tmp memory.db | stdout JSON parses; `additionalContext` contains all three reflection titles, `### do`, `### dont` headings, footer line |
| 2 | empty DB → injects "no memory yet" message | tmp memory.db with schema applied but zero reflection rows | stdout JSON's `additionalContext` equals `_EMPTY_PROJECT_MESSAGE` |
| 3 | missing DB / no schema → injects fallback directive | tmp dir with no memory.db | stdout JSON's `additionalContext` matches fallback regex `^better-memory: memory injection failed \(`; `hook_errors` row present (open conn separately to assert); stderr line contains `session_retrieve:` |
| 4 | simulated SQL error → injects fallback | monkeypatch `ReflectionSynthesisService.retrieve_reflections` to raise `sqlite3.OperationalError("simulated")` | as test 3 plus the simulated error class name in the directive |
| 5 | hint truncation | seed one reflection whose `hints` includes a 1500-char string | rendered output's longest hint line ≤ 605 chars (600 + ellipsis + bullet) |
| 6 | bucket cap | seed 12 `do` reflections | only top 10 by `confidence DESC` appear; remaining 2 absent |
| 7 | hook never exits non-zero | run via subprocess in each of (1)/(3)/(4) | `process.returncode == 0` |

Test scaffold mirrors `tests/hooks/test_session_start.py` and `tests/hooks/test_observer.py` (existing patterns for spawning subprocess hooks with stdin payload + capturing stdout/stderr).

## Commit 2 — Docs

### `README.md`

Add to the Manual setup section's hooks JSON example, after the existing `Stop` block:

```json
"SessionStart": [
  {
    "hooks": [
      {
        "type": "command",
        "command": "/absolute/path/to/.venv/bin/python -m better_memory.hooks.session_start"
      },
      {
        "type": "command",
        "command": "/absolute/path/to/.venv/bin/python -m better_memory.hooks.session_retrieve"
      }
    ]
  }
]
```

Add a one-paragraph note above the JSON: explain that two SessionStart hooks ship — `session_start` (writes a spool marker for episode lazy-open) and `session_retrieve` (injects reflections as `additionalContext` so Claude doesn't need to call `memory_retrieve` manually). Both should be registered.

### `website/configuration.md`

Cross-reference: under the existing filesystem-layout section, add a sentence noting that `~/.better-memory/spool/` is written by the `session_start` hook, and `~/.better-memory/memory.db` is read by the `session_retrieve` hook on every SessionStart event for context injection.

## Out of scope

Explicitly deferred:

- **Setup-script auto-install** — Track B. `setup.sh` will gain `--auto-install` flag that idempotently merges the hook entry into `~/.claude/settings.json`.
- **Surfacing semantic memories at session start** — future work. Rationale: a separate hook (or extension to this one) calling `SemanticMemoryService.list_for_retrieval(project)`. Adds a flat list section above the reflections.
- **Surfacing pending synthesis count** — future work. Rationale: today `memory_start_episode` reports it; surfacing at SessionStart would mean a separate query.
- **Surfacing open episodes from prior sessions** — future work. Rationale: `EpisodeService.list_open(project)` exists, easy to add as a section. Deferred because it could derail Claude toward a stale goal.
- **Configurability via env var** — YAGNI for v1. Add `BETTER_MEMORY_SESSION_INJECT=0` later if a user reports the injection causes problems.
- **Render-format experimentation** — wait for usage data. Plain Markdown rendering is the safe v1; tier-by-confidence or richer formatting can come later.

## Confidence per implementation step

| # | Step | Conf. |
|---|---|---|
| 1 | Hook scaffolding (stdin/stdout/exit pattern, mirror `session_start.py`) | 95% |
| 2 | `additionalContext` stdout JSON envelope (verified against Claude Code hooks docs) | 95% |
| 3 | `ReflectionSynthesisService.retrieve_reflections` call (signature confirmed via `services/reflection.py:1106` read) | 95% |
| 4 | Markdown rendering of buckets (hint truncation + footer) | 90% |
| 5 | Fallback inject on exception | 95% |
| 6 | `hook_errors` row via existing `record_hook_error` helper (used by `session_start.py:118`) | 95% |
| 7 | Empty-DB injection branch (rely on retrieve_reflections returning all-empty buckets) | 90% |
| 8 | Test suite (7 scenarios) | 90% |

All steps ≥90% — no plan-stage mitigations required.

## References

- Claude Code SessionStart hook contract — `code.claude.com/docs/en/hooks-guide.md`, `hooks.md` (`additionalContext` shape, multi-hook concatenation, sync blocking semantics).
- Existing hook patterns — `better_memory/hooks/session_start.py`, `session_close.py`, `observer.py`.
- Reflection service signature — `better_memory/services/reflection.py:1106` (`retrieve_reflections`).
- Hook-error helper — `better_memory/hooks/_error_log.py` (`record_hook_error`).
- Memory bucket consumer reference — `better_memory/mcp/server.py:838-845` (existing `memory.retrieve` MCP tool implementation).
