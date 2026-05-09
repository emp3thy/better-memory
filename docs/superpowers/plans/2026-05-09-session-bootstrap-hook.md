# Session Bootstrap Hook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two-hook SessionStart pipeline (`session_start.py` + `session_retrieve.py`) with a single user-level hook backed by a `SessionBootstrapService` that opens or reuses an episode, retrieves all project + general semantic memories and reflections, and injects them as `additionalContext`.

**Architecture:** Thin hook shim → in-process Python call → `SessionBootstrapService` (source detection, idempotent episode lifecycle, retrieval, markdown rendering). The same service is exposed as MCP tool `memory.session_bootstrap` for manual invocation. Episode scope = git-repo name (via `git rev-parse --git-common-dir`) when in a git tree; literal `"general"` otherwise.

**Tech Stack:** Python 3.12+, sqlite3, MCP Python SDK, pytest, subprocess for git resolution.

**Reference spec:** `docs/superpowers/specs/2026-05-09-session-bootstrap-hook-design.md`

**File Structure:**

| File | Action | Responsibility |
|---|---|---|
| `better_memory/config.py` | Modify | `project_name(cwd)` becomes git-aware via `--git-common-dir` |
| `better_memory/services/reflection.py` | Modify | `retrieve_reflections` accepts `limit_per_bucket=None` for unlimited |
| `better_memory/services/session_bootstrap.py` | Create | `SessionBootstrapService.bootstrap()` |
| `better_memory/services/spool.py` | Modify | Remove `_maybe_open_episode_for_session_start` + branch |
| `better_memory/mcp/server.py` | Modify | Register `memory.session_bootstrap` tool |
| `better_memory/hooks/session_bootstrap.py` | Create | Thin shim hook |
| `better_memory/hooks/session_start.py` | Delete | Obsolete |
| `better_memory/hooks/session_retrieve.py` | Delete | Obsolete |
| `better_memory/cli/install_hooks.py` | Modify | Single SessionStart entry + `_LEGACY_HOOK_MODULES` |
| `tests/test_config.py` | Modify | Add git-aware project_name tests |
| `tests/services/test_reflection.py` | Modify | Add unlimited-bucket test |
| `tests/services/test_session_bootstrap.py` | Create | Service unit tests |
| `tests/hooks/test_session_bootstrap.py` | Create | Hook subprocess tests |
| `tests/services/test_spool.py` | Modify | Remove session_start branch tests |
| `tests/cli/test_install_hooks.py` | Modify | Add legacy-cleanup test |
| `tests/mcp/test_session_bootstrap_tool.py` | Create | MCP tool integration test |
| `tests/hooks/test_session_start.py` | Delete | Obsolete |
| `tests/hooks/test_session_retrieve.py` | Delete | Obsolete |
| `tests/hooks/test_hooks_and_spool_integration.py` | Modify | Drop session_start scenarios if any |

---

## Task 1: Make `project_name(cwd)` git-aware

**Files:**
- Modify: `better_memory/config.py:34-49`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing test for git-repo resolution at root**

Add to `tests/test_config.py`:

```python
import subprocess
from pathlib import Path

import pytest

from better_memory.config import project_name


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=str(path), check=True)


def test_project_name_in_git_repo_root_returns_repo_dir_name(tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git_init(repo)

    assert project_name(repo) == "myrepo"
```

- [ ] **Step 2: Run test, confirm it fails**

```bash
pytest tests/test_config.py::test_project_name_in_git_repo_root_returns_repo_dir_name -v
```

Expected: FAIL — current `project_name` returns `cwd.name`, which would coincidentally pass for the repo-root case. Strengthen by adding a subdirectory case before running:

```python
def test_project_name_in_subdirectory_walks_to_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git_init(repo)
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)

    assert project_name(sub) == "myrepo"
```

Run: `pytest tests/test_config.py::test_project_name_in_subdirectory_walks_to_repo_root -v`
Expected: FAIL with `AssertionError: assert 'deep' == 'myrepo'`

- [ ] **Step 3: Update `project_name` to use `git rev-parse --git-common-dir`**

Replace `better_memory/config.py:34-49`:

```python
import subprocess


def project_name(cwd: Path | None = None) -> str:
    """Return the canonical project name for ``cwd`` (defaults to ``Path.cwd()``).

    Resolution order:
    1. ``<cwd>/.better-memory`` override file: first non-empty stripped line.
    2. ``git rev-parse --git-common-dir`` (handles worktrees: returns the main
       repo's .git directory). Project name = parent dir's ``.name``.
    3. ``"general"`` if no git tree is found or git is unavailable.

    Used uniformly by knowledge search, observation writes/reads, episode
    scoping, the UI panel filter, and hook payloads — every subsystem that
    buckets state by project must call this helper, never construct the
    name inline.
    """
    cwd = cwd if cwd is not None else Path.cwd()

    override = cwd / ".better-memory"
    if override.is_file():
        text = override.read_text(encoding="utf-8").strip()
        if text:
            return text.splitlines()[0].strip()

    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return "general"

    if result.returncode != 0:
        return "general"

    common_dir_str = result.stdout.strip()
    if not common_dir_str:
        return "general"

    common_dir = Path(common_dir_str)
    if not common_dir.is_absolute():
        common_dir = (cwd / common_dir).resolve()

    repo_root = common_dir.parent
    if repo_root.name:
        return repo_root.name
    return "general"
```

- [ ] **Step 4: Run failing tests, confirm they pass**

```bash
pytest tests/test_config.py -v
```

Expected: both new tests PASS, plus any pre-existing `project_name` tests still pass (override file behavior preserved).

- [ ] **Step 5: Add remaining cases**

```python
def test_project_name_outside_git_returns_general(tmp_path: Path) -> None:
    nongit = tmp_path / "loose"
    nongit.mkdir()

    assert project_name(nongit) == "general"


def test_project_name_in_worktree_returns_main_repo_name(tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git_init(repo)
    # Need at least one commit for `git worktree add` to succeed.
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "init", "--quiet"],
        cwd=str(repo), check=True,
    )
    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", str(worktree), "-b", "feat", "--quiet"],
        cwd=str(repo), check=True,
    )

    assert project_name(worktree) == "myrepo"


def test_project_name_override_file_beats_git(tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git_init(repo)
    (repo / ".better-memory").write_text("override-name\n")

    assert project_name(repo) == "override-name"
```

Run: `pytest tests/test_config.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add better_memory/config.py tests/test_config.py
git commit -m "feat(config): make project_name git-aware via --git-common-dir

Resolves project scope to the main repo dir, including correct handling
for worktrees (which share the main repo's name). Falls back to 'general'
when not in a git tree."
```

---

## Task 2: Extend `retrieve_reflections` to accept unlimited cap

**Files:**
- Modify: `better_memory/services/reflection.py:1106-1170`
- Modify: `tests/services/test_reflection.py`

- [ ] **Step 1: Write failing test for unlimited bucket**

Add to `tests/services/test_reflection.py` (or wherever existing reflection retrieval tests live):

```python
def test_retrieve_reflections_unlimited_bucket(tmp_memory_db_with_schema):
    """limit_per_bucket=None returns every matching reflection."""
    conn = tmp_memory_db_with_schema
    project = "myproj"
    # Seed 25 confirmed 'do' reflections (more than default cap of 20).
    for i in range(25):
        _seed_reflection(
            conn,
            title=f"do-{i}",
            project=project,
            polarity="do",
            confidence=0.9,
        )
    svc = ReflectionSynthesisService(conn)

    result = svc.retrieve_reflections(project=project, limit_per_bucket=None)

    assert len(result["do"]) == 25
```

(`_seed_reflection` already exists in `tests/hooks/test_session_retrieve.py`. Move to a shared helper or duplicate — implementer decides.)

- [ ] **Step 2: Run test, confirm it fails**

```bash
pytest tests/services/test_reflection.py::test_retrieve_reflections_unlimited_bucket -v
```

Expected: FAIL with `TypeError: ... type NoneType ... not int` (the current signature types `limit_per_bucket` as `int`).

- [ ] **Step 3: Update `retrieve_reflections` signature and bucket cap logic**

Modify `better_memory/services/reflection.py` lines 1106-1170. Change signature:

```python
def retrieve_reflections(
    self,
    *,
    project: str,
    tech: str | None = None,
    phase: str | None = None,
    polarity: str | None = None,
    limit_per_bucket: int | None = 20,
) -> dict[str, list[dict]]:
    """Return reflections bucketed by polarity, ordered by confidence DESC.

    limit_per_bucket=None disables the cap (returns every matching row per
    bucket). Default 20 preserved for back-compat.
    ...
    """
```

Replace the cap check inside the loop:

```python
buckets: dict[str, list[dict]] = {"do": [], "dont": [], "neutral": []}
for r in rows:
    bucket = buckets[r["polarity"]]
    if limit_per_bucket is not None and len(bucket) >= limit_per_bucket:
        continue
    bucket.append({...})
```

- [ ] **Step 4: Run test, confirm it passes**

```bash
pytest tests/services/test_reflection.py::test_retrieve_reflections_unlimited_bucket -v
```

Expected: PASS.

- [ ] **Step 5: Run full reflection test suite, confirm no regressions**

```bash
pytest tests/services/test_reflection.py -v
```

Expected: all tests PASS, including existing capped-bucket tests (default `limit_per_bucket=20` unchanged).

- [ ] **Step 6: Commit**

```bash
git add better_memory/services/reflection.py tests/services/test_reflection.py
git commit -m "feat(reflection): retrieve_reflections accepts limit_per_bucket=None for unlimited

Required by SessionBootstrapService which needs all reflections injected
on session start (no cap). Default cap of 20 preserved for existing
callers."
```

---

## Task 3: `SessionBootstrapService` — skeleton with source detection + episode lifecycle

**Files:**
- Create: `better_memory/services/session_bootstrap.py`
- Create: `tests/services/test_session_bootstrap.py`

- [ ] **Step 1: Write failing test for "startup" opens new episode**

Create `tests/services/test_session_bootstrap.py`:

```python
"""Unit tests for SessionBootstrapService."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.session_bootstrap import SessionBootstrapService

_MIGRATIONS = Path(__file__).resolve().parents[2] / "better_memory" / "db" / "migrations"


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    apply_migrations(c, migrations_dir=_MIGRATIONS)
    yield c
    c.close()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "demo-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=str(repo), check=True)
    return repo


def test_startup_source_opens_new_episode(conn, git_repo: Path) -> None:
    svc = SessionBootstrapService(conn)

    result = svc.bootstrap(source="startup", session_id="sess-1", cwd=git_repo)

    assert result.project == "demo-repo"
    assert result.source == "startup"
    assert result.episode_action == "opened"
    assert result.episode_id  # non-empty
```

- [ ] **Step 2: Run test, confirm it fails**

```bash
pytest tests/services/test_session_bootstrap.py::test_startup_source_opens_new_episode -v
```

Expected: FAIL with `ImportError` (module doesn't exist yet).

- [ ] **Step 3: Create the service skeleton**

Create `better_memory/services/session_bootstrap.py`:

```python
"""Session bootstrap: source-aware episode lifecycle + memory injection.

Invoked on every Claude Code SessionStart event by the
``better_memory.hooks.session_bootstrap`` hook and via the
``memory.session_bootstrap`` MCP tool. Owns:

- Source coercion (startup / resume / clear / compact; unknowns -> startup).
- Project resolution (delegates to ``better_memory.config.project_name``).
- Idempotent episode lifecycle (open new only on startup with no active
  episode; reuse otherwise).
- Retrieval of project + general semantic memories and reflections.
- Markdown rendering for ``additionalContext`` injection.

Connection ownership: caller owns the sqlite3 connection. The service does
not commit on its own — episode opens commit through ``EpisodeService``'s
existing SAVEPOINT envelope.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from better_memory.config import project_name
from better_memory.services.episode import EpisodeService

_VALID_SOURCES: frozenset[str] = frozenset({"startup", "resume", "clear", "compact"})


@dataclass(frozen=True)
class BootstrapResult:
    additional_context: str
    project: str
    source: str
    episode_id: str
    episode_action: Literal["opened", "reused"]
    semantic_count: int = 0
    reflections_counts: dict[str, int] = field(
        default_factory=lambda: {"do": 0, "dont": 0, "neutral": 0}
    )


class SessionBootstrapService:
    def __init__(self, conn) -> None:
        self._conn = conn
        self._episodes = EpisodeService(conn)

    def bootstrap(
        self,
        *,
        source: str | None,
        session_id: str,
        cwd: Path,
    ) -> BootstrapResult:
        coerced_source = source if source in _VALID_SOURCES else "startup"
        project = project_name(cwd)

        existing = self._episodes.active_episode(session_id)
        if existing is None:
            episode_id = self._episodes.open_background(
                session_id=session_id, project=project,
            )
            action: Literal["opened", "reused"] = "opened"
        else:
            episode_id = existing.id
            action = "reused"

        return BootstrapResult(
            additional_context="",  # filled in Task 5
            project=project,
            source=coerced_source,
            episode_id=episode_id,
            episode_action=action,
        )
```

- [ ] **Step 4: Run test, confirm it passes**

```bash
pytest tests/services/test_session_bootstrap.py::test_startup_source_opens_new_episode -v
```

Expected: PASS.

- [ ] **Step 5: Add idempotency and source-coercion tests**

```python
def test_compact_with_existing_episode_reuses(conn, git_repo: Path) -> None:
    svc = SessionBootstrapService(conn)
    first = svc.bootstrap(source="startup", session_id="sess-2", cwd=git_repo)

    second = svc.bootstrap(source="compact", session_id="sess-2", cwd=git_repo)

    assert second.episode_action == "reused"
    assert second.episode_id == first.episode_id
    assert second.source == "compact"


def test_resume_with_no_existing_episode_opens(conn, git_repo: Path) -> None:
    svc = SessionBootstrapService(conn)

    result = svc.bootstrap(source="resume", session_id="sess-cold", cwd=git_repo)

    assert result.episode_action == "opened"
    assert result.source == "resume"


@pytest.mark.parametrize("bad_source", ["", "unknown", None, "STARTUP"])
def test_bad_source_coerces_to_startup(conn, git_repo: Path, bad_source) -> None:
    svc = SessionBootstrapService(conn)

    result = svc.bootstrap(source=bad_source, session_id="sess-x", cwd=git_repo)

    assert result.source == "startup"


def test_cwd_not_in_git_uses_general_scope(conn, tmp_path: Path) -> None:
    nongit = tmp_path / "loose"
    nongit.mkdir()
    svc = SessionBootstrapService(conn)

    result = svc.bootstrap(source="startup", session_id="sess-g", cwd=nongit)

    assert result.project == "general"
```

Run: `pytest tests/services/test_session_bootstrap.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add better_memory/services/session_bootstrap.py tests/services/test_session_bootstrap.py
git commit -m "feat(services): add SessionBootstrapService skeleton

Source-aware episode lifecycle: 'startup' opens a new background episode,
other sources reuse the existing episode for the session_id when one
exists. Unknown source values coerce to 'startup'. Project resolution
delegates to project_name(cwd) (git-aware as of Task 1).

Retrieval and rendering wired in subsequent tasks."
```

---

## Task 4: `SessionBootstrapService` — semantic + reflection retrieval

**Files:**
- Modify: `better_memory/services/session_bootstrap.py`
- Modify: `tests/services/test_session_bootstrap.py`

- [ ] **Step 1: Write failing test for retrieval counts**

Add a test helper for seeding rows and an assertion:

```python
import json
import uuid
from datetime import UTC, datetime


def _seed_semantic(conn, *, content: str, project: str, scope: str) -> str:
    mid = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO semantic_memories "
        "(id, content, project, scope, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (mid, content, project, scope, now, now),
    )
    conn.commit()
    return mid


def _seed_reflection(conn, *, project: str, polarity: str, scope: str = "project") -> str:
    rid = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO reflections "
        "(id, title, project, tech, phase, polarity, use_cases, hints, "
        " confidence, status, evidence_count, created_at, updated_at, scope) "
        "VALUES (?, 't', ?, NULL, 'implementation', ?, 'uc', ?, 0.9, "
        " 'confirmed', 1, ?, ?, ?)",
        (rid, project, polarity, json.dumps(["h"]), now, now, scope),
    )
    conn.commit()
    return rid


def test_bootstrap_counts_retrieved_rows(conn, git_repo: Path) -> None:
    proj = git_repo.name  # demo-repo
    _seed_semantic(conn, content="proj-a", project=proj, scope="project")
    _seed_semantic(conn, content="gen-a", project="anything", scope="general")
    _seed_reflection(conn, project=proj, polarity="do", scope="project")
    _seed_reflection(conn, project=proj, polarity="dont", scope="project")
    _seed_reflection(conn, project="anything", polarity="do", scope="general")
    svc = SessionBootstrapService(conn)

    result = svc.bootstrap(source="startup", session_id="sess-r", cwd=git_repo)

    assert result.semantic_count == 2  # 1 project + 1 general
    assert result.reflections_counts["do"] == 2     # project + general
    assert result.reflections_counts["dont"] == 1
    assert result.reflections_counts["neutral"] == 0
```

- [ ] **Step 2: Run test, confirm it fails**

```bash
pytest tests/services/test_session_bootstrap.py::test_bootstrap_counts_retrieved_rows -v
```

Expected: FAIL — `semantic_count` is 0 (skeleton doesn't retrieve yet).

- [ ] **Step 3: Wire retrieval into the service**

Modify `better_memory/services/session_bootstrap.py`. Update imports:

```python
from better_memory.services.reflection import ReflectionSynthesisService
from better_memory.services.semantic import SemanticMemoryService
```

Update `bootstrap` method body (after episode resolution, before return):

```python
semantic_svc = SemanticMemoryService(self._conn)
if project == "general":
    semantic = semantic_svc.list_for_project(project=project, scope_filter="general")
else:
    semantic = semantic_svc.list_for_project(project=project, scope_filter=None)

reflection_svc = ReflectionSynthesisService(self._conn)
buckets = reflection_svc.retrieve_reflections(
    project=project, limit_per_bucket=None,
)

semantic_count = len(semantic)
reflections_counts = {
    "do": len(buckets["do"]),
    "dont": len(buckets["dont"]),
    "neutral": len(buckets["neutral"]),
}
```

Pass the new fields into `BootstrapResult`:

```python
return BootstrapResult(
    additional_context="",  # filled in Task 5
    project=project,
    source=coerced_source,
    episode_id=episode_id,
    episode_action=action,
    semantic_count=semantic_count,
    reflections_counts=reflections_counts,
)
```

Stash the retrieved rows on the service for the rendering task (Task 5) — store them as locals first; we'll pass through to render in Task 5. For now, the count fields are sufficient.

- [ ] **Step 4: Run test, confirm it passes**

```bash
pytest tests/services/test_session_bootstrap.py::test_bootstrap_counts_retrieved_rows -v
```

Expected: PASS.

- [ ] **Step 5: Run full service test suite, confirm no regressions**

```bash
pytest tests/services/test_session_bootstrap.py -v
```

Expected: all previous tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add better_memory/services/session_bootstrap.py tests/services/test_session_bootstrap.py
git commit -m "feat(services): SessionBootstrapService retrieves semantic + reflection rows

Calls SemanticMemoryService.list_for_project (scope-aware: scope_filter='general'
when project resolves to 'general', else default union) and
ReflectionSynthesisService.retrieve_reflections (unlimited cap). Counts
exposed on BootstrapResult; rendering wired in next task."
```

---

## Task 5: `SessionBootstrapService` — markdown rendering

**Files:**
- Modify: `better_memory/services/session_bootstrap.py`
- Modify: `tests/services/test_session_bootstrap.py`

- [ ] **Step 1: Write failing test for rendered markdown shape**

```python
def test_render_includes_header_with_project_source_episode(conn, git_repo: Path) -> None:
    svc = SessionBootstrapService(conn)
    result = svc.bootstrap(source="startup", session_id="sess-h", cwd=git_repo)

    text = result.additional_context
    assert "## better-memory: session bootstrap" in text
    assert "Project: demo-repo" in text
    assert "Source: startup" in text
    assert "Episode: opened" in text


def test_render_includes_semantic_and_reflections_sections(conn, git_repo: Path) -> None:
    proj = git_repo.name
    _seed_semantic(conn, content="my-fact", project=proj, scope="project")
    _seed_reflection(conn, project=proj, polarity="do", scope="project")
    svc = SessionBootstrapService(conn)

    text = svc.bootstrap(source="startup", session_id="sess-r2", cwd=git_repo).additional_context

    assert "Semantic memories (1 entries)" in text
    assert "my-fact" in text
    assert "Reflections — do (prior wins)" in text


def test_render_omits_empty_sections(conn, git_repo: Path) -> None:
    svc = SessionBootstrapService(conn)
    text = svc.bootstrap(source="startup", session_id="sess-empty", cwd=git_repo).additional_context

    assert "Semantic memories" not in text
    assert "Reflections" not in text
    # but the header and footer should still render
    assert "## better-memory: session bootstrap" in text
    assert "memory_record_use" in text  # footer
```

- [ ] **Step 2: Run tests, confirm they fail**

```bash
pytest tests/services/test_session_bootstrap.py -k render -v
```

Expected: FAIL — `additional_context` is empty string.

- [ ] **Step 3: Implement rendering**

Modify `better_memory/services/session_bootstrap.py`. Add module constants and helpers near the top:

```python
_HINT_MAX_CHARS = 600

_FOOTER = (
    "Use mcp__better-memory__memory_record_use(id, success|failure) when a "
    "memory materially helps or misleads. Use mcp__better-memory__memory_observe "
    "to write new ones."
)


def _truncate(s: str) -> str:
    return s if len(s) <= _HINT_MAX_CHARS else s[: _HINT_MAX_CHARS - 1] + "…"


def _render_header(*, project: str, source: str, action: str, episode_id: str) -> str:
    short = episode_id[:8] if episode_id else ""
    return (
        f"## better-memory: session bootstrap\n"
        f"Project: {project}  •  Source: {source}  •  "
        f"Episode: {action} id={short}"
    )


def _render_semantic(items) -> str:
    if not items:
        return ""
    lines = [f"### Semantic memories ({len(items)} entries)"]
    for m in items:
        lines.append(f"- [{m.id[:8]}] {_truncate(m.content)}")
    return "\n".join(lines)


def _render_reflection_bucket(name: str, items) -> str:
    if not items:
        return ""
    lines = [f"### Reflections — {name}"]
    for item in items:
        lines.append(f"**{item['title']}**")
        lines.append(f"_{item['use_cases']}_")
        for hint in item.get("hints", []):
            lines.append(f"- {_truncate(hint)}")
        lines.append(f"_id: {item['id']}_")
        lines.append("")
    return "\n".join(lines)
```

Replace the `bootstrap` method's tail to compose the markdown:

```python
sections: list[str] = [
    _render_header(
        project=project,
        source=coerced_source,
        action=action,
        episode_id=episode_id,
    ),
]
sem_section = _render_semantic(semantic)
if sem_section:
    sections.append(sem_section)
do_section = _render_reflection_bucket("do (prior wins)", buckets["do"])
if do_section:
    sections.append(do_section)
dont_section = _render_reflection_bucket("dont (approaches to avoid)", buckets["dont"])
if dont_section:
    sections.append(dont_section)
neutral_section = _render_reflection_bucket("neutral (context)", buckets["neutral"])
if neutral_section:
    sections.append(neutral_section)
sections.append(_FOOTER)

rendered = "\n\n".join(sections)

return BootstrapResult(
    additional_context=rendered,
    project=project,
    source=coerced_source,
    episode_id=episode_id,
    episode_action=action,
    semantic_count=semantic_count,
    reflections_counts=reflections_counts,
)
```

- [ ] **Step 4: Run rendering tests, confirm they pass**

```bash
pytest tests/services/test_session_bootstrap.py -k render -v
```

Expected: all PASS.

- [ ] **Step 5: Run full service test suite, confirm no regressions**

```bash
pytest tests/services/test_session_bootstrap.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add better_memory/services/session_bootstrap.py tests/services/test_session_bootstrap.py
git commit -m "feat(services): SessionBootstrapService renders additionalContext markdown

Header with project/source/episode-action, semantic memories section,
three reflection buckets (do/dont/neutral). Empty sections omitted.
Hints truncated to 600 chars. Footer with usage instructions."
```

---

## Task 6: Register `memory.session_bootstrap` MCP tool

**Files:**
- Modify: `better_memory/mcp/server.py`
- Create: `tests/mcp/test_session_bootstrap_tool.py`

- [ ] **Step 1: Write failing integration test for the MCP tool**

Create `tests/mcp/test_session_bootstrap_tool.py`:

```python
"""Integration test for the memory.session_bootstrap MCP tool."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.conftest import run_async
from tests.mcp.conftest import build_server  # existing fixture; adjust import if named differently


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "demo-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=str(repo), check=True)
    return repo


def test_session_bootstrap_tool_returns_structured_payload(tmp_path, git_repo, monkeypatch):
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path / "bm-home"))
    server, cleanup = build_server()  # adjust per existing harness
    try:
        result = run_async(
            server._call_tool(  # type: ignore[attr-defined]
                "memory.session_bootstrap",
                {"source": "startup", "session_id": "s-1", "cwd": str(git_repo)},
            )
        )
        payload = json.loads(result[0].text)

        assert payload["project"] == "demo-repo"
        assert payload["source"] == "startup"
        assert payload["episode"]["action"] == "opened"
        assert "additionalContext" in payload
        assert "## better-memory: session bootstrap" in payload["additionalContext"]
    finally:
        run_async(cleanup())
```

(If `tests/mcp/conftest.py` exposes a different fixture name, adapt the import. Inspect `tests/mcp/conftest.py` and `tests/mcp/test_server_integration.py` for the canonical pattern before writing.)

- [ ] **Step 2: Run test, confirm it fails**

```bash
pytest tests/mcp/test_session_bootstrap_tool.py -v
```

Expected: FAIL with `Unknown tool: memory.session_bootstrap` (or similar — depends on dispatcher).

- [ ] **Step 3: Add Tool definition to `_tool_definitions`**

In `better_memory/mcp/server.py`, append a new `Tool(...)` to the list returned by `_tool_definitions()`:

```python
Tool(
    name="memory.session_bootstrap",
    description=(
        "Open or reuse a session episode and inject all project + general "
        "semantic memories and reflections as additionalContext markdown. "
        "Mirrors what the SessionStart hook does; callable manually for "
        "recovery, testing, or post-/clear re-injection."
    ),
    inputSchema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source": {
                "type": "string",
                "enum": ["startup", "resume", "clear", "compact"],
                "description": (
                    "SessionStart payload source. Unknown values coerce "
                    "to 'startup' inside the service."
                ),
            },
            "session_id": {"type": "string"},
            "cwd": {
                "type": "string",
                "description": (
                    "Optional working directory. Defaults to server's "
                    "process cwd."
                ),
            },
        },
    },
),
```

- [ ] **Step 4: Add dispatch branch in `_call_tool`**

After the existing tool-name checks in `better_memory/mcp/server.py:_call_tool`:

```python
if name == "memory.session_bootstrap":
    from better_memory.services.session_bootstrap import SessionBootstrapService
    import os
    import uuid

    cwd_arg = args.get("cwd") or os.getcwd()
    session_id_arg = args.get("session_id") or os.environ.get("CLAUDE_SESSION_ID") or uuid.uuid4().hex
    svc = SessionBootstrapService(memory_conn)
    result = svc.bootstrap(
        source=args.get("source"),
        session_id=session_id_arg,
        cwd=Path(cwd_arg),
    )
    payload = {
        "additionalContext": result.additional_context,
        "project": result.project,
        "source": result.source,
        "episode": {"id": result.episode_id, "action": result.episode_action},
        "counts": {
            "semantic": result.semantic_count,
            "reflections": result.reflections_counts,
        },
    }
    return [TextContent(type="text", text=json.dumps(payload))]
```

(Add `from pathlib import Path` to the file's imports if not already present.)

- [ ] **Step 5: Run test, confirm it passes**

```bash
pytest tests/mcp/test_session_bootstrap_tool.py -v
```

Expected: PASS.

- [ ] **Step 6: Run full MCP test suite, confirm no regressions**

```bash
pytest tests/mcp -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add better_memory/mcp/server.py tests/mcp/test_session_bootstrap_tool.py
git commit -m "feat(mcp): register memory.session_bootstrap tool

Exposes SessionBootstrapService over MCP so Claude can manually re-invoke
the bootstrap (recovery after hook failure, post-/clear re-injection,
testing). Returns structured payload with additionalContext markdown plus
project/source/episode/counts."
```

---

## Task 7: Create the `session_bootstrap` hook

**Files:**
- Create: `better_memory/hooks/session_bootstrap.py`
- Create: `tests/hooks/test_session_bootstrap.py`

- [ ] **Step 1: Write failing subprocess test for the hook**

Create `tests/hooks/test_session_bootstrap.py`:

```python
"""Subprocess tests for better_memory.hooks.session_bootstrap."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations

_MIGRATIONS = Path(__file__).resolve().parents[2] / "better_memory" / "db" / "migrations"


def _run_hook(home_dir: Path, *, stdin: str = "", cwd: Path | None = None):
    env = {**os.environ, "BETTER_MEMORY_HOME": str(home_dir)}
    return subprocess.run(
        [sys.executable, "-m", "better_memory.hooks.session_bootstrap"],
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        cwd=str(cwd) if cwd else None,
    )


@pytest.fixture
def home_with_schema(tmp_path: Path) -> Path:
    home = tmp_path / "bm-home"
    home.mkdir()
    c = connect(home / "memory.db")
    try:
        apply_migrations(c, migrations_dir=_MIGRATIONS)
    finally:
        c.close()
    return home


@pytest.fixture
def git_cwd(tmp_path: Path) -> Path:
    repo = tmp_path / "demo-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=str(repo), check=True)
    return repo


def test_hook_emits_additional_context_envelope(home_with_schema, git_cwd):
    payload = json.dumps({"source": "startup", "session_id": "h-1"})

    proc = _run_hook(home_with_schema, stdin=payload, cwd=git_cwd)

    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "## better-memory: session bootstrap" in out["hookSpecificOutput"]["additionalContext"]
```

- [ ] **Step 2: Run test, confirm it fails**

```bash
pytest tests/hooks/test_session_bootstrap.py::test_hook_emits_additional_context_envelope -v
```

Expected: FAIL with `No module named better_memory.hooks.session_bootstrap`.

- [ ] **Step 3: Create the hook script**

Create `better_memory/hooks/session_bootstrap.py`:

```python
"""SessionStart hook: open/reuse episode + inject memories + reflections.

Reads Claude Code SessionStart payload from stdin (source, session_id, cwd),
calls SessionBootstrapService in-process (no MCP RPC on the hook critical
path), prints a hookSpecificOutput JSON envelope to stdout. Never raises;
on any error logs to hook_errors and injects a fallback directive instructing
Claude to call the MCP tool manually.
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from better_memory.config import get_config
from better_memory.db.connection import connect
from better_memory.hooks._error_log import record_hook_error
from better_memory.services.session_bootstrap import SessionBootstrapService

_MAX_STDIN_BYTES = 1_048_576


def _short_msg(exc: BaseException, *, limit: int = 200) -> str:
    msg = str(exc).splitlines()[0] if str(exc) else ""
    return msg[:limit]


def _fallback_directive(exc: BaseException) -> str:
    return (
        f"better-memory: session bootstrap failed "
        f"({type(exc).__name__}: {_short_msg(exc)}). "
        f"Call mcp__better-memory__memory_session_bootstrap manually before any task. "
        f"If the failure persists, check ~/.better-memory/hook_errors and "
        f"consider rolling back via the install-backups directory."
    )


def main() -> None:
    raw = ""
    try:
        raw = sys.stdin.read(_MAX_STDIN_BYTES + 1)
    except BaseException:  # noqa: BLE001 — hooks never fail
        pass
    if len(raw) > _MAX_STDIN_BYTES:
        raw = ""  # oversized; drop and proceed with defaults

    payload: dict = {}
    if raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except BaseException:  # noqa: BLE001
            pass

    source_val = payload.get("source")
    source = str(source_val) if source_val else None
    session_id = (
        str(payload.get("session_id"))
        if payload.get("session_id")
        else os.environ.get("CLAUDE_SESSION_ID") or uuid4().hex
    )
    cwd_str = str(payload.get("cwd")) if payload.get("cwd") else os.getcwd()

    try:
        cfg = get_config()
        with closing(connect(cfg.memory_db)) as conn:
            service = SessionBootstrapService(conn)
            result = service.bootstrap(
                source=source, session_id=session_id, cwd=Path(cwd_str),
            )
        rendered = result.additional_context
    except BaseException as exc:  # noqa: BLE001
        try:
            record_hook_error(hook_name="session_bootstrap", exc=exc)
        except BaseException:  # noqa: BLE001
            pass
        rendered = _fallback_directive(exc)

    try:
        print(
            json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": rendered,
                }
            }),
            flush=True,
        )
    except BaseException:  # noqa: BLE001
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test, confirm it passes**

```bash
pytest tests/hooks/test_session_bootstrap.py::test_hook_emits_additional_context_envelope -v
```

Expected: PASS.

- [ ] **Step 5: Add error / edge tests**

```python
def test_hook_handles_empty_stdin_with_defaults(home_with_schema, git_cwd):
    proc = _run_hook(home_with_schema, stdin="", cwd=git_cwd)

    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert "## better-memory: session bootstrap" in out["hookSpecificOutput"]["additionalContext"]


def test_hook_handles_malformed_json(home_with_schema, git_cwd):
    proc = _run_hook(home_with_schema, stdin="not json {{", cwd=git_cwd)

    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    # falls back to source=startup defaults
    assert "Source: startup" in out["hookSpecificOutput"]["additionalContext"]


def test_hook_falls_back_on_db_failure(tmp_path, git_cwd, monkeypatch):
    # Point at a directory instead of a DB file → connect / migrations should fail.
    bad_home = tmp_path / "bad-home"
    bad_home.mkdir()
    (bad_home / "memory.db").mkdir()  # directory in place of file
    payload = json.dumps({"source": "startup", "session_id": "x"})

    proc = _run_hook(bad_home, stdin=payload, cwd=git_cwd)

    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "session bootstrap failed" in text
    assert "memory_session_bootstrap" in text
```

Run: `pytest tests/hooks/test_session_bootstrap.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add better_memory/hooks/session_bootstrap.py tests/hooks/test_session_bootstrap.py
git commit -m "feat(hooks): add session_bootstrap hook

Thin shim — reads SessionStart stdin payload, calls SessionBootstrapService
in-process, prints hookSpecificOutput.additionalContext. Never raises;
on error logs to hook_errors and injects a fallback directive instructing
Claude to call the MCP tool manually."
```

---

## Task 8: Update install-hooks registry + legacy cleanup

**Files:**
- Modify: `better_memory/cli/install_hooks.py`
- Modify: `tests/cli/test_install_hooks.py`

- [ ] **Step 1: Write failing test for legacy cleanup**

Add to `tests/cli/test_install_hooks.py`:

```python
def test_merge_settings_strips_legacy_session_start_and_session_retrieve():
    """Re-running install_hooks after upgrade scrubs the two old hook entries."""
    from better_memory.cli.install_hooks import merge_settings_json

    legacy_pyw = "C:/old/pythonw.exe"
    existing = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {"type": "command",
                         "command": f'"{legacy_pyw}" -m better_memory.hooks.session_start'},
                        {"type": "command",
                         "command": f'"{legacy_pyw}" -m better_memory.hooks.session_retrieve'},
                    ],
                },
            ],
        },
    }
    new_pyw = "C:/new/pythonw.exe"

    result = merge_settings_json(existing, venv_pyw=new_pyw)

    session_start_groups = result["hooks"]["SessionStart"]
    # Single canonical group; both legacy commands gone; new bootstrap entry present.
    flattened = [
        h["command"]
        for g in session_start_groups
        for h in g["hooks"]
    ]
    assert all("session_start" not in c or "session_bootstrap" in c for c in flattened)
    assert all("session_retrieve" not in c for c in flattened)
    assert any("session_bootstrap" in c for c in flattened)


def test_merge_settings_writes_single_session_bootstrap_entry_on_empty():
    from better_memory.cli.install_hooks import merge_settings_json

    result = merge_settings_json({}, venv_pyw="/tmp/pythonw")

    groups = result["hooks"]["SessionStart"]
    assert len(groups) == 1
    assert len(groups[0]["hooks"]) == 1
    assert "session_bootstrap" in groups[0]["hooks"][0]["command"]
```

- [ ] **Step 2: Run tests, confirm they fail**

```bash
pytest tests/cli/test_install_hooks.py -v -k "legacy or single_session_bootstrap"
```

Expected: FAIL — current `_OUR_HOOKS` registers `session_start` + `session_retrieve`; legacy stripping doesn't yet target them by name.

- [ ] **Step 3: Update `_OUR_HOOKS` and add `_LEGACY_HOOK_MODULES`**

In `better_memory/cli/install_hooks.py`, replace the `_OUR_HOOKS` definition (around line 36):

```python
_OUR_HOOKS: tuple[HookSpec, ...] = (
    HookSpec("better_memory.hooks.session_bootstrap", "SessionStart", None,              False),
    HookSpec("better_memory.hooks.observer",          "PostToolUse",  "Write|Edit|Bash", True),
    HookSpec("better_memory.hooks.session_close",     "Stop",         None,              True),
)

# Module paths that are no longer registered but may be present in users'
# settings.json from prior installs. Scrubbed by the REMOVE pass on every
# install so upgrades land cleanly.
_LEGACY_HOOK_MODULES: frozenset[str] = frozenset({
    "better_memory.hooks.session_start",
    "better_memory.hooks.session_retrieve",
})
```

- [ ] **Step 4: Update REMOVE pass in `merge_settings_json`**

Around line 105 in `better_memory/cli/install_hooks.py`, change:

```python
config = dict(existing)
hooks = dict(config.get("hooks", {}))
our_module_paths = {spec.module for spec in _OUR_HOOKS}
strip_modules = our_module_paths | _LEGACY_HOOK_MODULES
```

And update the comprehension that filters hooks:

```python
kept_hooks = [
    h for h in group.get("hooks", [])
    if not any(mp in h.get("command", "") for mp in strip_modules)
]
```

- [ ] **Step 5: Run tests, confirm they pass**

```bash
pytest tests/cli/test_install_hooks.py -v
```

Expected: new tests PASS, all existing tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add better_memory/cli/install_hooks.py tests/cli/test_install_hooks.py
git commit -m "feat(cli): install single session_bootstrap hook + legacy cleanup

Replaces the two legacy SessionStart entries (session_start +
session_retrieve) with a single session_bootstrap entry. _LEGACY_HOOK_MODULES
ensures upgrades scrub the old commands from users' settings.json on the
next install run. Idempotent."
```

---

## Task 9: Remove `session_start` handling from `SpoolService`

**Files:**
- Modify: `better_memory/services/spool.py`
- Modify: `tests/services/test_spool.py`

- [ ] **Step 1: Identify and inspect existing tests**

Run:

```bash
pytest tests/services/test_spool.py -v --collect-only | grep session_start
```

Expected: lists test names that reference session_start handling. These are the tests to delete.

- [ ] **Step 2: Delete the test cases**

Open `tests/services/test_spool.py` and delete every test that exercises:

- `_maybe_open_episode_for_session_start`
- The `event_type == "session_start"` branch's side-effect

Leave `session_end` and `commit_close` tests untouched. If a test parametrizes across event types and one parameter is `"session_start"`, drop that parameter.

- [ ] **Step 3: Run remaining spool tests, confirm they still pass**

```bash
pytest tests/services/test_spool.py -v
```

Expected: PASS (with the session_start cases gone).

- [ ] **Step 4: Remove the dead code**

In `better_memory/services/spool.py`:

1. Delete the entire `_maybe_open_episode_for_session_start` method (lines 209–232).
2. In the `drain` method's pass 2.5 dispatch (around line 147), delete the `if event_type == "session_start":` branch and its body so the chain becomes:

```python
if self._episodes is not None:
    for payload in inserted_payloads:
        event_type = payload.get("event_type")
        if event_type == "commit_close":
            self._maybe_close_episode_for_commit(payload)
        elif event_type == "session_end":
            self._maybe_close_episode_for_session_end(payload)
```

3. Update the docstring at line 61 — remove the phrase about processing `session_start` events.

- [ ] **Step 5: Run full spool + episode test suite, confirm green**

```bash
pytest tests/services/test_spool.py tests/services/test_episode.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add better_memory/services/spool.py tests/services/test_spool.py
git commit -m "refactor(spool): remove session_start drain branch + lazy-open

Episodes now open eagerly in the session_bootstrap hook (in-process),
making the spool/drain path for session_start markers obsolete.
Pre-existing markers in users' spool dirs still parse, insert into
hook_events as benign history, and unlink on next drain."
```

---

## Task 10: Delete obsolete hook modules and tests

**Files:**
- Delete: `better_memory/hooks/session_start.py`
- Delete: `better_memory/hooks/session_retrieve.py`
- Delete: `tests/hooks/test_session_start.py`
- Delete: `tests/hooks/test_session_retrieve.py`
- Modify: `tests/hooks/test_hooks_and_spool_integration.py` (if it references the deleted hooks)

- [ ] **Step 1: Confirm no in-tree imports remain**

```bash
grep -r "better_memory.hooks.session_start\|better_memory.hooks.session_retrieve" better_memory/ tests/
```

Expected: only matches inside `_LEGACY_HOOK_MODULES` (in `install_hooks.py`) — those should remain. No other references.

If matches surface in `tests/hooks/test_hooks_and_spool_integration.py`, edit that file to drop the relevant tests (keep tests for observer, session_close, and the bootstrap hook).

- [ ] **Step 2: Delete the four files**

```bash
rm better_memory/hooks/session_start.py
rm better_memory/hooks/session_retrieve.py
rm tests/hooks/test_session_start.py
rm tests/hooks/test_session_retrieve.py
```

- [ ] **Step 3: Run full hook test suite, confirm green**

```bash
pytest tests/hooks -v
```

Expected: all PASS. Only `test_observer.py`, `test_session_close.py`, `test_session_bootstrap.py`, `test_post_commit.py`, `test_error_log.py`, and (modified) `test_hooks_and_spool_integration.py` remain.

- [ ] **Step 4: Run the full test suite, confirm green**

```bash
pytest -v
```

Expected: all PASS, no orphan import errors.

- [ ] **Step 5: Commit**

```bash
git add -A better_memory/hooks tests/hooks
git commit -m "chore(hooks): delete obsolete session_start + session_retrieve modules

Replaced by session_bootstrap. Legacy cleanup in install_hooks ensures
users' settings.json gets scrubbed on next install."
```

---

## Task 11: Final verification + documentation update

**Files:**
- Modify: `README.md` or hooks documentation page if it lists the four hooks

- [ ] **Step 1: Search for documentation references to the old hooks**

```bash
grep -rn "session_start\|session_retrieve" docs/ README.md mkdocs.yml 2>/dev/null
```

If matches are found, update them to reference `session_bootstrap` instead. The architecture is now: one SessionStart hook, one PostToolUse hook (observer), one Stop hook (session_close).

- [ ] **Step 2: Run install-hooks against a fresh dummy home and inspect output**

```bash
mkdir -p /tmp/dummy-bm-home
python -m better_memory.cli.install_hooks \
  --venv-py "$(which python)" \
  --venv-pyw "$(which python)" \
  --home /tmp/dummy-bm-home
```

(On Windows, use the appropriate paths for `pythonw.exe`.)

Expected: success message; written `~/.claude/settings.json` (or your test path) contains exactly one SessionStart entry pointing at `better_memory.hooks.session_bootstrap`.

- [ ] **Step 3: Run the new hook against a real cwd and inspect output**

```bash
echo '{"source":"startup","session_id":"manual-test-1","cwd":"'$(pwd)'"}' \
  | python -m better_memory.hooks.session_bootstrap
```

Expected: JSON envelope on stdout containing `## better-memory: session bootstrap` etc. Exit code 0.

- [ ] **Step 4: Final commit (if docs changed)**

```bash
git add docs README.md mkdocs.yml
git commit -m "docs: update hook references to session_bootstrap"
```

---

## Self-Review Checklist

Performed inline on the spec. Below is the cross-check against this plan:

**Spec coverage:**

| Spec section | Plan task(s) |
|---|---|
| §4 Architecture — SessionBootstrapService | Tasks 3-5 |
| §4 Architecture — MCP tool | Task 6 |
| §4 Architecture — hook script | Task 7 |
| §4 Architecture — `project_name` git-aware | Task 1 |
| §4 Architecture — install-hooks registry + legacy cleanup | Task 8 |
| §4 Architecture — spool/drain cleanup | Task 9 |
| §4 Architecture — file deletions | Task 10 |
| §5 Source detection & episode lifecycle | Task 3 (skeleton) |
| §6 Retrieval & rendering — semantic + reflections | Tasks 4 + 5 |
| §6 Retrieval & rendering — `general` scope_filter handling | Task 4 |
| §6 Retrieval & rendering — `limit_per_bucket=None` plumbing | Task 2 |
| §7 Hook script & install-hooks | Tasks 7 + 8 |
| §8 Error handling — fallback directive | Task 7 |
| §9 Spool/drain cleanup | Task 9 |
| §10 Testing — service unit tests | Tasks 3-5 |
| §10 Testing — `project_name` tests | Task 1 |
| §10 Testing — hook subprocess tests | Task 7 |
| §10 Testing — MCP tool integration | Task 6 |
| §10 Testing — install-hooks merge tests | Task 8 |
| §10 Testing — regression test removals | Tasks 9 + 10 |
| §11 Migration & rollout | Task 8 (legacy cleanup); Task 11 (verification) |
| §12 Open questions — connection sharing | Surfaced during Tasks 3-4 if it manifests |
| §12 Open questions — `limit_per_bucket=None` style | Resolved in Task 2 (chose `None`) |
| §12 Open questions — subprocess cost of git-aware | Implementation note in Task 1; out of scope to optimize unless it shows up in benchmarks |

**No outstanding gaps.**

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-09-session-bootstrap-hook.md`. Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration. Each task includes the implementer + spec reviewer + code quality reviewer cycle (per CLAUDE.md process discipline rules).

**2. Inline Execution** — execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints for review.

Which approach?
