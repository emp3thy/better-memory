# Phase D — Type & Identity Infra Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Pyright to CI in standard mode AND eliminate divergence in project-name resolution by promoting `KnowledgeService.project_for` semantics into a canonical `project_name(cwd)` helper used across all subsystems.

**Architecture:** No code changes to production behavior; all changes are non-functional improvements. Pyright is added as a dev dependency and enforced by a new GitHub Actions workflow. The `.better-memory` override-file behavior moves from `services/knowledge.py` into `config.py:project_name()`, then six call sites are routed through it. 44 existing test type errors are fixed by file (3 distinct fix patterns).

**Tech Stack:** Python 3.12, pyright 1.1.x, uv, GitHub Actions, pytest.

**Spec:** `docs/superpowers/specs/2026-05-03-type-identity-infra-design.md`

**Plan-wide confidence:** ~92% (commit 4 / Task 10 is the lowest at 85% due to long-tail risk in the 27-error helper-split — accepted per R1 with no preemptive safety valve).

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `pyproject.toml` | modify | Add `pyright` to dev deps, add `[tool.pyright]` config block |
| `.github/workflows/typecheck.yml` | create | CI workflow running `uv run pyright` on PR + push to main |
| `better_memory/config.py` | modify | Add `project_name(cwd: Path \| None = None) -> str` |
| `better_memory/services/knowledge.py` | modify | Replace `project_for` body with `return project_name(cwd)` |
| `better_memory/services/observation.py` | modify | Default `project_resolver` becomes `project_name` |
| `better_memory/ui/app.py` | modify | Delete `_project_name()`, inline `project_name()` at 6 call sites |
| `better_memory/mcp/server.py` | modify | Replace 3× `Path.cwd().name` with `project_name()`; tighten `cleanup` return type |
| `better_memory/hooks/session_start.py` | modify | Use `project_name(Path(cwd))` at 2 sites |
| `better_memory/hooks/post_commit.py` | modify | Use `project_name(Path(cwd))` at 1 site |
| `tests/test_config.py` | modify | Add tests for `project_name()` |
| `tests/mcp/test_server_integration.py` | modify | Split `_single_json` into typed `_single_json_dict` / `_single_json_list` helpers; update 8 call sites |
| `tests/ui/test_queries_episodes.py` | modify | Add `assert ... is not None` before optional-attr access (10 sites) |
| `tests/ui/test_queries_reflections.py` | modify | Add `assert ... is not None` before optional-attr access (4 sites) |
| `tests/mcp/test_start_ui_tool.py` | modify | Add `assert tool.description is not None` before `.lower()` / `in` |
| `README.md` | modify | New `### Project-name override` section under `## Configuration` |

No new test files; all test changes are to existing files.

---

## Task 1: Pre-flight verification

**Files:** none (read-only)

- [ ] **Step 1: Confirm we are on the `phase-d` branch**

Run: `git branch --show-current`
Expected: `phase-d`

- [ ] **Step 2: Confirm working tree is clean (only `.claude/` untracked)**

Run: `git status --short`
Expected: empty output, or only `?? .claude/` (which is git-ignored at repo level if not already).

- [ ] **Step 3: Confirm baseline test suite is green before any changes**

Run: `uv run pytest -m "not integration" -x -q`
Expected: passes (note count). Record the pass count for later comparison.

- [ ] **Step 4: Confirm baseline pyright count via project venv**

Run: `uv run --with pyright pyright better_memory tests 2>&1 | tail -3`
Expected: `44 errors, 0 warnings, 0 informations` (or close — production code 0, tests 44).

---

## Task 2: Add pyright dev dep + config block

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add `pyright` to `[dependency-groups].dev`**

Edit `pyproject.toml` lines 20-26 from:

```toml
[dependency-groups]
dev = [
    "pytest",
    "pytest-asyncio",
    "pytest-playwright",
    "ruff",
]
```

to:

```toml
[dependency-groups]
dev = [
    "pytest",
    "pytest-asyncio",
    "pytest-playwright",
    "ruff",
    "pyright",
]
```

- [ ] **Step 2: Append `[tool.pyright]` block after the `[tool.ruff.lint]` block**

Append to end of `pyproject.toml`:

```toml

[tool.pyright]
include = ["better_memory", "tests"]
pythonVersion = "3.12"
typeCheckingMode = "standard"
venvPath = "."
venv = ".venv"
```

- [ ] **Step 3: Sync the venv to install pyright**

Run: `uv sync --dev`
Expected: pyright is installed (confirm with `uv run pyright --version`).

- [ ] **Step 4: Run pyright on `better_memory/` only and confirm 0 errors**

Run: `uv run pyright better_memory 2>&1 | tail -3`
Expected: `0 errors, 0 warnings, 0 informations`.

This is the gate before adding the CI workflow — if production code shows errors, stop and address them first.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "$(cat <<'EOF'
build(pyright): add pyright dev dep + [tool.pyright] standard mode

- Adds pyright to dev deps so CI and local both use the same pinned version.
- Configures standard typeCheckingMode against better_memory/ and tests/.
- pythonVersion=3.12 silences a stub bug that misreads PEP 695 syntax.
- venvPath/venv pin the resolver to .venv so imports resolve through the
  project's installed deps (without these, pyright runs in an isolated
  venv and reports false-positive missing-import errors).

better_memory/ is verified clean at this commit (0 errors). The 44 known
test errors will be addressed in subsequent commits before the CI
workflow lands.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Confidence: 95%.** Standard config block; production code already 0 errors per scout. Failure mode: an unanticipated dep version mismatch in pyright's bundled stubs surfaces a new error in `better_memory/`. Mitigation embedded: step 4 gates the commit on production-code clean; if it fails, stop and address before adding the CI workflow.

---

## Task 3: Add `project_name()` canonical resolver to config.py (TDD)

**Files:**
- Modify: `better_memory/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_config.py`:

```python


def test_project_name_defaults_to_cwd_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no override file, project_name returns cwd's leaf name."""
    cwd = tmp_path / "my-service"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    from better_memory.config import project_name
    assert project_name() == "my-service"


def test_project_name_explicit_cwd(tmp_path: Path) -> None:
    """When cwd is passed explicitly, it is used over Path.cwd()."""
    cwd = tmp_path / "explicit"
    cwd.mkdir()
    from better_memory.config import project_name
    assert project_name(cwd) == "explicit"


def test_project_name_override_file_wins(tmp_path: Path) -> None:
    """A .better-memory file in cwd overrides the directory name."""
    cwd = tmp_path / "renamed"
    cwd.mkdir()
    (cwd / ".better-memory").write_text("canonical-name\n", encoding="utf-8")
    from better_memory.config import project_name
    assert project_name(cwd) == "canonical-name"


def test_project_name_empty_override_falls_back(tmp_path: Path) -> None:
    """An empty override file falls back to cwd.name."""
    cwd = tmp_path / "leaf"
    cwd.mkdir()
    (cwd / ".better-memory").write_text("", encoding="utf-8")
    from better_memory.config import project_name
    assert project_name(cwd) == "leaf"


def test_project_name_whitespace_only_override_falls_back(tmp_path: Path) -> None:
    """A whitespace-only override file falls back to cwd.name."""
    cwd = tmp_path / "leaf"
    cwd.mkdir()
    (cwd / ".better-memory").write_text("   \n  \n", encoding="utf-8")
    from better_memory.config import project_name
    assert project_name(cwd) == "leaf"


def test_project_name_multi_line_override_takes_first_non_empty(
    tmp_path: Path,
) -> None:
    """A multi-line override takes the first non-empty stripped line."""
    cwd = tmp_path / "leaf"
    cwd.mkdir()
    (cwd / ".better-memory").write_text(
        "  first-line  \nignored-second\n", encoding="utf-8"
    )
    from better_memory.config import project_name
    assert project_name(cwd) == "first-line"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -k project_name -v`
Expected: 6 errors with `ImportError: cannot import name 'project_name' from 'better_memory.config'`.

- [ ] **Step 3: Implement `project_name()` in `better_memory/config.py`**

Insert after the `resolve_home()` function (after line 32):

```python


def project_name(cwd: Path | None = None) -> str:
    """Return the canonical project name for ``cwd`` (defaults to ``Path.cwd()``).

    A ``<cwd>/.better-memory`` file overrides the default ``cwd.name``: the
    first non-empty stripped line is used. Used uniformly by knowledge
    search, observation writes/reads, episode scoping, the UI panel filter,
    and hook payloads — every subsystem that buckets state by project must
    call this helper, never construct the name inline.
    """
    cwd = cwd if cwd is not None else Path.cwd()
    override = cwd / ".better-memory"
    if override.is_file():
        text = override.read_text(encoding="utf-8").strip()
        if text:
            return text.splitlines()[0].strip()
    return cwd.name
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -k project_name -v`
Expected: 6 passed.

- [ ] **Step 5: Run pyright to confirm no new errors**

Run: `uv run pyright better_memory 2>&1 | tail -3`
Expected: `0 errors, 0 warnings, 0 informations`.

- [ ] **Step 6: Commit**

```bash
git add better_memory/config.py tests/test_config.py
git commit -m "$(cat <<'EOF'
feat(config): add project_name() canonical project-name resolver

Single source of truth for project-name resolution. Replaces six ad-hoc
Path.cwd().name call sites in subsequent commits. Honors a <cwd>/.better-memory
file as override (first non-empty stripped line wins; empty/whitespace-only
falls back to cwd.name).

Promotes the override semantics that were previously only honored by
KnowledgeService.project_for, so observations/episodes/UI/hooks now respect
the same override.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Confidence: 96%.** Trivial pure function; tests are exhaustive; mirrors a known-good pattern (`KnowledgeService.project_for`).

---

## Task 4: Migrate `KnowledgeService.project_for` to delegate

**Files:**
- Modify: `better_memory/services/knowledge.py:308-320`

- [ ] **Step 1: Replace the `project_for` body**

Edit `better_memory/services/knowledge.py` lines 308-320 from:

```python
    def project_for(self, cwd: Path) -> str:
        """Return the project name for ``cwd``.

        Defaults to ``cwd.name`` (the leaf directory). Overridden by the first
        non-empty line of ``<cwd>/.better-memory`` when present.
        """
        cwd = Path(cwd)
        override = cwd / ".better-memory"
        if override.is_file():
            text = override.read_text(encoding="utf-8").strip()
            if text:
                return text.splitlines()[0].strip()
        return cwd.name
```

to:

```python
    def project_for(self, cwd: Path) -> str:
        """Return the project name for ``cwd``.

        Thin wrapper around :func:`better_memory.config.project_name`,
        kept as a method to preserve the ``KnowledgeService`` public API.
        """
        return project_name(cwd)
```

- [ ] **Step 2: Add the import**

Find the existing `from better_memory.config import ...` line in `services/knowledge.py` (use Grep) and add `project_name` to the imports. If no such import exists, add a new line near the other `better_memory.*` imports:

```python
from better_memory.config import project_name
```

- [ ] **Step 3: Run the existing project_for tests to confirm public API stays stable**

Run: `uv run pytest tests/services/test_knowledge.py -k project_for -v`
Expected: 2 passed (`test_project_for_defaults_to_cwd_name`, `test_project_for_override_via_dot_better_memory`).

- [ ] **Step 4: Run pyright**

Run: `uv run pyright better_memory 2>&1 | tail -3`
Expected: `0 errors`.

No commit yet — bundled with subsequent migrations into a single "migrate all call sites" commit (see Task 9).

**Confidence: 96%.** Verified that the delegation preserves behavior via existing tests. Wrapper kept to avoid public-API churn in `KnowledgeService`.

---

## Task 5: Migrate UI `_project_name()` call sites

**Files:**
- Modify: `better_memory/ui/app.py:64-66, 209, 229, 295, 313, 499`

- [ ] **Step 1: Add the `project_name` import**

In `better_memory/ui/app.py`, find the existing `better_memory.config` import (or add one if absent — there isn't one currently). Add near the top with the other `better_memory.*` imports:

```python
from better_memory.config import project_name
```

- [ ] **Step 2: Delete the `_project_name()` helper**

Delete lines 64-66 in `better_memory/ui/app.py` (the entire `def _project_name() -> str: ... return Path.cwd().name` block, plus the blank line above and below as needed to avoid a double blank line).

- [ ] **Step 3: Replace all 5 inline call sites**

Use the Edit tool with `replace_all=true` to change every `_project_name()` to `project_name()`:

```python
# Find:
_project_name()
# Replace with:
project_name()
```

This affects lines 209, 229, 295, 313, and 499 (5 call sites — the def at line 64 was already deleted in step 2).

- [ ] **Step 4: Run UI tests to confirm no regression**

Run: `uv run pytest tests/ui/ -k "not browser" -q`
Expected: existing UI test count passes.

- [ ] **Step 5: Run pyright**

Run: `uv run pyright better_memory 2>&1 | tail -3`
Expected: `0 errors`.

No commit yet — bundled.

**Confidence: 95%.** Mechanical replacement of a 1-line wrapper with the canonical helper. UI tests validate no behavior change.

---

## Task 6: Migrate MCP `Path.cwd().name` call sites

**Files:**
- Modify: `better_memory/mcp/server.py:501, 513, 564`

- [ ] **Step 1: Add the `project_name` import**

In `better_memory/mcp/server.py`, find the existing `from better_memory.config import` line (or add one if absent). Add `project_name` to it. There IS likely already a config import — verify via:

Run: `grep "from better_memory.config" better_memory/mcp/server.py`

If the import exists, append `project_name` to the imported names. If not, add a new line near the other `better_memory.*` imports.

- [ ] **Step 2: Replace `Path.cwd().name` at lines 501, 513**

Edit `better_memory/mcp/server.py` lines 501 and 513 from:

```python
            project = args.get("project") or Path.cwd().name
```

to:

```python
            project = args.get("project") or project_name()
```

(Two identical lines — use Edit with appropriate context to disambiguate, or do them sequentially.)

- [ ] **Step 3: Replace `Path.cwd().name` at line 564**

Edit line 564 from:

```python
            project = Path.cwd().name
```

to:

```python
            project = project_name()
```

- [ ] **Step 4: Confirm no other `Path.cwd().name` remains in this file**

Run: `grep -n "Path.cwd().name" better_memory/mcp/server.py`
Expected: no matches.

- [ ] **Step 5: Run MCP tests**

Run: `uv run pytest tests/mcp/ -m "not integration" -q`
Expected: existing MCP test count passes.

- [ ] **Step 6: Run pyright**

Run: `uv run pyright better_memory 2>&1 | tail -3`
Expected: `0 errors`.

No commit yet — bundled.

**Confidence: 95%.** Three identical replacements. Test suite validates behavior parity.

---

## Task 7: Migrate observation service default `project_resolver`

**Files:**
- Modify: `better_memory/services/observation.py:130-132`

- [ ] **Step 1: Add the `project_name` import**

In `better_memory/services/observation.py`, find or add the import. Append `project_name` to the existing `from better_memory.config import` if present, otherwise add a new line.

- [ ] **Step 2: Replace the lambda with the function reference**

Edit `better_memory/services/observation.py` lines 130-132 from:

```python
        self._project_resolver: Callable[[], str] = (
            project_resolver if project_resolver is not None else (lambda: Path.cwd().name)
        )
```

to:

```python
        self._project_resolver: Callable[[], str] = (
            project_resolver if project_resolver is not None else project_name
        )
```

(Drop the lambda entirely. `project_name` matches the `Callable[[], str]` signature because its single argument has a default.)

- [ ] **Step 3: Run observation tests**

Run: `uv run pytest tests/services/test_observation.py -q`
Expected: existing test count passes.

- [ ] **Step 4: Run pyright**

Run: `uv run pyright better_memory 2>&1 | tail -3`
Expected: `0 errors`. (Verifies that `project_name` matches the `Callable[[], str]` annotation despite the optional `cwd` parameter.)

No commit yet — bundled.

**Confidence: 92%.** The function-reference vs. lambda swap depends on `Callable[[], str]` being satisfied by a function whose single parameter has a default. Pyright is reliable on this; if it complains, fall back to keeping a lambda: `(lambda: project_name())`. The dependency-injection contract is unchanged either way.

---

## Task 8: Migrate hook scripts

**Files:**
- Modify: `better_memory/hooks/session_start.py:58, 105`
- Modify: `better_memory/hooks/post_commit.py:157`

- [ ] **Step 1: `session_start.py` — add import**

In `better_memory/hooks/session_start.py`, add near the top with the other `better_memory.*` imports:

```python
from better_memory.config import project_name
```

- [ ] **Step 2: `session_start.py` — replace line 58**

Edit `better_memory/hooks/session_start.py` line 58 from:

```python
        "project": Path(cwd).name,
```

to:

```python
        "project": project_name(Path(cwd)),
```

- [ ] **Step 3: `session_start.py` — replace line 105**

Edit line 105 from:

```python
            data["project"] = Path(str(data["cwd"])).name
```

to:

```python
            data["project"] = project_name(Path(str(data["cwd"])))
```

- [ ] **Step 4: `post_commit.py` — add import**

In `better_memory/hooks/post_commit.py`, add the import near other `better_memory.*` imports:

```python
from better_memory.config import project_name
```

- [ ] **Step 5: `post_commit.py` — replace line 157**

Edit `better_memory/hooks/post_commit.py` line 157 from:

```python
            "project": Path(cwd).name,
```

to:

```python
            "project": project_name(Path(cwd)),
```

- [ ] **Step 6: Run hook tests**

Run: `uv run pytest tests/hooks/ -q`
Expected: existing hook test count passes.

- [ ] **Step 7: Run pyright**

Run: `uv run pyright better_memory 2>&1 | tail -3`
Expected: `0 errors`.

No commit yet — bundled.

**Confidence: 94%.** Three mechanical replacements. Hooks pass cwd from the payload; passing it through `project_name(Path(cwd))` preserves that behavior and adds override-file honoring as a free side-effect.

---

## Task 9: Commit all call-site migrations

**Files:** all of Tasks 4-8 (services/knowledge.py, services/observation.py, ui/app.py, mcp/server.py, hooks/session_start.py, hooks/post_commit.py)

- [ ] **Step 1: Run the full test suite as a final sanity check**

Run: `uv run pytest -m "not integration" -q`
Expected: full pass count matches Task 1 baseline.

- [ ] **Step 2: Run pyright on production code**

Run: `uv run pyright better_memory 2>&1 | tail -3`
Expected: `0 errors`.

- [ ] **Step 3: Confirm no `Path.cwd().name` remains in production code (other than inside `config.py:project_name()` itself)**

Run: `grep -rn "Path.cwd().name" better_memory/`
Expected: 1 match — in `better_memory/config.py` inside the `project_name()` body. No matches in `ui/`, `mcp/`, `services/`, or `hooks/`.

- [ ] **Step 4: Commit**

```bash
git add better_memory/config.py \
        better_memory/services/knowledge.py \
        better_memory/services/observation.py \
        better_memory/ui/app.py \
        better_memory/mcp/server.py \
        better_memory/hooks/session_start.py \
        better_memory/hooks/post_commit.py
git commit -m "$(cat <<'EOF'
refactor: route all project-name resolution through config.project_name()

Promotes the .better-memory override semantics (previously only honored
by KnowledgeService.project_for) into a single canonical helper used by
every subsystem. Six call sites migrated:

  * ui/app.py: deleted _project_name() wrapper, inlined project_name()
    at 5 sites.
  * mcp/server.py: 3 sites (memory.retrieve, memory.retrieve_observations,
    memory.start_episode).
  * services/observation.py: default project_resolver is now project_name
    function reference (drops the lambda).
  * services/knowledge.py: project_for() delegates to project_name() (kept
    as a thin method wrapper to preserve KnowledgeService public API).
  * hooks/session_start.py: 2 sites (synthesise + supplied-cwd path).
  * hooks/post_commit.py: 1 site.

Behavior change for users with a <cwd>/.better-memory file: observations,
episodes, UI panel filter, and hooks now respect the override (previously
only knowledge search did). Override remains undocumented before this PR
so the practical user impact is zero (R2 in spec).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Confidence: 93%.** Composite of Tasks 4-8. The bundling is for git history clarity (one logical change = one commit), not for review batching — each task above ran its own targeted test suite.

---

## Task 10: Test cleanup — `tests/mcp/test_server_integration.py` (27 errors)

**Files:**
- Modify: `tests/mcp/test_server_integration.py`

This is the highest-risk task in the plan. It involves splitting the `_single_json` helper and updating all 8 call sites. All 27 errors in this file collapse to two helper changes plus mechanical updates.

- [ ] **Step 1: Read the existing helper**

Read `tests/mcp/test_server_integration.py` lines 265-281 to confirm the current `_single_json` shape.

- [ ] **Step 2: Replace `_single_json` with three helpers**

Edit lines 265-281, replacing the existing `_single_json` definition with:

```python
def _parse_single_json(result: object) -> object:
    """Extract the single TextContent and parse its JSON payload.

    The MCP SDK returns a ``CallToolResult`` with ``.content`` as a list of
    content blocks. Our tools always emit exactly one ``TextContent`` whose
    ``text`` is JSON-encoded. When ``isError`` is set the SDK surfaces the
    error message as plain text — we raise instead of trying to parse.
    """
    content = result.content  # type: ignore[attr-defined]
    is_error = getattr(result, "isError", False)
    assert len(content) == 1, f"expected one content block, got {len(content)}"
    block = content[0]
    assert getattr(block, "type", None) == "text", f"not a text block: {block!r}"
    if is_error:
        raise AssertionError(f"tool returned error: {block.text}")
    return json.loads(block.text)


def _single_json_dict(result: object) -> dict[str, Any]:
    """Parse the single TextContent payload, asserting it's a JSON object."""
    parsed = _parse_single_json(result)
    assert isinstance(parsed, dict), (
        f"expected JSON object, got {type(parsed).__name__}"
    )
    return parsed


def _single_json_list(result: object) -> list[Any]:
    """Parse the single TextContent payload, asserting it's a JSON array."""
    parsed = _parse_single_json(result)
    assert isinstance(parsed, list), (
        f"expected JSON array, got {type(parsed).__name__}"
    )
    return parsed
```

- [ ] **Step 3: Add the `Any` import if missing**

Run: `grep -n "from typing import" tests/mcp/test_server_integration.py`
If `Any` is not already imported, add it: `from typing import Any` (or extend the existing typing import).

- [ ] **Step 4: Update the 6 dict-shaped call sites**

| Line | Current | New |
|---|---|---|
| 124 | `fail_id = _single_json(fail_resp)["id"]` | `fail_id = _single_json_dict(fail_resp)["id"]` |
| 133 | `success_id = _single_json(success_resp)["id"]` | `success_id = _single_json_dict(success_resp)["id"]` |
| 139 | `payload = _single_json(retrieve_resp)` | `payload = _single_json_dict(retrieve_resp)` |
| 172 | `obs_id = _single_json(observe_resp)["id"]` | `obs_id = _single_json_dict(observe_resp)["id"]` |
| 178 | `assert _single_json(record_resp) == {"ok": True}` | `assert _single_json_dict(record_resp) == {"ok": True}` |
| 217 | `payload = _single_json(resp)` | `payload = _single_json_dict(resp)` |

- [ ] **Step 5: Update the 2 list-shaped call sites + drop now-redundant `isinstance` asserts**

Lines 193-194 currently:

```python
            docs = _single_json(list_resp)
            assert isinstance(docs, list)
```

Change to:

```python
            docs = _single_json_list(list_resp)
```

(The `isinstance` check moves into the helper.)

Lines 201-202 currently:

```python
            hits = _single_json(search_resp)
            assert isinstance(hits, list)
```

Change to:

```python
            hits = _single_json_list(search_resp)
```

- [ ] **Step 6: Confirm no `_single_json(` (the old name) remains**

Run: `grep -n "_single_json(" tests/mcp/test_server_integration.py | grep -v "^\(265\|283\|292\):"`

(The `grep -v` excludes the helper definitions themselves.)

Expected: no matches (all call sites now use `_single_json_dict` or `_single_json_list`).

If line numbers in the exclude differ slightly after edits, just verify visually that only definitions remain.

- [ ] **Step 7: Run the test file to confirm no behavior change**

This is an integration test marker, so run with the marker enabled:

Run: `uv run pytest tests/mcp/test_server_integration.py -m integration -q`

Expected: same pass/skip status as before (likely all tests skip without an Ollama instance — that's fine; what matters is no `ImportError` or `NameError` from the rename).

For a faster smoke test that doesn't require Ollama, also run:

Run: `uv run python -c "import tests.mcp.test_server_integration"`
Expected: no error (validates the module imports cleanly).

- [ ] **Step 8: Run pyright on this file**

Run: `uv run pyright tests/mcp/test_server_integration.py 2>&1 | tail -3`
Expected: `0 errors, 0 warnings, 0 informations`.

If errors remain, they should be ones not anticipated in the spec — surface them rather than silencing.

- [ ] **Step 9: Commit**

```bash
git add tests/mcp/test_server_integration.py
git commit -m "$(cat <<'EOF'
test(mcp): split _single_json into _single_json_dict / _single_json_list

Pyright in standard mode rejected the dict | list return type because
list.__getitem__ rejects str keys (27 errors in this file). Splitting
the helper into typed dict/list variants gives pyright a definite type
at every call site AND moves the isinstance() check into the helper
itself, so the assertion runs at the API boundary instead of being
duplicated at every list-shaped call site.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Confidence: 85%.** This is the lowest-confidence task in the plan. Risk drivers:

1. **Long-tail risk per R1 (accepted):** an unforeseen call site or imports/scope issue surfaces during the rename. **Surfacing strategy:** if `pyright` shows a residual error after step 8, document the specific error and surface it before continuing — don't silently `# type: ignore`.
2. **Helper rename touching many lines:** 8 call sites × ~1 line each = 8 line changes; cumulative risk is real. **Mitigation:** step 7's `import` smoke-test is the cheapest way to catch a typo or missing import without requiring Ollama for the integration suite.
3. **`Any` import** may need an additional `# noqa` if ruff flags `from typing import Any` as unused (it won't be — it's used in the helper signatures — but worth knowing).

If at step 8 pyright still reports errors in this file: stop and consult — do not push forward to subsequent tasks until this file is clean.

---

## Task 11: Test cleanup — `tests/ui/test_queries_episodes.py` (10 errors)

**Files:**
- Modify: `tests/ui/test_queries_episodes.py:189, 218, 252, 267, 294`

All 10 errors are the same pattern: `result.attribute` access where `result` is `Optional[X]`. Fix is `assert result is not None` immediately before the first access in each test.

- [ ] **Step 1: Identify the 5 access sites needing assertions**

Read `tests/ui/test_queries_episodes.py` around lines 189, 218, 252, 267, 294 (each has 1-4 errors clustered).

- [ ] **Step 2: Add `assert detail is not None` before line 189**

Edit `tests/ui/test_queries_episodes.py` around line 188-189 from:

```python
        detail = episode_detail(conn, episode_id=ep_id)
        assert [o.id for o in detail.observations] == ["obs-new", "obs-old"]
```

to:

```python
        detail = episode_detail(conn, episode_id=ep_id)
        assert detail is not None, "episode_detail should return a result for the seeded episode"
        assert [o.id for o in detail.observations] == ["obs-new", "obs-old"]
```

- [ ] **Step 3: Apply the same pattern at lines 218, 252, 267, 294**

For each of these line clusters, find the `<varname> = <function_call>(...)` line that produces the Optional value, and insert `assert <varname> is not None, "<context message>"` immediately after it. The context message names the function and the test setup (e.g., `"episode_detail should return for ep_id seeded above"`, `"reflection_detail should return for the freshly-inserted reflection"`).

Use the pyright error output from Task 1 step 4 to guide which `varname` to assert per line:
- Lines 189-192: `detail` (4 errors collapse to 1 assert)
- Lines 218-219: `detail` (2 errors collapse to 1 assert)
- Line 252: `detail`
- Line 267: `detail`
- Lines 294-295: `detail`

So 5 assertions added in total.

- [ ] **Step 4: Run the test file**

Run: `uv run pytest tests/ui/test_queries_episodes.py -q`
Expected: same pass count as before (the new assertions should never trip — they document existing test invariants).

- [ ] **Step 5: Run pyright on the file**

Run: `uv run pyright tests/ui/test_queries_episodes.py 2>&1 | tail -3`
Expected: `0 errors`.

No commit yet — bundled with Task 12.

**Confidence: 95%.** Each assert is a documented invariant. If any assert trips at step 4, that's a real bug we surfaced — pause and investigate before continuing.

---

## Task 12: Test cleanup — `tests/ui/test_queries_reflections.py` (4 errors)

**Files:**
- Modify: `tests/ui/test_queries_reflections.py:195, 235, 236, 270`

Same pattern as Task 11. 4 errors collapse to 3 assertions (lines 235 and 236 share one).

- [ ] **Step 1: Read the relevant lines**

Read `tests/ui/test_queries_reflections.py` around lines 195, 235-236, 270.

- [ ] **Step 2: Add `assert <varname> is not None, "<context>"` at each cluster**

Insertion points (use pyright error output to confirm the variable being accessed):

- Before line 195 (`reflection.X` access)
- Before line 235 (`detail.sources` access — covers both 235 and 236)
- Before line 270 (`detail.sources` access)

- [ ] **Step 3: Run the test file**

Run: `uv run pytest tests/ui/test_queries_reflections.py -q`
Expected: same pass count.

- [ ] **Step 4: Run pyright on the file**

Run: `uv run pyright tests/ui/test_queries_reflections.py 2>&1 | tail -3`
Expected: `0 errors`.

- [ ] **Step 5: Commit Tasks 11 + 12 together**

```bash
git add tests/ui/test_queries_episodes.py tests/ui/test_queries_reflections.py
git commit -m "$(cat <<'EOF'
test(ui): assert non-None before optional-attr access in queries tests

Pyright in standard mode flagged 14 reportOptionalMemberAccess errors
where tests accessed .observations / .reflections / .sources /
.reflection on the Optional return of episode_detail / reflection_detail
without narrowing first. Each assertion documents an existing test
invariant — if a query ever does return None for the seeded data, the
test now fails with a meaningful message instead of an opaque
AttributeError.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Confidence: 96%.** Mechanical pattern; assertions document existing invariants; failures would be real bugs (good).

---

## Task 13: Test cleanup — `tests/mcp/test_start_ui_tool.py` (2 errors)

**Files:**
- Modify: `tests/mcp/test_start_ui_tool.py:39-40`

`tool.description` is `str | None`. Lines 39-40 call `.lower()` and use `not in` against it. Fix: assert non-None.

- [ ] **Step 1: Edit lines 39-40**

Edit `tests/mcp/test_start_ui_tool.py` lines 36-40 from:

```python
        tool = next(
            t for t in _tool_definitions() if t.name == "memory.start_ui"
        )
        assert "stub" not in tool.description.lower()
        assert "Plan 2" not in tool.description
```

to:

```python
        tool = next(
            t for t in _tool_definitions() if t.name == "memory.start_ui"
        )
        assert tool.description is not None, "memory.start_ui must have a description"
        assert "stub" not in tool.description.lower()
        assert "Plan 2" not in tool.description
```

- [ ] **Step 2: Run the test file**

Run: `uv run pytest tests/mcp/test_start_ui_tool.py -q`
Expected: same pass count.

- [ ] **Step 3: Run pyright on the file**

Run: `uv run pyright tests/mcp/test_start_ui_tool.py 2>&1 | tail -3`
Expected: `0 errors`.

No commit yet — bundled with Task 14.

**Confidence: 98%.** Single insertion of one assert.

---

## Task 14: Test cleanup — tighten `cleanup` return type to `Coroutine`

**Files:**
- Modify: `better_memory/mcp/server.py:38, 409`

The error is at `tests/mcp/test_episode_tools.py:227` (`asyncio.run(cleanup())`), but the right fix is in production code — tighten the return type of `cleanup` from `Awaitable[None]` to `Coroutine[Any, Any, None]` since `async def` functions always return Coroutines.

- [ ] **Step 1: Add `Coroutine` to the `collections.abc` import**

Edit `better_memory/mcp/server.py` line 38 from:

```python
from collections.abc import Awaitable, Callable
```

to:

```python
from collections.abc import Awaitable, Callable, Coroutine
```

- [ ] **Step 2: Tighten the `create_server` return type**

Edit line 409 from:

```python
def create_server() -> tuple[Server, Callable[[], Awaitable[None]]]:
```

to:

```python
def create_server() -> tuple[Server, Callable[[], Coroutine[Any, Any, None]]]:
```

- [ ] **Step 3: Confirm `Awaitable` is still used elsewhere; remove from import if not**

Run: `grep -n "Awaitable" better_memory/mcp/server.py`

If `Awaitable` is only used at the line you just edited (now gone), remove it from the `collections.abc` import to keep ruff happy:

```python
from collections.abc import Callable, Coroutine
```

- [ ] **Step 4: Run pyright on production code + the affected test**

Run: `uv run pyright better_memory tests/mcp/test_episode_tools.py 2>&1 | tail -3`
Expected: `0 errors`.

- [ ] **Step 5: Run the affected tests**

Run: `uv run pytest tests/mcp/test_episode_tools.py -q`
Expected: same pass count as Task 1 baseline.

Run: `uv run pytest tests/mcp/ -m "not integration" -q`
Expected: full MCP test count passes.

- [ ] **Step 6: Commit Tasks 13 + 14 together**

```bash
git add better_memory/mcp/server.py tests/mcp/test_start_ui_tool.py
git commit -m "$(cat <<'EOF'
fix(mcp): tighten types — cleanup returns Coroutine, description guarded

Two unrelated pyright fixes bundled because they both touch the MCP
test surface:

  * server.py: create_server's cleanup callable was typed as returning
    Awaitable[None], but async def always returns Coroutine. asyncio.run
    requires Coroutine. Tightening the source signature fixes the
    test_episode_tools.py:227 error without changing runtime behavior.
  * test_start_ui_tool.py: Tool.description is Optional[str]; assert
    not-None before calling .lower() / using `not in`.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Confidence: 95%.** Source type tightening is sound (`async def` returns `Coroutine` at runtime always); single assertion add is trivial.

---

## Task 15: Add `typecheck.yml` CI workflow

**Files:**
- Create: `.github/workflows/typecheck.yml`

This task lands AFTER all 44 errors are fixed, so the workflow goes green on first push.

- [ ] **Step 1: Run pyright on full surface to confirm 0 errors before adding CI**

Run: `uv run pyright 2>&1 | tail -3`
Expected: `0 errors, 0 warnings, 0 informations`. (No `better_memory tests` args needed — `[tool.pyright].include` covers both.)

If errors remain, STOP. Address them before adding the workflow.

- [ ] **Step 2: Create `.github/workflows/typecheck.yml`**

```yaml
name: Type checking

on:
  pull_request:
    paths:
      - "better_memory/**"
      - "tests/**"
      - "pyproject.toml"
      - "uv.lock"
      - ".github/workflows/typecheck.yml"
  push:
    branches: [main]
    paths:
      - "better_memory/**"
      - "tests/**"
      - "pyproject.toml"
      - "uv.lock"
      - ".github/workflows/typecheck.yml"

jobs:
  typecheck:
    name: Pyright
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v4
        with:
          python-version: "3.12"
          enable-cache: true

      - name: Install project
        run: uv sync --dev

      - name: Run pyright
        run: uv run pyright
```

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/typecheck.yml
git commit -m "$(cat <<'EOF'
ci(typecheck): add Pyright workflow on PR + push to main

Standard mode against better_memory/ and tests/ (configured in
pyproject.toml). Mirrors ui-tests.yml structure: setup-uv with cache,
uv sync --dev, single uv run pyright step.

Triggers on changes to source, tests, dependency manifest, or the
workflow file itself — same path filter as the UI workflow.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Confidence: 95%.** Step 1 gate ensures the workflow lands green. Workflow is a near-clone of `ui-tests.yml`.

---

## Task 16: README documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Find the `## Configuration` section**

Run: `grep -n "^## Configuration" README.md`

- [ ] **Step 2: Append a new subsection at the end of `## Configuration` (just before the next `## ...` heading)**

Insert:

```markdown

### Project-name override

Memory is bucketed by project name, derived from the cwd's leaf
directory name (`Path.cwd().name`). For situations where the leaf name
isn't right — multiple worktrees of the same logical project, or a
deeply-nested cwd — drop a `.better-memory` file at the project root
with a single line containing the desired project name:

```bash
echo "my-project" > .better-memory
```

This applies uniformly to knowledge search, observation writes/reads,
episode scoping, and the UI panel filter.

Note: this is a *file* in your repo root, not the data root directory
`~/.better-memory/` (set by `BETTER_MEMORY_HOME`). Different things
despite the shared name.
```

- [ ] **Step 3: Verify markdown renders cleanly**

Run: `head -60 README.md` (or open in preview) — confirm the new section is positioned under `## Configuration` and before the next top-level heading.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs(readme): document the .better-memory project-name override file

Now that the override is honored uniformly across all subsystems
(previously knowledge-only), document it. Includes the disambiguation
between the file (.better-memory in repo root) and the data directory
(~/.better-memory/, set by BETTER_MEMORY_HOME).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Confidence: 99%.** Pure documentation.

---

## Task 17: Commit the plan; record memory observation

**Files:**
- Already added: `docs/superpowers/specs/2026-05-03-type-identity-infra-design.md` (committed earlier as `09d1448`)
- Already created: `docs/superpowers/plans/2026-05-03-type-identity-infra.md` (this file)

- [ ] **Step 1: Stage and commit the plan**

```bash
git add docs/superpowers/plans/2026-05-03-type-identity-infra.md
git commit -m "$(cat <<'EOF'
docs(superpowers): phase D type & identity infra implementation plan

17 tasks producing 7 commits:
  1. Pyright dev dep + config block
  2. project_name() canonical helper in config.py (TDD)
  3. Migrate 6 call sites to project_name()
  4. Test cleanup: tests/mcp/test_server_integration.py (split helper)
  5. Test cleanup: tests/ui/test_queries_*.py (None-narrowing)
  6. Test cleanup: tests/mcp tests (Coroutine return type + Optional guard)
  7. typecheck.yml CI workflow + README + this plan

Plan-wide confidence ~92%. Lowest task is #10 at 85% (helper split in
test_server_integration.py — long-tail risk accepted per R1).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 2: Record a memory observation about the migration**

Use `mcp__better-memory__memory_observe` with:

```
content: "Phase D: project-name resolution unified via better_memory/config.py:project_name(cwd: Path | None = None). Replaces 6 ad-hoc Path.cwd().name call sites across ui/app.py, mcp/server.py (3 sites), services/observation.py, hooks/session_start.py (2 sites), hooks/post_commit.py. KnowledgeService.project_for() now delegates to project_name() (kept as thin wrapper for public API stability). The .better-memory override file is now honored uniformly across all subsystems (was knowledge-only). Pyright in standard mode added to CI via .github/workflows/typecheck.yml; pinned to project venv via [tool.pyright] venvPath/venv config."
component: "config"
theme: "architecture"
outcome: "success"
```

**Confidence: 99%.** Repository hygiene step.

---

## Task 18: Final verification

**Files:** none (read-only)

- [ ] **Step 1: Confirm pyright is fully clean**

Run: `uv run pyright 2>&1 | tail -3`
Expected: `0 errors, 0 warnings, 0 informations`.

- [ ] **Step 2: Confirm full test suite still passes**

Run: `uv run pytest -m "not integration" -q`
Expected: same pass count as Task 1 baseline.

- [ ] **Step 3: Confirm ruff is still clean**

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 4: Confirm git log is sensible**

Run: `git log main..HEAD --oneline`
Expected: 7 commits, one per logical change above.

- [ ] **Step 5: Confirm no uncommitted changes**

Run: `git status --short`
Expected: empty output (or only `.claude/`).

**Confidence: 99%.** Verification steps; no implementation risk.

---

## Task 19: Push branch + open PR (USER GATED)

**Files:** none

- [ ] **Step 1: Pause for user approval**

Per project process discipline, this step is user-gated. Confirm with the user before pushing.

- [ ] **Step 2: Push branch**

```bash
git push -u origin phase-d
```

- [ ] **Step 3: Open the PR**

```bash
gh pr create --title "Phase D: type & identity infra (Pyright in CI + project_name unification)" --body "$(cat <<'EOF'
## Summary

Bundles tech-debt audit items #8 (Pyright in CI) and #12 (`_project_name()` divergence) into one PR.

- **Pyright in CI** — standard mode, runs on PR and push to main against `better_memory/` and `tests/`. Production code was already clean (0 errors); 44 test errors fixed in 4 cleanup commits.
- **Project-name resolution** — promoted `KnowledgeService.project_for()`'s `.better-memory` override semantics into a canonical `project_name(cwd: Path | None = None)` in `config.py`. Six previously-divergent call sites (UI, MCP server, observation service default, two hooks) now route through it. The `.better-memory` override file is now honored uniformly across all subsystems and documented in README.

Spec: `docs/superpowers/specs/2026-05-03-type-identity-infra-design.md`
Plan: `docs/superpowers/plans/2026-05-03-type-identity-infra.md`

## Test plan

- [x] `uv run pytest -m "not integration" -q` — full suite passes
- [x] `uv run pyright` — 0 errors
- [x] `uv run ruff check .` — clean
- [ ] CI: `Pyright` workflow goes green on first run (added in this PR)
- [ ] CI: `UI test suite` workflow stays green

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Auto-babysit the PR**

Per memory `feedback_auto_babysit_pr.md`, after `gh pr create` schedule a 60s-cadence cron that polls Bugbot and merges when clean. The babysit cron prompt template lives in that memory.

**Confidence: 99%.** Push + PR creation; well-rehearsed workflow.

---

## Summary

| Task | Description | Confidence |
|---|---|---|
| 1 | Pre-flight + branch verification | 99% |
| 2 | Add pyright dev dep + `[tool.pyright]` block + commit | 95% |
| 3 | `project_name()` canonical helper in config.py + commit | 96% |
| 4 | Migrate `KnowledgeService.project_for` to delegate | 96% |
| 5 | Migrate UI `_project_name()` call sites | 95% |
| 6 | Migrate MCP `Path.cwd().name` call sites | 95% |
| 7 | Migrate observation service default `project_resolver` | 92% |
| 8 | Migrate hook scripts | 94% |
| 9 | Commit all call-site migrations | 93% |
| 10 | Test cleanup — `test_server_integration.py` (split helper) | **85%** |
| 11 | Test cleanup — `test_queries_episodes.py` (None-narrowing) | 95% |
| 12 | Test cleanup — `test_queries_reflections.py` (None-narrowing) | 96% |
| 13 | Test cleanup — `test_start_ui_tool.py` (None guard) | 98% |
| 14 | Test cleanup — tighten `cleanup` return type | 95% |
| 15 | Add `typecheck.yml` CI workflow | 95% |
| 16 | README documentation | 99% |
| 17 | Commit plan + record memory observation | 99% |
| 18 | Final verification | 99% |
| 19 | Push + open PR (USER GATED) | 99% |
| **Plan-wide** | | **~92%** |

The single sub-90% task is **Task 10** (helper split in `test_server_integration.py`). Per R1 in the spec, the long-tail balloon risk is accepted with no preemptive safety valve — if pyright reports a residual error after the migration, surface it before continuing rather than silently `# type: ignore`.
