# Session-Retrieve Hook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `better_memory.hooks.session_retrieve` — a SessionStart hook that fetches and injects project reflections as `additionalContext`, eliminating the failure mode where Claude skips `memory_retrieve` on first turn.

**Architecture:** A new sibling module under `better_memory/hooks/`, registered as a second SessionStart hook (Claude Code concatenates `additionalContext` from multiple SessionStart hooks). Reads `memory.db` directly via `ReflectionSynthesisService`. Failure-isolated from the existing `session_start.py` spool-marker.

**Tech Stack:** Python 3.12, `sqlite3` (stdlib), `better_memory.*` (in-process imports), pytest, subprocess

**Spec:** [`docs/superpowers/specs/2026-05-06-session-memory-injection-hook-design.md`](../specs/2026-05-06-session-memory-injection-hook-design.md)

**File structure:**
- `better_memory/hooks/session_retrieve.py` (new) — entry point + render helpers
- `tests/hooks/test_session_retrieve.py` (new) — subprocess-style hook tests
- `README.md` — extend Manual setup section's hooks JSON
- `website/configuration.md` — cross-reference

---

## Task 1: Hook entry + populated-DB happy path

**Confidence:** 95%
**Files:**
- Create: `tests/hooks/test_session_retrieve.py`
- Create: `better_memory/hooks/session_retrieve.py`

- [ ] **Step 1: Write the failing test**

Create `tests/hooks/test_session_retrieve.py` with:

```python
"""Tests for ``better_memory.hooks.session_retrieve``.

Mirrors the subprocess pattern used by tests/hooks/test_session_start.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.migrations import apply_migrations

_MIGRATIONS = Path(__file__).resolve().parents[2] / "better_memory" / "db" / "migrations"


def _run_hook(
    home_dir: Path,
    *,
    stdin: str = "",
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "BETTER_MEMORY_HOME": str(home_dir)}
    return subprocess.run(
        [sys.executable, "-m", "better_memory.hooks.session_retrieve"],
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        cwd=str(cwd) if cwd is not None else None,
    )


def _seed_reflection(
    conn,
    *,
    title: str,
    project: str,
    polarity: str,
    use_cases: str = "Test scenario",
    hints: list[str] | None = None,
    confidence: float = 0.9,
    tech: str | None = None,
    phase: str = "implementation",
) -> str:
    rid = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO reflections (
            id, title, project, tech, phase, polarity, use_cases, hints,
            confidence, status, evidence_count, created_at, updated_at, scope
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', 1, ?, ?, 'project')
        """,
        (
            rid, title, project, tech, phase, polarity, use_cases,
            json.dumps(hints or ["a hint"]), confidence, now, now,
        ),
    )
    conn.commit()
    return rid


@pytest.fixture
def home_with_schema(tmp_path: Path) -> Path:
    home = tmp_path / "better-memory-home"
    home.mkdir()
    conn = connect(home / "memory.db")
    try:
        apply_migrations(conn, migrations_dir=_MIGRATIONS)
    finally:
        conn.close()
    return home


def test_populated_db_renders_buckets(home_with_schema: Path, tmp_path: Path) -> None:
    project_dir = tmp_path / "demo-project"
    project_dir.mkdir()
    conn = connect(home_with_schema / "memory.db")
    try:
        _seed_reflection(
            conn, title="Use Timespan.hour", project="demo-project",
            polarity="do", use_cases="Growatt API consumption queries",
            hints=["Switch to Timespan.hour", "12-snapshot aggregate"],
        )
        _seed_reflection(
            conn, title="Avoid Timespan.day", project="demo-project",
            polarity="dont", use_cases="Growatt API queries",
            hints=["Returns inflated values"],
        )
    finally:
        conn.close()

    result = _run_hook(home_with_schema, cwd=project_dir)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "Use Timespan.hour" in ctx
    assert "Avoid Timespan.day" in ctx
    assert "### do" in ctx
    assert "### dont" in ctx
    assert "memory_record_use" in ctx  # footer present
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/hooks/test_session_retrieve.py::test_populated_db_renders_buckets -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'better_memory.hooks.session_retrieve'`

- [ ] **Step 3: Write minimal implementation**

Create `better_memory/hooks/session_retrieve.py`:

```python
"""Session-start hook: inject persisted reflections as additionalContext.

Companion to ``session_start.py`` (which writes a spool marker for episode
lazy-open). This module surfaces prior reflections at the start of every
Claude Code session so Claude does not have to remember to call
``memory_retrieve`` on first turn.

Reads stdin payload (Claude Code SessionStart event JSON, currently unused —
reserved for future source-aware behaviour), opens memory.db, calls
ReflectionSynthesisService.retrieve_reflections, renders the three buckets
to Markdown, prints a hookSpecificOutput JSON envelope to stdout, and exits
0. Never raises; on any error, logs to hook_errors and injects a fallback
directive so Claude still gets a signal to retrieve manually.
"""

from __future__ import annotations

import json
import sys
from contextlib import closing

from better_memory.config import get_config, project_name
from better_memory.db.connection import connect
from better_memory.hooks._error_log import record_hook_error
from better_memory.services.reflection import ReflectionSynthesisService

# Per-bucket cap and per-hint truncation. See spec §Decisions log.
_LIMIT_PER_BUCKET = 10
_HINT_MAX_CHARS = 600

_FOOTER = (
    "Use mcp__better-memory__memory_record_use(id, success|failure) when a "
    "memory materially helps or misleads. Use mcp__better-memory__memory_observe "
    "to write new ones."
)

_EMPTY_PROJECT_MESSAGE = (
    "better-memory: no reflections recorded yet for this project. Use "
    "mcp__better-memory__memory_observe to record observations as you work; "
    "reflections will be distilled from them on episode close."
)


def _truncate_hint(hint: str) -> str:
    if len(hint) <= _HINT_MAX_CHARS:
        return hint
    return hint[: _HINT_MAX_CHARS - 1] + "…"


def _render_bucket(name: str, items: list[dict]) -> str:
    lines = [f"### {name}"]
    for item in items:
        lines.append(f"**{item['title']}**")
        lines.append(f"_{item['use_cases']}_")
        for hint in item.get("hints", []):
            lines.append(f"- {_truncate_hint(hint)}")
        lines.append(f"_id: {item['id']}_")
        lines.append("")
    return "\n".join(lines)


def _render(buckets: dict[str, list[dict]]) -> str:
    if not any(buckets[k] for k in ("do", "dont", "neutral")):
        return _EMPTY_PROJECT_MESSAGE

    sections = ["## Persisted reflections for this project (better-memory)"]
    if buckets["do"]:
        sections.append(_render_bucket("do (prior wins)", buckets["do"]))
    if buckets["dont"]:
        sections.append(_render_bucket("dont (approaches to avoid)", buckets["dont"]))
    if buckets["neutral"]:
        sections.append(_render_bucket("neutral (context)", buckets["neutral"]))
    sections.append(_FOOTER)
    return "\n\n".join(sections)


def _short_msg(exc: BaseException, *, limit: int = 200) -> str:
    msg = str(exc).splitlines()[0] if str(exc) else ""
    return msg[:limit]


def _fallback_directive(exc: BaseException) -> str:
    return (
        f"better-memory: memory injection failed "
        f"({type(exc).__name__}: {_short_msg(exc)}). "
        f"Call mcp__better-memory__memory_retrieve manually before any task in this session."
    )


def _record_failure(exc: BaseException) -> None:
    try:
        record_hook_error(hook_name="session_retrieve", exc=exc)
    except BaseException:  # noqa: BLE001
        pass
    try:
        sys.stderr.write(
            f"[better-memory] session_retrieve: {type(exc).__name__}: {_short_msg(exc)}\n"
        )
    except BaseException:  # noqa: BLE001
        pass


def _print_hook_output(text: str) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }
    print(json.dumps(payload), flush=True)


def main() -> None:
    # Drain stdin defensively — Claude Code may pipe a session payload, but we
    # currently don't act on it. Reading prevents PIPE-related write blocking.
    try:
        sys.stdin.read()
    except BaseException:  # noqa: BLE001
        pass

    rendered: str
    try:
        cfg = get_config()
        proj = project_name()
        with closing(connect(cfg.memory_db)) as conn:
            service = ReflectionSynthesisService(conn)
            buckets = service.retrieve_reflections(
                project=proj, limit_per_bucket=_LIMIT_PER_BUCKET,
            )
        rendered = _render(buckets)
    except BaseException as exc:  # noqa: BLE001
        _record_failure(exc)
        rendered = _fallback_directive(exc)

    try:
        _print_hook_output(rendered)
    except BaseException:  # noqa: BLE001
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/hooks/test_session_retrieve.py::test_populated_db_renders_buckets -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/hooks/session_retrieve.py tests/hooks/test_session_retrieve.py
git commit -m "feat(hooks): session_retrieve injects reflection buckets as additionalContext"
```

---

## Task 2: Empty-DB branch ("no memory yet" message)

**Confidence:** 90%
**Files:**
- Modify: `tests/hooks/test_session_retrieve.py` (append test)

The implementation already covers this branch via `_render`'s empty check — we just need to verify it.

- [ ] **Step 1: Write the failing test**

Append to `tests/hooks/test_session_retrieve.py`:

```python
def test_empty_db_injects_no_memory_yet_message(
    home_with_schema: Path, tmp_path: Path
) -> None:
    project_dir = tmp_path / "fresh-project"
    project_dir.mkdir()

    result = _run_hook(home_with_schema, cwd=project_dir)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "no reflections recorded yet" in ctx
    assert "memory_observe" in ctx
    # No bucket headings, no fallback "memory injection failed" text.
    assert "### do" not in ctx
    assert "memory injection failed" not in ctx
```

- [ ] **Step 2: Run test to verify it passes (already implemented in Task 1)**

```
uv run pytest tests/hooks/test_session_retrieve.py::test_empty_db_injects_no_memory_yet_message -v
```

Expected: PASS (no implementation change needed; `_render` already returns `_EMPTY_PROJECT_MESSAGE` when all buckets are empty).

- [ ] **Step 3: Commit**

```bash
git add tests/hooks/test_session_retrieve.py
git commit -m "test(hooks): cover session_retrieve empty-DB branch"
```

---

## Task 3: Failure handling — missing DB and simulated SQL error

**Confidence:** 90%
**Files:**
- Modify: `tests/hooks/test_session_retrieve.py` (append two tests)

Two distinct failure modes both flow through the same exception handler. We assert: fallback directive injection + `hook_errors` row + stderr line + exit 0.

- [ ] **Step 1: Write the failing tests**

Append to `tests/hooks/test_session_retrieve.py`:

```python
def _read_hook_errors(home: Path) -> list[dict]:
    """Read the hook_errors table after a hook ran. Returns rows as dicts."""
    conn = connect(home / "memory.db")
    try:
        rows = conn.execute(
            "SELECT id, hook_name, exception_type, exception_message FROM hook_errors"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def test_missing_db_injects_fallback_directive(tmp_path: Path) -> None:
    """No memory.db at all (first install before MCP server has booted)."""
    home = tmp_path / "no-db-home"
    home.mkdir()
    project_dir = tmp_path / "demo"
    project_dir.mkdir()

    result = _run_hook(home, cwd=project_dir)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("better-memory: memory injection failed (")
    assert "memory_retrieve manually" in ctx
    assert "session_retrieve:" in result.stderr


def test_simulated_sql_error_injects_fallback(
    home_with_schema: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """retrieve_reflections raises → fallback path."""
    project_dir = tmp_path / "demo"
    project_dir.mkdir()

    # Use a bootstrap script to monkeypatch the service inside the subprocess.
    # subprocess.run's monkeypatch fixture only affects the parent process, so
    # we point sys.executable at a -c that imports + patches + runs main().
    bootstrap = (
        "import sqlite3, sys\n"
        "from better_memory.services import reflection as rmod\n"
        "def _boom(self, **kw):\n"
        "    raise sqlite3.OperationalError('simulated retrieve failure')\n"
        "rmod.ReflectionSynthesisService.retrieve_reflections = _boom\n"
        "from better_memory.hooks.session_retrieve import main\n"
        "main()\n"
    )
    env = {**os.environ, "BETTER_MEMORY_HOME": str(home_with_schema)}
    result = subprocess.run(
        [sys.executable, "-c", bootstrap],
        text=True, capture_output=True, env=env, timeout=30,
        cwd=str(project_dir),
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "memory injection failed (OperationalError: simulated retrieve failure" in ctx
    rows = _read_hook_errors(home_with_schema)
    assert len(rows) == 1
    assert rows[0]["hook_name"] == "session_retrieve"
    assert rows[0]["exception_type"] == "OperationalError"
```

- [ ] **Step 2: Run tests to verify they pass**

```
uv run pytest tests/hooks/test_session_retrieve.py::test_missing_db_injects_fallback_directive tests/hooks/test_session_retrieve.py::test_simulated_sql_error_injects_fallback -v
```

Expected: BOTH PASS (the implementation's `try/except BaseException` already routes both to `_fallback_directive` + `_record_failure`).

If either fails because the missing-DB case raises an exception type the handler doesn't catch, inspect `_record_failure` and ensure `record_hook_error` is wrapped (it already is per the implementation in Task 1).

- [ ] **Step 3: Commit**

```bash
git add tests/hooks/test_session_retrieve.py
git commit -m "test(hooks): cover session_retrieve fallback on missing DB and SQL error"
```

---

## Task 4: Hint truncation to 600 chars

**Confidence:** 95%
**Files:**
- Modify: `tests/hooks/test_session_retrieve.py` (append test)

- [ ] **Step 1: Write the failing test**

Append to `tests/hooks/test_session_retrieve.py`:

```python
def test_hint_truncation(home_with_schema: Path, tmp_path: Path) -> None:
    project_dir = tmp_path / "trunc-project"
    project_dir.mkdir()
    long_hint = "x" * 1500  # 2.5x the 600 cap
    conn = connect(home_with_schema / "memory.db")
    try:
        _seed_reflection(
            conn, title="Long-hint reflection", project="trunc-project",
            polarity="do", hints=[long_hint, "short hint"],
        )
    finally:
        conn.close()

    result = _run_hook(home_with_schema, cwd=project_dir)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    # Find the long-hint line; assert it was truncated and ends with ellipsis.
    long_lines = [ln for ln in ctx.split("\n") if ln.startswith("- xxxx")]
    assert len(long_lines) == 1
    truncated = long_lines[0]
    # "- " prefix (2) + truncated hint (599 chars + "…" = 600) = 602 chars on the line
    assert len(truncated) == 2 + 600
    assert truncated.endswith("…")
    # Short hint remained intact.
    assert "- short hint" in ctx
```

- [ ] **Step 2: Run test to verify it passes**

```
uv run pytest tests/hooks/test_session_retrieve.py::test_hint_truncation -v
```

Expected: PASS (`_truncate_hint` in Task 1's implementation already enforces this).

- [ ] **Step 3: Commit**

```bash
git add tests/hooks/test_session_retrieve.py
git commit -m "test(hooks): assert session_retrieve hint truncation at 600 chars"
```

---

## Task 5: Bucket cap top-10

**Confidence:** 90%
**Files:**
- Modify: `tests/hooks/test_session_retrieve.py` (append test)

`ReflectionSynthesisService.retrieve_reflections` already enforces `limit_per_bucket=10` (see `services/reflection.py:1158`); this test pins the contract.

- [ ] **Step 1: Write the failing test**

Append to `tests/hooks/test_session_retrieve.py`:

```python
def test_bucket_cap_top_10(home_with_schema: Path, tmp_path: Path) -> None:
    project_dir = tmp_path / "cap-project"
    project_dir.mkdir()
    conn = connect(home_with_schema / "memory.db")
    try:
        # Seed 12 do-reflections with varying confidence so we can detect the cap.
        for i in range(12):
            _seed_reflection(
                conn, title=f"Reflection-{i:02d}", project="cap-project",
                polarity="do", confidence=0.5 + (i * 0.04),  # 0.50..0.94 ascending
            )
    finally:
        conn.close()

    result = _run_hook(home_with_schema, cwd=project_dir)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    rendered = [f"Reflection-{i:02d}" for i in range(12)]
    present = [name for name in rendered if name in ctx]
    assert len(present) == 10
    # The two with lowest confidence (Reflection-00, Reflection-01) should be excluded.
    assert "Reflection-00" not in ctx
    assert "Reflection-01" not in ctx
```

- [ ] **Step 2: Run test to verify it passes**

```
uv run pytest tests/hooks/test_session_retrieve.py::test_bucket_cap_top_10 -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/hooks/test_session_retrieve.py
git commit -m "test(hooks): assert session_retrieve caps each bucket at 10"
```

---

## Task 6: Hook never exits non-zero (across all paths)

**Confidence:** 95%
**Files:**
- Modify: `tests/hooks/test_session_retrieve.py` (append parametrized test)

Belt-and-braces: walks the three documented states (populated, missing-DB, simulated error) and asserts `returncode == 0` for each. Existing per-test asserts already check this; this consolidated test makes the contract explicit.

- [ ] **Step 1: Write the failing test**

Append to `tests/hooks/test_session_retrieve.py`:

```python
def test_returncode_always_zero(home_with_schema: Path, tmp_path: Path) -> None:
    """Belt-and-braces: hook MUST never exit non-zero, regardless of state."""
    project_dir = tmp_path / "exit-project"
    project_dir.mkdir()

    # 1. Empty DB.
    r1 = _run_hook(home_with_schema, cwd=project_dir)
    assert r1.returncode == 0

    # 2. Populated DB.
    conn = connect(home_with_schema / "memory.db")
    try:
        _seed_reflection(conn, title="Some lesson", project="exit-project", polarity="do")
    finally:
        conn.close()
    r2 = _run_hook(home_with_schema, cwd=project_dir)
    assert r2.returncode == 0

    # 3. Missing DB entirely.
    no_db_home = tmp_path / "missing"
    no_db_home.mkdir()
    r3 = _run_hook(no_db_home, cwd=project_dir)
    assert r3.returncode == 0
```

- [ ] **Step 2: Run test to verify it passes**

```
uv run pytest tests/hooks/test_session_retrieve.py::test_returncode_always_zero -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/hooks/test_session_retrieve.py
git commit -m "test(hooks): pin session_retrieve never-exit-nonzero contract"
```

---

## Task 7: README.md hooks-JSON update

**Confidence:** 95%
**Files:**
- Modify: `README.md` (extend Manual setup section)

- [ ] **Step 1: Make the edit**

In `README.md`, find the Manual setup section's `~/.claude/settings.json` example. After the `Stop` hook block, before the closing `}` of the `hooks` object, add the SessionStart entries.

The current section (around line 65–95) shows `PostToolUse` and `Stop` only. Replace the entire `"hooks": { ... }` JSON block with this expanded version:

```json
{
  "hooks": {
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
    ],
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

Immediately above the JSON block, add this paragraph:

```markdown
Two SessionStart hooks ship: `session_start` writes a spool marker so the MCP server can lazy-open a background episode for the session, and `session_retrieve` queries `memory.db` and injects the project's reflections (`do` / `dont` / `neutral` buckets) as `additionalContext` so Claude has prior memory available without needing to call `memory_retrieve` first. Both should be registered — Claude Code concatenates `additionalContext` across hooks.
```

- [ ] **Step 2: Verify the README parses as valid Markdown / JSON**

```
uv run mkdocs build --strict 2>&1 | tail -5
```

Expected: build succeeds (the README isn't part of the mkdocs site, but the build is a fast smoke check that nothing else regressed).

Manually validate the JSON snippet by pasting into `python -c "import json,sys; json.loads(sys.stdin.read())"`:

```bash
python -c "import json; json.loads(open('README.md').read().split('\`\`\`json')[1].split('\`\`\`')[0])"
```

Expected: no error (the first JSON block in README is now the expanded hooks config).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): document new SessionStart memory-injection hook"
```

---

## Task 8: website/configuration.md cross-reference

**Confidence:** 95%
**Files:**
- Modify: `website/configuration.md`

- [ ] **Step 1: Make the edit**

In `website/configuration.md`, find the "Filesystem layout" section (search for `## Filesystem layout`). After the existing paragraph that begins "The two SQLite files are never shared between processes…", append:

```markdown

## Hooks

Three Claude Code hooks ship with better-memory and read or write the filesystem layout above:

- **`better_memory.hooks.session_start`** (SessionStart) — writes a marker JSON to `spool/` so the MCP server can lazy-open a background episode for the session.
- **`better_memory.hooks.session_retrieve`** (SessionStart) — opens `memory.db` and injects the project's distilled reflections (`do` / `dont` / `neutral`, capped at 10 per bucket) as `additionalContext` for Claude's first turn. Failure-isolated: if injection breaks, a fallback directive is injected instead and the failure is recorded in the `hook_errors` table.
- **`better_memory.hooks.observer`** (PostToolUse) — captures tool-call snapshots into `spool/` for later observation creation.
- **`better_memory.hooks.session_close`** (Stop) — writes a session-close marker into `spool/`.

See [`README.md`](https://github.com/emp3thy/better-memory/blob/main/README.md#manual-setup) for the exact `~/.claude/settings.json` registration JSON.
```

- [ ] **Step 2: Verify mkdocs build**

```
uv run mkdocs build --strict 2>&1 | tail -5
```

Expected: build succeeds.

- [ ] **Step 3: Commit**

```bash
git add website/configuration.md
git commit -m "docs(website): document SessionStart memory-injection hook"
```

---

## Manual smoke test (post-implementation)

Not a step but a recommended sanity check before merging:

1. Register both SessionStart hooks in your real `~/.claude/settings.json` (using the absolute path to the dev venv's `python`).
2. Restart Claude Code.
3. Open a fresh session in this repo.
4. Verify the first turn's system reminder includes the rendered Markdown (you can ask Claude `what reflections did you receive at session start?` — it should quote them back).
5. Move to a directory without `BETTER_MEMORY_HOME` content; verify the hook injects the "no memory yet" message instead of erroring.

---

## Confidence summary

All 8 tasks ≥ 90% confidence:
- 95%: Tasks 1, 4, 6, 7, 8 (well-defined, all primitives verified during spec)
- 90%: Tasks 2, 3, 5 (depend on Task 1 implementation behaving as designed; covered by tests)

Should any test in Tasks 2-6 reveal a bug in Task 1's implementation, fix it in Task 1's commit (re-open) rather than hot-patching downstream.

---

## Out of scope (explicitly deferred)

- Setup-script auto-install — Track B (`scripts/setup.sh` integration).
- Surfacing semantic memories, pending synthesis count, open episodes — future work.
- Configurability via env var — YAGNI for v1.

---

## References

- Spec: [`docs/superpowers/specs/2026-05-06-session-memory-injection-hook-design.md`](../specs/2026-05-06-session-memory-injection-hook-design.md)
- Existing hook patterns: `better_memory/hooks/session_start.py`, `tests/hooks/test_session_start.py`
- Service signature: `better_memory/services/reflection.py:1106`
- `record_hook_error` helper: `better_memory/hooks/_error_log.py`
- Reflections schema: `better_memory/db/migrations/0002_episodic.sql:155`, `0007_reflection_scope.sql`
