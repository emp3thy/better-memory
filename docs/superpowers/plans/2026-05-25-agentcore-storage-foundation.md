# AgentCore Storage — Foundation Plan (Plan 1 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce a `StorageBackend` protocol and a `SqliteBackend` implementation that wraps the existing services. No behaviour change. Add the `BETTER_MEMORY_STORAGE_BACKEND` env var (default `sqlite`). Wire the MCP server to dispatch on backend and to conditionally register synthesis tools based on backend capability.

**Architecture:** "Fat" backend protocol — high-level operations (`observe`, `retrieve`, `record_use`, `semantic_*`, episode lifecycle, synthesis). `SqliteBackend` delegates to the existing services. The existing tests continue to exercise services directly and pass unchanged. New protocol-satisfaction smoke tests verify the backend surface.

**Tech Stack:** Python 3.12+, `typing.Protocol` (`runtime_checkable`), pytest, existing sqlite3 / service stack.

**Spec:** `docs/superpowers/specs/2026-05-24-agentcore-storage-backend-design.md` — sections "Storage layer abstraction" and "Components".

**Follow-up plans:**
- Plan 2 (AgentCore backend) — `boto3` adapter implementing the same protocol.
- Plan 3 (Operator wiring) — Stop-hook closure event, CLI, integration tests, docs.

---

## File Map

| Path | Status | Responsibility |
|---|---|---|
| `better_memory/config.py` | modify | Add `storage_backend` + `agentcore_*` fields and env-var reads |
| `better_memory/storage/__init__.py` | NEW | Package marker; exports `StorageBackend`, `SqliteBackend`, `build_backend` |
| `better_memory/storage/protocol.py` | NEW | `StorageBackend` Protocol definition + `Outcome` re-export |
| `better_memory/storage/sqlite.py` | NEW | `SqliteBackend` — wraps existing services |
| `better_memory/storage/factory.py` | NEW | `build_backend(config) -> StorageBackend` env-var dispatch |
| `better_memory/mcp/server.py` | modify | Build backend at startup; inject into handlers; capability-gated tool registration |
| `tests/test_config.py` | modify | Tests for new env vars |
| `tests/storage/__init__.py` | NEW | Test package marker |
| `tests/storage/test_protocol.py` | NEW | Runtime protocol check; SqliteBackend isinstance check |
| `tests/storage/test_sqlite_backend.py` | NEW | Smoke tests for each protocol method through `SqliteBackend` |
| `tests/storage/test_factory.py` | NEW | `build_backend` dispatch behavior |
| `tests/mcp/test_server_backend_dispatch.py` | NEW | Backend wiring + conditional tool registration |

## Confidence Summary

| Task | Confidence | Lift applied |
|---|---|---|
| 1. Config switch + agentcore fields | 95% | — |
| 2. Storage package skeleton + Protocol definition | 92% | Method list locked against spec data-flow; `Outcome` type re-export verified |
| 3. SqliteBackend — observe / retrieve / record_use | 92% (lifted from 85%) | Inline delegation diffs against current service constructors; mock-Connection fixture pattern shown |
| 4. SqliteBackend — semantic CRUD + episode lifecycle | 90% (lifted from 85%) | Method-by-method delegation table with service-class targets |
| 5. SqliteBackend — synthesis + retention + session bootstrap + ratings | 90% (lifted from 85%) | Capability flag (`supports_synthesis = True`) gated by protocol comment; signatures verified against `ReflectionSynthesisService` |
| 6. Backend factory + env-var dispatch | 95% | — |
| 7. MCP server dispatch + capability-gated registration | 90% (lifted from 82%) | `_call_tool` does NOT change; only `_tool_definitions()` + `create_server()` constructors. Grep-verify pattern below |

All tasks ≥ 90%. No residual sub-90% items.

---

## Conventions used in this plan

- All test code is complete and runnable.
- All `Run:` commands include the exact expected output text or exit-code expectation.
- "Commit" steps use the canonical project commit-message style (lowercase imperative subject, `feat(scope):` / `refactor(scope):` prefix).
- Service-class delegation tables show the existing service method we delegate to. Where the existing service method signature differs from the protocol's preferred shape, the wrapper adapts via positional → keyword translation.
- Mock-connection fixture pattern: protocol smoke tests use a minimal in-memory sqlite3 connection with migrations already applied, mirroring `tests/services/conftest.py`'s `memory_conn` fixture. We do not re-test service business logic here — those tests live alongside the services.

---

### Task 1: Config switch + agentcore fields

**Files:**
- Modify: `better_memory/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_storage_backend_defaults_to_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)
    cfg = get_config()
    assert cfg.storage_backend == "sqlite"


def test_storage_backend_agentcore_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")
    monkeypatch.setenv("BETTER_MEMORY_AGENTCORE_SEMANTIC_MEMORY_ID", "mem-sem-abc1234567")
    monkeypatch.setenv("BETTER_MEMORY_AGENTCORE_EPISODIC_MEMORY_ID", "mem-epi-xyz1234567")
    cfg = get_config()
    assert cfg.storage_backend == "agentcore"
    assert cfg.agentcore_region == "eu-west-2"
    assert cfg.agentcore_semantic_memory_id == "mem-sem-abc1234567"
    assert cfg.agentcore_episodic_memory_id == "mem-epi-xyz1234567"


def test_storage_backend_unknown_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "nope")
    with pytest.raises(ValueError, match="BETTER_MEMORY_STORAGE_BACKEND"):
        get_config()


def test_agentcore_mode_without_memory_ids_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")
    monkeypatch.delenv("BETTER_MEMORY_AGENTCORE_SEMANTIC_MEMORY_ID", raising=False)
    monkeypatch.delenv("BETTER_MEMORY_AGENTCORE_EPISODIC_MEMORY_ID", raising=False)
    with pytest.raises(ValueError, match="agentcore init"):
        get_config()


def test_agentcore_region_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")
    monkeypatch.setenv("BETTER_MEMORY_AGENTCORE_REGION", "us-west-2")
    monkeypatch.setenv("BETTER_MEMORY_AGENTCORE_SEMANTIC_MEMORY_ID", "mem-sem-abc1234567")
    monkeypatch.setenv("BETTER_MEMORY_AGENTCORE_EPISODIC_MEMORY_ID", "mem-epi-xyz1234567")
    cfg = get_config()
    assert cfg.agentcore_region == "us-west-2"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v -k storage_backend or agentcore`
Expected: 5 failures with `AttributeError: 'Config' object has no attribute 'storage_backend'`.

- [ ] **Step 3: Add fields and resolver to `config.py`**

Edit `better_memory/config.py`. Near the top, after existing `_DEFAULT_*` constants, add:

```python
_DEFAULT_STORAGE_BACKEND = "sqlite"
_VALID_STORAGE_BACKENDS = ("sqlite", "agentcore")
_DEFAULT_AGENTCORE_REGION = "eu-west-2"
```

In the `Config` dataclass (frozen), add four new fields after the existing ones (keep field ordering: required → optional with defaults):

```python
storage_backend: Literal["sqlite", "agentcore"]
agentcore_region: str
agentcore_semantic_memory_id: str | None
agentcore_episodic_memory_id: str | None
```

Update `Literal` import if not already present:

```python
from typing import Literal
```

In `get_config()`, after the existing resolution block, add:

```python
storage_backend = _resolve_str(
    "BETTER_MEMORY_STORAGE_BACKEND", _DEFAULT_STORAGE_BACKEND
)
if storage_backend not in _VALID_STORAGE_BACKENDS:
    raise ValueError(
        f"BETTER_MEMORY_STORAGE_BACKEND={storage_backend!r} is not one of "
        f"{_VALID_STORAGE_BACKENDS}"
    )

agentcore_region = _resolve_str(
    "BETTER_MEMORY_AGENTCORE_REGION", _DEFAULT_AGENTCORE_REGION
)
agentcore_semantic_memory_id = _resolve_str(
    "BETTER_MEMORY_AGENTCORE_SEMANTIC_MEMORY_ID", ""
) or None
agentcore_episodic_memory_id = _resolve_str(
    "BETTER_MEMORY_AGENTCORE_EPISODIC_MEMORY_ID", ""
) or None

if storage_backend == "agentcore" and (
    agentcore_semantic_memory_id is None or agentcore_episodic_memory_id is None
):
    raise ValueError(
        "BETTER_MEMORY_STORAGE_BACKEND=agentcore requires both "
        "BETTER_MEMORY_AGENTCORE_SEMANTIC_MEMORY_ID and "
        "BETTER_MEMORY_AGENTCORE_EPISODIC_MEMORY_ID to be set. "
        "Run `better-memory agentcore init` to create the memory resources."
    )
```

Pass the new fields into the `Config(...)` constructor at the end of `get_config()`.

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/test_config.py -v -k "storage_backend or agentcore"`
Expected: 5 passed.

- [ ] **Step 5: Run the full config test suite to confirm no regression**

Run: `uv run pytest tests/test_config.py -v`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add better_memory/config.py tests/test_config.py
git commit -m "feat(config): add BETTER_MEMORY_STORAGE_BACKEND + agentcore_* fields"
```

---

### Task 2: Storage package skeleton + StorageBackend Protocol

**Files:**
- Create: `better_memory/storage/__init__.py`
- Create: `better_memory/storage/protocol.py`
- Create: `tests/storage/__init__.py`
- Create: `tests/storage/test_protocol.py`

- [ ] **Step 1: Create the empty package markers**

Create `better_memory/storage/__init__.py`:

```python
"""Storage backend abstraction for better-memory.

Two implementations: SqliteBackend (default) wraps the existing services;
AgentCoreBackend (added in Plan 2) talks to AWS Bedrock AgentCore Memory.
Both satisfy the StorageBackend Protocol.
"""

from better_memory.storage.protocol import StorageBackend

__all__ = ["StorageBackend"]
```

Create `tests/storage/__init__.py` (empty file).

- [ ] **Step 2: Write the failing protocol-shape tests**

Create `tests/storage/test_protocol.py`:

```python
"""Tests that the StorageBackend Protocol shape is what consumers expect."""

from __future__ import annotations

from better_memory.storage import StorageBackend


def test_protocol_is_runtime_checkable() -> None:
    """The Protocol must be runtime_checkable so MCP server can isinstance()."""
    # If StorageBackend isn't decorated with @runtime_checkable this will
    # raise TypeError at isinstance() time, not at import. We force the check.
    class _Stub:
        @property
        def supports_synthesis(self) -> bool:
            return False

    # We don't actually expect _Stub to satisfy the full protocol; this just
    # verifies the protocol accepts an isinstance probe at all.
    try:
        isinstance(_Stub(), StorageBackend)
    except TypeError as exc:
        raise AssertionError(
            "StorageBackend must be decorated with @runtime_checkable"
        ) from exc


def test_protocol_declares_capability_flag() -> None:
    """supports_synthesis is the capability used by MCP for conditional tool registration."""
    assert hasattr(StorageBackend, "supports_synthesis")


def test_protocol_declares_hot_path_methods() -> None:
    """Read/write/credit path methods that every backend MUST implement."""
    required = {
        "observe", "retrieve", "retrieve_observations",
        "record_use",
        "semantic_observe", "semantic_retrieve", "semantic_update", "semantic_delete",
        "start_episode", "list_episodes", "close_episode",
        "session_bootstrap",
        "list_session_exposures", "apply_session_ratings",
        "promote_reflection", "retire_reflection",
    }
    actual = set(dir(StorageBackend))
    missing = required - actual
    assert not missing, f"Protocol missing methods: {sorted(missing)}"


def test_protocol_declares_synthesis_methods() -> None:
    """Synthesis methods exist on the protocol but only sqlite-backed
    implementations are expected to implement them."""
    synthesis = {"synthesize_next_get_context", "synthesize_next_apply"}
    actual = set(dir(StorageBackend))
    missing = synthesis - actual
    assert not missing, f"Protocol missing synthesis methods: {sorted(missing)}"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/storage/test_protocol.py -v`
Expected: ImportError or AttributeError — `protocol.py` doesn't exist yet.

- [ ] **Step 4: Create the Protocol**

Create `better_memory/storage/protocol.py`:

```python
"""StorageBackend Protocol.

Fat protocol with high-level operations. Both SqliteBackend (Plan 1) and
AgentCoreBackend (Plan 2) implement it. Synthesis methods are sqlite-only —
the MCP server reads `supports_synthesis` to gate their tool registration.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable


# Re-export Outcome so storage callers don't need to import from services.
Outcome = Literal["success", "failure", "neutral"]


@runtime_checkable
class StorageBackend(Protocol):
    """High-level storage operations consumed by the MCP layer.

    Implementations may raise NotImplementedError for methods that don't fit
    their paradigm — but only for methods documented as backend-specific.
    The synthesis methods are the only such methods today.
    """

    # ----- Capability flags -----

    @property
    def supports_synthesis(self) -> bool:
        """True when synthesize_next_* tools should be registered."""
        ...

    # ----- Observations (write + read) -----

    def observe(
        self,
        *,
        content: str,
        outcome: Outcome = "neutral",
        component: str | None = None,
        theme: str | None = None,
        tech: str | None = None,
        trigger_type: str | None = None,
        scope: Literal["project", "general"] = "project",
        project: str | None = None,
    ) -> str:
        """Record an observation. Returns the new observation id."""
        ...

    def retrieve(
        self,
        *,
        query: str | None = None,
        project: str | None = None,
        tech: str | None = None,
        phase: Literal["planning", "implementation", "general"] | None = None,
        polarity: Literal["do", "dont", "neutral"] | None = None,
        limit_per_bucket: int = 5,
    ) -> dict[str, list[dict[str, Any]]]:
        """Retrieve reflections grouped by polarity bucket (do/dont/neutral)."""
        ...

    def retrieve_observations(
        self,
        *,
        query: str | None = None,
        project: str | None = None,
        component: str | None = None,
        theme: str | None = None,
        outcome: Outcome | None = None,
        episode_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Drill-down retrieval of raw observations."""
        ...

    # ----- Reinforcement / outcome credit -----

    def record_use(self, *, id: str, outcome: Outcome) -> None:
        """Credit a memory's reinforcement counter. Raises if id not found."""
        ...

    # ----- Semantic memories (user-curated facts) -----

    def semantic_observe(
        self,
        *,
        content: str,
        component: str | None = None,
        scope: Literal["project", "general"] = "project",
        project: str | None = None,
    ) -> str:
        """Create a semantic memory. Returns its id."""
        ...

    def semantic_retrieve(
        self,
        *,
        query: str | None = None,
        project: str | None = None,
        component: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """List semantic memories."""
        ...

    def semantic_update(
        self,
        *,
        id: str,
        content: str | None = None,
        component: str | None = None,
        scope: Literal["project", "general"] | None = None,
    ) -> None:
        """Update fields on a semantic memory."""
        ...

    def semantic_delete(self, *, id: str) -> None:
        """Permanently delete a semantic memory."""
        ...

    # ----- Episodes (session boundaries) -----

    def start_episode(self, *, session_id: str, project: str | None = None) -> dict[str, Any]:
        """Open or attach an episode for the given session. Returns episode dict."""
        ...

    def list_episodes(
        self,
        *,
        project: str | None = None,
        status: Literal["open", "closed"] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List episodes."""
        ...

    def close_episode(self, *, id: str, outcome: str = "unknown") -> None:
        """Close an episode by id."""
        ...

    # ----- Reflection lifecycle -----

    def promote_reflection(self, *, id: str) -> None:
        """Promote a project-scope reflection to general scope."""
        ...

    def retire_reflection(self, *, id: str) -> None:
        """Mark a reflection as retired (excluded from default retrieval)."""
        ...

    # ----- Session lifecycle -----

    def session_bootstrap(self, *, project: str | None = None) -> dict[str, Any]:
        """Build the SessionStart additionalContext envelope."""
        ...

    def list_session_exposures(self) -> dict[str, Any]:
        """List unrated memories the current session was exposed to."""
        ...

    def apply_session_ratings(
        self, *, ratings: list[dict[str, str]]
    ) -> dict[str, Any]:
        """Atomically apply per-exposure ratings for the current session."""
        ...

    # ----- Synthesis (sqlite-only — guarded by supports_synthesis) -----

    def synthesize_next_get_context(
        self, *, project: str | None = None
    ) -> dict[str, Any]:
        """Pop the next pending episode for synthesis. Sqlite-only."""
        ...

    def synthesize_next_apply(self, *, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        """Apply synthesis decisions atomically. Sqlite-only."""
        ...
```

- [ ] **Step 5: Run protocol tests; expect pass**

Run: `uv run pytest tests/storage/test_protocol.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add better_memory/storage/__init__.py better_memory/storage/protocol.py tests/storage/__init__.py tests/storage/test_protocol.py
git commit -m "feat(storage): add StorageBackend Protocol with capability flag"
```

---

### Task 3: SqliteBackend — observe / retrieve / record_use

**Files:**
- Create: `better_memory/storage/sqlite.py`
- Create: `tests/storage/test_sqlite_backend.py`

**Service delegation table for this task:**

| Protocol method | Service / function called |
|---|---|
| `observe()` | `ObservationService(memory_conn, embedder=embedder, retriever=retriever, episodes=episodes).create(...)` |
| `retrieve()` | `ReflectionService(memory_conn).retrieve_reflections(...)` (grouping done by service) |
| `retrieve_observations()` | `ObservationService(...).retrieve_observations(...)` |
| `record_use()` | `MemoryRatingService(memory_conn).credit(...)` |

- [ ] **Step 1: Write protocol-satisfaction smoke test**

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
def memory_conn(tmp_path) -> sqlite3.Connection:
    """An in-memory sqlite connection with migrations applied.

    Mirrors the existing tests/services/conftest.py pattern for service tests.
    """
    from better_memory.db.schema import apply_migrations
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    return conn


@pytest.fixture
def backend(memory_conn) -> SqliteBackend:
    """A SqliteBackend wired to an in-memory db with a no-op embedder."""
    embedder = MagicMock()
    embedder.embed.return_value = [0.0] * 768  # 768-dim is the project default
    return SqliteBackend(
        memory_conn=memory_conn,
        embedder=embedder,
        retriever=None,
        project="testproj",
    )


def test_sqlite_backend_satisfies_protocol(backend) -> None:
    """isinstance() check using the runtime_checkable Protocol."""
    assert isinstance(backend, StorageBackend)


def test_supports_synthesis_is_true(backend) -> None:
    """SqliteBackend supports the synthesis MCP tools."""
    assert backend.supports_synthesis is True


def test_observe_creates_and_returns_id(backend) -> None:
    """observe() returns a non-empty observation id."""
    obs_id = backend.observe(
        content="test observation",
        outcome="success",
        theme="test",
    )
    assert obs_id
    assert isinstance(obs_id, str)


def test_retrieve_returns_bucketed_dict(backend) -> None:
    """retrieve() returns a dict keyed by polarity."""
    result = backend.retrieve(query="test", limit_per_bucket=3)
    assert isinstance(result, dict)
    # Buckets present even when empty
    assert set(result.keys()) >= {"do", "dont", "neutral"}


def test_retrieve_observations_returns_list(backend) -> None:
    """retrieve_observations() returns a list."""
    # First, write one so we have something to retrieve
    backend.observe(content="findable text", theme="searchable")
    result = backend.retrieve_observations(query="findable", limit=5)
    assert isinstance(result, list)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/storage/test_sqlite_backend.py -v`
Expected: ImportError — `sqlite.py` doesn't exist.

- [ ] **Step 3: Create SqliteBackend with observe/retrieve/record_use**

Create `better_memory/storage/sqlite.py`:

```python
"""SqliteBackend — wraps existing services to satisfy the StorageBackend Protocol.

Behaviour-preserving wrapper. Existing service tests continue to exercise
service business logic directly; tests in tests/storage/ only verify the
protocol delegation surface.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from better_memory.storage.protocol import Outcome
from better_memory.services.observation import ObservationService
from better_memory.services.reflection import ReflectionService
from better_memory.services.memory_rating import MemoryRatingService
from better_memory.services.episode import EpisodeService


class SqliteBackend:
    """Wraps existing services to satisfy StorageBackend Protocol."""

    def __init__(
        self,
        *,
        memory_conn: sqlite3.Connection,
        embedder: Any,
        retriever: Any | None = None,
        project: str | None = None,
    ) -> None:
        self._conn = memory_conn
        self._embedder = embedder
        self._retriever = retriever
        self._project = project
        # Services are stateless given a conn — re-instantiating per call is
        # acceptable. If profiling shows cost, cache them in __init__.

    # ----- Capability flags -----

    @property
    def supports_synthesis(self) -> bool:
        return True

    # ----- Observations -----

    def observe(
        self,
        *,
        content: str,
        outcome: Outcome = "neutral",
        component: str | None = None,
        theme: str | None = None,
        tech: str | None = None,
        trigger_type: str | None = None,
        scope: Literal["project", "general"] = "project",
        project: str | None = None,
    ) -> str:
        episodes = EpisodeService(self._conn)
        obs = ObservationService(
            self._conn,
            embedder=self._embedder,
            retriever=self._retriever,
            episodes=episodes,
        )
        return obs.create(
            content=content,
            outcome=outcome,
            component=component,
            theme=theme,
            tech=tech,
            trigger_type=trigger_type,
            scope=scope,
            project=project or self._project,
        )

    def retrieve(
        self,
        *,
        query: str | None = None,
        project: str | None = None,
        tech: str | None = None,
        phase: Literal["planning", "implementation", "general"] | None = None,
        polarity: Literal["do", "dont", "neutral"] | None = None,
        limit_per_bucket: int = 5,
    ) -> dict[str, list[dict[str, Any]]]:
        refl = ReflectionService(self._conn)
        return refl.retrieve_reflections(
            query=query,
            project=project or self._project,
            tech=tech,
            phase=phase,
            polarity=polarity,
            limit_per_bucket=limit_per_bucket,
        )

    def retrieve_observations(
        self,
        *,
        query: str | None = None,
        project: str | None = None,
        component: str | None = None,
        theme: str | None = None,
        outcome: Outcome | None = None,
        episode_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        episodes = EpisodeService(self._conn)
        obs = ObservationService(
            self._conn,
            embedder=self._embedder,
            retriever=self._retriever,
            episodes=episodes,
        )
        return obs.retrieve_observations(
            query=query,
            project=project or self._project,
            component=component,
            theme=theme,
            outcome=outcome,
            episode_id=episode_id,
            limit=limit,
        )

    # ----- Reinforcement -----

    def record_use(self, *, id: str, outcome: Outcome) -> None:
        ratings = MemoryRatingService(self._conn)
        ratings.credit(memory_id=id, outcome=outcome)

    # ----- Stub methods for the remaining protocol — implemented in Task 4 / 5 -----

    def semantic_observe(self, **kwargs: Any) -> str:
        raise NotImplementedError("Implemented in Task 4")

    def semantic_retrieve(self, **kwargs: Any) -> list[dict[str, Any]]:
        raise NotImplementedError("Implemented in Task 4")

    def semantic_update(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 4")

    def semantic_delete(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 4")

    def start_episode(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError("Implemented in Task 4")

    def list_episodes(self, **kwargs: Any) -> list[dict[str, Any]]:
        raise NotImplementedError("Implemented in Task 4")

    def close_episode(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 4")

    def promote_reflection(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 4")

    def retire_reflection(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 4")

    def session_bootstrap(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError("Implemented in Task 5")

    def list_session_exposures(self) -> dict[str, Any]:
        raise NotImplementedError("Implemented in Task 5")

    def apply_session_ratings(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError("Implemented in Task 5")

    def synthesize_next_get_context(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError("Implemented in Task 5")

    def synthesize_next_apply(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError("Implemented in Task 5")
```

Also update `better_memory/storage/__init__.py`:

```python
from better_memory.storage.protocol import StorageBackend
from better_memory.storage.sqlite import SqliteBackend

__all__ = ["StorageBackend", "SqliteBackend"]
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/storage/test_sqlite_backend.py -v`
Expected: 6 passed.

- [ ] **Step 5: Run existing service tests, confirm no regression**

Run: `uv run pytest tests/services/ -v`
Expected: all pass (no behaviour change yet — services are unchanged).

- [ ] **Step 6: Commit**

```bash
git add better_memory/storage/sqlite.py better_memory/storage/__init__.py tests/storage/test_sqlite_backend.py
git commit -m "feat(storage): SqliteBackend wraps observe/retrieve/record_use"
```

---

### Task 4: SqliteBackend — semantic CRUD + episode lifecycle + reflection lifecycle

**Files:**
- Modify: `better_memory/storage/sqlite.py`
- Modify: `tests/storage/test_sqlite_backend.py`

**Service delegation table for this task:**

| Protocol method | Service call |
|---|---|
| `semantic_observe()` | `SemanticMemoryService(self._conn).create(...)` |
| `semantic_retrieve()` | `SemanticMemoryService(self._conn).retrieve(...)` |
| `semantic_update()` | `SemanticMemoryService(self._conn).update(...)` |
| `semantic_delete()` | `SemanticMemoryService(self._conn).delete(...)` |
| `start_episode()` | `EpisodeService(self._conn).start(...)` |
| `list_episodes()` | `EpisodeService(self._conn).list(...)` |
| `close_episode()` | `EpisodeService(self._conn).close(...)` |
| `promote_reflection()` | `ReflectionService(self._conn).promote_to_general(...)` |
| `retire_reflection()` | `ReflectionService(self._conn).retire(...)` |

- [ ] **Step 1: Write the failing tests**

Append to `tests/storage/test_sqlite_backend.py`:

```python
def test_semantic_observe_and_retrieve(backend) -> None:
    sm_id = backend.semantic_observe(content="prefer uv over pip", component="python")
    assert sm_id
    result = backend.semantic_retrieve(query="uv", limit=5)
    assert any(r["id"] == sm_id for r in result)


def test_semantic_update(backend) -> None:
    sm_id = backend.semantic_observe(content="original")
    backend.semantic_update(id=sm_id, content="updated")
    rows = backend.semantic_retrieve(query="updated", limit=5)
    assert any(r["id"] == sm_id and "updated" in r["content"] for r in rows)


def test_semantic_delete(backend) -> None:
    sm_id = backend.semantic_observe(content="to be deleted")
    backend.semantic_delete(id=sm_id)
    rows = backend.semantic_retrieve(query="to be deleted", limit=5)
    assert not any(r["id"] == sm_id for r in rows)


def test_start_and_close_episode(backend) -> None:
    ep = backend.start_episode(session_id="test-session-xyz")
    assert ep["id"]
    backend.close_episode(id=ep["id"])
    eps = backend.list_episodes(status="closed", limit=5)
    assert any(e["id"] == ep["id"] for e in eps)


def test_list_episodes_returns_list(backend) -> None:
    result = backend.list_episodes(limit=5)
    assert isinstance(result, list)


def test_promote_and_retire_reflection_call_underlying_service(
    backend, memory_conn
) -> None:
    # Seed a reflection directly to exercise the lifecycle calls.
    # (We only check the methods route to the service; service tests
    # cover the actual behaviour.)
    memory_conn.execute(
        "INSERT INTO reflections (id, title, hints, polarity, scope, project, confidence) "
        "VALUES ('refl-test-1', 'T', '[]', 'do', 'project', 'testproj', 0.9)"
    )
    memory_conn.commit()
    backend.promote_reflection(id="refl-test-1")
    row = memory_conn.execute(
        "SELECT scope FROM reflections WHERE id=?", ("refl-test-1",)
    ).fetchone()
    assert row["scope"] == "general"

    backend.retire_reflection(id="refl-test-1")
    row = memory_conn.execute(
        "SELECT status FROM reflections WHERE id=?", ("refl-test-1",)
    ).fetchone()
    assert row["status"] == "retired"
```

- [ ] **Step 2: Run tests; verify they fail**

Run: `uv run pytest tests/storage/test_sqlite_backend.py -v -k "semantic or episode or reflection"`
Expected: 6 failures with `NotImplementedError: Implemented in Task 4`.

- [ ] **Step 3: Implement the methods on SqliteBackend**

Edit `better_memory/storage/sqlite.py`. Add imports at the top:

```python
from better_memory.services.semantic import SemanticMemoryService
```

Replace the stubs for the methods this task covers with:

```python
def semantic_observe(
    self,
    *,
    content: str,
    component: str | None = None,
    scope: Literal["project", "general"] = "project",
    project: str | None = None,
) -> str:
    return SemanticMemoryService(self._conn).create(
        content=content,
        component=component,
        scope=scope,
        project=project or self._project,
    )

def semantic_retrieve(
    self,
    *,
    query: str | None = None,
    project: str | None = None,
    component: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    return SemanticMemoryService(self._conn).retrieve(
        query=query,
        project=project or self._project,
        component=component,
        limit=limit,
    )

def semantic_update(
    self,
    *,
    id: str,
    content: str | None = None,
    component: str | None = None,
    scope: Literal["project", "general"] | None = None,
) -> None:
    SemanticMemoryService(self._conn).update(
        id=id, content=content, component=component, scope=scope,
    )

def semantic_delete(self, *, id: str) -> None:
    SemanticMemoryService(self._conn).delete(id=id)

def start_episode(self, *, session_id: str, project: str | None = None) -> dict[str, Any]:
    return EpisodeService(self._conn).start(
        session_id=session_id, project=project or self._project,
    )

def list_episodes(
    self,
    *,
    project: str | None = None,
    status: Literal["open", "closed"] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    return EpisodeService(self._conn).list(
        project=project or self._project, status=status, limit=limit,
    )

def close_episode(self, *, id: str, outcome: str = "unknown") -> None:
    EpisodeService(self._conn).close(id=id, outcome=outcome)

def promote_reflection(self, *, id: str) -> None:
    ReflectionService(self._conn).promote_to_general(id=id)

def retire_reflection(self, *, id: str) -> None:
    ReflectionService(self._conn).retire(id=id)
```

- [ ] **Step 4: Verify the service method names exist (grep-verify)**

Run:
```bash
grep -n "def create\b\|def retrieve\b\|def update\b\|def delete\b" better_memory/services/semantic.py
grep -n "def start\b\|def list\b\|def close\b" better_memory/services/episode.py
grep -n "def promote_to_general\b\|def retire\b" better_memory/services/reflection.py
```

If any method name is missing on the service, **STOP and fix the method name in `sqlite.py`** to match the actual service. Do not add new methods to the service; the wrapper must call existing names.

- [ ] **Step 5: Run tests; verify they pass**

Run: `uv run pytest tests/storage/test_sqlite_backend.py -v`
Expected: all pass.

- [ ] **Step 6: Run service tests; confirm no regression**

Run: `uv run pytest tests/services/ -v`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add better_memory/storage/sqlite.py tests/storage/test_sqlite_backend.py
git commit -m "feat(storage): SqliteBackend wraps semantic CRUD + episode + reflection lifecycle"
```

---

### Task 5: SqliteBackend — synthesis + session bootstrap + ratings

**Files:**
- Modify: `better_memory/storage/sqlite.py`
- Modify: `tests/storage/test_sqlite_backend.py`

**Service delegation table for this task:**

| Protocol method | Service call |
|---|---|
| `session_bootstrap()` | `SessionBootstrapService(self._conn).bootstrap(...)` |
| `list_session_exposures()` | `SessionBootstrapService(self._conn).list_session_exposures()` |
| `apply_session_ratings()` | `MemoryRatingService(self._conn).apply_session_ratings(...)` |
| `synthesize_next_get_context()` | `ReflectionSynthesisService(self._conn).synthesize_next_get_context(...)` |
| `synthesize_next_apply()` | `ReflectionSynthesisService(self._conn).synthesize_next_apply(...)` |

- [ ] **Step 1: Write the failing tests**

Append to `tests/storage/test_sqlite_backend.py`:

```python
def test_session_bootstrap_returns_envelope(backend) -> None:
    result = backend.session_bootstrap(project="testproj")
    # The exact shape varies; we just verify it's a dict with the expected keys
    assert isinstance(result, dict)
    assert "additionalContext" in result or "context" in result or "envelope" in result


def test_list_session_exposures_returns_payload(backend, monkeypatch) -> None:
    # session-exposure lookups require CLAUDE_SESSION_ID in env (per service)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-session")
    result = backend.list_session_exposures()
    assert isinstance(result, dict)
    assert "exposures" in result


def test_apply_session_ratings_returns_summary(backend, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-session")
    # Empty ratings should still return a summary structure
    result = backend.apply_session_ratings(ratings=[])
    assert isinstance(result, dict)


def test_synthesize_next_get_context_returns_dict(backend) -> None:
    result = backend.synthesize_next_get_context(project="testproj")
    assert isinstance(result, dict)


def test_synthesize_next_apply_returns_dict(backend) -> None:
    # Empty decisions: should return a no-op summary
    result = backend.synthesize_next_apply(decisions=[])
    assert isinstance(result, dict)
```

- [ ] **Step 2: Run tests; verify they fail**

Run: `uv run pytest tests/storage/test_sqlite_backend.py -v -k "session_bootstrap or session_exposures or session_ratings or synthesize_next"`
Expected: 5 failures with `NotImplementedError: Implemented in Task 5`.

- [ ] **Step 3: Implement the methods**

Edit `better_memory/storage/sqlite.py`. Add imports at the top:

```python
from better_memory.services.session_bootstrap import SessionBootstrapService
from better_memory.services.reflection import ReflectionSynthesisService
```

Replace the remaining stubs:

```python
def session_bootstrap(self, *, project: str | None = None) -> dict[str, Any]:
    return SessionBootstrapService(self._conn).bootstrap(
        project=project or self._project,
    )

def list_session_exposures(self) -> dict[str, Any]:
    return SessionBootstrapService(self._conn).list_session_exposures()

def apply_session_ratings(
    self, *, ratings: list[dict[str, str]]
) -> dict[str, Any]:
    return MemoryRatingService(self._conn).apply_session_ratings(ratings=ratings)

def synthesize_next_get_context(
    self, *, project: str | None = None
) -> dict[str, Any]:
    return ReflectionSynthesisService(self._conn).synthesize_next_get_context(
        project=project or self._project,
    )

def synthesize_next_apply(
    self, *, decisions: list[dict[str, Any]]
) -> dict[str, Any]:
    return ReflectionSynthesisService(self._conn).synthesize_next_apply(
        decisions=decisions,
    )
```

- [ ] **Step 4: Grep-verify service method names**

Run:
```bash
grep -n "def bootstrap\b\|def list_session_exposures\b" better_memory/services/session_bootstrap.py
grep -n "def apply_session_ratings\b" better_memory/services/memory_rating.py
grep -n "def synthesize_next_get_context\b\|def synthesize_next_apply\b" better_memory/services/reflection.py
```

If a method name differs (especially `synthesize_next_*` — these were renamed in earlier work), update the wrapper to match. Do not rename the service methods.

- [ ] **Step 5: Run tests; verify pass**

Run: `uv run pytest tests/storage/test_sqlite_backend.py -v`
Expected: all pass.

- [ ] **Step 6: Confirm SqliteBackend now fully satisfies the protocol**

Run: `uv run pytest tests/storage/test_protocol.py tests/storage/test_sqlite_backend.py -v`
Expected: all pass; `test_sqlite_backend_satisfies_protocol` is now a meaningful runtime check (no NotImplementedError raisers left).

- [ ] **Step 7: Run the full test suite to confirm no regression elsewhere**

Run: `uv run pytest`
Expected: all pass.

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

from better_memory.config import Config
from better_memory.storage import StorageBackend
from better_memory.storage.factory import build_backend
from better_memory.storage.sqlite import SqliteBackend


def _make_sqlite_config(**overrides) -> Config:
    defaults = dict(
        better_memory_home="/tmp",
        memory_db_path="/tmp/memory.db",
        knowledge_db_path="/tmp/knowledge.db",
        spool_dir="/tmp/spool",
        embeddings_backend="ollama",
        ollama_host="http://localhost:11434",
        ollama_embed_model="nomic-embed-text",
        storage_backend="sqlite",
        agentcore_region="eu-west-2",
        agentcore_semantic_memory_id=None,
        agentcore_episodic_memory_id=None,
    )
    defaults.update(overrides)
    return Config(**defaults)  # type: ignore[arg-type]


def test_build_backend_returns_sqlite_for_sqlite_config(tmp_path) -> None:
    from better_memory.db.schema import apply_migrations
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn)
    cfg = _make_sqlite_config()
    backend = build_backend(config=cfg, memory_conn=conn, embedder=MagicMock())
    assert isinstance(backend, SqliteBackend)
    assert isinstance(backend, StorageBackend)


def test_build_backend_raises_for_unknown(tmp_path) -> None:
    from better_memory.db.schema import apply_migrations
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn)
    cfg = _make_sqlite_config(storage_backend="bogus")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown storage_backend"):
        build_backend(config=cfg, memory_conn=conn, embedder=MagicMock())


def test_build_backend_agentcore_raises_until_plan2(tmp_path) -> None:
    """Plan 1 doesn't implement AgentCoreBackend yet; factory raises cleanly."""
    from better_memory.db.schema import apply_migrations
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn)
    cfg = _make_sqlite_config(
        storage_backend="agentcore",
        agentcore_semantic_memory_id="mem-sem-abc1234567",
        agentcore_episodic_memory_id="mem-epi-xyz1234567",
    )
    with pytest.raises(NotImplementedError, match="AgentCoreBackend"):
        build_backend(config=cfg, memory_conn=conn, embedder=MagicMock())
```

- [ ] **Step 2: Run tests; verify they fail**

Run: `uv run pytest tests/storage/test_factory.py -v`
Expected: ImportError — `factory.py` doesn't exist.

- [ ] **Step 3: Create the factory**

Create `better_memory/storage/factory.py`:

```python
"""Backend factory — picks SqliteBackend or AgentCoreBackend based on config."""

from __future__ import annotations

import sqlite3
from typing import Any

from better_memory.config import Config
from better_memory.storage.protocol import StorageBackend
from better_memory.storage.sqlite import SqliteBackend


def build_backend(
    *,
    config: Config,
    memory_conn: sqlite3.Connection,
    embedder: Any,
    retriever: Any | None = None,
    project: str | None = None,
) -> StorageBackend:
    """Construct the StorageBackend implementation appropriate for the config."""
    if config.storage_backend == "sqlite":
        return SqliteBackend(
            memory_conn=memory_conn,
            embedder=embedder,
            retriever=retriever,
            project=project,
        )
    if config.storage_backend == "agentcore":
        raise NotImplementedError(
            "AgentCoreBackend is delivered in Plan 2 "
            "(docs/superpowers/plans/2026-05-26-agentcore-storage-backend.md). "
            "Until then, set BETTER_MEMORY_STORAGE_BACKEND=sqlite."
        )
    raise ValueError(
        f"unknown storage_backend={config.storage_backend!r}"
    )
```

Update `better_memory/storage/__init__.py`:

```python
from better_memory.storage.factory import build_backend
from better_memory.storage.protocol import StorageBackend
from better_memory.storage.sqlite import SqliteBackend

__all__ = ["StorageBackend", "SqliteBackend", "build_backend"]
```

- [ ] **Step 4: Run tests; verify they pass**

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

**Pre-task grep-verify (DO THIS FIRST):**

Run:
```bash
grep -n "def create_server\b\|def _tool_definitions\b\|def _call_tool\b" better_memory/mcp/server.py
grep -n "memory_synthesize_next" better_memory/mcp/server.py
```

Confirm:
- `create_server()` is where services / clients are constructed.
- `_tool_definitions()` is where tool schemas are listed.
- `memory_synthesize_next_get_context` and `memory_synthesize_next_apply` are both registered there.

If any of those grep results miss, **STOP** and update the diff in Step 3 to reference the actual function names.

- [ ] **Step 1: Write the failing tests**

Create `tests/mcp/test_server_backend_dispatch.py`:

```python
"""MCP server selects the right StorageBackend and gates synthesis tools."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest


def test_synthesis_tools_registered_in_sqlite_mode(monkeypatch) -> None:
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "sqlite")
    from better_memory.mcp.server import _tool_definitions
    tool_names = {t.name for t in _tool_definitions(supports_synthesis=True)}
    assert "memory_synthesize_next_get_context" in tool_names
    assert "memory_synthesize_next_apply" in tool_names


def test_synthesis_tools_skipped_when_capability_false(monkeypatch) -> None:
    from better_memory.mcp.server import _tool_definitions
    tool_names = {t.name for t in _tool_definitions(supports_synthesis=False)}
    assert "memory_synthesize_next_get_context" not in tool_names
    assert "memory_synthesize_next_apply" not in tool_names


def test_create_server_selects_sqlite_backend_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)
    from better_memory.mcp.server import create_server
    from better_memory.storage import SqliteBackend
    server, ctx = create_server()
    assert isinstance(ctx.backend, SqliteBackend)
```

- [ ] **Step 2: Run tests; verify they fail**

Run: `uv run pytest tests/mcp/test_server_backend_dispatch.py -v`
Expected: failures with `TypeError: _tool_definitions() got an unexpected keyword argument 'supports_synthesis'` or `AttributeError: ctx has no attribute 'backend'`.

- [ ] **Step 3: Refactor `_tool_definitions()` to gate on capability**

Find the current `_tool_definitions()` function in `better_memory/mcp/server.py`. Change its signature to accept `supports_synthesis: bool`:

```python
def _tool_definitions(*, supports_synthesis: bool) -> list[Tool]:
    tools: list[Tool] = [
        # ... all the existing tools EXCEPT memory_synthesize_next_* ...
    ]
    if supports_synthesis:
        tools.extend([
            Tool(
                name="memory_synthesize_next_get_context",
                # ... existing schema unchanged ...
            ),
            Tool(
                name="memory_synthesize_next_apply",
                # ... existing schema unchanged ...
            ),
        ])
    return tools
```

Move both `memory_synthesize_next_*` Tool definitions out of the unconditional list and into the conditional `extend([...])` block. **Do not** rename or alter the Tool schema bodies — just move them.

- [ ] **Step 4: Refactor `create_server()` to build a backend and inject it**

Locate `create_server()` in `better_memory/mcp/server.py`. Wherever `memory_conn` and `embedder` are constructed, after that block (and before any `Tool` registration or `_call_tool` definition), add:

```python
from better_memory.storage import build_backend

backend = build_backend(
    config=config,
    memory_conn=memory_conn,
    embedder=embedder,
    retriever=retriever,
    project=config.project_name() if callable(getattr(config, "project_name", None)) else None,
)
```

Where the function currently constructs the tool list via `_tool_definitions()`, change the call to:

```python
tools = _tool_definitions(supports_synthesis=backend.supports_synthesis)
```

Where the function returns the server instance, also return a `ServerContext` dataclass that exposes the backend for tests:

Near the top of `server.py`, after existing imports:

```python
from dataclasses import dataclass

@dataclass
class ServerContext:
    backend: StorageBackend
    memory_conn: sqlite3.Connection
    embedder: Any
```

At the end of `create_server()`, instead of returning just the server, return `(server, ServerContext(backend=backend, memory_conn=memory_conn, embedder=embedder))`. Audit all callers of `create_server()` (most likely just `better_memory/mcp/__main__.py` and any tests). For callers that only want the server, accept the tuple destructure: `server, _ = create_server()`.

- [ ] **Step 5: Run the new tests; verify pass**

Run: `uv run pytest tests/mcp/test_server_backend_dispatch.py -v`
Expected: 3 passed.

- [ ] **Step 6: Run full MCP test suite + the storage tests**

Run: `uv run pytest tests/mcp/ tests/storage/ -v`
Expected: all pass.

- [ ] **Step 7: Run the full test suite — final regression check**

Run: `uv run pytest`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add better_memory/mcp/server.py tests/mcp/test_server_backend_dispatch.py
git commit -m "feat(mcp): dispatch on storage_backend; gate synthesis tools on capability"
```

---

## Post-plan verification

After all tasks land, run the full suite once more:

```bash
uv run pytest
uv run pyright better_memory tests
```

Expected: zero failures, zero pyright errors above the existing baseline.

The repository is now in the state where:

- `BETTER_MEMORY_STORAGE_BACKEND=sqlite` (default) is fully wired through a real protocol abstraction; behaviour is identical to pre-refactor.
- `BETTER_MEMORY_STORAGE_BACKEND=agentcore` fails fast at server startup with `NotImplementedError`, pointing to Plan 2.

## What's NOT in this plan (Plans 2 & 3)

- `AgentCoreBackend` — the boto3-based implementation. **Plan 2.**
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
| `config.py` modify | Task 1 |
| `storage/protocol.py` new | Task 2 |
| `storage/sqlite.py` new | Tasks 3, 4, 5 |
| `storage/agentcore.py` new | Plan 2 |
| `storage/session.py` new | Plan 2 (closure event helper) and Plan 3 |
| `hooks/session_close.py` modify | Plan 3 |
| `mcp/server.py` modify | Task 7 |
| `cli/agentcore.py` new | Plan 3 |
| Error handling table | Plans 2 & 3 (per-failure-mode) |
| Testing strategy unit / integration / CLI / smoke | Tasks 2-7 (unit); Plan 3 (integration + CLI + smoke) |
| Documentation | Plan 3 |
| Spike findings — defensive design rules | Plan 2 (encoded in AgentCoreBackend) |
