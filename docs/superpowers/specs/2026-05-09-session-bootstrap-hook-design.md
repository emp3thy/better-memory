# Session Bootstrap Hook — Design

**Date:** 2026-05-09
**Status:** Draft, awaiting user review
**Supersedes:** `2026-05-06-session-memory-injection-hook-design.md` (the earlier two-hook design that produced `session_start.py` + `session_retrieve.py`)

## 1. Background

The current SessionStart story uses two hooks installed by `better_memory.cli.install_hooks`:

- `session_start.py` — writes a spool marker; `SpoolService.drain` lazy-opens a background episode for the session on first MCP call.
- `session_retrieve.py` — calls `ReflectionSynthesisService.retrieve_reflections(project=cwd_project)` and injects three buckets of reflections (do / dont / neutral, capped at 10 per bucket) into the new session as `additionalContext`.

Three gaps with this setup:

1. **Source-blind.** Both hooks ignore the `source` field on the SessionStart payload (`startup` / `resume` / `clear` / `compact`). Compaction-triggered restarts get the same treatment as fresh startups.
2. **Reflections only.** No semantic memories are injected at session start. Claude (per global CLAUDE.md) is expected to call `mcp__better-memory__memory_retrieve` and `memory_semantic_retrieve` manually on first turn — easy to forget, and silently skipped this session that prompted this redesign.
3. **Project-scope only.** General-scope memories (cross-project lessons) are not surfaced. The retrieval queries do union project + general internally, but reflections-only injection still misses semantic content.

Additional architectural concern: episode opening is split across two layers (hook writes a marker; spool drain processes it later) which adds indirection without delivering benefit on this code path.

## 2. Goals

- Single user-level SessionStart hook installed by `install_hooks`.
- Source-aware behavior: `startup` opens a new episode; `resume` / `clear` / `compact` reuse the existing episode for the session_id (idempotent).
- Episode scope = git repo name if cwd is in a git repo; literal `"general"` otherwise.
- Inject **all** general semantic memories, **all** project semantic memories, **all** general reflections, and **all** project reflections (no caps; user-accepted trade-off).
- Hook is a thin shim. All logic lives in a `SessionBootstrapService`, exposed both as an MCP tool (`mcp__better-memory__memory_session_bootstrap`) and via direct Python import for the hook critical path.

## 3. Non-goals

- No changes to `Stop` / `session_close.py` — out of scope.
- No changes to `observer.py`, `post_commit.py`, or `PreCompact`.
- No migration script for memories scoped to subdirectory names that get orphaned by the `project_name()` change. Risk accepted (user always invokes Claude at repo root in practice).
- No daemon / background process. Hook stays in-process Python like the rest of the better-memory hooks.

## 4. Architecture

**New components:**

- **`better_memory/services/session_bootstrap.py`** — `SessionBootstrapService.bootstrap(source, session_id, cwd)` returns a `BootstrapResult` containing the rendered markdown plus structured side-effect data (project, source, episode action, counts). Owns: source coercion, scope resolution, idempotent episode lifecycle, retrieval, rendering.
- **`mcp__better-memory__memory_session_bootstrap`** MCP tool — registered on the server; signature mirrors the service. Lets Claude or tests re-invoke the bootstrap manually (e.g. recovery after hook failure, post-`/clear` re-injection).
- **`better_memory/hooks/session_bootstrap.py`** — thin shim. Reads stdin, calls the service in-process (no MCP RPC on the hook critical path), prints `hookSpecificOutput.additionalContext` JSON envelope. Mirrors `session_retrieve.py`'s defensive shape (never raises, always exits 0, fallback directive on error).

**Updated components:**

- **`better_memory.config.project_name(cwd)`** — git-aware. Resolution order:
  1. `.better-memory` override file in cwd → first non-empty stripped line (kept verbatim from current implementation).
  2. Run `git -C <cwd> rev-parse --git-common-dir` (subprocess; handles worktrees correctly). On success, the common-dir is the main repo's `.git` directory; the project name is `<common_dir.parent>.name`. This way every worktree of a repo shares the same project scope as the main checkout.
  3. Otherwise (subprocess fails / no git available / not in any git tree) return literal `"general"`.

  Implementation note: invoking subprocess on every observation could be expensive. Cache the resolution per-cwd in-process (the `project_name` callers in observers already short-circuit on cached values where possible). Verify performance during implementation.
- **`install-hooks` CLI** — `_OUR_HOOKS` registry collapses two SessionStart entries into one. New `_LEGACY_HOOK_MODULES` set ensures users upgrading from the two-hook era get the old `session_start` and `session_retrieve` entries scrubbed from `~/.claude/settings.json` on next run.

**Removed components:**

- `better_memory/hooks/session_start.py`
- `better_memory/hooks/session_retrieve.py`
- `SpoolService._maybe_open_episode_for_session_start` and the `event_type == "session_start"` branch of `SpoolService.drain`. Episodes now open eagerly in the bootstrap hook; the spool/drain plumbing for session_start markers becomes unnecessary.

**Untouched:** `Stop` hook, `session_close.py`, `observer.py`, `post_commit.py`, `PreCompact` hook, MCP server registration in `~/.claude.json`, all observer/commit drain paths.

## 5. Source detection & episode lifecycle

The hook reads the SessionStart payload from stdin and parses `source`. Defaults to `"startup"` if absent, unparseable, or unrecognized.

| Source | Episode behavior | Context injection |
|---|---|---|
| `startup` | Open new background episode for `session_id`, scoped to resolved project | Full inject |
| `resume` | Reuse existing episode if found; else open new | Full inject |
| `clear` | Reuse existing episode if found; else open new | Full inject |
| `compact` | Reuse existing episode if found; else open new | Full inject |

In all four cases the service guards with:

```python
if self._episodes.active_episode(session_id) is None:
    self._episodes.open_background(session_id=session_id, project=project)
```

This makes the bootstrap idempotent across **sequential** firings within a single Claude Code session (Claude Code emits SessionStart events sequentially — there is no concurrent firing to protect against). Compact / resume / clear after a startup all hit the `is not None` branch and skip the open.

**Why "full inject" for all four?** Compact strips prior context entirely. Resume's prompt is reconstituted from history but reflections and semantic memories live outside that history. Clear is a deliberate user-initiated wipe. All four benefit from re-injection. Only the episode-open decision varies, and even that is decided by idempotency rather than by source.

**Project resolution** is performed once via the updated `project_name(cwd)` helper. The same value is used for both the episode record and the retrieval scoping. This eliminates the divergence risk that would otherwise exist between session episode scope and observation scope (since observers use the same helper).

## 6. Retrieval & rendering

**Retrieval — two service calls:**

1. `SemanticMemoryService.list_for_project(project=resolved_project, scope_filter=<see below>)` — when `resolved_project != "general"`, pass `scope_filter=None`: returns project-scope rows for `resolved_project` UNION all `scope='general'` rows, ordered `created_at DESC`. When `resolved_project == "general"`, pass `scope_filter="general"`: returns only rows with `scope='general'` from any project. Avoids the corner case of rows tagged `project='general'` with `scope='project'` polluting the result.
2. `ReflectionSynthesisService.retrieve_reflections(project=resolved_project, limit_per_bucket=<unlimited>)` — returns three buckets (do / dont / neutral), each containing project rows + `scope='general'` rows, ordered `confidence DESC, updated_at DESC`. The `limit_per_bucket=<unlimited>` is implemented either by passing `sys.maxsize` or by extending the service to accept `None` — implementer's choice. Note: `retrieve_reflections` already handles the union via `(project = ? OR scope = 'general')` regardless of whether the project is `"general"`, so no special-casing is needed for reflections — only for semantic memories.

Single retrieval call per service. The union is built into both queries.

**Rendered markdown structure:**

```markdown
## better-memory: session bootstrap
Project: <resolved_project>  •  Source: <source>  •  Episode: <opened|reused> id=<short-id>

### Semantic memories (<N> entries)
- [<id>] <content>
- [<id>] <content>
…

### Reflections — do (prior wins)
**<title>**
_<use_cases>_
- <hint 1>
- <hint 2>
_id: <id>_
…

### Reflections — dont (approaches to avoid)
…

### Reflections — neutral (context)
…

---
Use mcp__better-memory__memory_record_use(id, success|failure) when a memory materially helps or misleads. Use mcp__better-memory__memory_observe to write new ones.
```

**Format notes:**

- Per-hint truncation kept at 600 chars (mirrors `_HINT_MAX_CHARS` in current `session_retrieve.py`).
- Section headers carry their counts so volume is visible at a glance — important since the user opted for "no cap".
- Header line carries source + episode action — visible confirmation of what just happened, no need to query `memory_list_episodes`.
- Empty sections omitted entirely (clean output for fresh projects with no memories yet).

**MCP tool signature:**

```python
memory_session_bootstrap(
    source: Literal["startup", "resume", "clear", "compact"] | None = None,
    session_id: str | None = None,
    cwd: str | None = None,
) -> {
    "additionalContext": str,
    "project": str,
    "source": str,
    "episode": {"id": str, "action": "opened" | "reused"},
    "counts": {
        "semantic": int,
        "reflections": {"do": int, "dont": int, "neutral": int},
    },
}
```

When called with no args: `source="startup"`, `session_id=$CLAUDE_SESSION_ID env var or new uuid`, `cwd=os.getcwd()`. Manual invocation is cheap.

## 7. Hook script & install-hooks integration

**Hook script (`better_memory/hooks/session_bootstrap.py`):**

```python
def main() -> None:
    raw = ""
    try:
        raw = sys.stdin.read(_MAX_STDIN_BYTES + 1)
    except BaseException:
        pass

    payload: dict = {}
    if raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except BaseException:
            pass

    source = str(payload.get("source") or "startup")
    session_id = str(payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID") or uuid4().hex)
    cwd = str(payload.get("cwd") or os.getcwd())

    rendered: str
    try:
        cfg = get_config()
        with closing(connect(cfg.memory_db)) as conn:
            service = SessionBootstrapService(conn)
            result = service.bootstrap(source=source, session_id=session_id, cwd=Path(cwd))
        rendered = result.additional_context
    except BaseException as exc:
        record_hook_error(hook_name="session_bootstrap", exc=exc)
        rendered = _fallback_directive(exc)

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": rendered,
        }
    }), flush=True)
    sys.exit(0)
```

Defensive shape: bounded stdin read (`_MAX_STDIN_BYTES = 1_048_576`), never raises, always exits 0.

**install-hooks changes:**

```python
_OUR_HOOKS: tuple[HookSpec, ...] = (
    HookSpec("better_memory.hooks.session_bootstrap", "SessionStart", None,              False),
    HookSpec("better_memory.hooks.observer",          "PostToolUse",  "Write|Edit|Bash", True),
    HookSpec("better_memory.hooks.session_close",     "Stop",         None,              True),
)

_LEGACY_HOOK_MODULES: frozenset[str] = frozenset({
    "better_memory.hooks.session_start",
    "better_memory.hooks.session_retrieve",
})
```

In `merge_settings_json`, the REMOVE pass strips by `module in command`, extended:

```python
strip_modules = our_module_paths | _LEGACY_HOOK_MODULES
kept_hooks = [
    h for h in group.get("hooks", [])
    if not any(mp in h.get("command", "") for mp in strip_modules)
]
```

Re-running `install_hooks` after upgrading scrubs the two legacy entries and writes the single `session_bootstrap` entry. Idempotent. Existing backup mechanism (`_backup`) runs before any write so a user can roll back.

No changes to `merge_claude_json`. The new MCP tool is auto-registered when the server restarts because tool registration happens at server startup, not at install time.

## 8. Error handling

| Failure | Response |
|---|---|
| stdin unparseable / missing `source` | Default `source="startup"`, default ids, service runs. Idempotency guard handles the case where it was actually a continuity event. |
| Unknown `source` value | Coerce to `"startup"`. |
| DB connection fails (locked, missing, corrupt) | `record_hook_error("session_bootstrap", exc)` logs the failure; hook injects fallback directive (see below); exit 0. |
| Episode open succeeds, retrieval fails | Service raises; hook's outer `try` catches; same fallback directive; exit 0. |
| Service returns very large markdown | No truncation. User accepted "no cap". A future limit, if needed, lives on the service, not the hook. |
| Hook process timeout | No SessionStart timeout currently set in settings.json (default 60s). Untouched. In-process DB read should complete well under that. |

**Fallback directive:**

```
better-memory: session bootstrap failed (<ExceptionClass>: <one-line message>).
Call mcp__better-memory__memory_session_bootstrap manually before any task.
If the failure persists, check ~/.better-memory/hook_errors and consider rolling back via the install-backups directory.
```

This gives Claude a concrete recovery path without requiring user intervention.

**Concurrent firing concern:** Not applicable. Claude Code emits SessionStart events sequentially. The idempotency guard protects against sequential re-firings within a session_id (compact / resume / clear after startup), not against concurrent ones.

**Connection / transaction sharing:** The plan should include a verification step confirming `EpisodeService.open_background` and the retrieval queries can run on the same connection (likely yes, given existing patterns). If they require independent transaction envelopes, the failure rollback story may need adjustment — flag for the implementation phase.

## 9. Spool/drain cleanup

In `better_memory/services/spool.py`:

1. Remove `_maybe_open_episode_for_session_start` method.
2. Remove the `if event_type == "session_start"` branch in `drain`.
3. Update the docstring at line 61 to drop the reference to processing `session_start` events.
4. Leave the `event_type == "session_end"` branch and `_maybe_close_episode_for_session_end` unchanged (`session_close.py` is out of scope).
5. Leave commit_close handling unchanged.

In `better_memory/hooks/`:

1. Delete `session_start.py`.
2. Delete `session_retrieve.py`.

## 10. Testing strategy

**Unit tests for `SessionBootstrapService`:**

- `bootstrap(source="startup", session_id=NEW)` → `action="opened"`, correct project, full retrieval rendered.
- `bootstrap(source="compact" | "resume" | "clear", session_id=EXISTING)` → `action="reused"`, same render.
- `bootstrap(source=None | "unknown", session_id=...)` → coerces to `"startup"`.
- Project resolution variants (parametrized):
  - cwd at repo root → main repo dir name
  - cwd in a subdirectory of the repo → main repo dir name (via `--git-common-dir`)
  - cwd in a worktree of the repo → main repo dir name (NOT the worktree dir name)
  - cwd not in any git repo → `"general"`
  - cwd has `.better-memory` override → override wins (takes priority over git)
- Empty DB → empty sections, no crash.
- DB with mix of project + general scope rows → render contains both, ordered correctly.
- "No cap" verification: seed 50 reflections per bucket + 100 semantic memories, assert all rendered.

**Unit tests for `project_name(cwd)`:**

- Standalone tests for git-aware resolution. Use `tmp_path` + `subprocess.run("git init")`. Cover:
  - Override priority (`.better-memory` file in cwd beats git resolution).
  - cwd at repo root → repo name.
  - cwd in a subdirectory of the repo → repo name (`--git-common-dir` resolution).
  - cwd in a worktree (`git worktree add`) → main repo name, NOT the worktree dir name.
  - cwd has no git tree → `"general"`.
  - Subprocess failure (e.g. git not installed, permission error) → graceful fallback to `"general"`.

**Hook subprocess tests:**

- Run `python -m better_memory.hooks.session_bootstrap` with controlled stdin.
- Valid SessionStart payload → stdout has correct `hookSpecificOutput.additionalContext`, exit 0.
- Empty stdin → defaults applied, exit 0.
- Malformed JSON stdin → defaults applied, exit 0.
- DB pointed at a corrupt file → fallback directive in stdout, `hook_errors` row written, exit 0.
- Bounded stdin (>1 MiB) → silently dropped, exit 0.

**MCP tool integration test:**

- Invoke `memory_session_bootstrap` via the MCP test harness; assert returned dict has `additionalContext`, `project`, `source`, `episode.action`, `counts`.
- Invoke with no args → defaults applied, episode opened.
- Invoke twice with same `session_id` → second call reports `action="reused"`.

**`install-hooks` merge tests:**

- `merge_settings_json(empty)` → produces config with one `session_bootstrap` entry under SessionStart.
- `merge_settings_json(legacy)` (config has two old SessionStart entries) → REMOVE pass strips both legacy entries, ADD pass writes the new single entry. Idempotent on re-run.
- `merge_settings_json(mixed)` (user has a non-better-memory hook in SessionStart) → user's hook preserved.

**Regression tests to delete:**

- Spool drain tests exercising `event_type == "session_start"` handling.
- `_maybe_open_episode_for_session_start` tests.
- Existing `test_session_start.py` / `test_session_retrieve.py` hook tests (hooks are gone).

**Test fixtures:**

- Existing `tmp_path` SQLite fixture used by other service tests — reuse.
- New helper: `git_repo_at(path)` fixture that runs `git init` and returns the path. Allows realistic repo-structure tests without touching the real filesystem.

## 11. Migration & rollout

**For users running `install_hooks` after upgrading:**

1. `_LEGACY_HOOK_MODULES` ensures `session_start` and `session_retrieve` entries are scrubbed from `~/.claude/settings.json`.
2. New single `session_bootstrap` entry is written.
3. `_backup()` writes a timestamped backup of the prior `settings.json` to `~/.better-memory/install-backups/`.
4. The user must restart Claude Code for the new hook to take effect (existing instruction printed by `install_hooks`).

**Memory data:**

- No schema changes.
- No data migration. Memories already scoped to a `cwd.name` that happens to coincide with a git repo name (the common case) keep working unchanged.
- Memories scoped to subdirectory names (rare; only if Claude was ever invoked in a subdirectory of a git repo) become orphaned — they remain in the DB but won't be retrieved by the new bootstrap unless the user manually re-tags them. Risk accepted.

**Existing spool markers:**

- `*_session_start_*.json` files in `~/.better-memory/spool/` from previous sessions still parse and validate (the only required fields are `event_type` and `timestamp`). On the next drain they are inserted into `hook_events` (benign — just history), the if/elif chain in pass 2.5 has no matching branch (so no episode side-effect), and the file is unlinked in pass 3. No quarantine, no error. The hook_events rows accumulate harmlessly.

## 12. Open questions / verification points

These are flagged for the implementation phase, not blockers for the spec:

1. **Connection sharing:** confirm `EpisodeService.open_background`, `SemanticMemoryService.list_for_project`, and `ReflectionSynthesisService.retrieve_reflections` can share one connection inside the bootstrap method without transaction envelope conflicts. If they require independent envelopes, restructure the service to use a per-query connection or commit between calls.
2. **`limit_per_bucket=None` semantics:** decide between extending `retrieve_reflections` to accept `None` (cleaner API) or passing `sys.maxsize` (no service signature change). Implementer's call.
3. **Subprocess cost of git-aware `project_name()`:** the new resolution invokes `git rev-parse --git-common-dir` per call. Observers call `project_name()` on every Write/Edit/Bash. Measure cost on a typical workload; if it's measurable, add per-cwd in-process memoization.
