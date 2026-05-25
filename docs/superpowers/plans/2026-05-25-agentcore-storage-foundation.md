# AgentCore Storage — Foundation Plan (Plan 1 of 3) — REWRITE v2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Rewrite note (2026-05-25):** v1 of this plan was executed up to and including Task 2 commit `bff6506`. Code-quality review found that the v1 Protocol shape and v1 delegation tables did NOT match the real service signatures (sync vs async drift, wrong method names, wrong kwargs, references to nonexistent service methods). This v2 is the corrected plan, written against the verified service inventory at HEAD `bff6506`.
>
> **Before starting Task 2:** revert v1's Task 2 commit so the worktree is clean for the corrected protocol:
>
> ```bash
> git revert --no-edit bff6506
> ```
>
> Task 1 (config switch) is unaffected and stays as shipped (`0df6410` + `75ab56b`).

**Goal:** Introduce a `StorageBackend` protocol and a `SqliteBackend` implementation that wraps the existing services with their real signatures. No behaviour change. Wire the MCP server to dispatch on backend and to conditionally register synthesis tools based on backend capability.

**Architecture:** "Fat" backend protocol — high-level operations matching the existing service surface (async where services are async, sync where services are sync). `SqliteBackend` delegates to existing services. Existing service tests continue to exercise services directly and pass unchanged. New protocol-satisfaction smoke tests verify the backend surface. One missing service helper (`SessionBootstrapService.list_session_exposures`) is extracted from the inline MCP handler before the wrapper can call it.

**Tech Stack:** Python 3.12+, `typing.Protocol` (`runtime_checkable`), pytest, existing sqlite3 / service stack.

**Spec:** `docs/superpowers/specs/2026-05-24-agentcore-storage-backend-design.md` — sections "Storage layer abstraction" and "Components".

**Follow-up plans:**
- Plan 2 (AgentCore backend) — `boto3` adapter implementing the same protocol.
- Plan 3 (Operator wiring) — Stop-hook closure event, CLI, integration tests, docs.

---

## What changed vs v1

| Concern | v1 (wrong) | v2 (verified) |
|---|---|---|
| `observe()` / `retrieve()` / `list_observations()` | sync | **async** — match `ObservationService.{create,retrieve,list_observations}` |
| Method name for raw observation list | `retrieve_observations` | **`list_observations`** |
| `record_use` kwargs | `*, id, outcome: Outcome` | `*, observation_id, outcome: UseOutcome \| None` |
| Synthesis methods | `synthesize_next_get_context` / `_apply` | name retained, but `apply` takes `*, episode_id, response: SynthesisResponse, project` (not a `decisions` list) |
| Promote/retire reflection kwargs | `*, id` | `*, reflection_id` |
| `MemoryRatingService.credit(...)` | called as `credit` | **does not exist**; use `credit_one(*, session_id, kind, id, classification)` |
| `SemanticMemoryService.update(...)` | called as `update` | doesn't exist; methods are `update_text(*, id, content)` and `set_scope(*, id, scope)` |
| `SemanticMemoryService` retrieve | called `retrieve` | **`list_for_project(*, project, scope_filter, search, track_exposure)`** |
| Episode lifecycle methods | `start` / `close` / `list` | **`open_background` / `start_foreground` / `close_active` / `close_by_id` / `list_episodes`** |
| `close_episode(outcome="unknown")` | one kwarg | requires `outcome` AND `close_reason` (both `str`) |
| `SessionBootstrapService.list_session_exposures` | called as a service method | **NOT IMPLEMENTED**; logic inline at `mcp/server.py:1423`. New Task 5a extracts it. |
| MCP tool name format | `memory_synthesize_next_*` | tools are registered as `memory.X` (dot syntax) |
| `create_server()` return | scalar `Server` | already `(Server, async cleanup)` tuple; Task 7 must preserve this shape |
| `Outcome` export | only from `protocol.py` | **also re-export from `better_memory.storage`** |

## File Map

| Path | Status | Responsibility |
|---|---|---|
| `better_memory/config.py` | **DONE in Task 1** | `storage_backend` + `agentcore_*` fields wired |
| `better_memory/storage/__init__.py` | NEW (Task 2) | Package; re-exports `StorageBackend`, `SqliteBackend`, `build_backend`, `Outcome`, `UseOutcome` |
| `better_memory/storage/protocol.py` | NEW (Task 2) | `StorageBackend` Protocol; `Outcome` and `UseOutcome` re-exports |
| `better_memory/storage/sqlite.py` | NEW (Tasks 3-5) | `SqliteBackend` — wraps existing services |
| `better_memory/storage/factory.py` | NEW (Task 6) | `build_backend(config, ...) -> StorageBackend` |
| `better_memory/services/session_bootstrap.py` | modify (Task 5a) | Add `list_session_exposures(*, session_id)` method by extracting inline MCP handler logic |
| `better_memory/mcp/server.py` | modify (Task 7) | Build backend at startup; inject; capability-gated tool registration |
| `tests/test_config.py` | **DONE in Task 1** | env-var tests live here |
| `tests/storage/__init__.py` | NEW (Task 2) | empty package marker |
| `tests/storage/test_protocol.py` | NEW (Task 2) | Protocol shape + `@runtime_checkable` introspection |
| `tests/storage/test_sqlite_backend.py` | NEW (Tasks 3-5) | Smoke tests for each protocol method through `SqliteBackend` |
| `tests/storage/test_factory.py` | NEW (Task 6) | `build_backend` dispatch behavior |
| `tests/services/test_session_bootstrap.py` | modify (Task 5a) | Test the new `list_session_exposures` method |
| `tests/mcp/test_server_backend_dispatch.py` | NEW (Task 7) | Backend wiring + conditional tool registration |

## Verified service inventory (the source of truth for delegation)

The numbers below are absolute file:line at HEAD `bff6506`. Re-grep before relying on them if many commits have landed.

### `better_memory/services/observation.py`

| Method | Async? | Signature | Returns |
|---|---|---|---|
| `ObservationService.__init__` (line 115) | sync | `conn, embedder, *, clock=None, project_resolver=None, scope_resolver=None, session_id=None, audit_log_retrieved=None, episodes=None` | None |
| `.create` (line 160) | **async** | `content, *, component=None, theme=None, trigger_type=None, outcome="neutral", scope_path=None, project=None, tech=None, scope="project"` | `str` |
| `.retrieve` (line 286) | **async** | `query=None, *, component=None, status="active", window_days=30, scope_path=None, project=None, do_limit=10, dont_limit=10, neutral_limit=5, candidate_k=50, reinforcement_alpha=0.1` | `BucketedResults` |
| `.record_use` (line 410) | sync | `observation_id, *, outcome: UseOutcome \| None = None` | None |
| `.list_observations` (line 472) | **async** | `*, project=None, episode_id=None, component=None, theme=None, outcome=None, query=None, limit=50` | `list[dict[str, Any]]` |

### `better_memory/services/reflection.py`

| Class.Method | Async? | Signature | Returns |
|---|---|---|---|
| `ReflectionService.__init__` (1331) | sync | `conn, *, clock=None` | None |
| `ReflectionService.retire` (1362) | sync | `*, reflection_id: str` | None |
| `ReflectionService.promote_to_general` (1430) | sync | `*, reflection_id: str` | None |
| `ReflectionSynthesisService.__init__` (303) | sync | `conn, *, clock=None` | None |
| `ReflectionSynthesisService.get_next_pending_context` (1074) | sync | `*, project: str` | `EpisodeContext \| None` |
| `ReflectionSynthesisService.apply_decision` (1090) | sync | `*, episode_id: str, response: SynthesisResponse, project: str` | `SynthesisStep` |
| `ReflectionSynthesisService.retrieve_reflections` (1175) | sync | `*, project: str, tech=None, phase=None, polarity=None, limit_per_bucket=20, track_exposure=True` | `dict[str, list[dict]]` |

### `better_memory/services/memory_rating.py`

| Method | Async? | Signature | Returns |
|---|---|---|---|
| `MemoryRatingService.__init__` (79) | sync | `conn, *, clock=None` | None |
| `.credit_one` (89) | sync | `*, session_id, kind, id, classification` | `ApplyOutcome` |
| `.apply_session_ratings` (235) | sync | `*, session_id, ratings: list[dict[str, str]]` | `ApplySessionRatingsResult` |

### `better_memory/services/semantic.py`

| Method | Async? | Signature | Returns |
|---|---|---|---|
| `SemanticMemoryService.__init__` (54) | sync | `conn, *, clock=None` | None |
| `.create` (63) | sync | `*, content: str, project: str, scope: str = "project"` | `str` |
| `.update_text` (85) | sync | `*, id: str, content: str` | None |
| `.set_scope` (103) | sync | `*, id: str, scope: str` | None |
| `.delete` (187) | sync | `*, id: str` | None |
| `.list_for_project` (194) | sync | `*, project: str, scope_filter=None, search=None, track_exposure=True` | `list[SemanticMemory]` |
| `.create_from_observation` (125) | sync | `*, observation_id: str, scope: str = "project"` | `str` |

### `better_memory/services/episode.py`

| Method | Async? | Signature | Returns |
|---|---|---|---|
| `EpisodeService.__init__` (70) | sync | `conn, *, clock=None` | None |
| `.open_background` (79) | sync | `*, session_id: str, project: str` | `str` (episode id) |
| `.start_foreground` (131) | sync | `*, session_id: str, project: str, goal: str, tech: str \| None = None` | `str` |
| `.active_episode` (117) | sync | `session_id` (positional) | `Episode \| None` |
| `.close_active` (226) | sync | `*, session_id: str, outcome: str, close_reason: str, summary: str \| None = None` | `str` |
| `.close_by_id` (275) | sync | `*, episode_id: str, outcome: str, close_reason: str, summary: str \| None = None` | `str` |
| `.list_episodes` (371) | sync | `*, project: str \| None = None, outcome: str \| None = None, only_open: bool = False` | `list[Episode]` |

### `better_memory/services/session_bootstrap.py`

| Method | Async? | Signature | Returns |
|---|---|---|---|
| `SessionBootstrapService.__init__` (99) | sync | `conn, *, clock=None` | None |
| `.bootstrap` (141) | sync | `*, source=None, session_id: str, cwd: Path \| None = None, project: str \| None = None` | `BootstrapResult` |
| `.list_session_exposures` | **MISSING** | — | added in Task 5a |

### `better_memory/mcp/server.py`

- `create_server() -> tuple[Server, async cleanup]` (line 919) — already a tuple, do not change its shape.
- `_tool_definitions() -> list[Tool]` (line 256) — static list; Task 7 rewrites to take `*, supports_synthesis: bool`.
- Tool names use dot-syntax: `memory.observe`, `memory.retrieve`, `memory.synthesize_next_get_context`, etc. (see file map above for line numbers).
- `list_session_exposures` tool dispatched at line 1423 — handler logic is inline (this is the code Task 5a extracts).
- Handler is one large `if name == "..."` chain in `_call_tool` (lines 989–1494).

## Confidence Summary

| Task | Confidence | Lift applied |
|---|---|---|
| 1. Config switch + agentcore fields | DONE | shipped 0df6410 + 75ab56b |
| 2. Storage package skeleton + Protocol (corrected) | 95% | every method on the protocol mapped 1:1 to a verified service signature in the inventory above |
| 3. SqliteBackend — observe / retrieve / list_observations / record_use | 92% | tests dispatch the same `await` shape the MCP server uses today |
| 4. SqliteBackend — semantic CRUD + episode lifecycle + reflection lifecycle | 92% | service method names + kwargs locked against inventory |
| 5a. Extract `list_session_exposures` to `SessionBootstrapService` | 90% | source rows of inline MCP code copied verbatim into the service method |
| 5b. SqliteBackend — synthesis + session bootstrap + ratings | 92% | session_id held as `SqliteBackend` constructor state to drop env-coupling from Protocol surface |
| 6. Backend factory + env-var dispatch | 95% | unchanged conceptually from v1 |
| 7. MCP server dispatch + capability-gated registration | 88% (lifted from 75%) | `create_server` already returns a tuple, so we only insert a `ServerContext` into the existing return; tool names use dot-syntax; handler stays untouched |

All tasks ≥ 88%.

---

## Conventions used in this plan

- All test code is complete and runnable.
- All `Run:` commands include the exact expected output text or exit-code expectation.
- "Commit" steps use the canonical project commit-message style (lowercase imperative subject, `feat(scope):` / `refactor(scope):` prefix).
- Each task's delegation table shows the EXACT verified service signature.
- Protocol method bodies use `...` (Protocol convention).
- Bash output truncation: pytest `-v` against the whole repo can exceed the bash tool's ~30KB stdout buffer. Use `-q --tb=line` for terse output, or redirect to a file and `tail`, or pass `--junitxml=PATH` and parse the result.

---

### Task 2 (REWRITE): Storage package skeleton + StorageBackend Protocol

**Prerequisite:** Revert v1's Task 2 commit so the worktree is clean:

```bash
git revert --no-edit bff6506
```

This produces a revert commit titled `Revert "feat(storage): add StorageBackend Protocol with capability flag"`. Verify with `git status` that `better_memory/storage/` and `tests/storage/` are both gone after the revert.

**Files:**
- Create: `better_memory/storage/__init__.py`
- Create: `better_memory/storage/protocol.py`
- Create: `tests/storage/__init__.py` (empty)
- Create: `tests/storage/test_protocol.py`

- [ ] **Step 1: Create empty test package marker**

Create `tests/storage/__init__.py` as a zero-byte file (no docstring).

- [ ] **Step 2: Create `better_memory/storage/__init__.py`**

```python
"""Storage backend abstraction for better-memory.

Two implementations: SqliteBackend (default) wraps the existing services;
AgentCoreBackend (added in Plan 2) talks to AWS Bedrock AgentCore Memory.
Both satisfy the StorageBackend Protocol.
"""

from better_memory.storage.protocol import (
    Outcome,
    StorageBackend,
    UseOutcome,
)

__all__ = ["Outcome", "StorageBackend", "UseOutcome"]
```

(SqliteBackend and build_backend are appended to `__all__` later, in Tasks 3 and 6.)

- [ ] **Step 3: Write the failing protocol tests**

Create `tests/storage/test_protocol.py`:

```python
"""Tests that the StorageBackend Protocol shape is what consumers expect."""

from __future__ import annotations

import inspect

from better_memory.storage import StorageBackend


def test_protocol_is_runtime_checkable() -> None:
    """@runtime_checkable sets _is_runtime_protocol = True. MCP server relies on isinstance()."""
    assert getattr(StorageBackend, "_is_runtime_protocol", False), (
        "StorageBackend must be decorated with @runtime_checkable"
    )


def test_protocol_declares_capability_flag() -> None:
    """supports_synthesis is the capability MCP uses for conditional tool registration."""
    assert hasattr(StorageBackend, "supports_synthesis")


def test_protocol_declares_async_hot_path() -> None:
    """observe / retrieve / list_observations are async to match the existing service surface."""
    for name in ("observe", "retrieve", "list_observations"):
        method = getattr(StorageBackend, name, None)
        assert method is not None, f"Protocol missing {name}"
        assert inspect.iscoroutinefunction(method), (
            f"Protocol method {name!r} must be async"
        )


def test_protocol_declares_sync_record_use() -> None:
    """record_use is sync (ObservationService.record_use is sync)."""
    method = getattr(StorageBackend, "record_use", None)
    assert method is not None
    assert not inspect.iscoroutinefunction(method)


def test_protocol_declares_all_required_methods() -> None:
    """Every method backends must implement."""
    required = {
        # Observations
        "observe", "retrieve", "list_observations", "record_use",
        # Semantic memories
        "semantic_observe", "semantic_list", "semantic_update_text",
        "semantic_set_scope", "semantic_delete",
        # Episodes
        "open_background_episode", "start_foreground_episode",
        "close_active_episode", "close_episode_by_id", "list_episodes",
        # Reflection lifecycle
        "promote_reflection", "retire_reflection",
        # Session lifecycle
        "session_bootstrap", "list_session_exposures",
        "apply_session_ratings", "credit_one",
        # Synthesis (sqlite-only — gated by supports_synthesis)
        "synthesize_next_get_context", "synthesize_next_apply",
    }
    actual = set(dir(StorageBackend))
    missing = required - actual
    assert not missing, f"Protocol missing methods: {sorted(missing)}"


def test_protocol_methods_are_keyword_only() -> None:
    """Stable cross-backend interface requires kwarg-only signatures."""
    for name in (
        "observe", "semantic_observe", "open_background_episode",
        "close_active_episode", "apply_session_ratings", "credit_one",
        "synthesize_next_apply",
    ):
        method = getattr(StorageBackend, name)
        sig = inspect.signature(method)
        non_self = [p for p in sig.parameters.values() if p.name != "self"]
        kinds = {p.kind for p in non_self}
        assert kinds <= {inspect.Parameter.KEYWORD_ONLY}, (
            f"{name} must be keyword-only; got "
            f"{[(p.name, p.kind.name) for p in non_self]}"
        )
```

- [ ] **Step 4: Run protocol tests; verify ImportError**

Run: `uv run pytest tests/storage/test_protocol.py -v`
Expected: ImportError — `better_memory.storage.protocol` doesn't exist yet.

- [ ] **Step 5: Create the Protocol**

Create `better_memory/storage/protocol.py`:

```python
"""StorageBackend Protocol.

Fat protocol with high-level operations. Both SqliteBackend (Plan 1) and
AgentCoreBackend (Plan 2) implement it. Synthesis methods are sqlite-only —
the MCP server reads `supports_synthesis` to gate their tool registration.

Method shapes mirror the existing service surface verified at HEAD bff6506:
- observe / retrieve / list_observations are async (ObservationService is async)
- record_use is sync
- semantic / episode / reflection lifecycle methods are sync
- synthesis methods are sync

session_id and project are held on the implementation (e.g. as constructor
state on SqliteBackend) rather than passed per-call, since one backend
instance serves exactly one MCP session.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable


# Re-export the Outcome aliases so storage callers don't need to import from services.
# These mirror the literal sets in better_memory/services/observation.py.
Outcome = Literal["success", "failure", "neutral"]
UseOutcome = Literal["success", "failure"]


@runtime_checkable
class StorageBackend(Protocol):
    """High-level storage operations consumed by the MCP layer."""

    # ----- Capability flags -----

    @property
    def supports_synthesis(self) -> bool:
        """True when the synthesize_next_* MCP tools should be registered."""
        ...

    # ----- Observations -----

    async def observe(
        self,
        *,
        content: str,
        component: str | None = None,
        theme: str | None = None,
        trigger_type: str | None = None,
        outcome: Outcome = "neutral",
        scope_path: str | None = None,
        project: str | None = None,
        tech: str | None = None,
        scope: str = "project",
    ) -> str:
        """Record an observation. Returns the new observation id."""
        ...

    async def retrieve(
        self,
        query: str | None = None,
        *,
        component: str | None = None,
        status: str | None = "active",
        window_days: int | None = 30,
        scope_path: str | None = None,
        project: str | None = None,
        do_limit: int = 10,
        dont_limit: int = 10,
        neutral_limit: int = 5,
        candidate_k: int = 50,
        reinforcement_alpha: float = 0.1,
    ) -> Any:
        """Bucketed retrieval. Returns BucketedResults from the observation service."""
        ...

    async def list_observations(
        self,
        *,
        project: str | None = None,
        episode_id: str | None = None,
        component: str | None = None,
        theme: str | None = None,
        outcome: Outcome | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Drill-down list of raw observations."""
        ...

    def record_use(
        self,
        observation_id: str,
        *,
        outcome: UseOutcome | None = None,
    ) -> None:
        """Credit an observation's reinforcement counter. Raises ValueError if missing."""
        ...

    # ----- Semantic memories -----

    def semantic_observe(
        self,
        *,
        content: str,
        project: str | None = None,
        scope: str = "project",
    ) -> str:
        """Create a semantic memory. Returns its id."""
        ...

    def semantic_list(
        self,
        *,
        project: str | None = None,
        scope_filter: str | None = None,
        search: str | None = None,
        track_exposure: bool = True,
    ) -> list[Any]:
        """List semantic memories for the project. Returns list[SemanticMemory]."""
        ...

    def semantic_update_text(self, *, id: str, content: str) -> None:
        """Update the text of a semantic memory."""
        ...

    def semantic_set_scope(self, *, id: str, scope: str) -> None:
        """Change the scope (project/general) of a semantic memory."""
        ...

    def semantic_delete(self, *, id: str) -> None:
        """Permanently delete a semantic memory (idempotent)."""
        ...

    # ----- Episodes -----

    def open_background_episode(
        self,
        *,
        session_id: str,
        project: str,
    ) -> str:
        """Open a background episode for the given session. Returns episode id."""
        ...

    def start_foreground_episode(
        self,
        *,
        session_id: str,
        project: str,
        goal: str,
        tech: str | None = None,
    ) -> str:
        """Start a foreground episode. Returns episode id."""
        ...

    def close_active_episode(
        self,
        *,
        session_id: str,
        outcome: str,
        close_reason: str,
        summary: str | None = None,
    ) -> str:
        """Close the active episode for this session."""
        ...

    def close_episode_by_id(
        self,
        *,
        episode_id: str,
        outcome: str,
        close_reason: str,
        summary: str | None = None,
    ) -> str:
        """Close a specific episode by id."""
        ...

    def list_episodes(
        self,
        *,
        project: str | None = None,
        outcome: str | None = None,
        only_open: bool = False,
    ) -> list[Any]:
        """List episodes. Returns list[Episode]."""
        ...

    # ----- Reflection lifecycle -----

    def promote_reflection(self, *, reflection_id: str) -> None:
        """Promote a project-scope reflection to general scope."""
        ...

    def retire_reflection(self, *, reflection_id: str) -> None:
        """Retire a reflection (exclude from default retrieval)."""
        ...

    # ----- Session lifecycle -----

    def session_bootstrap(
        self,
        *,
        session_id: str,
        source: str | None = None,
        cwd: Any | None = None,
        project: str | None = None,
    ) -> Any:
        """Build the SessionStart additionalContext envelope. Returns BootstrapResult."""
        ...

    def list_session_exposures(
        self,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """List unrated memory exposures for the given session."""
        ...

    def apply_session_ratings(
        self,
        *,
        session_id: str,
        ratings: list[dict[str, str]],
    ) -> Any:
        """Atomically apply per-exposure ratings. Returns ApplySessionRatingsResult."""
        ...

    def credit_one(
        self,
        *,
        session_id: str,
        kind: str,
        id: str,
        classification: str,
    ) -> Any:
        """Apply a single rating for an exposed memory. Returns ApplyOutcome."""
        ...

    # ----- Synthesis (sqlite-only — guarded by supports_synthesis) -----

    def synthesize_next_get_context(
        self,
        *,
        project: str,
    ) -> Any:
        """Pop the next pending episode context. Returns EpisodeContext | None. Sqlite-only."""
        ...

    def synthesize_next_apply(
        self,
        *,
        episode_id: str,
        response: Any,
        project: str,
    ) -> Any:
        """Apply a SynthesisResponse. Returns SynthesisStep. Sqlite-only."""
        ...
```

- [ ] **Step 6: Run protocol tests; verify pass**

Run: `uv run pytest tests/storage/test_protocol.py -v`
Expected: 6 passed.

- [ ] **Step 7: pyright clean**

Run: `uv run pyright better_memory/storage tests/storage`
Expected: 0 errors.

- [ ] **Step 8: Commit**

```bash
git add better_memory/storage/__init__.py better_memory/storage/protocol.py tests/storage/__init__.py tests/storage/test_protocol.py
git commit -m "feat(storage): StorageBackend Protocol (v2, verified service shapes)"
```

---

### Task 3: SqliteBackend — observe / retrieve / list_observations / record_use

**Files:**
- Create: `better_memory/storage/sqlite.py`
- Create: `tests/storage/test_sqlite_backend.py`

**Service delegation table:**

| Protocol method | Service call (verified) |
|---|---|
| `observe()` (async) | `await ObservationService(conn, embedder, episodes=EpisodeService(conn), session_id=session_id, project_resolver=lambda: project).create(...)` |
| `retrieve()` (async) | `await ObservationService(...).retrieve(query, ...)` |
| `list_observations()` (async) | `await ObservationService(...).list_observations(...)` |
| `record_use()` (sync) | `ObservationService(...).record_use(observation_id, outcome=outcome)` |

- [ ] **Step 1: Write protocol-satisfaction smoke tests**

Create `tests/storage/test_sqlite_backend.py`:

```python
"""Smoke tests for SqliteBackend.

These verify the wrapper delegates correctly to underlying services. We do
NOT re-test service business logic — that lives in tests/services/.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from better_memory.storage import StorageBackend
from better_memory.storage.sqlite import SqliteBackend


@pytest.fixture
def memory_conn() -> sqlite3.Connection:
    """An in-memory sqlite connection with migrations applied."""
    from better_memory.db.schema import apply_migrations
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    return conn


@pytest.fixture
def backend(memory_conn) -> SqliteBackend:
    """A SqliteBackend wired to an in-memory db with a no-op embedder."""
    embedder = MagicMock()
    embedder.embed = MagicMock(return_value=[0.0] * 768)
    return SqliteBackend(
        memory_conn=memory_conn,
        embedder=embedder,
        session_id="test-session",
        project="testproj",
    )


def test_sqlite_backend_satisfies_protocol(backend) -> None:
    assert isinstance(backend, StorageBackend)


def test_supports_synthesis_is_true(backend) -> None:
    assert backend.supports_synthesis is True


@pytest.mark.asyncio
async def test_observe_returns_string_id(backend) -> None:
    obs_id = await backend.observe(
        content="test observation",
        outcome="success",
        theme="test",
    )
    assert isinstance(obs_id, str) and obs_id


@pytest.mark.asyncio
async def test_retrieve_returns_bucketed_results(backend) -> None:
    result = await backend.retrieve(query="anything", do_limit=3, dont_limit=3, neutral_limit=3)
    # BucketedResults is the service-defined return; we just confirm it has the buckets.
    do_bucket = getattr(result, "do", None) if not isinstance(result, dict) else result.get("do")
    assert do_bucket is not None


@pytest.mark.asyncio
async def test_list_observations_returns_list(backend) -> None:
    await backend.observe(content="findable text", theme="searchable")
    result = await backend.list_observations(query="findable", limit=5)
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_record_use_credits_recorded_observation(backend) -> None:
    obs_id = await backend.observe(content="will be credited", theme="x")
    backend.record_use(obs_id, outcome="success")  # No exception = pass.
```

- [ ] **Step 2: Run tests; verify ImportError**

Run: `uv run pytest tests/storage/test_sqlite_backend.py -v`
Expected: ImportError — `sqlite.py` doesn't exist.

- [ ] **Step 3: Create SqliteBackend with the hot path**

Create `better_memory/storage/sqlite.py`:

```python
"""SqliteBackend — wraps existing services to satisfy the StorageBackend Protocol.

Behaviour-preserving wrapper. Existing service tests continue to exercise
service business logic directly; tests in tests/storage/ only verify the
protocol delegation surface.

Held state:
- `memory_conn` — open sqlite3 Connection
- `embedder` — passed to ObservationService
- `session_id` — used for episode lookups, ratings, exposures
- `project` — default project for any method whose project kwarg is omitted

Services are constructed per call. They're light objects over the same conn;
re-instantiating avoids holding cyclic references and keeps tests simple.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from better_memory.services.episode import EpisodeService
from better_memory.services.observation import ObservationService
from better_memory.storage.protocol import Outcome, UseOutcome


class SqliteBackend:
    """Wraps existing services to satisfy StorageBackend Protocol."""

    def __init__(
        self,
        *,
        memory_conn: sqlite3.Connection,
        embedder: Any,
        session_id: str,
        project: str,
    ) -> None:
        self._conn = memory_conn
        self._embedder = embedder
        self._session_id = session_id
        self._project = project

    # ----- Capability flags -----

    @property
    def supports_synthesis(self) -> bool:
        return True

    # ----- Internal helpers -----

    def _observations(self) -> ObservationService:
        episodes = EpisodeService(self._conn)
        return ObservationService(
            self._conn,
            self._embedder,
            session_id=self._session_id,
            project_resolver=lambda: self._project,
            episodes=episodes,
        )

    # ----- Observations -----

    async def observe(
        self,
        *,
        content: str,
        component: str | None = None,
        theme: str | None = None,
        trigger_type: str | None = None,
        outcome: Outcome = "neutral",
        scope_path: str | None = None,
        project: str | None = None,
        tech: str | None = None,
        scope: str = "project",
    ) -> str:
        return await self._observations().create(
            content=content,
            component=component,
            theme=theme,
            trigger_type=trigger_type,
            outcome=outcome,
            scope_path=scope_path,
            project=project or self._project,
            tech=tech,
            scope=scope,
        )

    async def retrieve(
        self,
        query: str | None = None,
        *,
        component: str | None = None,
        status: str | None = "active",
        window_days: int | None = 30,
        scope_path: str | None = None,
        project: str | None = None,
        do_limit: int = 10,
        dont_limit: int = 10,
        neutral_limit: int = 5,
        candidate_k: int = 50,
        reinforcement_alpha: float = 0.1,
    ) -> Any:
        return await self._observations().retrieve(
            query,
            component=component,
            status=status,
            window_days=window_days,
            scope_path=scope_path,
            project=project or self._project,
            do_limit=do_limit,
            dont_limit=dont_limit,
            neutral_limit=neutral_limit,
            candidate_k=candidate_k,
            reinforcement_alpha=reinforcement_alpha,
        )

    async def list_observations(
        self,
        *,
        project: str | None = None,
        episode_id: str | None = None,
        component: str | None = None,
        theme: str | None = None,
        outcome: Outcome | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._observations().list_observations(
            project=project or self._project,
            episode_id=episode_id,
            component=component,
            theme=theme,
            outcome=outcome,
            query=query,
            limit=limit,
        )

    def record_use(
        self,
        observation_id: str,
        *,
        outcome: UseOutcome | None = None,
    ) -> None:
        self._observations().record_use(observation_id, outcome=outcome)
```

Also update `better_memory/storage/__init__.py` to export `SqliteBackend`:

```python
"""Storage backend abstraction for better-memory."""

from better_memory.storage.protocol import (
    Outcome,
    StorageBackend,
    UseOutcome,
)
from better_memory.storage.sqlite import SqliteBackend

__all__ = ["Outcome", "SqliteBackend", "StorageBackend", "UseOutcome"]
```

- [ ] **Step 4: Run tests; verify pass**

Run: `uv run pytest tests/storage/test_sqlite_backend.py -v`
Expected: 7 passed.

- [ ] **Step 5: Run existing service tests; verify no regression**

Run: `uv run pytest tests/services/test_observation.py -q --tb=line`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add better_memory/storage/sqlite.py better_memory/storage/__init__.py tests/storage/test_sqlite_backend.py
git commit -m "feat(storage): SqliteBackend wraps observe/retrieve/list_observations/record_use"
```

---

### Task 4: SqliteBackend — semantic + episode + reflection lifecycle

**Files:**
- Modify: `better_memory/storage/sqlite.py`
- Modify: `tests/storage/test_sqlite_backend.py`

**Service delegation table:**

| Protocol method | Service call |
|---|---|
| `semantic_observe()` | `SemanticMemoryService(conn).create(content=..., project=project or self._project, scope=scope)` |
| `semantic_list()` | `SemanticMemoryService(conn).list_for_project(project=project or self._project, scope_filter=..., search=..., track_exposure=...)` |
| `semantic_update_text()` | `SemanticMemoryService(conn).update_text(id=id, content=content)` |
| `semantic_set_scope()` | `SemanticMemoryService(conn).set_scope(id=id, scope=scope)` |
| `semantic_delete()` | `SemanticMemoryService(conn).delete(id=id)` |
| `open_background_episode()` | `EpisodeService(conn).open_background(session_id=session_id, project=project)` |
| `start_foreground_episode()` | `EpisodeService(conn).start_foreground(session_id=..., project=..., goal=..., tech=...)` |
| `close_active_episode()` | `EpisodeService(conn).close_active(session_id=..., outcome=..., close_reason=..., summary=...)` |
| `close_episode_by_id()` | `EpisodeService(conn).close_by_id(episode_id=..., outcome=..., close_reason=..., summary=...)` |
| `list_episodes()` | `EpisodeService(conn).list_episodes(project=project or self._project, outcome=..., only_open=...)` |
| `promote_reflection()` | `ReflectionService(conn).promote_to_general(reflection_id=reflection_id)` |
| `retire_reflection()` | `ReflectionService(conn).retire(reflection_id=reflection_id)` |

- [ ] **Step 1: Write the failing tests**

Append to `tests/storage/test_sqlite_backend.py`:

```python
def test_semantic_observe_and_list(backend) -> None:
    sm_id = backend.semantic_observe(content="prefer uv over pip")
    rows = backend.semantic_list()
    assert any(getattr(r, "id", None) == sm_id for r in rows)


def test_semantic_update_text(backend) -> None:
    sm_id = backend.semantic_observe(content="original")
    backend.semantic_update_text(id=sm_id, content="updated")
    rows = backend.semantic_list(search="updated")
    assert any(getattr(r, "id", None) == sm_id for r in rows)


def test_semantic_set_scope(backend) -> None:
    sm_id = backend.semantic_observe(content="to be promoted", scope="project")
    backend.semantic_set_scope(id=sm_id, scope="general")
    # Listing for the project no longer surfaces general-scope rows by default;
    # the call should succeed and not raise.


def test_semantic_delete(backend) -> None:
    sm_id = backend.semantic_observe(content="to be deleted")
    backend.semantic_delete(id=sm_id)
    rows = backend.semantic_list()
    assert not any(getattr(r, "id", None) == sm_id for r in rows)


def test_open_and_close_background_episode(backend) -> None:
    ep_id = backend.open_background_episode(
        session_id="test-session", project="testproj",
    )
    assert ep_id
    backend.close_episode_by_id(
        episode_id=ep_id, outcome="success", close_reason="test",
    )


def test_list_episodes_returns_list(backend) -> None:
    assert isinstance(backend.list_episodes(), list)


def test_promote_and_retire_reflection(backend, memory_conn) -> None:
    # Seed a confirmed reflection so promote/retire have a valid target.
    # Schema-aware seed: confirmed status, project-scope.
    memory_conn.execute(
        "INSERT INTO reflections (id, title, hints, use_cases, polarity, scope, "
        "project, status, confidence) VALUES "
        "('refl-test-1', 'T', 'H', 'U', 'do', 'project', 'testproj', 'confirmed', 0.9)"
    )
    memory_conn.commit()

    backend.promote_reflection(reflection_id="refl-test-1")
    row = memory_conn.execute(
        "SELECT scope FROM reflections WHERE id=?", ("refl-test-1",)
    ).fetchone()
    assert row["scope"] == "general"

    backend.retire_reflection(reflection_id="refl-test-1")
    row = memory_conn.execute(
        "SELECT status FROM reflections WHERE id=?", ("refl-test-1",)
    ).fetchone()
    assert row["status"] == "retired"
```

- [ ] **Step 2: Pre-implementation grep-verify**

```bash
grep -n "def create\b\|def update_text\b\|def set_scope\b\|def delete\b\|def list_for_project\b" better_memory/services/semantic.py
grep -n "def open_background\b\|def start_foreground\b\|def close_active\b\|def close_by_id\b\|def list_episodes\b" better_memory/services/episode.py
grep -n "def promote_to_general\b\|def retire\b" better_memory/services/reflection.py
```

If any method name is missing on the service, **STOP** and report — the inventory at the top of this plan needs updating; do not rename the service.

If the reflection seed INSERT in Step 1 fails with a schema column error, run `sqlite3 :memory:` against `apply_migrations(conn)` then `PRAGMA table_info(reflections);` to discover the actual columns, and adjust the INSERT — never adjust the schema.

- [ ] **Step 3: Run the new tests; verify failures**

Run: `uv run pytest tests/storage/test_sqlite_backend.py::test_semantic_observe_and_list -v`
Expected: `AttributeError: 'SqliteBackend' object has no attribute 'semantic_observe'`.

- [ ] **Step 4: Implement the methods on SqliteBackend**

Edit `better_memory/storage/sqlite.py`. Add imports:

```python
from better_memory.services.reflection import ReflectionService
from better_memory.services.semantic import SemanticMemoryService
```

Append to the class:

```python
    # ----- Semantic memories -----

    def semantic_observe(
        self,
        *,
        content: str,
        project: str | None = None,
        scope: str = "project",
    ) -> str:
        return SemanticMemoryService(self._conn).create(
            content=content,
            project=project or self._project,
            scope=scope,
        )

    def semantic_list(
        self,
        *,
        project: str | None = None,
        scope_filter: str | None = None,
        search: str | None = None,
        track_exposure: bool = True,
    ) -> list[Any]:
        return SemanticMemoryService(self._conn).list_for_project(
            project=project or self._project,
            scope_filter=scope_filter,
            search=search,
            track_exposure=track_exposure,
        )

    def semantic_update_text(self, *, id: str, content: str) -> None:
        SemanticMemoryService(self._conn).update_text(id=id, content=content)

    def semantic_set_scope(self, *, id: str, scope: str) -> None:
        SemanticMemoryService(self._conn).set_scope(id=id, scope=scope)

    def semantic_delete(self, *, id: str) -> None:
        SemanticMemoryService(self._conn).delete(id=id)

    # ----- Episodes -----

    def open_background_episode(
        self,
        *,
        session_id: str,
        project: str,
    ) -> str:
        return EpisodeService(self._conn).open_background(
            session_id=session_id, project=project,
        )

    def start_foreground_episode(
        self,
        *,
        session_id: str,
        project: str,
        goal: str,
        tech: str | None = None,
    ) -> str:
        return EpisodeService(self._conn).start_foreground(
            session_id=session_id, project=project, goal=goal, tech=tech,
        )

    def close_active_episode(
        self,
        *,
        session_id: str,
        outcome: str,
        close_reason: str,
        summary: str | None = None,
    ) -> str:
        return EpisodeService(self._conn).close_active(
            session_id=session_id,
            outcome=outcome,
            close_reason=close_reason,
            summary=summary,
        )

    def close_episode_by_id(
        self,
        *,
        episode_id: str,
        outcome: str,
        close_reason: str,
        summary: str | None = None,
    ) -> str:
        return EpisodeService(self._conn).close_by_id(
            episode_id=episode_id,
            outcome=outcome,
            close_reason=close_reason,
            summary=summary,
        )

    def list_episodes(
        self,
        *,
        project: str | None = None,
        outcome: str | None = None,
        only_open: bool = False,
    ) -> list[Any]:
        return EpisodeService(self._conn).list_episodes(
            project=project or self._project,
            outcome=outcome,
            only_open=only_open,
        )

    # ----- Reflection lifecycle -----

    def promote_reflection(self, *, reflection_id: str) -> None:
        ReflectionService(self._conn).promote_to_general(reflection_id=reflection_id)

    def retire_reflection(self, *, reflection_id: str) -> None:
        ReflectionService(self._conn).retire(reflection_id=reflection_id)
```

- [ ] **Step 5: Run tests; verify pass**

Run: `uv run pytest tests/storage/test_sqlite_backend.py -v`
Expected: all pass (including pre-existing tests from Task 3).

- [ ] **Step 6: Run services tests; confirm no regression**

Run: `uv run pytest tests/services/ -q --tb=line`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add better_memory/storage/sqlite.py tests/storage/test_sqlite_backend.py
git commit -m "feat(storage): SqliteBackend wraps semantic CRUD + episode + reflection lifecycle"
```

---

### Task 5a: Extract `list_session_exposures` into `SessionBootstrapService`

**Why this task exists:** The MCP `memory.list_session_exposures` tool's handler at `better_memory/mcp/server.py:1423` is implemented inline (it directly queries the `session_memory_exposure` table). For the StorageBackend wrapper to call it via `SessionBootstrapService.list_session_exposures(session_id=...)`, the method must exist on the service. Extracting it now keeps Task 5b a pure delegation wrapper.

**Files:**
- Modify: `better_memory/services/session_bootstrap.py`
- Modify: `better_memory/mcp/server.py` (replace the inline body of the handler at line 1423 with a call to the new method)
- Modify or create: `tests/services/test_session_bootstrap.py`

- [ ] **Step 1: Read the inline handler to capture exact semantics**

Open `better_memory/mcp/server.py` around line 1423. Identify the full block under `if name == "memory.list_session_exposures":`. Copy its SQL, transformation logic, and the exact return shape (it returns a `dict[str, Any]` with keys like `session_id`, `exposures: list[...]`).

- [ ] **Step 2: Write the failing test**

Append to `tests/services/test_session_bootstrap.py` (or create it if missing — mirror the conftest pattern used by other service tests):

```python
def test_list_session_exposures_returns_envelope(memory_conn) -> None:
    from better_memory.services.session_bootstrap import SessionBootstrapService

    svc = SessionBootstrapService(memory_conn)
    result = svc.list_session_exposures(session_id="test-session-no-data")

    assert isinstance(result, dict)
    assert result.get("session_id") == "test-session-no-data"
    assert "exposures" in result
    assert result["exposures"] == []


def test_list_session_exposures_returns_seeded_row(memory_conn) -> None:
    from better_memory.services.session_bootstrap import SessionBootstrapService

    # Seed one exposure row. Adapt the INSERT to match the actual schema —
    # check `PRAGMA table_info(session_memory_exposure)` if column names differ.
    memory_conn.execute(
        "INSERT INTO session_memory_exposure "
        "(session_id, kind, memory_id, exposed_at, source) VALUES "
        "('s1', 'reflection', 'r1', '2026-05-25T12:00:00Z', 'bootstrap')"
    )
    memory_conn.commit()

    svc = SessionBootstrapService(memory_conn)
    result = svc.list_session_exposures(session_id="s1")
    assert len(result["exposures"]) == 1
    assert result["exposures"][0]["id"] == "r1"
```

(If the test fixtures need a different shape — column names, additional fields, etc. — adjust to match the MCP handler's verbatim output.)

- [ ] **Step 3: Run test; verify failure**

Run: `uv run pytest tests/services/test_session_bootstrap.py::test_list_session_exposures_returns_envelope -v`
Expected: `AttributeError: 'SessionBootstrapService' object has no attribute 'list_session_exposures'`.

- [ ] **Step 4: Add `list_session_exposures` to `SessionBootstrapService`**

Append a method to the service class in `better_memory/services/session_bootstrap.py`. Copy the SQL + post-processing from the MCP handler (Step 1) verbatim into the method body. Signature:

```python
def list_session_exposures(self, *, session_id: str) -> dict[str, Any]:
    """List unrated memory exposures for the given session.

    Extracted from the inline MCP handler so StorageBackend.list_session_exposures
    can delegate. Behaviour preserving — return shape matches the MCP tool's payload.
    """
    # ... paste the SQL + transform from server.py:1423 here ...
```

- [ ] **Step 5: Replace the MCP handler body with a call to the service**

In `better_memory/mcp/server.py`, locate the `if name == "memory.list_session_exposures":` branch (around line 1423). Replace its body with:

```python
elif name == "memory.list_session_exposures":
    payload = bootstrap_service.list_session_exposures(
        session_id=arguments.get("session_id") or _session_id(),
    )
    return [TextContent(type="text", text=json.dumps(payload))]
```

(Use the existing `bootstrap_service` instance from the surrounding scope, and the existing `_session_id()` helper used by adjacent branches. Read the file to confirm the exact pattern before writing — branch styles may differ.)

- [ ] **Step 6: Run the new service test; verify pass**

Run: `uv run pytest tests/services/test_session_bootstrap.py -v`
Expected: 2 passed.

- [ ] **Step 7: Run MCP tests; verify no regression of the existing handler**

Run: `uv run pytest tests/mcp/ -q --tb=line`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add better_memory/services/session_bootstrap.py better_memory/mcp/server.py tests/services/test_session_bootstrap.py
git commit -m "refactor(session_bootstrap): extract list_session_exposures from MCP handler"
```

---

### Task 5b: SqliteBackend — synthesis + session bootstrap + ratings

**Files:**
- Modify: `better_memory/storage/sqlite.py`
- Modify: `tests/storage/test_sqlite_backend.py`

**Service delegation table:**

| Protocol method | Service call |
|---|---|
| `session_bootstrap()` | `SessionBootstrapService(conn).bootstrap(session_id=..., source=..., cwd=..., project=...)` |
| `list_session_exposures()` | `SessionBootstrapService(conn).list_session_exposures(session_id=session_id)` — added in 5a |
| `apply_session_ratings()` | `MemoryRatingService(conn).apply_session_ratings(session_id=session_id, ratings=ratings)` |
| `credit_one()` | `MemoryRatingService(conn).credit_one(session_id=session_id, kind=kind, id=id, classification=classification)` |
| `synthesize_next_get_context()` | `ReflectionSynthesisService(conn).get_next_pending_context(project=project)` |
| `synthesize_next_apply()` | `ReflectionSynthesisService(conn).apply_decision(episode_id=episode_id, response=response, project=project)` |

- [ ] **Step 1: Write the failing tests**

Append to `tests/storage/test_sqlite_backend.py`:

```python
def test_session_bootstrap_returns_result(backend) -> None:
    result = backend.session_bootstrap(session_id="test-session", project="testproj")
    # BootstrapResult is a dataclass-like; check a stable attribute / key.
    assert result is not None


def test_list_session_exposures_returns_envelope(backend) -> None:
    result = backend.list_session_exposures(session_id="test-session")
    assert isinstance(result, dict)
    assert result.get("session_id") == "test-session"
    assert "exposures" in result


def test_apply_session_ratings_empty_raises(backend) -> None:
    # Service raises ValueError on empty ratings — the wrapper must surface it.
    with pytest.raises(ValueError):
        backend.apply_session_ratings(session_id="test-session", ratings=[])


def test_credit_one_for_missing_memory_returns_skip(backend) -> None:
    result = backend.credit_one(
        session_id="test-session",
        kind="reflection",
        id="does-not-exist",
        classification="cited",
    )
    assert result is not None  # ApplyOutcome dict-like


def test_synthesize_next_get_context_returns_none_when_no_pending(backend) -> None:
    # Fresh in-memory db — no pending episodes for synthesis.
    assert backend.synthesize_next_get_context(project="testproj") is None
```

- [ ] **Step 2: Pre-implementation grep-verify**

```bash
grep -n "def bootstrap\b\|def list_session_exposures\b" better_memory/services/session_bootstrap.py
grep -n "def apply_session_ratings\b\|def credit_one\b" better_memory/services/memory_rating.py
grep -n "def get_next_pending_context\b\|def apply_decision\b" better_memory/services/reflection.py
```

If `list_session_exposures` is missing, return to **Task 5a**.

- [ ] **Step 3: Run tests; verify failure**

Run: `uv run pytest tests/storage/test_sqlite_backend.py::test_session_bootstrap_returns_result -v`
Expected: `AttributeError: 'SqliteBackend' object has no attribute 'session_bootstrap'`.

- [ ] **Step 4: Implement the methods**

Edit `better_memory/storage/sqlite.py`. Add imports:

```python
from better_memory.services.memory_rating import MemoryRatingService
from better_memory.services.reflection import ReflectionSynthesisService
from better_memory.services.session_bootstrap import SessionBootstrapService
```

Append to the class:

```python
    # ----- Session lifecycle -----

    def session_bootstrap(
        self,
        *,
        session_id: str,
        source: str | None = None,
        cwd: Any | None = None,
        project: str | None = None,
    ) -> Any:
        return SessionBootstrapService(self._conn).bootstrap(
            session_id=session_id,
            source=source,
            cwd=cwd,
            project=project or self._project,
        )

    def list_session_exposures(self, *, session_id: str) -> dict[str, Any]:
        return SessionBootstrapService(self._conn).list_session_exposures(
            session_id=session_id,
        )

    def apply_session_ratings(
        self,
        *,
        session_id: str,
        ratings: list[dict[str, str]],
    ) -> Any:
        return MemoryRatingService(self._conn).apply_session_ratings(
            session_id=session_id, ratings=ratings,
        )

    def credit_one(
        self,
        *,
        session_id: str,
        kind: str,
        id: str,
        classification: str,
    ) -> Any:
        return MemoryRatingService(self._conn).credit_one(
            session_id=session_id, kind=kind, id=id, classification=classification,
        )

    # ----- Synthesis -----

    def synthesize_next_get_context(self, *, project: str) -> Any:
        return ReflectionSynthesisService(self._conn).get_next_pending_context(
            project=project,
        )

    def synthesize_next_apply(
        self,
        *,
        episode_id: str,
        response: Any,
        project: str,
    ) -> Any:
        return ReflectionSynthesisService(self._conn).apply_decision(
            episode_id=episode_id, response=response, project=project,
        )
```

- [ ] **Step 5: Run tests; verify pass**

Run: `uv run pytest tests/storage/test_sqlite_backend.py -v`
Expected: all pass.

- [ ] **Step 6: Confirm protocol full satisfaction**

Run: `uv run pytest tests/storage/test_protocol.py tests/storage/test_sqlite_backend.py -v`
Expected: all pass; `test_sqlite_backend_satisfies_protocol` now exercises every protocol method.

- [ ] **Step 7: Full regression**

Run: `uv run pytest -q --tb=line --junitxml=t5b.xml`
Then check `t5b.xml` for `failures="0"` / `errors="0"` (parse with python if the bash buffer truncates the progress dots).

- [ ] **Step 8: Commit**

```bash
git add better_memory/storage/sqlite.py tests/storage/test_sqlite_backend.py
git commit -m "feat(storage): SqliteBackend wraps synthesis + bootstrap + ratings"
```

---

### Task 6: Backend factory + env-var dispatch

**Files:**
- Create: `better_memory/storage/factory.py`
- Create: `tests/storage/test_factory.py`
- Modify: `better_memory/storage/__init__.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/storage/test_factory.py`:

```python
"""Tests for build_backend dispatch on config.storage_backend."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from better_memory.storage import StorageBackend, SqliteBackend
from better_memory.storage.factory import build_backend


def _config(**overrides):
    """Build a Config-like object with only the storage-backend fields the
    factory reads. Other fields aren't touched by build_backend."""
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class FakeConfig:
        storage_backend: str = "sqlite"
        agentcore_region: str = "eu-west-2"
        agentcore_semantic_memory_id: str | None = None
        agentcore_episodic_memory_id: str | None = None

    return FakeConfig(**overrides)


@pytest.fixture
def memory_conn() -> sqlite3.Connection:
    from better_memory.db.schema import apply_migrations
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    return conn


def test_build_backend_returns_sqlite_for_sqlite_config(memory_conn) -> None:
    cfg = _config()
    backend = build_backend(
        config=cfg,
        memory_conn=memory_conn,
        embedder=MagicMock(),
        session_id="s",
        project="p",
    )
    assert isinstance(backend, SqliteBackend)
    assert isinstance(backend, StorageBackend)


def test_build_backend_raises_for_unknown(memory_conn) -> None:
    cfg = _config(storage_backend="bogus")
    with pytest.raises(ValueError, match="unknown storage_backend"):
        build_backend(
            config=cfg,
            memory_conn=memory_conn,
            embedder=MagicMock(),
            session_id="s",
            project="p",
        )


def test_build_backend_agentcore_raises_until_plan2(memory_conn) -> None:
    cfg = _config(
        storage_backend="agentcore",
        agentcore_semantic_memory_id="mem-sem-abc1234567",
        agentcore_episodic_memory_id="mem-epi-xyz1234567",
    )
    with pytest.raises(NotImplementedError, match="AgentCoreBackend"):
        build_backend(
            config=cfg,
            memory_conn=memory_conn,
            embedder=MagicMock(),
            session_id="s",
            project="p",
        )
```

- [ ] **Step 2: Run tests; verify failure**

Run: `uv run pytest tests/storage/test_factory.py -v`
Expected: ImportError — `factory.py` doesn't exist.

- [ ] **Step 3: Create the factory**

Create `better_memory/storage/factory.py`:

```python
"""Backend factory — picks SqliteBackend or AgentCoreBackend based on config."""

from __future__ import annotations

import sqlite3
from typing import Any, Protocol

from better_memory.storage.protocol import StorageBackend
from better_memory.storage.sqlite import SqliteBackend


class _ConfigLike(Protocol):
    """Structural type — the factory only reads storage_backend."""

    storage_backend: str


def build_backend(
    *,
    config: _ConfigLike,
    memory_conn: sqlite3.Connection,
    embedder: Any,
    session_id: str,
    project: str,
) -> StorageBackend:
    """Construct the StorageBackend implementation appropriate for the config."""
    if config.storage_backend == "sqlite":
        return SqliteBackend(
            memory_conn=memory_conn,
            embedder=embedder,
            session_id=session_id,
            project=project,
        )
    if config.storage_backend == "agentcore":
        raise NotImplementedError(
            "AgentCoreBackend is delivered in Plan 2. "
            "Until then, set BETTER_MEMORY_STORAGE_BACKEND=sqlite."
        )
    raise ValueError(f"unknown storage_backend={config.storage_backend!r}")
```

Update `better_memory/storage/__init__.py`:

```python
"""Storage backend abstraction for better-memory."""

from better_memory.storage.factory import build_backend
from better_memory.storage.protocol import (
    Outcome,
    StorageBackend,
    UseOutcome,
)
from better_memory.storage.sqlite import SqliteBackend

__all__ = [
    "Outcome",
    "SqliteBackend",
    "StorageBackend",
    "UseOutcome",
    "build_backend",
]
```

- [ ] **Step 4: Run tests; verify pass**

Run: `uv run pytest tests/storage/test_factory.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add better_memory/storage/factory.py better_memory/storage/__init__.py tests/storage/test_factory.py
git commit -m "feat(storage): backend factory dispatches on config.storage_backend"
```

---

### Task 7: MCP server backend dispatch + capability-gated tool registration

**Files:**
- Modify: `better_memory/mcp/server.py`
- Create: `tests/mcp/test_server_backend_dispatch.py`

**Pre-task grep-verify (do this first):**

```bash
grep -n "def create_server\b\|def _tool_definitions\b\|async def _call_tool\b\|async def call_tool\b" better_memory/mcp/server.py
grep -n "memory.synthesize_next_get_context\|memory.synthesize_next_apply" better_memory/mcp/server.py
```

Confirm:
- `create_server() -> tuple[Server, ...]` returns a tuple (it already does).
- `_tool_definitions() -> list[Tool]` is the static tool listing.
- Both synthesis tool names appear once in `_tool_definitions` (as Tool() objects) and once in the call handler.

If anything has shifted, update the diff below to reference the actual function names before writing code.

- [ ] **Step 1: Write the failing tests**

Create `tests/mcp/test_server_backend_dispatch.py`:

```python
"""MCP server selects the right StorageBackend and gates synthesis tools."""

from __future__ import annotations

import pytest


def test_synthesis_tools_registered_when_capability_true() -> None:
    from better_memory.mcp.server import _tool_definitions

    tools = _tool_definitions(supports_synthesis=True)
    tool_names = {t.name for t in tools}
    assert "memory.synthesize_next_get_context" in tool_names
    assert "memory.synthesize_next_apply" in tool_names


def test_synthesis_tools_skipped_when_capability_false() -> None:
    from better_memory.mcp.server import _tool_definitions

    tools = _tool_definitions(supports_synthesis=False)
    tool_names = {t.name for t in tools}
    assert "memory.synthesize_next_get_context" not in tool_names
    assert "memory.synthesize_next_apply" not in tool_names


def test_create_server_returns_three_tuple_with_backend(monkeypatch, tmp_path) -> None:
    """create_server returns (server, cleanup, ctx) with ctx.backend set."""
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)

    from better_memory.mcp.server import create_server
    from better_memory.storage import SqliteBackend

    result = create_server()
    assert isinstance(result, tuple) and len(result) == 3
    server, cleanup, ctx = result
    assert isinstance(ctx.backend, SqliteBackend)
```

- [ ] **Step 2: Run tests; verify failure**

Run: `uv run pytest tests/mcp/test_server_backend_dispatch.py -v`
Expected: `TypeError: _tool_definitions() got an unexpected keyword argument 'supports_synthesis'`.

- [ ] **Step 3: Refactor `_tool_definitions()` to gate on capability**

Change the signature of `_tool_definitions()` in `better_memory/mcp/server.py`:

```python
def _tool_definitions(*, supports_synthesis: bool = True) -> list[Tool]:
    tools: list[Tool] = [
        # ... all existing Tool(...) entries EXCEPT memory.synthesize_next_* ...
    ]
    if supports_synthesis:
        tools.extend([
            Tool(name="memory.synthesize_next_get_context", ...),  # existing entry, moved
            Tool(name="memory.synthesize_next_apply", ...),         # existing entry, moved
        ])
    return tools
```

Keep `supports_synthesis: bool = True` as the default so any existing caller that doesn't pass the kwarg still gets the full tool set. Move the two synthesis Tool definitions (currently somewhere in the unconditional list) into the conditional `extend([...])` block. Do NOT change the Tool schema bodies — just move them.

- [ ] **Step 4: Refactor `create_server()` to build a backend and expose it**

Near the top of `server.py` (after imports), add:

```python
from dataclasses import dataclass

@dataclass
class ServerContext:
    backend: "StorageBackend"
    memory_conn: "sqlite3.Connection"
    embedder: Any
```

Inside `create_server()`, after `memory_conn` and `embedder` are constructed (and after `session_id` and `project` are resolved — these are already computed by the existing code; grep for them), add:

```python
from better_memory.storage import build_backend

backend = build_backend(
    config=config,
    memory_conn=memory_conn,
    embedder=embedder,
    session_id=session_id,
    project=project,
)
```

Change the `_tool_definitions()` call site to:

```python
tools = _tool_definitions(supports_synthesis=backend.supports_synthesis)
```

(Search for the existing `_tool_definitions(` call inside `create_server` and replace.)

Change the `return` statement of `create_server` from the current 2-tuple to a 3-tuple:

```python
return server, cleanup, ServerContext(
    backend=backend, memory_conn=memory_conn, embedder=embedder,
)
```

- [ ] **Step 5: Audit and update callers of `create_server()`**

```bash
grep -rn "create_server(" better_memory/ tests/
```

For each caller (most likely `better_memory/mcp/__main__.py` and a handful of MCP tests):
- If the caller only wants the server: change `server, cleanup = create_server()` to `server, cleanup, _ = create_server()`.
- If the caller wants the backend: bind the third element.

- [ ] **Step 6: Run the new tests; verify pass**

Run: `uv run pytest tests/mcp/test_server_backend_dispatch.py -v`
Expected: 3 passed.

- [ ] **Step 7: Run MCP + storage test suites**

Run: `uv run pytest tests/mcp/ tests/storage/ -q --tb=line`
Expected: all pass.

- [ ] **Step 8: Full regression**

Run: `uv run pytest -q --tb=line --junitxml=t7.xml`
Then check the junit file for `failures="0"` and `errors="0"`. The full suite output exceeds the bash buffer; the xml is the source of truth.

- [ ] **Step 9: pyright**

Run: `uv run pyright better_memory tests`
Expected: 0 errors above the pre-Task-2 baseline. If the baseline is unknown, run pyright once at HEAD~ before this task and diff the totals.

- [ ] **Step 10: Commit**

```bash
git add better_memory/mcp/server.py tests/mcp/test_server_backend_dispatch.py
git commit -m "feat(mcp): dispatch on storage_backend; gate synthesis tools on capability"
```

---

## Post-plan verification

```bash
uv run pytest -q --tb=line --junitxml=final.xml
uv run pyright better_memory tests
```

Expected: zero failures, zero pyright errors above the pre-Task-2 baseline.

The repository is now in the state where:

- `BETTER_MEMORY_STORAGE_BACKEND=sqlite` (default) is fully wired through a real protocol abstraction; behaviour is identical to pre-refactor.
- `BETTER_MEMORY_STORAGE_BACKEND=agentcore` fails fast at server startup with `NotImplementedError`, pointing to Plan 2.

## What's NOT in this plan (Plans 2 & 3)

- `AgentCoreBackend` — boto3-based implementation. **Plan 2.**
- Stop hook closure-event integration. **Plan 3.**
- `better-memory agentcore` CLI commands. **Plan 3.**
- Integration tests against real AWS. **Plan 3.**
- README + website docs for the agentcore mode. **Plan 3.**

## Spec coverage check

| Spec section | Covered by |
|---|---|
| Storage layer abstraction (Protocol + dispatch) | Tasks 2, 6, 7 |
| Two AgentCore memory resources | Plan 2 |
| `actorId` encodes project | Plan 2 |
| Namespace shape | Plan 2 |
| Memory record metadata schema | Plan 2 |
| Reflection content shape | Plan 2 |
| Session lifecycle / closure event | Plan 3 |
| `config.py` modify | Task 1 (shipped) |
| `storage/protocol.py` new | Task 2 |
| `storage/sqlite.py` new | Tasks 3, 4, 5b |
| `storage/agentcore.py` new | Plan 2 |
| `storage/session.py` new | Plan 2 (closure event helper) and Plan 3 |
| `hooks/session_close.py` modify | Plan 3 |
| `mcp/server.py` modify | Tasks 5a (extract handler) + 7 (dispatch) |
| `cli/agentcore.py` new | Plan 3 |
| Error handling table | Plans 2 & 3 (per-failure-mode) |
| Testing strategy unit / integration / CLI / smoke | Tasks 2–7 (unit); Plan 3 (integration + CLI + smoke) |
| Documentation | Plan 3 |
| Spike findings — defensive design rules | Plan 2 (encoded in AgentCoreBackend) |
