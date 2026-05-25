# AgentCore Backend — Plan 2 of 3

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `AgentCoreBackend` — the `boto3`-based `StorageBackend` that talks to AWS Bedrock AgentCore Memory. Wire it into the factory. After this plan, `BETTER_MEMORY_STORAGE_BACKEND=agentcore` starts a working MCP server (assuming memory IDs are already in `$BETTER_MEMORY_HOME/agentcore.json`). CLI `agentcore init`, the Stop-hook closure event, integration tests against real AWS, and docs land in **Plan 3**.

**Architecture:** AgentCoreBackend wraps two boto3 clients (`bedrock-agentcore` data plane, `bedrock-agentcore-control` control plane). Observations → events. Reflections → records (extracted by AgentCore's built-in episodic strategy). Semantic memories → records (extracted by built-in userPreference strategy + directly created via batch). Episode-lifecycle methods are no-ops because AgentCore manages event grouping internally via `sessionId`. Rating uses per-record metadata-counter increments via `BatchUpdateMemoryRecords` with full-snapshot semantics; the per-session exposure log doesn't exist in agentcore mode.

**Tech Stack:** Python 3.12+, `boto3 1.43.14` / `botocore 1.43.14`, existing `StorageBackend` Protocol from Plan 1.

**Spec:** `docs/superpowers/specs/2026-05-24-agentcore-storage-backend-design.md` — with the **Verified API surface** section (post-spec boto3 introspection) as the source of truth for shapes.

**Follow-up plan:**
- **Plan 3** (Operator wiring) — `better-memory agentcore init / status / smoke / migrate-from-sqlite` CLI, Stop-hook agentcore-mode closure event, integration tests against real AWS, README + website docs.

---

## File Map

| Path | Status | Responsibility |
|---|---|---|
| `better_memory/storage/protocol.py` | modify (Task 1) | Add `supports_episodes` capability flag (default True for backwards compat); keep all method declarations unchanged |
| `better_memory/storage/sqlite.py` | modify (Task 1) | Expose `supports_episodes = True` |
| `better_memory/mcp/server.py` | modify (Task 1) | (Optional) thread `supports_episodes` to the management UI; tool registration is unchanged |
| `better_memory/storage/session.py` | NEW (Task 2) | `resolve_actor_id`, `resolve_session_id`, `resolve_namespace`, `closure_event_payload`, `parse_hints_prose` |
| `better_memory/storage/agentcore_persistence.py` | NEW (Task 3) | Load / save `$BETTER_MEMORY_HOME/agentcore.json` (memory + strategy IDs for semantic + episodic) |
| `better_memory/storage/agentcore.py` | NEW (Tasks 4-12) | `AgentCoreBackend` — boto3 wrapper satisfying `StorageBackend` |
| `better_memory/storage/factory.py` | modify (Task 13) | Replace `NotImplementedError` agentcore branch with real `AgentCoreBackend(...)` constructor |
| `tests/storage/test_session.py` | NEW (Task 2) | session.py unit tests |
| `tests/storage/test_agentcore_persistence.py` | NEW (Task 3) | persistence load/save round-trip |
| `tests/storage/test_agentcore_unit.py` | NEW (Tasks 4-12) | Mocked-boto3 unit tests covering every protocol method through `AgentCoreBackend` |
| `tests/storage/test_factory.py` | modify (Task 13) | Drop the "agentcore raises until Plan 2" test; replace with a "factory returns AgentCoreBackend when memory IDs present" test |

Integration tests (`tests/integration/test_agentcore_roundtrip.py`) and the CLI (`better_memory/cli/agentcore.py`) are **Plan 3**.

## Verified service surface

See spec: `docs/superpowers/specs/2026-05-24-agentcore-storage-backend-design.md` → **Verified API surface (boto3 introspection, 2026-05-25)** section. The boto3 method names, required / optional params, and shape members listed there are the source of truth — DO NOT re-introspect during implementation. If a method signature surprises you mid-task, STOP and amend the spec first (Plan 1 v1 review lesson: never paper over surface drift in the implementation).

## Confidence Summary

| Task | Confidence | Lift applied |
|---|---|---|
| 1. Protocol: `supports_episodes` capability flag | 95% | Same pattern as `supports_synthesis` (Plan 1 Task 7) |
| 2. session.py helpers | 95% | Pure-stdlib utilities; no AWS |
| 3. agentcore_persistence.py | 95% | JSON load/save; shape pinned in spec |
| 4. AgentCoreBackend skeleton + clients + no-op methods | 92% | Constructor wiring against verified boto3 surface; capability flags + episode no-ops + synthesize_next NotImplementedError |
| 5. observe → CreateEvent | 92% | Payload shape verified; sessionId resolution from backend state |
| 6. list_observations → ListEvents (current session) | 90% | Single-session listing; cross-session enumeration deferred |
| 7. retrieve → 3 × RetrieveMemoryRecords + reinforcement decay | 92% (lifted from 88%) | Reinforcement formula pinned against sqlite shape (rrf_score × (1 + α × reinforcement_score) × 0.5^(age_days/14)); searchQuery branches on empty → list_memory_records for filter-only path; per-polarity searchCriteria + client-side scoring; hints split |
| 8. record_use → GetMemoryRecord + BatchUpdate (full snapshot) | 90% | Read-modify-write pattern is straightforward once metadata schema is locked |
| 9. credit_one / apply_session_ratings / list_session_exposures | 90% | Rating-model spec section pins the mapping; list_session_exposures returns empty in agentcore mode |
| 10. promote_reflection / retire_reflection → BatchUpdate w/ namespace mutation | 92% | Pattern documented in spec line 359-374 |
| 11. semantic CRUD (observe / list / update_text / set_scope / delete) | 90% | semantic_set_scope is namespace mutation (project ↔ general); requestIdentifier dedupe-key for observe |
| 12. session_bootstrap → parallel ListMemoryRecords + envelope assembly | 93% (lifted from 88%) | 4 parallel calls via asyncio.gather + run_in_executor (true fan-out, not sequential); list_memory_records (bootstrap is metadata-only, no semantic search); envelope shape pinned to BootstrapResult so MCP handler unwrap (server.py:1398-1411) keeps working — episode_id = session_id placeholder, episode_action = "opened" |
| 13. Factory wire-up + isinstance check passes | 95% | Drops the Plan-1 NotImplementedError; loads agentcore.json |

All tasks ≥ 90%. (Was 88% before Task 7 + 12 confidence lift: investigator verified BucketedResults shape, sqlite reinforcement formula at search/hybrid.py:367-384, BootstrapResult shape at session_bootstrap.py:85-95, and MCP handler unwrap at server.py:1398-1411.)

---

## Conventions used in this plan

- All test code is complete and runnable.
- All `Run:` commands include the exact expected output text or exit-code expectation.
- "Commit" steps use the canonical project commit-message style (lowercase imperative subject, `feat(scope):` / `refactor(scope):` prefix).
- Every AgentCore call uses the snake_case boto3 method name from the verified API surface; every param uses the verified key name.
- AgentCoreBackend caches boto3 clients on `__init__` (built once per server boot — same convention as the cached services in Plan 1's SqliteBackend).
- Mocked-boto3 tests use `unittest.mock.MagicMock` for the client; assertions verify the call args (method name + kwargs) to lock in the wire shape. Per-method tests also assert the return mapping (parsed reflections, decoded metadata, etc.).
- "Full metadata snapshot on update" is a hard rule: every `batch_update_memory_records` call sends `metadata` as the COMPLETE current state of the keys we manage, not a partial. Spike Finding 5 + spec line 355-356.
- For `record_use` / `credit_one`: read current via `get_memory_record` first (single-record op exists on data plane). Use `list_memory_records` only when filtering a collection.

---

### Task 1: Protocol: `supports_episodes` capability flag

**Files:**
- Modify: `better_memory/storage/protocol.py`
- Modify: `better_memory/storage/sqlite.py`
- Modify: `tests/storage/test_protocol.py`
- Modify: `tests/storage/test_sqlite_backend.py`

- [ ] **Step 1: Write the failing protocol test**

Append to `tests/storage/test_protocol.py`:

```python
def test_protocol_declares_supports_episodes_flag() -> None:
    """supports_episodes is the capability used by management UI to hide the Episodes tab in agentcore mode."""
    assert hasattr(StorageBackend, "supports_episodes")
```

And to `tests/storage/test_sqlite_backend.py`:

```python
def test_supports_episodes_is_true(backend) -> None:
    """SqliteBackend exposes episode lifecycle; UI shows the Episodes tab."""
    assert backend.supports_episodes is True
```

- [ ] **Step 2: Run; verify failures**

Run: `uv run pytest tests/storage/test_protocol.py tests/storage/test_sqlite_backend.py -k "supports_episodes" -v`
Expected: 2 failures — `supports_episodes` attribute missing.

- [ ] **Step 3: Add the flag to the Protocol**

Edit `better_memory/storage/protocol.py`. Add immediately after the existing `supports_synthesis` property:

```python
    @property
    def supports_episodes(self) -> bool:
        """True when the backend exposes the episode-lifecycle methods as
        first-class operations. False when episodes are an internal
        implementation detail (e.g. agentcore mode, where AgentCore manages
        event grouping via sessionId). The management UI hides the Episodes
        tab when this is False."""
        ...
```

Update the class docstring to mention the second flag if the docstring lists `supports_synthesis`.

- [ ] **Step 4: Implement on SqliteBackend**

Edit `better_memory/storage/sqlite.py`. Add immediately after `supports_synthesis`:

```python
    @property
    def supports_episodes(self) -> bool:
        return True
```

- [ ] **Step 5: Update the all-required-methods test**

Edit `tests/storage/test_protocol.py` `test_protocol_declares_all_required_methods` — leave the existing method-list intact; the test doesn't enumerate capability flags, so no edit needed. (Verify by re-reading the test before moving on.)

- [ ] **Step 6: Run; verify pass**

Run: `uv run pytest tests/storage/ -v`
Expected: all pass.

- [ ] **Step 7: pyright clean**

Run: `uv run pyright better_memory/storage tests/storage`
Expected: 0 errors.

- [ ] **Step 8: Commit**

```bash
git add better_memory/storage/protocol.py better_memory/storage/sqlite.py tests/storage/test_protocol.py tests/storage/test_sqlite_backend.py
git commit -m "feat(storage): add supports_episodes capability flag to Protocol"
```

---

### Task 2: `session.py` helpers

**Files:**
- Create: `better_memory/storage/session.py`
- Create: `tests/storage/test_session.py`

These helpers are pure-stdlib (no boto3) so they unit-test cleanly without mocks.

- [ ] **Step 1: Write the failing tests**

Create `tests/storage/test_session.py`:

```python
"""Unit tests for storage/session.py — identity resolution helpers."""

from __future__ import annotations

import pytest

from better_memory.storage import session as sess


def test_resolve_actor_id_returns_project() -> None:
    assert sess.resolve_actor_id("better-memory") == "better-memory"


def test_resolve_actor_id_returns_general_for_none() -> None:
    assert sess.resolve_actor_id(None) == "general"


def test_resolve_actor_id_returns_general_for_empty_string() -> None:
    assert sess.resolve_actor_id("") == "general"


def test_resolve_session_id_uses_env_first(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-session-xyz")
    assert sess.resolve_session_id() == "env-session-xyz"


def test_resolve_session_id_falls_back_to_claude_code_env(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "code-env-session")
    assert sess.resolve_session_id() == "code-env-session"


def test_resolve_session_id_generates_when_no_env(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    sid = sess.resolve_session_id()
    assert isinstance(sid, str) and len(sid) >= 16


def test_resolve_namespace_project_reflections() -> None:
    assert sess.resolve_namespace("better-memory", "reflections") == "projects/better-memory/reflections/"


def test_resolve_namespace_project_episodes_nests_under_reflections() -> None:
    # Per spec line 123: reflectionConfiguration.namespaceTemplates must be a
    # prefix of (or equal to) the strategy's namespaceTemplates. Episodes nest
    # under reflections to satisfy that constraint.
    assert sess.resolve_namespace("better-memory", "episodes") == "projects/better-memory/reflections/episodes/"


def test_resolve_namespace_general_semantic() -> None:
    assert sess.resolve_namespace("general", "semantic") == "general/semantic/"


def test_resolve_namespace_project_retired() -> None:
    assert sess.resolve_namespace("better-memory", "retired") == "projects/better-memory/retired/"


def test_resolve_namespace_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        sess.resolve_namespace("better-memory", "bogus")  # type: ignore[arg-type]


def test_closure_event_payload_uses_role_other() -> None:
    """OTHER is the right enum value for a system-emitted closure marker
    (Conversational.role enum: ASSISTANT | USER | TOOL | OTHER per
    verified boto3 surface)."""
    payload = sess.closure_event_payload()
    assert isinstance(payload, list)
    assert len(payload) == 1
    block = payload[0]["conversational"]
    assert block["role"] == "OTHER"
    assert "Session complete" in block["content"]["text"]


def test_parse_hints_prose_splits_on_markdown_bullets() -> None:
    prose = "First hint here.\n- Second hint here.\n- Third hint."
    assert sess.parse_hints_prose(prose) == [
        "First hint here.",
        "Second hint here.",
        "Third hint.",
    ]


def test_parse_hints_prose_returns_single_element_when_no_bullets() -> None:
    prose = "Just one prose paragraph with no bullets at all."
    assert sess.parse_hints_prose(prose) == [prose]


def test_parse_hints_prose_handles_empty_string() -> None:
    assert sess.parse_hints_prose("") == []
```

- [ ] **Step 2: Run; verify ImportError**

Run: `uv run pytest tests/storage/test_session.py -v`
Expected: ImportError — `session` module doesn't exist.

- [ ] **Step 3: Create session.py**

Create `better_memory/storage/session.py`:

```python
"""Identity + payload helpers for the AgentCore backend.

Pure stdlib — no boto3 dependency. These helpers are used by
AgentCoreBackend (per-call namespace + session resolution) and by the
Stop hook (closure event payload, in Plan 3). Kept separate from
agentcore.py so that the Stop hook can import them without pulling in
boto3 (the Stop hook must remain fast — no boto3 import unless we're
actually firing an agentcore-mode closure event).
"""

from __future__ import annotations

import os
import re
from typing import Literal
from uuid import uuid4

_NamespaceKind = Literal["reflections", "episodes", "semantic", "retired"]
_VALID_NAMESPACE_KINDS: tuple[_NamespaceKind, ...] = (
    "reflections",
    "episodes",
    "semantic",
    "retired",
)


def resolve_actor_id(project: str | None) -> str:
    """Return the AgentCore actorId for this project, or `"general"` if no
    project is in scope (cross-project bucket)."""
    if not project:
        return "general"
    return project


def resolve_session_id() -> str:
    """Return the current Claude session id, generating one if no env var
    is set. Reads CLAUDE_SESSION_ID first, then CLAUDE_CODE_SESSION_ID,
    then generates a uuid4 hex (32 chars)."""
    return (
        os.environ.get("CLAUDE_SESSION_ID")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or uuid4().hex
    )


def resolve_namespace(actor_id: str, kind: _NamespaceKind) -> str:
    """Build the AgentCore namespace string for a (actor, kind) pair.

    Tree (per spec):
        projects/{actorId}/reflections/             ← reflection records
        projects/{actorId}/reflections/episodes/    ← episode records (nested)
        projects/{actorId}/semantic/                ← user-preference records
        projects/{actorId}/retired/                 ← retired records
        general/...                                  ← cross-project bucket
    """
    if kind not in _VALID_NAMESPACE_KINDS:
        raise ValueError(
            f"kind must be one of {_VALID_NAMESPACE_KINDS}, got {kind!r}"
        )
    root = "general" if actor_id == "general" else f"projects/{actor_id}"
    if kind == "episodes":
        # Nested under reflections (spec line 123).
        return f"{root}/reflections/episodes/"
    return f"{root}/{kind}/"


def closure_event_payload() -> list[dict]:
    """Canonical closure-marker payload for the Stop hook's final CreateEvent.

    Conversational.role enum per boto3 surface: ASSISTANT | USER | TOOL | OTHER.
    OTHER is the right semantic match for a system-emitted closure marker;
    spec example (line 177-184) previously used USER but was corrected in
    the Verified API surface section."""
    return [
        {
            "conversational": {
                "role": "OTHER",
                "content": {
                    "text": "Session complete. All work for this session has been recorded."
                },
            }
        }
    ]


_HINTS_BULLET_SPLIT = re.compile(r"\n-\s+")


def parse_hints_prose(prose: str) -> list[str]:
    """Split AgentCore's reflection hints (single prose string) into a
    list[str] for better-memory's Reflection.hints field.

    Working approach (per spec Open Question 1):
    - Split on the markdown bullet pattern `\\n- ` (newline then dash-space).
    - If no bullets, return [prose] as a single element.
    - Empty input returns []."""
    if not prose:
        return []
    parts = _HINTS_BULLET_SPLIT.split(prose)
    return [p.strip() for p in parts if p.strip()]
```

- [ ] **Step 4: Run; verify pass**

Run: `uv run pytest tests/storage/test_session.py -v`
Expected: 13 passed.

- [ ] **Step 5: pyright clean**

Run: `uv run pyright better_memory/storage tests/storage`
Expected: 0 errors.

- [ ] **Step 6: Commit**

```bash
git add better_memory/storage/session.py tests/storage/test_session.py
git commit -m "feat(storage): session.py helpers for actor/session/namespace/closure"
```

---

### Task 3: `agentcore_persistence.py` — load/save agentcore.json

**Files:**
- Create: `better_memory/storage/agentcore_persistence.py`
- Create: `tests/storage/test_agentcore_persistence.py`

Persistence shape verified in spec → **Verified API surface → Persistence file shape** section.

- [ ] **Step 1: Write the failing tests**

Create `tests/storage/test_agentcore_persistence.py`:

```python
"""Round-trip tests for the agentcore.json persistence layer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from better_memory.storage.agentcore_persistence import (
    AgentCoreConfig,
    MemoryRecord,
    load_agentcore_config,
    save_agentcore_config,
    AgentCoreConfigError,
)


def test_save_then_load_round_trip(tmp_path: Path) -> None:
    cfg = AgentCoreConfig(
        schema_version=1,
        region="eu-west-2",
        semantic=MemoryRecord(
            memory_id="better-memory-semantic-abc1234567",
            memory_arn="arn:aws:bedrock-agentcore:eu-west-2:123:memory/better-memory-semantic-abc1234567",
            memory_name="better-memory-semantic",
            strategy_id="userPreference-zXy1234567",
            strategy_name="userPreference",
            event_expiry_duration_days=365,
        ),
        episodic=MemoryRecord(
            memory_id="better-memory-episodic-def4567890",
            memory_arn="arn:aws:bedrock-agentcore:eu-west-2:123:memory/better-memory-episodic-def4567890",
            memory_name="better-memory-episodic",
            strategy_id="episodicReflections-qPr9876543",
            strategy_name="episodicReflections",
            event_expiry_duration_days=90,
        ),
    )
    save_agentcore_config(cfg, tmp_path)
    loaded = load_agentcore_config(tmp_path)
    assert loaded == cfg


def test_load_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert load_agentcore_config(tmp_path) is None


def test_load_raises_on_corrupt_json(tmp_path: Path) -> None:
    (tmp_path / "agentcore.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(AgentCoreConfigError, match="parse"):
        load_agentcore_config(tmp_path)


def test_load_raises_on_unsupported_schema_version(tmp_path: Path) -> None:
    (tmp_path / "agentcore.json").write_text(
        json.dumps({"schema_version": 999, "region": "eu-west-2"}),
        encoding="utf-8",
    )
    with pytest.raises(AgentCoreConfigError, match="schema_version"):
        load_agentcore_config(tmp_path)


def test_load_raises_on_missing_required_field(tmp_path: Path) -> None:
    (tmp_path / "agentcore.json").write_text(
        json.dumps({"schema_version": 1, "region": "eu-west-2"}),  # semantic + episodic missing
        encoding="utf-8",
    )
    with pytest.raises(AgentCoreConfigError, match="semantic"):
        load_agentcore_config(tmp_path)
```

- [ ] **Step 2: Run; verify ImportError**

Run: `uv run pytest tests/storage/test_agentcore_persistence.py -v`
Expected: ImportError.

- [ ] **Step 3: Create the persistence module**

Create `better_memory/storage/agentcore_persistence.py`:

```python
"""Load and save `$BETTER_MEMORY_HOME/agentcore.json`.

The file is written once by `better-memory agentcore init` (Plan 3) and
read on every server boot. Schema is pinned to `schema_version: 1` for
forward-compat; loaders refuse unknown schema versions to fail loudly
rather than misinterpret an old-shape file.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

_AGENTCORE_FILE = "agentcore.json"
_CURRENT_SCHEMA_VERSION = 1


class AgentCoreConfigError(Exception):
    """Raised when agentcore.json is missing required fields, corrupt,
    or carries an unsupported schema_version."""


@dataclass(frozen=True)
class MemoryRecord:
    """Per-memory metadata persisted at init time and consumed at startup."""

    memory_id: str
    memory_arn: str
    memory_name: str
    strategy_id: str
    strategy_name: str
    event_expiry_duration_days: int


@dataclass(frozen=True)
class AgentCoreConfig:
    """Top-level shape of `agentcore.json`."""

    schema_version: int
    region: str
    semantic: MemoryRecord
    episodic: MemoryRecord


def _config_path(home: Path) -> Path:
    return home / _AGENTCORE_FILE


def save_agentcore_config(cfg: AgentCoreConfig, home: Path) -> None:
    """Write the config to `<home>/agentcore.json` atomically (write to
    tmp, then rename)."""
    home.mkdir(parents=True, exist_ok=True)
    target = _config_path(home)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(asdict(cfg), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(target)


def load_agentcore_config(home: Path) -> AgentCoreConfig | None:
    """Load the config from `<home>/agentcore.json`. Returns None if the
    file does not exist. Raises AgentCoreConfigError on corruption or
    unsupported schema version."""
    target = _config_path(home)
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AgentCoreConfigError(
            f"failed to parse {target}: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise AgentCoreConfigError(f"{target} is not a JSON object")

    schema_version = raw.get("schema_version")
    if schema_version != _CURRENT_SCHEMA_VERSION:
        raise AgentCoreConfigError(
            f"{target} has unsupported schema_version={schema_version!r}; "
            f"expected {_CURRENT_SCHEMA_VERSION}"
        )

    for required in ("region", "semantic", "episodic"):
        if required not in raw:
            raise AgentCoreConfigError(
                f"{target} missing required field {required!r}"
            )

    try:
        return AgentCoreConfig(
            schema_version=schema_version,
            region=raw["region"],
            semantic=MemoryRecord(**raw["semantic"]),
            episodic=MemoryRecord(**raw["episodic"]),
        )
    except TypeError as exc:
        raise AgentCoreConfigError(
            f"{target} has malformed semantic / episodic block: {exc}"
        ) from exc
```

- [ ] **Step 4: Run; verify pass**

Run: `uv run pytest tests/storage/test_agentcore_persistence.py -v`
Expected: 5 passed.

- [ ] **Step 5: pyright clean**

Run: `uv run pyright better_memory/storage tests/storage`
Expected: 0 errors.

- [ ] **Step 6: Commit**

```bash
git add better_memory/storage/agentcore_persistence.py tests/storage/test_agentcore_persistence.py
git commit -m "feat(storage): agentcore.json load/save with schema version + error class"
```

---

### Task 4: AgentCoreBackend skeleton + clients + no-op methods

**Files:**
- Create: `better_memory/storage/agentcore.py`
- Create: `tests/storage/test_agentcore_unit.py`

This task lands the class shell — constructor wires boto3 clients, capability flags are set, episode-lifecycle methods return synthetic results / empty lists, synthesize_next_* raises `NotImplementedError`. Subsequent tasks fill in real implementations.

- [ ] **Step 1: Write the skeleton tests**

Create `tests/storage/test_agentcore_unit.py`:

```python
"""Unit tests for AgentCoreBackend. All boto3 calls are mocked — these
tests verify wire shape (call args + return mapping), NOT live AWS
behavior. Integration tests against real AWS land in Plan 3."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from better_memory.storage import StorageBackend
from better_memory.storage.agentcore import AgentCoreBackend
from better_memory.storage.agentcore_persistence import (
    AgentCoreConfig,
    MemoryRecord,
)


@pytest.fixture
def ac_config() -> AgentCoreConfig:
    return AgentCoreConfig(
        schema_version=1,
        region="eu-west-2",
        semantic=MemoryRecord(
            memory_id="mem-sem-abc1234567",
            memory_arn="arn:aws:bedrock-agentcore:eu-west-2:123:memory/mem-sem-abc1234567",
            memory_name="better-memory-semantic",
            strategy_id="userPreference-zXy1234567",
            strategy_name="userPreference",
            event_expiry_duration_days=365,
        ),
        episodic=MemoryRecord(
            memory_id="mem-epi-def4567890",
            memory_arn="arn:aws:bedrock-agentcore:eu-west-2:123:memory/mem-epi-def4567890",
            memory_name="better-memory-episodic",
            strategy_id="episodicReflections-qPr9876543",
            strategy_name="episodicReflections",
            event_expiry_duration_days=90,
        ),
    )


@pytest.fixture
def mock_data_client() -> MagicMock:
    return MagicMock(name="bedrock-agentcore-data")


@pytest.fixture
def mock_control_client() -> MagicMock:
    return MagicMock(name="bedrock-agentcore-control")


@pytest.fixture
def backend(ac_config, mock_data_client, mock_control_client) -> AgentCoreBackend:
    return AgentCoreBackend(
        config=ac_config,
        data_client=mock_data_client,
        control_client=mock_control_client,
        session_id="test-session-xyz",
        project="testproj",
    )


def test_agentcore_backend_satisfies_protocol(backend) -> None:
    assert isinstance(backend, StorageBackend)


def test_supports_synthesis_is_false(backend) -> None:
    """Synthesis runs inside AgentCore; the MCP synthesize_next_* tools
    are not registered in agentcore mode."""
    assert backend.supports_synthesis is False


def test_supports_episodes_is_false(backend) -> None:
    """AgentCore manages event grouping via sessionId; episodes are not a
    first-class concept the UI exposes."""
    assert backend.supports_episodes is False


def test_synthesize_next_get_context_raises_not_implemented(backend) -> None:
    with pytest.raises(NotImplementedError, match="agentcore"):
        backend.synthesize_next_get_context(project="testproj")


def test_synthesize_next_apply_raises_not_implemented(backend) -> None:
    with pytest.raises(NotImplementedError, match="agentcore"):
        backend.synthesize_next_apply(
            episode_id="ep-x", response={}, project="testproj"
        )


def test_open_background_episode_returns_synthetic_id(backend) -> None:
    """No-op in agentcore mode; returns a sentinel id so the existing MCP
    tool path doesn't break."""
    result = backend.open_background_episode(
        session_id="test-session", project="testproj"
    )
    assert isinstance(result, str) and result


def test_start_foreground_episode_returns_synthetic_id(backend) -> None:
    result = backend.start_foreground_episode(
        session_id="test-session",
        project="testproj",
        goal="ship plan 2",
    )
    assert isinstance(result, str) and result


def test_close_active_episode_returns_empty_string(backend) -> None:
    """No-op close; returns empty string. (The MCP handler converts this
    to a no-content tool result.)"""
    result = backend.close_active_episode(
        session_id="test-session",
        outcome="success",
        close_reason="goal_complete",
    )
    assert result == ""


def test_close_episode_by_id_returns_empty_string(backend) -> None:
    result = backend.close_episode_by_id(
        episode_id="ep-x",
        outcome="success",
        close_reason="goal_complete",
    )
    assert result == ""


def test_list_episodes_returns_empty_list(backend) -> None:
    """No episodes in agentcore mode; UI hides the tab via supports_episodes."""
    assert backend.list_episodes() == []
```

- [ ] **Step 2: Run; verify ImportError**

Run: `uv run pytest tests/storage/test_agentcore_unit.py -v`
Expected: ImportError — `agentcore` module doesn't exist.

- [ ] **Step 3: Create the AgentCoreBackend skeleton**

Create `better_memory/storage/agentcore.py`:

```python
"""AgentCoreBackend — boto3 wrapper satisfying StorageBackend Protocol.

Constructor takes pre-built boto3 clients (data plane + control plane)
plus the loaded AgentCoreConfig. Tests inject MagicMock clients; the
factory (Task 13) constructs real clients via boto3.client(...). This
inversion keeps tests fast and free of botocore deps.

Capability flags:
- supports_synthesis = False — AgentCore's built-in episodicMemoryStrategy
  performs extraction internally; the MCP synthesize_next_* tools are not
  registered in agentcore mode.
- supports_episodes = False — AgentCore manages event grouping via
  sessionId; the better-memory episodes table has no equivalent record
  type. Episode lifecycle methods are no-ops returning synthetic ids /
  empty results; the management UI hides the Episodes tab.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from better_memory.storage.agentcore_persistence import AgentCoreConfig
from better_memory.storage.protocol import Outcome, UseOutcome


class AgentCoreBackend:
    """boto3-backed StorageBackend implementation."""

    def __init__(
        self,
        *,
        config: AgentCoreConfig,
        data_client: Any,
        control_client: Any,
        session_id: str | None,
        project: str,
    ) -> None:
        self._cfg = config
        self._data = data_client
        self._control = control_client
        self._session_id = session_id
        self._project = project

    # ----- Capability flags -----

    @property
    def supports_synthesis(self) -> bool:
        return False

    @property
    def supports_episodes(self) -> bool:
        return False

    # ----- Observations: filled in by Tasks 5-6 -----

    async def observe(self, **kwargs: Any) -> str:
        raise NotImplementedError("Implemented in Task 5")

    async def retrieve(self, query: str | None = None, **kwargs: Any) -> Any:
        raise NotImplementedError("Implemented in Task 7")

    async def list_observations(self, **kwargs: Any) -> list[dict[str, Any]]:
        raise NotImplementedError("Implemented in Task 6")

    def record_use(
        self, observation_id: str, *, outcome: UseOutcome | None = None
    ) -> None:
        raise NotImplementedError("Implemented in Task 8")

    # ----- Semantic memories: Task 11 -----

    def semantic_observe(self, **kwargs: Any) -> str:
        raise NotImplementedError("Implemented in Task 11")

    def semantic_list(self, **kwargs: Any) -> list[Any]:
        raise NotImplementedError("Implemented in Task 11")

    def semantic_update_text(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 11")

    def semantic_set_scope(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 11")

    def semantic_delete(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 11")

    # ----- Episodes: no-ops in agentcore mode (this task) -----

    def open_background_episode(
        self, *, session_id: str, project: str
    ) -> str:
        # No real episode; return a synthetic id so the MCP tool path
        # works. The id is not used anywhere downstream in agentcore mode.
        return f"agentcore-noop-bg-{uuid4().hex[:12]}"

    def start_foreground_episode(
        self,
        *,
        session_id: str,
        project: str,
        goal: str,
        tech: str | None = None,
    ) -> str:
        return f"agentcore-noop-fg-{uuid4().hex[:12]}"

    def close_active_episode(
        self,
        *,
        session_id: str,
        outcome: str,
        close_reason: str,
        summary: str | None = None,
    ) -> str:
        return ""

    def close_episode_by_id(
        self,
        *,
        episode_id: str,
        outcome: str,
        close_reason: str,
        summary: str | None = None,
    ) -> str:
        return ""

    def list_episodes(
        self,
        *,
        project: str | None = None,
        outcome: str | None = None,
        only_open: bool = False,
    ) -> list[Any]:
        return []

    # ----- Reflection lifecycle: Task 10 -----

    def promote_reflection(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 10")

    def retire_reflection(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 10")

    # ----- Session lifecycle: Tasks 9, 12 -----

    def session_bootstrap(self, **kwargs: Any) -> Any:
        raise NotImplementedError("Implemented in Task 12")

    def list_session_exposures(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError("Implemented in Task 9")

    def apply_session_ratings(self, **kwargs: Any) -> Any:
        raise NotImplementedError("Implemented in Task 9")

    def credit_one(self, **kwargs: Any) -> Any:
        raise NotImplementedError("Implemented in Task 9")

    # ----- Synthesis: NEVER implemented (capability gate handles this) -----

    def synthesize_next_get_context(self, *, project: str) -> Any:
        raise NotImplementedError(
            "synthesize_next_get_context is not supported in agentcore mode. "
            "AgentCore's built-in episodicMemoryStrategy performs extraction "
            "internally. The MCP synthesize_next_* tools are not registered "
            "when backend.supports_synthesis is False."
        )

    def synthesize_next_apply(
        self, *, episode_id: str, response: Any, project: str
    ) -> Any:
        raise NotImplementedError(
            "synthesize_next_apply is not supported in agentcore mode."
        )
```

- [ ] **Step 4: Run; verify pass**

Run: `uv run pytest tests/storage/test_agentcore_unit.py -v`
Expected: 10 passed (capability flags + episode no-ops + synthesize_next NotImplementedError + Protocol satisfaction).

- [ ] **Step 5: pyright clean**

Run: `uv run pyright better_memory/storage tests/storage`
Expected: 0 errors.

- [ ] **Step 6: Commit**

```bash
git add better_memory/storage/agentcore.py tests/storage/test_agentcore_unit.py
git commit -m "feat(storage): AgentCoreBackend skeleton with capability flags + episode no-ops"
```

---

### Task 5: AgentCoreBackend.observe → CreateEvent

**Files:**
- Modify: `better_memory/storage/agentcore.py`
- Modify: `tests/storage/test_agentcore_unit.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/storage/test_agentcore_unit.py`:

```python
import pytest
from datetime import datetime, UTC


@pytest.mark.asyncio
async def test_observe_calls_create_event_with_correct_kwargs(backend, mock_data_client) -> None:
    """observe builds a CreateEvent against the EPISODIC memory with
    actorId=project, sessionId=backend session, and a conversational
    payload carrying the observation content."""
    mock_data_client.create_event.return_value = {
        "event": {"eventId": "evt-abc123", "memoryId": "mem-epi-def4567890"}
    }

    result = await backend.observe(
        content="Test observation.",
        outcome="success",
        component="parser",
        theme="bug",
    )

    assert result == "evt-abc123"
    mock_data_client.create_event.assert_called_once()
    call_kwargs = mock_data_client.create_event.call_args.kwargs

    assert call_kwargs["memoryId"] == "mem-epi-def4567890"
    assert call_kwargs["actorId"] == "testproj"
    assert call_kwargs["sessionId"] == "test-session-xyz"
    assert isinstance(call_kwargs["eventTimestamp"], datetime)
    assert call_kwargs["eventTimestamp"].tzinfo is UTC

    # Payload shape: list[{conversational: {role, content: {text}}}]
    payload = call_kwargs["payload"]
    assert isinstance(payload, list) and len(payload) == 1
    block = payload[0]["conversational"]
    assert block["role"] == "USER"  # observations are model-side inputs
    assert block["content"]["text"] == "Test observation."

    # Metadata: outcome / component / theme as stringValue only.
    metadata = call_kwargs["metadata"]
    assert metadata["outcome"]["stringValue"] == "success"
    assert metadata["component"]["stringValue"] == "parser"
    assert metadata["theme"]["stringValue"] == "bug"


@pytest.mark.asyncio
async def test_observe_resolves_project_when_kwarg_is_none(backend, mock_data_client) -> None:
    mock_data_client.create_event.return_value = {"event": {"eventId": "evt-x"}}
    await backend.observe(content="x", project=None)
    assert mock_data_client.create_event.call_args.kwargs["actorId"] == "testproj"


@pytest.mark.asyncio
async def test_observe_general_project_uses_general_actor(backend, mock_data_client) -> None:
    mock_data_client.create_event.return_value = {"event": {"eventId": "evt-x"}}
    await backend.observe(content="x", project="general")
    assert mock_data_client.create_event.call_args.kwargs["actorId"] == "general"


@pytest.mark.asyncio
async def test_observe_drops_none_metadata_keys(backend, mock_data_client) -> None:
    """Don't send `{"key": {"stringValue": None}}` — None-valued metadata
    keys are omitted entirely so the payload validates."""
    mock_data_client.create_event.return_value = {"event": {"eventId": "evt-x"}}
    await backend.observe(content="x", component=None, theme="bug")
    metadata = mock_data_client.create_event.call_args.kwargs["metadata"]
    assert "component" not in metadata
    assert metadata["theme"]["stringValue"] == "bug"


@pytest.mark.asyncio
async def test_observe_raises_value_error_when_session_id_is_none(ac_config, mock_data_client, mock_control_client) -> None:
    """CreateEvent on the episodic memory requires sessionId (per the
    output schema and our usage pattern). A backend with session_id=None
    cannot fire events — raise so the operator sees the misconfiguration."""
    backend = AgentCoreBackend(
        config=ac_config,
        data_client=mock_data_client,
        control_client=mock_control_client,
        session_id=None,
        project="testproj",
    )
    with pytest.raises(ValueError, match="session_id"):
        await backend.observe(content="x")
```

- [ ] **Step 2: Run; verify failures**

Run: `uv run pytest tests/storage/test_agentcore_unit.py -v -k observe`
Expected: 5 failures — `observe` raises `NotImplementedError`.

- [ ] **Step 3: Implement `observe`**

Edit `better_memory/storage/agentcore.py`. Add the import:

```python
from datetime import datetime, UTC

from better_memory.storage.session import resolve_actor_id
```

Replace the `observe` stub with:

```python
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
    """Write an observation as a CreateEvent against the episodic memory.

    sessionId is the backend's held session id (raised if None — events
    require a real session). actorId is resolved from project (or
    "general" when no project is in scope). Returns the AgentCore
    eventId."""
    if self._session_id is None:
        raise ValueError(
            "AgentCoreBackend.observe requires session_id at construction "
            "time. The MCP server populates it from CLAUDE_SESSION_ID at "
            "startup; if you see this in production, the env var is missing."
        )
    actor_id = resolve_actor_id(project or self._project)

    # Event-level metadata is stringValue-only (verified API surface);
    # richer typing only on memory record metadata. Drop None values.
    metadata: dict[str, dict[str, Any]] = {}
    raw = {
        "outcome": outcome,
        "component": component,
        "theme": theme,
        "trigger_type": trigger_type,
        "tech": tech,
        "scope": scope,
        "scope_path": scope_path,
    }
    for key, value in raw.items():
        if value is None:
            continue
        metadata[key] = {"stringValue": str(value)}

    response = self._data.create_event(
        memoryId=self._cfg.episodic.memory_id,
        actorId=actor_id,
        sessionId=self._session_id,
        eventTimestamp=datetime.now(UTC),
        payload=[
            {
                "conversational": {
                    "role": "USER",
                    "content": {"text": content},
                }
            }
        ],
        metadata=metadata,
    )
    return response["event"]["eventId"]
```

- [ ] **Step 4: Run; verify pass**

Run: `uv run pytest tests/storage/test_agentcore_unit.py -v -k observe`
Expected: 5 passed.

- [ ] **Step 5: pyright clean**

Run: `uv run pyright better_memory/storage tests/storage`
Expected: 0 errors.

- [ ] **Step 6: Commit**

```bash
git add better_memory/storage/agentcore.py tests/storage/test_agentcore_unit.py
git commit -m "feat(agentcore): observe wires CreateEvent against episodic memory"
```

---

### Task 6: AgentCoreBackend.list_observations → ListEvents

**Files:**
- Modify: `better_memory/storage/agentcore.py`
- Modify: `tests/storage/test_agentcore_unit.py`

**Constraint:** `ListEvents` requires `sessionId` (verified API surface). In v1, agentcore-mode `list_observations` returns events from the CURRENT session only — cross-session enumeration is not directly supported by ListEvents and is deferred to Plan 3 (CLI `status` can paginate by session if needed).

- [ ] **Step 1: Write the failing tests**

Append:

```python
@pytest.mark.asyncio
async def test_list_observations_returns_current_session_events(backend, mock_data_client) -> None:
    mock_data_client.list_events.return_value = {
        "events": [
            {
                "eventId": "evt-1",
                "memoryId": "mem-epi-def4567890",
                "actorId": "testproj",
                "sessionId": "test-session-xyz",
                "eventTimestamp": datetime(2026, 5, 25, 12, tzinfo=UTC),
                "payload": [
                    {"conversational": {"role": "USER", "content": {"text": "obs one"}}}
                ],
                "metadata": {
                    "outcome": {"stringValue": "success"},
                    "theme": {"stringValue": "test"},
                },
            },
            {
                "eventId": "evt-2",
                "memoryId": "mem-epi-def4567890",
                "actorId": "testproj",
                "sessionId": "test-session-xyz",
                "eventTimestamp": datetime(2026, 5, 25, 12, 30, tzinfo=UTC),
                "payload": [
                    {"conversational": {"role": "USER", "content": {"text": "obs two"}}}
                ],
                "metadata": {"outcome": {"stringValue": "failure"}},
            },
        ],
    }

    result = await backend.list_observations(limit=10)
    assert isinstance(result, list) and len(result) == 2

    # Mapping: eventId -> id, content extracted from payload, metadata
    # flattened (stringValue unwrapped).
    assert result[0]["id"] == "evt-1"
    assert result[0]["content"] == "obs one"
    assert result[0]["outcome"] == "success"
    assert result[0]["theme"] == "test"

    # ListEvents call shape
    call_kwargs = mock_data_client.list_events.call_args.kwargs
    assert call_kwargs["memoryId"] == "mem-epi-def4567890"
    assert call_kwargs["actorId"] == "testproj"
    assert call_kwargs["sessionId"] == "test-session-xyz"
    assert call_kwargs["maxResults"] == 10
    assert call_kwargs["includePayloads"] is True


@pytest.mark.asyncio
async def test_list_observations_returns_empty_when_no_events(backend, mock_data_client) -> None:
    mock_data_client.list_events.return_value = {"events": []}
    assert await backend.list_observations(limit=5) == []


@pytest.mark.asyncio
async def test_list_observations_raises_when_session_id_is_none(ac_config, mock_data_client, mock_control_client) -> None:
    backend = AgentCoreBackend(
        config=ac_config,
        data_client=mock_data_client,
        control_client=mock_control_client,
        session_id=None,
        project="testproj",
    )
    with pytest.raises(ValueError, match="session_id"):
        await backend.list_observations(limit=5)
```

- [ ] **Step 2: Run; verify failures**

Run: `uv run pytest tests/storage/test_agentcore_unit.py -v -k list_observations`
Expected: 3 failures.

- [ ] **Step 3: Implement `list_observations`**

Replace the `list_observations` stub:

```python
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
    """List raw events from the CURRENT session as observations. Cross-
    session enumeration is deferred (ListEvents requires sessionId)."""
    if self._session_id is None:
        raise ValueError(
            "AgentCoreBackend.list_observations requires session_id at "
            "construction time."
        )
    actor_id = resolve_actor_id(project or self._project)

    response = self._data.list_events(
        memoryId=self._cfg.episodic.memory_id,
        actorId=actor_id,
        sessionId=self._session_id,
        maxResults=limit,
        includePayloads=True,
    )

    results: list[dict[str, Any]] = []
    for event in response.get("events", []):
        payload_text = ""
        for block in event.get("payload", []):
            conv = block.get("conversational")
            if conv:
                payload_text = conv.get("content", {}).get("text", "")
                break

        flat_metadata = {
            k: v.get("stringValue") for k, v in event.get("metadata", {}).items()
        }

        results.append(
            {
                "id": event["eventId"],
                "content": payload_text,
                "session_id": event.get("sessionId"),
                "actor_id": event.get("actorId"),
                "event_timestamp": event.get("eventTimestamp"),
                **flat_metadata,
            }
        )

    # Apply post-filter for theme/component/outcome since ListEvents.filter
    # surface is limited to branch/eventType — not the per-event metadata
    # keys we set. Filter client-side.
    if theme is not None:
        results = [r for r in results if r.get("theme") == theme]
    if component is not None:
        results = [r for r in results if r.get("component") == component]
    if outcome is not None:
        results = [r for r in results if r.get("outcome") == outcome]
    if query is not None and query.strip():
        q = query.lower()
        results = [r for r in results if q in r.get("content", "").lower()]
    return results
```

- [ ] **Step 4: Run; verify pass**

Run: `uv run pytest tests/storage/test_agentcore_unit.py -v -k list_observations`
Expected: 3 passed.

- [ ] **Step 5: pyright clean** — 0 errors.

- [ ] **Step 6: Commit**

```bash
git add better_memory/storage/agentcore.py tests/storage/test_agentcore_unit.py
git commit -m "feat(agentcore): list_observations via ListEvents (current session)"
```

---

### Task 7: AgentCoreBackend.retrieve → bucketed RetrieveMemoryRecords + reinforcement decay

**Files:**
- Modify: `better_memory/storage/agentcore.py`
- Modify: `tests/storage/test_agentcore_unit.py`

This is the most intricate method. Per spec line 313-340 + the reflection content shape (spec line 156-169):

- Fire 3 × `retrieve_memory_records` calls in parallel — one per polarity bucket — against the episodic memory reflections namespace.
- Each call passes `searchCriteria` with `searchQuery=query_text`, `topK=candidate_k`, and `metadataFilters` for `polarity=<bucket>` AND `status=active`.
- Parse `MemoryRecordSummary.content.text` as JSON into the existing `Reflection` dataclass. Hints prose → `list[str]` via `parse_hints_prose`.
- Apply client-side reinforcement scoring **mirroring the sqlite-mode formula** at `better_memory/search/hybrid.py:367-384`:

  ```
  reinforcement_score = useful_count - missed_count - times_misled - overlooked_count - ignored_count
  reinforcement_mult  = 1.0 + reinforcement_alpha * reinforcement_score
  age_days            = (now - created_at).total_days
  recency_mult        = 0.5 ** (max(age_days, 0) / half_life)   # half_life default 14 days
  final_score         = base_score * reinforcement_mult * recency_mult
  ```

  Note: in sqlite mode `reinforcement_score` is a pre-computed column on the observations row. In agentcore mode there is no equivalent column — we compute it client-side from the per-counter metadata values declared in spec. The age baseline is `created_at` (when the record was first written), NOT `last_credited_at` (which represents the most recent rating event and is used elsewhere for staleness display but NOT for retrieval scoring). Sort + truncate per-bucket limit.
- Return `BucketedResults` (the existing dataclass from `services/observation.py`).

- [ ] **Step 1: Write the failing tests**

Append:

```python
from better_memory.services.observation import BucketedResults


@pytest.mark.asyncio
async def test_retrieve_fires_one_call_per_polarity_bucket(backend, mock_data_client) -> None:
    """Three parallel RetrieveMemoryRecords calls: do, dont, neutral."""
    mock_data_client.retrieve_memory_records.return_value = {"memoryRecordSummaries": []}
    result = await backend.retrieve(query="how to handle migrations")
    assert isinstance(result, BucketedResults)
    assert mock_data_client.retrieve_memory_records.call_count == 3

    polarities_filtered = []
    for call in mock_data_client.retrieve_memory_records.call_args_list:
        criteria = call.kwargs["searchCriteria"]
        for f in criteria["metadataFilters"]:
            if f["left"]["metadataKey"] == "polarity":
                polarities_filtered.append(f["right"]["metadataValue"]["stringValue"])
    assert set(polarities_filtered) == {"do", "dont", "neutral"}


@pytest.mark.asyncio
async def test_retrieve_parses_reflection_json_content(backend, mock_data_client) -> None:
    """content.text is a JSON blob with title/use_cases/hints/confidence."""
    import json
    record_json = json.dumps({
        "title": "Test reflection title",
        "use_cases": "Applies when X",
        "hints": "First hint.\n- Second hint.\n- Third hint.",
        "confidence": "0.85",
    })
    mock_data_client.retrieve_memory_records.return_value = {
        "memoryRecordSummaries": [
            {
                "memoryRecordId": "rec-1",
                "content": {"text": record_json},
                "memoryStrategyId": "episodicReflections-qPr9876543",
                "namespaces": ["projects/testproj/reflections/"],
                "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
                "score": 0.9,
                "metadata": {
                    "polarity": {"stringValue": "do"},
                    "useful_count": {"numberValue": 3},
                    "missed_count": {"numberValue": 0},
                    "ignored_count": {"numberValue": 1},
                    "times_misled": {"numberValue": 0},
                    "overlooked_count": {"numberValue": 0},
                    "status": {"stringValue": "active"},
                },
            }
        ]
    }
    result = await backend.retrieve(query="anything")
    do_bucket = result.do
    assert len(do_bucket) >= 1
    refl = do_bucket[0]
    # Match the existing Reflection dataclass field set
    assert refl["title"] == "Test reflection title"
    assert refl["use_cases"] == "Applies when X"
    assert refl["hints"] == ["First hint.", "Second hint.", "Third hint."]
    assert float(refl["confidence"]) == 0.85
    assert refl["id"] == "rec-1"


@pytest.mark.asyncio
async def test_retrieve_applies_reinforcement_to_score(backend, mock_data_client) -> None:
    """Reinforcement multiplier: useful_count boosts; misled/overlooked penalize."""
    import json
    def make_record(rec_id: str, useful: int, misled: int) -> dict:
        return {
            "memoryRecordId": rec_id,
            "content": {"text": json.dumps({
                "title": rec_id, "use_cases": "u", "hints": "h", "confidence": "0.9",
            })},
            "memoryStrategyId": "x",
            "namespaces": ["projects/testproj/reflections/"],
            "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
            "score": 0.5,
            "metadata": {
                "polarity": {"stringValue": "do"},
                "useful_count": {"numberValue": useful},
                "missed_count": {"numberValue": 0},
                "ignored_count": {"numberValue": 0},
                "times_misled": {"numberValue": misled},
                "overlooked_count": {"numberValue": 0},
                "status": {"stringValue": "active"},
            },
        }

    def stub(**kwargs):
        criteria = kwargs["searchCriteria"]
        for f in criteria["metadataFilters"]:
            if f["left"]["metadataKey"] == "polarity":
                pol = f["right"]["metadataValue"]["stringValue"]
                if pol == "do":
                    return {"memoryRecordSummaries": [
                        make_record("boosted", useful=5, misled=0),
                        make_record("penalized", useful=0, misled=5),
                    ]}
        return {"memoryRecordSummaries": []}

    mock_data_client.retrieve_memory_records.side_effect = stub
    result = await backend.retrieve(query="x", reinforcement_alpha=0.1)
    do_titles = [r["title"] for r in result.do]
    # Boosted should outrank penalized after reinforcement.
    assert do_titles.index("boosted") < do_titles.index("penalized")
```

- [ ] **Step 2: Run; verify failures** — 3 failures (retrieve raises NotImplementedError).

- [ ] **Step 3: Implement `retrieve`**

Add imports:

```python
import asyncio
import json
from typing import cast

from better_memory.services.observation import BucketedResults
from better_memory.storage.session import parse_hints_prose, resolve_namespace
```

Replace stub:

```python
_POLARITIES: tuple[str, str, str] = ("do", "dont", "neutral")


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
) -> BucketedResults:
    """Three parallel retrieve_memory_records calls (one per polarity).

    Each returns up to candidate_k candidates; client-side reinforcement
    multiplier + recency decay re-rank; truncate to per-bucket limit."""
    actor_id = resolve_actor_id(project or self._project)
    namespace = resolve_namespace(actor_id, "reflections")
    # AgentCore RetrieveMemoryRecords.searchCriteria.searchQuery is REQUIRED
    # (non-empty string). When the caller passes None / empty query, sqlite
    # mode skips the embedder + falls back to filter-only mode (no semantic
    # search). AgentCore has no filter-only path on retrieve_memory_records;
    # use list_memory_records (no search) instead. Branch here.
    search_query = (query or "").strip()

    bucket_limits = {
        "do": do_limit,
        "dont": dont_limit,
        "neutral": neutral_limit,
    }

    results: dict[str, list[dict[str, Any]]] = {}

    async def fetch(polarity: str) -> tuple[str, list[dict[str, Any]]]:
        filters: list[dict[str, Any]] = [
            {
                "left": {"metadataKey": "polarity"},
                "operator": "EQUALS_TO",
                "right": {"metadataValue": {"stringValue": polarity}},
            }
        ]
        if status is not None:
            filters.append(
                {
                    "left": {"metadataKey": "status"},
                    "operator": "EQUALS_TO",
                    "right": {"metadataValue": {"stringValue": status}},
                }
            )
        loop = asyncio.get_running_loop()
        if search_query:
            # Semantic search path.
            response = await loop.run_in_executor(
                None,
                lambda: self._data.retrieve_memory_records(
                    memoryId=self._cfg.episodic.memory_id,
                    namespace=namespace,
                    searchCriteria={
                        "searchQuery": search_query,
                        "topK": candidate_k,
                        "metadataFilters": filters,
                    },
                ),
            )
        else:
            # Filter-only path (mirrors sqlite mode's skip-embedder behaviour
            # when query is None/empty). ListMemoryRecords accepts the same
            # metadataFilters but doesn't require a searchQuery.
            response = await loop.run_in_executor(
                None,
                lambda: self._data.list_memory_records(
                    memoryId=self._cfg.episodic.memory_id,
                    namespace=namespace,
                    maxResults=candidate_k,
                    metadataFilters=filters,
                ),
            )
        parsed = [
            self._parse_reflection_record(rec)
            for rec in response.get("memoryRecordSummaries", [])
        ]
        ranked = sorted(
            parsed,
            key=lambda r: r["_final_score"],
            reverse=True,
        )[: bucket_limits[polarity]]
        return polarity, ranked

    pairs = await asyncio.gather(*(fetch(p) for p in _POLARITIES))
    for polarity, ranked in pairs:
        results[polarity] = ranked
    return BucketedResults(
        do=results["do"], dont=results["dont"], neutral=results["neutral"]
    )


def _parse_reflection_record(
    self, rec: dict[str, Any]
) -> dict[str, Any]:
    """Map MemoryRecordSummary → better-memory Reflection-shaped dict."""
    text = rec.get("content", {}).get("text", "")
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        body = {"title": "", "use_cases": "", "hints": text, "confidence": "0"}

    metadata_raw = rec.get("metadata", {})

    def _num(key: str) -> float:
        entry = metadata_raw.get(key, {})
        return float(entry.get("numberValue", 0))

    useful = _num("useful_count")
    missed = _num("missed_count")
    ignored = _num("ignored_count")
    misled = _num("times_misled")
    overlooked = _num("overlooked_count")

    base_score = float(rec.get("score") or 0.0)
    reinforcement_score = useful - missed - misled - overlooked - ignored

    # Mirror sqlite-mode formula (search/hybrid.py:367-384):
    #   final = base_score * (1 + alpha * reinforcement_score) * 0.5^(age_days / half_life)
    # Age baseline is `created_at` (when the record was first written), NOT
    # `last_credited_at`. Half-life default 14 days matches the sqlite default.
    reinforcement_alpha_local = 0.1
    half_life_days = 14.0

    created_at = rec.get("createdAt")
    if isinstance(created_at, datetime):
        age_days = max(
            (datetime.now(UTC) - created_at).total_seconds() / 86400.0,
            0.0,
        )
    else:
        age_days = 0.0

    recency_mult = 0.5 ** (age_days / half_life_days) if half_life_days > 0 else 1.0
    reinforcement_mult = 1.0 + reinforcement_alpha_local * reinforcement_score
    final_score = base_score * reinforcement_mult * recency_mult

    polarity_meta = metadata_raw.get("polarity", {}).get("stringValue", "neutral")
    hints_prose = body.get("hints", "") if isinstance(body, dict) else ""

    return {
        "id": rec["memoryRecordId"],
        "title": body.get("title", "") if isinstance(body, dict) else "",
        "use_cases": body.get("use_cases", "") if isinstance(body, dict) else "",
        "hints": parse_hints_prose(hints_prose if isinstance(hints_prose, str) else ""),
        "confidence": body.get("confidence", "0") if isinstance(body, dict) else "0",
        "polarity": polarity_meta,
        "useful_count": useful,
        "missed_count": missed,
        "ignored_count": ignored,
        "times_misled": misled,
        "overlooked_count": overlooked,
        "_final_score": final_score,
        "namespaces": rec.get("namespaces", []),
    }
```

- [ ] **Step 4: Run; verify pass**

Run: `uv run pytest tests/storage/test_agentcore_unit.py -v -k retrieve`
Expected: 3 passed.

- [ ] **Step 5: pyright clean** — 0 errors.

- [ ] **Step 6: Commit**

```bash
git add better_memory/storage/agentcore.py tests/storage/test_agentcore_unit.py
git commit -m "feat(agentcore): retrieve bucketed reflections + reinforcement scoring"
```

---

### Task 8: AgentCoreBackend.record_use → GetMemoryRecord + BatchUpdate

**Files:**
- Modify: `better_memory/storage/agentcore.py`
- Modify: `tests/storage/test_agentcore_unit.py`

Pattern: `get_memory_record` to fetch current state → bump counter + write `last_credited_at` → `batch_update_memory_records` with FULL metadata snapshot.

- [ ] **Step 1: Write the failing tests**

Append:

```python
def _make_record_response(rec_id: str, **counters) -> dict:
    """Helper: build a MemoryRecord response with the standard metadata."""
    base = {
        "useful_count": 0, "missed_count": 0, "ignored_count": 0,
        "times_misled": 0, "overlooked_count": 0,
    }
    base.update(counters)
    return {
        "memoryRecord": {
            "memoryRecordId": rec_id,
            "content": {"text": "{}"},
            "memoryStrategyId": "episodicReflections-qPr9876543",
            "namespaces": ["projects/testproj/reflections/"],
            "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
            "metadata": {
                **{k: {"numberValue": v} for k, v in base.items()},
                "status": {"stringValue": "active"},
                "polarity": {"stringValue": "do"},
            },
        }
    }


def test_record_use_success_bumps_useful_count(backend, mock_data_client) -> None:
    mock_data_client.get_memory_record.return_value = _make_record_response(
        "rec-x", useful_count=2,
    )
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "rec-x", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    backend.record_use("rec-x", outcome="success")
    call = mock_data_client.batch_update_memory_records.call_args.kwargs
    rec = call["records"][0]
    assert rec["memoryRecordId"] == "rec-x"
    assert rec["metadata"]["useful_count"]["numberValue"] == 3
    assert rec["metadata"]["missed_count"]["numberValue"] == 0
    # last_credited_at refreshed
    assert "last_credited_at" in rec["metadata"]


def test_record_use_failure_bumps_missed_count(backend, mock_data_client) -> None:
    mock_data_client.get_memory_record.return_value = _make_record_response(
        "rec-y", missed_count=4,
    )
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "rec-y", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    backend.record_use("rec-y", outcome="failure")
    call = mock_data_client.batch_update_memory_records.call_args.kwargs
    rec = call["records"][0]
    assert rec["metadata"]["missed_count"]["numberValue"] == 5
    assert rec["metadata"]["useful_count"]["numberValue"] == 0


def test_record_use_none_outcome_is_noop(backend, mock_data_client) -> None:
    """record_use(id) without outcome should not touch the record (no
    classification, no counter change)."""
    backend.record_use("rec-z", outcome=None)
    mock_data_client.get_memory_record.assert_not_called()
    mock_data_client.batch_update_memory_records.assert_not_called()


def test_record_use_propagates_failed_records(backend, mock_data_client) -> None:
    mock_data_client.get_memory_record.return_value = _make_record_response("rec-fail")
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [],
        "failedRecords": [{
            "memoryRecordId": "rec-fail",
            "status": "FAILED",
            "errorCode": 500,
            "errorMessage": "internal error",
        }],
    }
    with pytest.raises(RuntimeError, match="rec-fail"):
        backend.record_use("rec-fail", outcome="success")
```

- [ ] **Step 2: Run; verify failures** — 4 failures.

- [ ] **Step 3: Implement `record_use` + the GetMemoryRecord helper**

Add to `AgentCoreBackend`:

```python
def _get_record(self, record_id: str) -> dict[str, Any]:
    """Fetch a single memory record from the EPISODIC memory.

    Tries get_memory_record first; falls back to list_memory_records
    with metadataFilters if the record is a BASE record (spike Finding 3:
    get-memory-record returns 404 for BASE records). For our use
    (record_use against extracted reflections), get_memory_record is
    the right call — BASE records aren't ratable."""
    return self._data.get_memory_record(
        memoryId=self._cfg.episodic.memory_id,
        memoryRecordId=record_id,
    )["memoryRecord"]


def _full_metadata_snapshot(
    self, current: dict[str, Any], updates: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Produce a full metadata snapshot for BatchUpdateMemoryRecords.

    Merge-vs-replace semantics on AgentCore metadata updates are
    undetermined (spike Finding 5); always send the FULL snapshot.
    `current` is the existing metadata dict from MemoryRecord;
    `updates` is the diff to apply (already in the wire-shape dict form
    with stringValue/numberValue/etc.)."""
    snapshot = dict(current)
    snapshot.update(updates)
    return snapshot


def record_use(
    self,
    observation_id: str,
    *,
    outcome: UseOutcome | None = None,
) -> None:
    """Credit a record's reinforcement counter. outcome=None is a no-op
    (no classification, no counter change)."""
    if outcome is None:
        return

    record = self._get_record(observation_id)
    metadata = record.get("metadata", {})

    counter_key = "useful_count" if outcome == "success" else "missed_count"
    current_count = float(
        metadata.get(counter_key, {}).get("numberValue", 0)
    )

    updates: dict[str, dict[str, Any]] = {
        counter_key: {"numberValue": current_count + 1},
        "last_credited_at": {"dateTimeValue": datetime.now(UTC)},
    }
    snapshot = self._full_metadata_snapshot(metadata, updates)

    response = self._data.batch_update_memory_records(
        memoryId=self._cfg.episodic.memory_id,
        records=[
            {
                "memoryRecordId": observation_id,
                "timestamp": datetime.now(UTC),
                "metadata": snapshot,
            }
        ],
    )
    failed = response.get("failedRecords", [])
    if failed:
        raise RuntimeError(
            f"AgentCore record_use failed for {observation_id}: "
            f"{failed[0].get('errorMessage', 'unknown')}"
        )
```

- [ ] **Step 4: Run; verify pass** — 4 passed.

- [ ] **Step 5: pyright clean** — 0 errors.

- [ ] **Step 6: Commit**

```bash
git add better_memory/storage/agentcore.py tests/storage/test_agentcore_unit.py
git commit -m "feat(agentcore): record_use via GetMemoryRecord + BatchUpdate full-snapshot"
```

---

### Task 9: AgentCoreBackend rating methods — credit_one / apply_session_ratings / list_session_exposures

**Files:**
- Modify: `better_memory/storage/agentcore.py`
- Modify: `tests/storage/test_agentcore_unit.py`

Per spec Rating model section:
- `list_session_exposures` → return `{"session_id": session_id, "exposures": []}` (always empty in agentcore mode)
- `credit_one(session_id, kind, id, classification)` → maps the 5-class classification onto the per-record metadata update (cited/shaped → useful_count++; ignored → ignored_count++; misled → times_misled++; overlooked → overlooked_count++). Always updates `last_credited_at`.
- `apply_session_ratings(session_id, ratings)` → iterate ratings, call `credit_one` per entry, return summary dict.

- [ ] **Step 1: Write the failing tests**

Append:

```python
_RATING_TO_COUNTER = {
    "cited": "useful_count",
    "shaped": "useful_count",
    "ignored": "ignored_count",
    "misled": "times_misled",
    "overlooked": "overlooked_count",
}


def test_list_session_exposures_returns_empty_envelope(backend) -> None:
    result = backend.list_session_exposures(session_id="test-session-xyz")
    assert result == {"session_id": "test-session-xyz", "exposures": []}


@pytest.mark.parametrize(
    "classification,counter_key",
    list(_RATING_TO_COUNTER.items()),
)
def test_credit_one_bumps_correct_counter(
    backend, mock_data_client, classification, counter_key
) -> None:
    mock_data_client.get_memory_record.return_value = _make_record_response("rec-c")
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "rec-c", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    result = backend.credit_one(
        session_id="test-session-xyz",
        kind="reflection",
        id="rec-c",
        classification=classification,
    )
    assert result["applied"] == "rec-c"
    rec = mock_data_client.batch_update_memory_records.call_args.kwargs["records"][0]
    assert rec["metadata"][counter_key]["numberValue"] == 1


def test_credit_one_rejects_unknown_classification(backend) -> None:
    with pytest.raises(ValueError, match="classification"):
        backend.credit_one(
            session_id="s",
            kind="reflection",
            id="rec-c",
            classification="bogus",
        )


def test_apply_session_ratings_credits_each_rating(backend, mock_data_client) -> None:
    mock_data_client.get_memory_record.side_effect = [
        _make_record_response("rec-1"),
        _make_record_response("rec-2"),
    ]
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "x", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    result = backend.apply_session_ratings(
        session_id="test-session-xyz",
        ratings=[
            {"kind": "reflection", "id": "rec-1", "classification": "cited"},
            {"kind": "reflection", "id": "rec-2", "classification": "overlooked"},
        ],
    )
    assert mock_data_client.batch_update_memory_records.call_count == 2
    assert result["applied"] == 2
    assert result["failed"] == 0


def test_apply_session_ratings_empty_returns_zero_summary(backend) -> None:
    result = backend.apply_session_ratings(session_id="x", ratings=[])
    assert result == {"applied": 0, "failed": 0}
```

- [ ] **Step 2: Run; verify failures** — 8 failures (5 parametrized + 3 standalone).

- [ ] **Step 3: Implement the rating methods**

Add module-level constant near `_POLARITIES`:

```python
_RATING_TO_COUNTER: dict[str, str] = {
    "cited": "useful_count",
    "shaped": "useful_count",
    "ignored": "ignored_count",
    "misled": "times_misled",
    "overlooked": "overlooked_count",
}
```

Replace the three rating stubs:

```python
def list_session_exposures(self, *, session_id: str) -> dict[str, Any]:
    """Per spec Rating model section: no exposure log in agentcore mode.
    Returns the standard envelope shape with an empty exposures list."""
    return {"session_id": session_id, "exposures": []}


def credit_one(
    self,
    *,
    session_id: str,
    kind: str,
    id: str,
    classification: str,
) -> dict[str, Any]:
    """Apply one classification → counter increment on a record.

    Counter mapping (spec Rating model section):
        cited / shaped → useful_count
        ignored        → ignored_count
        misled         → times_misled
        overlooked     → overlooked_count"""
    counter_key = _RATING_TO_COUNTER.get(classification)
    if counter_key is None:
        raise ValueError(
            f"classification={classification!r} is not one of "
            f"{sorted(_RATING_TO_COUNTER)}"
        )

    record = self._get_record(id)
    metadata = record.get("metadata", {})
    current = float(metadata.get(counter_key, {}).get("numberValue", 0))

    updates: dict[str, dict[str, Any]] = {
        counter_key: {"numberValue": current + 1},
        "last_credited_at": {"dateTimeValue": datetime.now(UTC)},
    }
    snapshot = self._full_metadata_snapshot(metadata, updates)

    response = self._data.batch_update_memory_records(
        memoryId=self._cfg.episodic.memory_id,
        records=[
            {
                "memoryRecordId": id,
                "timestamp": datetime.now(UTC),
                "metadata": snapshot,
            }
        ],
    )
    failed = response.get("failedRecords", [])
    if failed:
        return {
            "applied": None,
            "skipped": failed[0].get("errorMessage", "unknown"),
        }
    return {"applied": id, "skipped": None}


def apply_session_ratings(
    self,
    *,
    session_id: str,
    ratings: list[dict[str, str]],
) -> dict[str, Any]:
    """Iterate ratings, call credit_one per entry. Return summary dict
    with `applied` (count of successful credits) and `failed` (count of
    skip / error results)."""
    applied = 0
    failed = 0
    for r in ratings:
        result = self.credit_one(
            session_id=session_id,
            kind=r["kind"],
            id=r["id"],
            classification=r["classification"],
        )
        if result["applied"] is not None:
            applied += 1
        else:
            failed += 1
    return {"applied": applied, "failed": failed}
```

- [ ] **Step 4: Run; verify pass** — 8 passed.

- [ ] **Step 5: pyright clean** — 0 errors.

- [ ] **Step 6: Commit**

```bash
git add better_memory/storage/agentcore.py tests/storage/test_agentcore_unit.py
git commit -m "feat(agentcore): credit_one + apply_session_ratings; empty session exposures"
```

---

### Task 10: AgentCoreBackend.promote_reflection + retire_reflection → BatchUpdate w/ namespace mutation

**Files:**
- Modify: `better_memory/storage/agentcore.py`
- Modify: `tests/storage/test_agentcore_unit.py`

Per spec line 359-374:

- Promote → namespaces set to `["general/reflections/"]` + metadata `status=promoted`
- Retire → namespaces set to `[f"projects/{actorId}/retired/"]` + metadata `status=retired`

Read current metadata first (full snapshot rule), apply update, send batch_update.

- [ ] **Step 1: Write the failing tests**

```python
def test_promote_reflection_moves_to_general_namespace(backend, mock_data_client) -> None:
    mock_data_client.get_memory_record.return_value = _make_record_response("rec-p")
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "rec-p", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    backend.promote_reflection(reflection_id="rec-p")
    rec = mock_data_client.batch_update_memory_records.call_args.kwargs["records"][0]
    assert rec["namespaces"] == ["general/reflections/"]
    assert rec["metadata"]["status"]["stringValue"] == "promoted"


def test_retire_reflection_moves_to_retired_namespace(backend, mock_data_client) -> None:
    mock_data_client.get_memory_record.return_value = _make_record_response("rec-r")
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "rec-r", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    backend.retire_reflection(reflection_id="rec-r")
    rec = mock_data_client.batch_update_memory_records.call_args.kwargs["records"][0]
    assert rec["namespaces"] == ["projects/testproj/retired/"]
    assert rec["metadata"]["status"]["stringValue"] == "retired"


def test_promote_reflection_raises_when_batch_fails(backend, mock_data_client) -> None:
    mock_data_client.get_memory_record.return_value = _make_record_response("rec-fail")
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [],
        "failedRecords": [{"memoryRecordId": "rec-fail", "status": "FAILED", "errorMessage": "boom"}],
    }
    with pytest.raises(RuntimeError, match="rec-fail"):
        backend.promote_reflection(reflection_id="rec-fail")
```

- [ ] **Step 2: Run; verify failures** — 3 failures.

- [ ] **Step 3: Implement promote / retire**

```python
def _mutate_namespace_and_status(
    self,
    *,
    reflection_id: str,
    new_namespaces: list[str],
    new_status: str,
) -> None:
    """Shared helper: read current metadata, mutate namespace + status,
    write back with full metadata snapshot."""
    record = self._get_record(reflection_id)
    metadata = record.get("metadata", {})

    updates: dict[str, dict[str, Any]] = {
        "status": {"stringValue": new_status},
    }
    snapshot = self._full_metadata_snapshot(metadata, updates)

    response = self._data.batch_update_memory_records(
        memoryId=self._cfg.episodic.memory_id,
        records=[
            {
                "memoryRecordId": reflection_id,
                "timestamp": datetime.now(UTC),
                "namespaces": new_namespaces,
                "metadata": snapshot,
            }
        ],
    )
    failed = response.get("failedRecords", [])
    if failed:
        raise RuntimeError(
            f"AgentCore reflection mutation failed for {reflection_id}: "
            f"{failed[0].get('errorMessage', 'unknown')}"
        )


def promote_reflection(self, *, reflection_id: str) -> None:
    self._mutate_namespace_and_status(
        reflection_id=reflection_id,
        new_namespaces=[resolve_namespace("general", "reflections")],
        new_status="promoted",
    )


def retire_reflection(self, *, reflection_id: str) -> None:
    actor_id = resolve_actor_id(self._project)
    self._mutate_namespace_and_status(
        reflection_id=reflection_id,
        new_namespaces=[resolve_namespace(actor_id, "retired")],
        new_status="retired",
    )
```

- [ ] **Step 4: Run; verify pass** — 3 passed.

- [ ] **Step 5: pyright clean** — 0 errors.

- [ ] **Step 6: Commit**

```bash
git add better_memory/storage/agentcore.py tests/storage/test_agentcore_unit.py
git commit -m "feat(agentcore): promote_reflection + retire_reflection via namespace mutation"
```

---

### Task 11: AgentCoreBackend semantic CRUD

**Files:**
- Modify: `better_memory/storage/agentcore.py`
- Modify: `tests/storage/test_agentcore_unit.py`

Five methods all hitting the SEMANTIC memory.

| Method | Op | Notes |
|---|---|---|
| `semantic_observe` | `batch_create_memory_records` | `memoryStrategyId=cfg.semantic.strategy_id`; `requestIdentifier=` content-hash; content text is the preference text directly; namespace = `projects/{actorId}/semantic/`; initial metadata = all counters 0 + status=active |
| `semantic_list` | `retrieve_memory_records` (search) OR `list_memory_records` (no query) | Namespace filter on `projects/{actorId}/semantic/`; if search provided, use `retrieve_memory_records` with `searchQuery` |
| `semantic_update_text` | `get_memory_record` + `batch_update_memory_records` | Update `content.text` only; metadata snapshot unchanged |
| `semantic_set_scope` | `get_memory_record` + `batch_update_memory_records` | Mutate namespaces from `projects/{actorId}/semantic/` → `general/semantic/` (or vice versa) |
| `semantic_delete` | `batch_delete_memory_records` | Records take only `memoryRecordId` |

- [ ] **Step 1: Write the failing tests**

```python
import hashlib


def test_semantic_observe_calls_batch_create_against_semantic_memory(backend, mock_data_client) -> None:
    mock_data_client.batch_create_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "sm-1", "status": "SUCCEEDED", "requestIdentifier": "any"}],
        "failedRecords": [],
    }
    sm_id = backend.semantic_observe(content="prefer uv over pip")
    assert sm_id == "sm-1"
    call = mock_data_client.batch_create_memory_records.call_args.kwargs
    assert call["memoryId"] == "mem-sem-abc1234567"
    rec = call["records"][0]
    assert rec["memoryStrategyId"] == "userPreference-zXy1234567"
    assert rec["namespaces"] == ["projects/testproj/semantic/"]
    assert rec["content"]["text"] == "prefer uv over pip"
    assert len(rec["requestIdentifier"]) <= 80
    # Initial metadata
    assert rec["metadata"]["status"]["stringValue"] == "active"
    assert rec["metadata"]["useful_count"]["numberValue"] == 0
    assert rec["metadata"]["overlooked_count"]["numberValue"] == 0


def test_semantic_observe_general_scope_uses_general_namespace(backend, mock_data_client) -> None:
    mock_data_client.batch_create_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "sm-2", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    backend.semantic_observe(content="x", scope="general")
    rec = mock_data_client.batch_create_memory_records.call_args.kwargs["records"][0]
    assert rec["namespaces"] == ["general/semantic/"]


def test_semantic_list_with_search_uses_retrieve_memory_records(backend, mock_data_client) -> None:
    mock_data_client.retrieve_memory_records.return_value = {
        "memoryRecordSummaries": [
            {
                "memoryRecordId": "sm-1",
                "content": {"text": "prefer uv"},
                "memoryStrategyId": "userPreference-zXy1234567",
                "namespaces": ["projects/testproj/semantic/"],
                "createdAt": datetime(2026, 5, 25, tzinfo=UTC),
                "metadata": {"status": {"stringValue": "active"}},
            }
        ]
    }
    result = backend.semantic_list(search="uv")
    assert len(result) == 1
    assert result[0]["id"] == "sm-1"
    assert result[0]["content"] == "prefer uv"


def test_semantic_list_without_search_uses_list_memory_records(backend, mock_data_client) -> None:
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    backend.semantic_list()
    mock_data_client.list_memory_records.assert_called_once()
    mock_data_client.retrieve_memory_records.assert_not_called()


def test_semantic_update_text_calls_batch_update(backend, mock_data_client) -> None:
    mock_data_client.get_memory_record.return_value = {
        "memoryRecord": {
            "memoryRecordId": "sm-1",
            "content": {"text": "original"},
            "memoryStrategyId": "userPreference-zXy1234567",
            "namespaces": ["projects/testproj/semantic/"],
            "createdAt": datetime(2026, 5, 25, tzinfo=UTC),
            "metadata": {"status": {"stringValue": "active"}},
        }
    }
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "sm-1", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    backend.semantic_update_text(id="sm-1", content="updated")
    rec = mock_data_client.batch_update_memory_records.call_args.kwargs["records"][0]
    assert rec["content"]["text"] == "updated"


def test_semantic_set_scope_swaps_namespace(backend, mock_data_client) -> None:
    mock_data_client.get_memory_record.return_value = {
        "memoryRecord": {
            "memoryRecordId": "sm-1",
            "content": {"text": "x"},
            "memoryStrategyId": "userPreference-zXy1234567",
            "namespaces": ["projects/testproj/semantic/"],
            "createdAt": datetime(2026, 5, 25, tzinfo=UTC),
            "metadata": {"status": {"stringValue": "active"}},
        }
    }
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "sm-1", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    backend.semantic_set_scope(id="sm-1", scope="general")
    rec = mock_data_client.batch_update_memory_records.call_args.kwargs["records"][0]
    assert rec["namespaces"] == ["general/semantic/"]


def test_semantic_delete_calls_batch_delete(backend, mock_data_client) -> None:
    mock_data_client.batch_delete_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "sm-x", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    backend.semantic_delete(id="sm-x")
    call = mock_data_client.batch_delete_memory_records.call_args.kwargs
    assert call["memoryId"] == "mem-sem-abc1234567"
    assert call["records"] == [{"memoryRecordId": "sm-x"}]
```

- [ ] **Step 2: Run; verify failures** — 7 failures.

- [ ] **Step 3: Implement semantic CRUD**

```python
def _semantic_initial_metadata(self) -> dict[str, dict[str, Any]]:
    """Initial metadata snapshot for a newly-created semantic record."""
    return {
        "useful_count": {"numberValue": 0},
        "missed_count": {"numberValue": 0},
        "ignored_count": {"numberValue": 0},
        "times_misled": {"numberValue": 0},
        "overlooked_count": {"numberValue": 0},
        "status": {"stringValue": "active"},
    }


def _get_semantic_record(self, record_id: str) -> dict[str, Any]:
    return self._data.get_memory_record(
        memoryId=self._cfg.semantic.memory_id,
        memoryRecordId=record_id,
    )["memoryRecord"]


def semantic_observe(
    self,
    *,
    content: str,
    project: str | None = None,
    scope: str = "project",
) -> str:
    """Create a semantic memory record. Bypasses LLM extraction —
    the content is the preference text directly, written under the
    userPreferenceMemoryStrategy so AWS applies its schema validation."""
    actor_id = resolve_actor_id(project or self._project)
    if scope == "general":
        namespace = resolve_namespace("general", "semantic")
    else:
        namespace = resolve_namespace(actor_id, "semantic")

    # requestIdentifier: max 80 chars, content-hash for natural dedup
    # if the same preference is observed twice in quick succession.
    req_id = hashlib.sha256(content.encode("utf-8")).hexdigest()[:80]

    response = self._data.batch_create_memory_records(
        memoryId=self._cfg.semantic.memory_id,
        records=[
            {
                "requestIdentifier": req_id,
                "namespaces": [namespace],
                "content": {"text": content},
                "timestamp": datetime.now(UTC),
                "memoryStrategyId": self._cfg.semantic.strategy_id,
                "metadata": self._semantic_initial_metadata(),
            }
        ],
    )
    failed = response.get("failedRecords", [])
    if failed:
        raise RuntimeError(
            f"AgentCore semantic_observe failed: "
            f"{failed[0].get('errorMessage', 'unknown')}"
        )
    return response["successfulRecords"][0]["memoryRecordId"]


def semantic_list(
    self,
    *,
    project: str | None = None,
    scope_filter: str | None = None,
    search: str | None = None,
    track_exposure: bool = True,
) -> list[Any]:
    """List semantic records. With search → retrieve_memory_records;
    without → list_memory_records."""
    actor_id = resolve_actor_id(project or self._project)
    if scope_filter == "general":
        namespace = resolve_namespace("general", "semantic")
    else:
        namespace = resolve_namespace(actor_id, "semantic")

    if search and search.strip():
        response = self._data.retrieve_memory_records(
            memoryId=self._cfg.semantic.memory_id,
            namespace=namespace,
            searchCriteria={
                "searchQuery": search.strip(),
                "topK": 50,
            },
        )
    else:
        response = self._data.list_memory_records(
            memoryId=self._cfg.semantic.memory_id,
            namespace=namespace,
            maxResults=100,
        )

    return [
        {
            "id": rec["memoryRecordId"],
            "content": rec.get("content", {}).get("text", ""),
            "namespaces": rec.get("namespaces", []),
            "scope": "general" if rec.get("namespaces", [""])[0].startswith("general/")
                     else "project",
        }
        for rec in response.get("memoryRecordSummaries", [])
    ]


def semantic_update_text(self, *, id: str, content: str) -> None:
    """Update the text of a semantic record. Metadata snapshot unchanged."""
    record = self._get_semantic_record(id)
    metadata = record.get("metadata", {})

    response = self._data.batch_update_memory_records(
        memoryId=self._cfg.semantic.memory_id,
        records=[
            {
                "memoryRecordId": id,
                "timestamp": datetime.now(UTC),
                "content": {"text": content},
                "metadata": metadata,  # full snapshot, unchanged
            }
        ],
    )
    failed = response.get("failedRecords", [])
    if failed:
        raise RuntimeError(
            f"AgentCore semantic_update_text failed for {id}: "
            f"{failed[0].get('errorMessage', 'unknown')}"
        )


def semantic_set_scope(self, *, id: str, scope: str) -> None:
    """Move a semantic record between project and general namespaces."""
    if scope not in ("project", "general"):
        raise ValueError(f"scope must be 'project' or 'general', got {scope!r}")
    record = self._get_semantic_record(id)
    metadata = record.get("metadata", {})

    target_namespace = (
        resolve_namespace("general", "semantic")
        if scope == "general"
        else resolve_namespace(resolve_actor_id(self._project), "semantic")
    )

    response = self._data.batch_update_memory_records(
        memoryId=self._cfg.semantic.memory_id,
        records=[
            {
                "memoryRecordId": id,
                "timestamp": datetime.now(UTC),
                "namespaces": [target_namespace],
                "metadata": metadata,
            }
        ],
    )
    failed = response.get("failedRecords", [])
    if failed:
        raise RuntimeError(
            f"AgentCore semantic_set_scope failed for {id}: "
            f"{failed[0].get('errorMessage', 'unknown')}"
        )


def semantic_delete(self, *, id: str) -> None:
    """Permanently delete a semantic record."""
    self._data.batch_delete_memory_records(
        memoryId=self._cfg.semantic.memory_id,
        records=[{"memoryRecordId": id}],
    )
```

- [ ] **Step 4: Run; verify pass** — 7 passed.

- [ ] **Step 5: pyright clean** — 0 errors.

- [ ] **Step 6: Commit**

```bash
git add better_memory/storage/agentcore.py tests/storage/test_agentcore_unit.py
git commit -m "feat(agentcore): semantic CRUD (observe/list/update_text/set_scope/delete)"
```

---

### Task 12: AgentCoreBackend.session_bootstrap → parallel RetrieveMemoryRecords

**Files:**
- Modify: `better_memory/storage/agentcore.py`
- Modify: `tests/storage/test_agentcore_unit.py`

Per spec line 383-385: parallel retrieves — one per polarity bucket against the episodic memory, plus one against the semantic memory — and assemble the same `additionalContext` payload as today.

- [ ] **Step 1: Write the failing tests**

```python
def test_session_bootstrap_fires_4_parallel_list_calls(backend, mock_data_client) -> None:
    """One per polarity (do/dont/neutral) against episodic + one against
    semantic — all 4 dispatched via asyncio.gather + run_in_executor.

    Uses list_memory_records (not retrieve_memory_records) because
    bootstrap is recency / metadata-only — no semantic search query."""
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    result = backend.session_bootstrap(session_id="test-session", project="testproj")
    # 4 calls total — 3 reflection (episodic) + 1 semantic
    assert mock_data_client.list_memory_records.call_count == 4

    targets = []
    for call in mock_data_client.list_memory_records.call_args_list:
        targets.append((call.kwargs["memoryId"], call.kwargs["namespace"]))

    assert ("mem-epi-def4567890", "projects/testproj/reflections/") in targets
    assert ("mem-sem-abc1234567", "projects/testproj/semantic/") in targets


def test_session_bootstrap_returns_envelope_matching_sqlite_shape(backend, mock_data_client) -> None:
    """Envelope must match the BootstrapResult shape the MCP handler at
    server.py:1398-1411 unwraps. Keys: additional_context, project, source,
    episode_id, episode_action, semantic_count, reflections_counts. In
    agentcore mode there is no real episode — episode_id = the session_id
    placeholder and episode_action = 'opened'."""
    mock_data_client.retrieve_memory_records.return_value = {"memoryRecordSummaries": []}
    result = backend.session_bootstrap(session_id="s", project="testproj", source="bootstrap")

    assert result["project"] == "testproj"
    assert result["source"] == "bootstrap"
    assert result["additional_context"]  # non-empty string
    assert result["episode_id"] == "s"
    assert result["episode_action"] == "opened"
    assert result["semantic_count"] == 0
    assert result["reflections_counts"] == {"do": 0, "dont": 0, "neutral": 0}
```

- [ ] **Step 2: Run; verify failures** — 2 failures.

- [ ] **Step 3: Implement `session_bootstrap` (parallel via asyncio.gather)**

The Protocol declares this method sync, but the 4 boto3 calls are independent
network round-trips. Fan them out via `asyncio.gather + run_in_executor` (same
pattern Task 7 uses for retrieve). Since the Protocol signature is sync, run
the gather under `asyncio.run(...)` inside the method body so callers stay
sync. The cost (event-loop startup per bootstrap) is negligible — bootstrap
runs once per session.

```python
def session_bootstrap(
    self,
    *,
    session_id: str,
    source: str | None = None,
    cwd: Any | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Fetch top-N reflections per polarity bucket + top-N semantic
    preferences IN PARALLEL; assemble the additional_context envelope
    matching the BootstrapResult shape the MCP handler at
    server.py:1398-1411 unwraps."""
    actor_id = resolve_actor_id(project or self._project)
    reflections_namespace = resolve_namespace(actor_id, "reflections")
    semantic_namespace = resolve_namespace(actor_id, "semantic")

    def _fetch_reflections(polarity: str) -> dict[str, Any]:
        return self._data.list_memory_records(
            memoryId=self._cfg.episodic.memory_id,
            namespace=reflections_namespace,
            maxResults=5,
            metadataFilters=[
                {
                    "left": {"metadataKey": "polarity"},
                    "operator": "EQUALS_TO",
                    "right": {"metadataValue": {"stringValue": polarity}},
                },
                {
                    "left": {"metadataKey": "status"},
                    "operator": "EQUALS_TO",
                    "right": {"metadataValue": {"stringValue": "active"}},
                },
            ],
        )

    def _fetch_semantic() -> dict[str, Any]:
        return self._data.list_memory_records(
            memoryId=self._cfg.semantic.memory_id,
            namespace=semantic_namespace,
            maxResults=10,
        )

    async def _gather_all() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        loop = asyncio.get_running_loop()
        reflection_tasks = {
            polarity: loop.run_in_executor(None, _fetch_reflections, polarity)
            for polarity in _POLARITIES
        }
        semantic_task = loop.run_in_executor(None, _fetch_semantic)

        reflection_responses = {
            polarity: await task for polarity, task in reflection_tasks.items()
        }
        semantic_response = await semantic_task
        return reflection_responses, semantic_response

    reflection_calls, semantic_response = asyncio.run(_gather_all())

    reflections_counts = {
        polarity: len(reflection_calls[polarity].get("memoryRecordSummaries", []))
        for polarity in _POLARITIES
    }
    semantic_count = len(semantic_response.get("memoryRecordSummaries", []))

    # additional_context is the JSON-serialized payload Claude sees on
    # SessionStart. Format matches what the MCP handler emits today: a
    # short text summary of reflection + semantic counts plus the raw lists.
    # The handler at server.py:1398 wraps this under the `additionalContext`
    # key in the final tool payload.
    reflection_lines = []
    for polarity in _POLARITIES:
        items = reflection_calls[polarity].get("memoryRecordSummaries", [])
        reflection_lines.append(
            f"{polarity}: {len(items)} reflections"
        )
    semantic_items = semantic_response.get("memoryRecordSummaries", [])
    additional_context = (
        f"Project: {actor_id}\n"
        f"Reflections — {', '.join(reflection_lines)}\n"
        f"Semantic memories: {len(semantic_items)}"
    )

    return {
        # Match sqlite-mode BootstrapResult exactly (server.py:1398-1411
        # unwraps these keys). In agentcore mode there is no real episode,
        # so episode_id is the placeholder session_id and episode_action
        # is always "opened" (the bootstrap handler treats agentcore-mode
        # sessions as fresh each time; AgentCore-side episode tracking is
        # internal and invisible to the bootstrap wire shape).
        "additional_context": additional_context,
        "project": actor_id,
        "source": source or "",
        "episode_id": session_id,
        "episode_action": "opened",
        "semantic_count": semantic_count,
        "reflections_counts": reflections_counts,
        # pending_synthesis intentionally omitted in agentcore mode;
        # the MCP handler must branch on its absence (backend.supports_synthesis
        # already signals this at the Protocol level).
    }
```

- [ ] **Step 4: Run; verify pass** — 2 passed.

- [ ] **Step 5: pyright clean** — 0 errors.

- [ ] **Step 6: Commit**

```bash
git add better_memory/storage/agentcore.py tests/storage/test_agentcore_unit.py
git commit -m "feat(agentcore): session_bootstrap assembles envelope from 4 retrieves"
```

---

### Task 13: Factory wire-up — replace NotImplementedError with real AgentCoreBackend

**Files:**
- Modify: `better_memory/storage/factory.py`
- Modify: `tests/storage/test_factory.py`

After this task, `build_backend(config=cfg_with_agentcore)` constructs a real `AgentCoreBackend`. The Plan-1 NotImplementedError is replaced by a load of `agentcore.json` + boto3 client construction.

- [ ] **Step 1: Update the failing test in test_factory.py**

In `tests/storage/test_factory.py`, replace `test_build_backend_agentcore_raises_until_plan2` with:

```python
def test_build_backend_returns_agentcore_when_config_loaded(tmp_path, monkeypatch) -> None:
    """With agentcore.json present + valid memory IDs, factory returns AgentCoreBackend."""
    import json
    home = tmp_path
    (home / "agentcore.json").write_text(
        json.dumps({
            "schema_version": 1,
            "region": "eu-west-2",
            "semantic": {
                "memory_id": "mem-sem-abc1234567",
                "memory_arn": "arn:aws:bedrock-agentcore:eu-west-2:123:memory/mem-sem-abc1234567",
                "memory_name": "better-memory-semantic",
                "strategy_id": "userPreference-zXy1234567",
                "strategy_name": "userPreference",
                "event_expiry_duration_days": 365,
            },
            "episodic": {
                "memory_id": "mem-epi-def4567890",
                "memory_arn": "arn:aws:bedrock-agentcore:eu-west-2:123:memory/mem-epi-def4567890",
                "memory_name": "better-memory-episodic",
                "strategy_id": "episodicReflections-qPr9876543",
                "strategy_name": "episodicReflections",
                "event_expiry_duration_days": 90,
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))

    cfg = _config(
        storage_backend="agentcore",
        agentcore_semantic_memory_id="mem-sem-abc1234567",
        agentcore_episodic_memory_id="mem-epi-def4567890",
    )

    from better_memory.storage.agentcore import AgentCoreBackend
    backend = build_backend(
        config=cfg,
        memory_conn=None,  # not used in agentcore mode
        embedder=None,
        session_id="s",
        project="p",
    )
    assert isinstance(backend, AgentCoreBackend)


def test_build_backend_agentcore_raises_when_config_missing(tmp_path, monkeypatch) -> None:
    """Without agentcore.json, factory raises — operator must run `agentcore init`."""
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    cfg = _config(
        storage_backend="agentcore",
        agentcore_semantic_memory_id="mem-sem-abc1234567",
        agentcore_episodic_memory_id="mem-epi-def4567890",
    )
    with pytest.raises(FileNotFoundError, match="agentcore.json"):
        build_backend(
            config=cfg,
            memory_conn=None,
            embedder=None,
            session_id="s",
            project="p",
        )
```

Drop the now-obsolete `test_build_backend_agentcore_raises_until_plan2` test.

Also update the `_ConfigLike` Protocol in `factory.py` if it now needs `agentcore_region` / `agentcore_semantic_memory_id` etc. Or add a new structural type for the agentcore branch.

- [ ] **Step 2: Run; verify failures** — 2 failures.

- [ ] **Step 3: Replace the agentcore branch in factory.py**

```python
"""Backend factory — picks SqliteBackend or AgentCoreBackend based on config."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Protocol

import boto3
from botocore.config import Config as BotoConfig

from better_memory.storage.agentcore import AgentCoreBackend
from better_memory.storage.agentcore_persistence import (
    AgentCoreConfigError,
    load_agentcore_config,
)
from better_memory.storage.protocol import StorageBackend
from better_memory.storage.sqlite import SqliteBackend


class _ConfigLike(Protocol):
    @property
    def storage_backend(self) -> str: ...
    @property
    def agentcore_region(self) -> str: ...


def _resolve_home() -> Path:
    home = os.environ.get("BETTER_MEMORY_HOME", "~/.better-memory")
    return Path(home).expanduser()


def build_backend(
    *,
    config: _ConfigLike,
    memory_conn: sqlite3.Connection | None,
    embedder: Any = None,
    session_id: str | None,
    project: str,
) -> StorageBackend:
    if config.storage_backend == "sqlite":
        if memory_conn is None:
            raise ValueError("sqlite backend requires memory_conn")
        return SqliteBackend(
            memory_conn=memory_conn,
            embedder=embedder,
            session_id=session_id,
            project=project,
        )
    if config.storage_backend == "agentcore":
        home = _resolve_home()
        ac_cfg = load_agentcore_config(home)
        if ac_cfg is None:
            raise FileNotFoundError(
                f"{home}/agentcore.json not found. Run `better-memory agentcore init` "
                f"to create the memory resources and persist their IDs."
            )

        boto_config = BotoConfig(
            region_name=config.agentcore_region,
            retries={"mode": "standard", "max_attempts": 5},
        )
        data_client = boto3.client(
            "bedrock-agentcore", config=boto_config
        )
        control_client = boto3.client(
            "bedrock-agentcore-control", config=boto_config
        )
        return AgentCoreBackend(
            config=ac_cfg,
            data_client=data_client,
            control_client=control_client,
            session_id=session_id,
            project=project,
        )
    raise ValueError(f"unknown storage_backend={config.storage_backend!r}")
```

The factory now imports `boto3` unconditionally. If we want to keep the import optional (sqlite-only operators shouldn't need boto3 installed), wrap the boto3 + agentcore imports in the agentcore branch with a try/except or move them inside the function body. Pin the choice at impl time.

- [ ] **Step 4: Update existing sqlite-mode factory tests**

The `memory_conn: sqlite3.Connection | None` signature change may affect sqlite tests. Audit existing tests in `tests/storage/test_factory.py` and fix any that now miss a kwarg.

- [ ] **Step 5: Run; verify pass**

Run: `uv run pytest tests/storage/test_factory.py -v`
Expected: all pass (sqlite branch unchanged behaviour; agentcore branch returns AgentCoreBackend with valid config; raises FileNotFoundError without).

- [ ] **Step 6: Run all storage + mcp tests**

Run: `uv run pytest tests/storage/ tests/mcp/ -q --tb=line`
Expected: all pass (Plan 1's MCP wiring still works; agentcore mode is now constructible).

- [ ] **Step 7: pyright clean**

Run: `uv run pyright better_memory tests`
Expected: 0 errors.

- [ ] **Step 8: Commit**

```bash
git add better_memory/storage/factory.py tests/storage/test_factory.py
git commit -m "feat(storage): factory builds AgentCoreBackend from agentcore.json"
```

---

## Post-plan verification

```bash
uv run pytest tests/storage/ tests/services/ tests/mcp/ tests/test_config.py -q --tb=line --junitxml=plan2.xml
uv run pyright better_memory tests
```

Expected:
- All passing in sqlite mode (no regression).
- All passing in agentcore mode under mocked boto3 (every protocol method exercised through `AgentCoreBackend`).
- 0 pyright errors above the post-Plan-1 baseline.

After this plan, `BETTER_MEMORY_STORAGE_BACKEND=agentcore` starts a working MCP server (assuming a valid `agentcore.json` exists). The server will reach AWS on the first observe / retrieve / record_use call.

## What's NOT in this plan (Plan 3)

- `better-memory agentcore init / status / smoke / migrate-from-sqlite` CLI — Plan 3.
- Stop-hook closure event firing in agentcore mode (`better_memory/hooks/session_close.py` modify) — Plan 3.
- Integration tests against real AWS (`tests/integration/test_agentcore_roundtrip.py`) — Plan 3.
- README + website docs (agentcore-setup, configuration, troubleshooting) — Plan 3.

## Spec coverage check

| Spec section | Covered by |
|---|---|
| Storage layer abstraction (Protocol + dispatch) | Plan 1 + Plan 2 Task 1 (supports_episodes flag) |
| Two AgentCore memory resources | Plan 3 (CLI init) |
| `actorId` encodes project | Task 2 (session.py) |
| Namespace shape | Task 2 (session.py) |
| Memory record metadata schema (episodic) | Plan 3 (CLI init declares the schema) |
| Memory record metadata schema (semantic) | Plan 3 (CLI init declares the schema) |
| Reflection content shape parsing | Task 7 (retrieve) |
| Session lifecycle / closure event | Plan 3 |
| Rating model (cross-backend parity) | Tasks 8, 9 |
| `config.py` modify | Plan 1 (shipped) |
| `storage/protocol.py` modify | Task 1 |
| `storage/sqlite.py` modify | Task 1 |
| `storage/agentcore.py` new | Tasks 4–12 |
| `storage/session.py` new | Task 2 |
| `storage/agentcore_persistence.py` new | Task 3 |
| `hooks/session_close.py` modify | Plan 3 |
| `mcp/server.py` modify | (Plan 1 already; Plan 2 Task 1 may add UI flag plumbing) |
| `cli/agentcore.py` new | Plan 3 |
| Error handling table | Tasks 5–12 (per-method failure paths surface RuntimeError / RetryableStorageError) |
| Testing strategy unit | Tasks 2–12 |
| Testing strategy integration / CLI / smoke | Plan 3 |
| Documentation | Plan 3 |
| Spike findings — defensive design rules | Tasks 4–12 (full metadata snapshot, declared-only metadata keys, namespace string semantics, `OTHER` role) |
