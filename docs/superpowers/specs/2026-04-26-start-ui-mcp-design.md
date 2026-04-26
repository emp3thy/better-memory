# `memory.start_ui()` MCP tool — design

**Status:** draft · **Date:** 2026-04-26 · **Supersedes:** the `memory.start_ui()` portion of `2026-04-18-better-memory-ui-design.md` §6 (Phase 10)

---

## 1. Scope, goals, non-goals

### Scope

Replace the stubbed `memory.start_ui` MCP handler with a working implementation that spawns the existing `better_memory.ui` Flask app as a detached subprocess, returns the bound URL, and reuses an existing live UI when one is already running. Move the spawn/liveness/cleanup logic into a dedicated service module so it is unit-testable in isolation from the MCP framework.

### Goals

1. Calling the MCP tool from any client (Claude Code, the user-level skill, manual MCP tooling) returns a usable URL.
2. Repeated calls are idempotent: a live UI is reused; a stale one is cleaned up and replaced.
3. The spawned UI survives MCP server termination (the MCP server is per-session; the UI must outlive it).
4. All logic outside the 3-line handler passthrough is unit-testable without spawning a real subprocess.

### Non-goals

- `memory.shutdown_ui` companion tool. The UI's existing 30-min idle timeout and `POST /shutdown` button are sufficient for the workflows this design serves.
- PID-file tracking. HTTP `/healthz` liveness covers the same detection need without an additional file.
- Browser opening. Client (the user-level skill, or whoever calls the tool) is responsible for opening the returned URL.
- Audit logging for `start_ui` calls. The action does not transition any persisted database state. The existing `audit.py` exclusion stays; the "stub today" wording is removed.
- Cross-platform parity beyond Windows + POSIX (the only platforms in scope for this project).

---

## 2. Background — what already exists

- `better_memory/ui/__main__.py` is the spawn target. It binds `127.0.0.1:0`, writes the bound URL atomically to `$BETTER_MEMORY_HOME/ui.url`, and unlinks that file on clean exit (via `atexit`).
- `better_memory/ui/app.py` provides:
  - `GET /healthz` — `200 ok` liveness probe.
  - `POST /shutdown` — exits the process (via `threading.Timer(0.1, os._exit, (0,))`).
  - 30-minute inactivity watchdog that calls `os._exit(0)` after the idle period and unlinks `ui.url` on its way out.
  - Origin-check on non-`GET`/`HEAD` requests (loopback safety).
- `better_memory/mcp/server.py` registers `memory.start_ui` as a Tool but the handler returns `{"error": "UI not yet implemented — planned for Plan 2."}` as a JSON payload.
- `better_memory/services/audit.py` documents `memory.start_ui` as "a stub today and performs no state transition; nothing to audit until it actually does something."
- `README.md` documents a manual workaround: `BETTER_MEMORY_HOME=~/.better-memory uv run python -m better_memory.ui`. This workaround is what the MCP tool replaces.

The 2026-04-18 spec proposed a PID-file-based single-instance guard and 5 s spawn timeout; this design diverges from both — see §9.

---

## 3. Architecture

### Handler/service split

A new module — `better_memory/services/ui_launcher.py` — owns all spawn, liveness, and cleanup logic.

The MCP handler in `better_memory/mcp/server.py` becomes a thin passthrough:

```python
if name == "memory.start_ui":
    result = ui_launcher.start_ui()
    return [TextContent(type="text", text=json.dumps(result))]
```

Rationale: matches the existing handler-thin / service-fat pattern used by `episodes`, `reflections`, and `observations`. Keeps the MCP server free of platform-conditional spawn code. Makes the launcher unit-testable without an MCP fixture.

### Public service interface

```python
def start_ui() -> dict:
    """Return {"url": str, "reused": bool}. Raises on failure."""
```

- `reused=True` — a live UI was found at the URL recorded in `ui.url`; no new process was spawned.
- `reused=False` — no live UI was running; a fresh process was spawned and is now serving.
- Failures (spawn refused, timeout, URL appears but unresponsive) raise exceptions. The MCP framework already converts uncaught exceptions to `CallToolResult(isError=True)` per the convention documented at the top of `server.py`. The stub's JSON-error pattern was a deliberate one-off because the feature was not built yet; with a real implementation, real exceptions are correct.

### Tool description and audit doc updates

- `memory.start_ui` Tool description in `server.py`: `"Spawn or reuse the better-memory review UI. Returns {url, reused}."`
- `services/audit.py` module docstring: drop the "stub today" sentence. The action still performs no DB state transition, so the bullet remains in the "deliberate non-audit surfaces" list with revised wording.

---

## 4. Liveness detection and spawn flow

`start_ui()` executes the following sequence. All paths share `home = resolve_home()` and `url_path = home / "ui.url"`.

1. **Liveness check.**
   - If `url_path` does not exist, fall through to step 3.
   - Otherwise read it. If unreadable (corrupt, empty, parse error), unlink and fall through to step 3.
   - HTTP `GET <url>/healthz` with a 1 s timeout (`urllib.request`, stdlib only — no new dependency). HTTP 200 with body `ok` ⇒ return `{"url": url, "reused": True}`.
   - Any other response, connection refused, or timeout ⇒ stale. Unlink `url_path` and fall through.

2. **Spawn detached subprocess.**

   ```python
   log_path = home / "ui.log"
   log_fh = log_path.open("ab")  # append-binary; truncation handled by retention
   subprocess.Popen(
       [sys.executable, "-m", "better_memory.ui"],
       stdin=subprocess.DEVNULL,
       stdout=log_fh,
       stderr=log_fh,
       close_fds=True,
       **_detach_kwargs(),
   )
   ```

   `_detach_kwargs()`:
   - POSIX: `{"start_new_session": True}`
   - Windows: `{"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}`

   `sys.executable` is the venv's Python (the MCP server itself runs under it), so no `uv run` wrapper is needed. Env is inherited so `BETTER_MEMORY_HOME` and friends propagate.

   `ui.log` is opened in append mode so successive launches accumulate. Rotation is out-of-scope (mirrors the 2026-04-18 spec's "rotated manually out-of-band" decision).

3. **Wait for `ui.url` to appear.**
   - Poll `url_path` every 100 ms up to a 10 s timeout (configurable parameter for tests).
   - Timeout exceeded ⇒ raise `RuntimeError(f"UI did not write ui.url within {timeout}s; check ui.log")`. The orphaned subprocess (if any) is not killed — `Popen` with detach flags does not give us a clean PID to target across both platforms. The strand exits on its own via the UI's existing 30-minute idle watchdog. Cost of strand: one idle Python process for up to 30 minutes.

4. **Confirm liveness.** Same `GET /healthz` as step 1, 1 s timeout.
   - 200 ⇒ return `{"url": url, "reused": False}`.
   - Anything else ⇒ wait one more 1 s cycle and retry once. Still failing ⇒ raise `RuntimeError("UI wrote ui.url but /healthz did not respond")`.

### Why 10 s, not 5 s

The 2026-04-18 spec specified 5 s. This design uses 10 s as a deliberate margin for cold Python startup on Windows, where importing Flask + werkzeug + sqlite-vec + the ui module takes several seconds on first run after a cold cache. The timeout is a worst-case bound, not a target — typical startup is sub-second.

### Why no PID file

The 2026-04-18 spec proposed `ui.pid` as a single-instance guard. The same detection ("is there a live UI on the URL recorded in `ui.url`?") is achievable via HTTP `/healthz` alone:

- A stranded process whose URL file was lost — undetectable via PID file too (no record).
- A dead process whose URL file persists — `/healthz` times out; we unlink and respawn.
- A live process whose URL file persists — `/healthz` returns 200; we reuse.

Adding a PID file would add a second piece of state to keep in sync without changing what we can detect. Skipped on YAGNI grounds.

---

## 5. Concurrency

The MCP server is single-session and serialises tool calls per-session, so two concurrent `start_ui` invocations within one MCP server are not possible.

Two MCP servers (e.g. two Claude sessions) racing on the same `BETTER_MEMORY_HOME` is possible. Mitigation:

- Both run the liveness check first. Whichever sees a live UI reuses it.
- If both see no live UI, both spawn. The second `python -m better_memory.ui` to bind picks a different random port and overwrites `ui.url` last. The first child's URL becomes orphaned. The first user's previously-returned URL is now dead.
- Cost: a single stranded process. The next `start_ui` call detects it on the next liveness pass and the strand expires within 30 minutes of idleness via the watchdog.

A filesystem lock (`fcntl.flock` / `msvcrt.locking`) would close this race but adds Windows complexity for a single-user local tool. Documented as a known limitation rather than fixed.

---

## 6. Error handling

| Failure mode | Behaviour |
|---|---|
| `BETTER_MEMORY_HOME` not writable | `log_path.open("ab")` raises `OSError`; the launcher wraps it as `RuntimeError("cannot write to BETTER_MEMORY_HOME: ...")`. |
| `subprocess.Popen` raises (missing python, bad argv) | `Popen`'s `OSError` propagates with a wrapping message. |
| `ui.url` never appears within 10 s | `RuntimeError(f"UI did not write ui.url within {timeout}s; check ui.log")`. |
| `ui.url` appears but `/healthz` fails twice | `RuntimeError("UI wrote ui.url but /healthz did not respond")`. |
| `ui.url` exists but is corrupt | Treated as "no URL file"; unlinked and fall through to spawn. |

All `RuntimeError`s propagate out of the handler and are converted to `CallToolResult(isError=True)` by the MCP framework.

---

## 7. Testing

Tests live in `tests/services/test_ui_launcher.py`. Each test sets `BETTER_MEMORY_HOME` to a `tmp_path`-rooted directory.

| Test | Setup | Assert |
|---|---|---|
| `test_returns_existing_url_when_alive` | Pre-write `ui.url` pointing at a stub `http.server` thread serving `/healthz` → 200. Mock `subprocess.Popen`. | Returns `reused=True`; `Popen` not called. |
| `test_cleans_stale_url_file_then_spawns` | Pre-write `ui.url` pointing at an unbound port. Mock `Popen` to write a fresh `ui.url` after 50 ms and serve `/healthz` from a stub thread. | Returns `reused=False`; original `ui.url` was unlinked; `Popen` called once. |
| `test_spawns_when_no_url_file` | No pre-existing `ui.url`. Mock `Popen` as above. | Returns `reused=False`; `Popen` called with correct argv (`[sys.executable, "-m", "better_memory.ui"]`), env inherited, platform-correct detach kwargs. |
| `test_spawn_timeout_raises` | Mock `Popen` that never writes `ui.url`. Inject 1 s timeout. | Raises `RuntimeError` mentioning `ui.url`. |
| `test_url_appears_but_healthz_fails_raises` | Mock `Popen` that writes `ui.url` pointing at a 404-only stub server. | Raises `RuntimeError` mentioning `/healthz`. |
| `test_corrupt_ui_url_treated_as_missing` | Pre-write empty `ui.url`. Mock `Popen` as in spawn test. | Returns `reused=False`; old file replaced. |
| `test_log_file_used_for_stdout_stderr` | Spy on `Popen` kwargs. | `stdout` and `stderr` point at an open file handle for `ui.log`; `stdin=DEVNULL`. |

Platform branch coverage: a single `test_detach_kwargs_per_platform` parametrised over `sys.platform` (mocked via `monkeypatch.setattr`) asserts the dict contents.

MCP-handler integration test in `tests/mcp/test_server_start_ui.py` mirrors the shape of existing handler tests (e.g. `test_memory_observe`): patch `ui_launcher.start_ui` to return a fixed dict, call the handler, assert the JSON `TextContent`.

---

## 8. Skill update (out-of-tree, separate commit)

Once the MCP work merges, edit `~/.claude/skills/start-better-memory-ui/SKILL.md` to:

1. Call `mcp__better-memory__memory_start_ui` with no arguments.
2. Read `url` from the response.
3. Open the browser via `start "$url"` (Windows) / `open "$url"` (macOS) / `xdg-open "$url"` (Linux).
4. Tell the user whether the UI was reused (`reused: true`) or freshly spawned.

This is a documentation-only change to a user-level skill outside the better-memory repo; it does not block the MCP merge.

---

## 9. Deviations from 2026-04-18 spec

| Item | 2026-04-18 spec | This design | Reason |
|---|---|---|---|
| Single-instance guard | `ui.pid` PID file + `/healthz` | `/healthz` only | Detection is equivalent without the second state file. |
| Spawn timeout | 5 s | 10 s | Margin for cold Python startup on Windows. |
| Spawn argv | `["python", "-m", "better_memory.ui"]` | `[sys.executable, "-m", "better_memory.ui"]` | `sys.executable` resolves to the venv's interpreter without relying on `python` being on PATH. |
| Service split | Logic inline in the MCP handler | Extracted to `services/ui_launcher.py` | Unit-testability; mirrors existing handler-thin pattern. |

---

## 10. Out of scope (will not build)

- `memory.shutdown_ui` companion tool.
- PID-file tracking.
- Browser opening from the MCP.
- Audit logging for `start_ui` calls.
- `ui.log` rotation (truncation, size cap, archival).
- Filesystem-lock cross-MCP-server race protection.
