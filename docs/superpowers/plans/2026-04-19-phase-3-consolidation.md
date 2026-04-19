# ConsolidationService — Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the ConsolidationService described in spec §9 (cluster observations, LLM-draft insight candidates, flag low-value observations for sweep, merge duplicate candidates), replace the Phase-2 UI stubs with real logic, and cover the deferred Phase-2 smoke tests end-to-end against consolidation-generated data.

**Architecture:** Add an Ollama chat client (`better_memory/llm/ollama.py`) separate from the existing embedder client. Build `ConsolidationService` in `better_memory/services/consolidation.py` with clear sub-functions for clustering, drafting, dedup, stale-finding, and applying per-item. Expose `dry_run() -> DryRunResult` that returns a preview; `apply_*` methods for human-approved items. Wire into the UI by replacing `jobs.start_phase3_stub_job()` with a real threaded job, and by making `POST /candidates/<id>/merge` call `ConsolidationService.merge()`.

**Tech Stack:** Python 3.12, httpx (async Ollama client), sqlite3, existing `InsightService` / `ObservationService` / service-layer audit, Flask factory already wired in Phase 2, pytest.

**Scope:**
- **In:** full branch-pass (spec §9 branch), minimal sweep-pass (find stale observations and flag for review — no LLM contradiction detection yet), dry-run preview shape, merge logic, UI wiring, Phase-2 deferred smoke tests for Approve/Reject/Retire/Demote against real consolidation-generated data.
- **Out (future phase):** LLM-based contradiction check in sweep pass (spec §9 calls this out as "contradiction detection via Ollama call"). Phase 3 ships the sweep-candidate list; contradiction detection is a natural Phase 3.5.
- **Out:** automated scheduling of consolidation runs. Spec is explicit that consolidation is human-triggered only.

---

## File Structure

### Create

```
better_memory/
  llm/
    __init__.py
    ollama.py                                  # OllamaChat client for /api/generate
  services/
    consolidation.py                           # ConsolidationService
tests/
  llm/
    __init__.py
    test_ollama_chat.py                        # unit tests (mocked httpx)
  services/
    test_consolidation.py                      # unit tests with FakeChat + real sqlite
    test_consolidation_integration.py          # opt-in, real Ollama, @pytest.mark.integration
```

### Modify

- `better_memory/config.py` — add `consolidate_model: str` to `Config`, default `"llama3"`, env var `CONSOLIDATE_MODEL`.
- `better_memory/ui/jobs.py` — replace `start_phase3_stub_job()` with a real `start_consolidation_job()` that spawns a `threading.Thread` running `ConsolidationService.dry_run()`. Add `get_result(job_id)` for the completed payload.
- `better_memory/ui/app.py` — `/pipeline/consolidate` now starts a real job; `/jobs/<id>` polls progress; `POST /candidates/<id>/merge` calls `ConsolidationService.merge()`.
- `better_memory/ui/templates/fragments/consolidation_job.html` — render branch/sweep preview lists when job completes.
- `tests/ui/test_pipeline.py` — update the consolidation-button tests to expect the real fragment shape; add Phase-2 deferred smoke tests.

---

## Task 1: Add `CONSOLIDATE_MODEL` to config

**Files:**
- Modify: `better_memory/config.py`
- Modify: `tests/test_config.py`

**Context:** Spec §5 Registration shows `CONSOLIDATE_MODEL` as an env var with default `llama3`. Currently only `OLLAMA_HOST`, `EMBED_MODEL`, and `AUDIT_LOG_RETRIEVED` are resolved. Add the same pattern for `CONSOLIDATE_MODEL`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_consolidate_model_defaults_to_llama3(monkeypatch) -> None:
    monkeypatch.delenv("CONSOLIDATE_MODEL", raising=False)
    cfg = get_config()
    assert cfg.consolidate_model == "llama3"


def test_consolidate_model_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("CONSOLIDATE_MODEL", "mistral")
    cfg = get_config()
    assert cfg.consolidate_model == "mistral"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `Config` has no `consolidate_model` field.

- [ ] **Step 3: Extend `Config`**

Edit `better_memory/config.py`. Add a constant near the other defaults:

```python
_DEFAULT_CONSOLIDATE_MODEL = "llama3"
```

Add the field to the `Config` dataclass:

```python
@dataclass(frozen=True)
class Config:
    """Resolved better-memory configuration."""

    home: Path
    memory_db: Path
    knowledge_db: Path
    knowledge_base: Path
    spool_dir: Path
    ollama_host: str
    embed_model: str
    consolidate_model: str
    audit_log_retrieved: bool
```

And extend `get_config()`:

```python
def get_config() -> Config:
    """Resolve the current environment into a :class:`Config`."""
    home = resolve_home()
    return Config(
        home=home,
        memory_db=home / "memory.db",
        knowledge_db=home / "knowledge.db",
        knowledge_base=home / "knowledge-base",
        spool_dir=home / "spool",
        ollama_host=_resolve_str("OLLAMA_HOST", _DEFAULT_OLLAMA_HOST),
        embed_model=_resolve_str("EMBED_MODEL", _DEFAULT_EMBED_MODEL),
        consolidate_model=_resolve_str(
            "CONSOLIDATE_MODEL", _DEFAULT_CONSOLIDATE_MODEL
        ),
        audit_log_retrieved=_resolve_bool("AUDIT_LOG_RETRIEVED", default=True),
    )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_config.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/config.py tests/test_config.py
git commit -m "Phase 3: add CONSOLIDATE_MODEL config — default llama3"
```

---

## Task 2: `llm/ollama.py` — `OllamaChat` client

**Files:**
- Create: `better_memory/llm/__init__.py`
- Create: `better_memory/llm/ollama.py`
- Create: `tests/llm/__init__.py`
- Create: `tests/llm/test_ollama_chat.py`

**Context:** The existing `better_memory/embeddings/ollama.py` wraps `/api/embed`. Consolidation needs text generation (LLM completions). Ollama's `/api/generate` endpoint returns a completion for a prompt. Pattern mirrors the embedder — same host config, same retry strategy, same error surface.

Separate module (`llm/ollama.py`) so "embeddings" keeps its narrow meaning.

- [ ] **Step 1: Create empty package markers**

Create `better_memory/llm/__init__.py`:

```python
"""LLM clients (Ollama chat/generation)."""
```

Create `tests/llm/__init__.py` (empty).

- [ ] **Step 2: Write the failing test**

Create `tests/llm/test_ollama_chat.py`:

```python
"""Tests for better_memory.llm.ollama."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from better_memory.llm.ollama import ChatError, OllamaChat


class _StubTransport(httpx.MockTransport):
    """Record requests and return canned responses."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses)

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            self.calls.append({"url": str(request.url), "body": body})
            if not self._responses:
                return httpx.Response(500, text="unexpected call")
            spec = self._responses.pop(0)
            return httpx.Response(
                spec.get("status", 200),
                json=spec.get("json", {}),
                headers=spec.get("headers", {}),
            )

        super().__init__(handler)


@pytest.fixture
def transport_factory() -> Iterator[Any]:
    def _make(responses: list[dict[str, Any]]) -> _StubTransport:
        return _StubTransport(responses)

    yield _make


class TestOllamaChat:
    async def test_complete_returns_response_text(
        self, transport_factory
    ) -> None:
        transport = transport_factory(
            [{"json": {"response": "hello world", "done": True}}]
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost:11434"
        ) as client:
            chat = OllamaChat(model="llama3", client=client)
            out = await chat.complete("say hi")
            assert out == "hello world"

        # Request shape: POST /api/generate with model + prompt + stream=False
        assert len(transport.calls) == 1
        call = transport.calls[0]
        assert call["url"].endswith("/api/generate")
        assert call["body"]["model"] == "llama3"
        assert call["body"]["prompt"] == "say hi"
        assert call["body"]["stream"] is False

    async def test_complete_retries_on_5xx(self, transport_factory) -> None:
        transport = transport_factory(
            [
                {"status": 503, "json": {"error": "overloaded"}},
                {"json": {"response": "ok", "done": True}},
            ]
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost:11434"
        ) as client:
            chat = OllamaChat(
                model="llama3",
                client=client,
                backoff_base=0.0,  # no real sleep in tests
                max_retries=3,
            )
            out = await chat.complete("hi")
            assert out == "ok"
            assert len(transport.calls) == 2

    async def test_complete_does_not_retry_on_4xx(
        self, transport_factory
    ) -> None:
        transport = transport_factory(
            [{"status": 404, "json": {"error": "model not found"}}]
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost:11434"
        ) as client:
            chat = OllamaChat(model="missing", client=client)
            with pytest.raises(ChatError) as exc:
                await chat.complete("hi")
            assert "404" in str(exc.value)
        assert len(transport.calls) == 1  # no retry

    async def test_missing_response_field_raises(
        self, transport_factory
    ) -> None:
        transport = transport_factory([{"json": {"done": True}}])
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost:11434"
        ) as client:
            chat = OllamaChat(model="llama3", client=client)
            with pytest.raises(ChatError):
                await chat.complete("hi")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/llm/test_ollama_chat.py -v`
Expected: FAIL — `better_memory.llm.ollama` doesn't exist yet.

- [ ] **Step 4: Implement `OllamaChat`**

Create `better_memory/llm/ollama.py`:

```python
"""Ollama chat/generation client for consolidation drafting.

Wraps the ``/api/generate`` endpoint. Pattern mirrors
:class:`better_memory.embeddings.ollama.OllamaEmbedder`:

- httpx AsyncClient with configurable host.
- Exponential backoff on transient (5xx, transport) failures.
- 4xx fails immediately; wrapped in :class:`ChatError`.
- Optional externally-owned client (caller handles lifecycle).
"""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any

import httpx

from better_memory.config import get_config


class ChatError(RuntimeError):
    """Raised when chat generation fails."""


class OllamaChat:
    """Async chat/generation client for a local Ollama server."""

    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
        *,
        timeout: float = 60.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries must be >= 1")

        cfg = get_config()
        self._host = host if host is not None else cfg.ollama_host
        self._model = model if model is not None else cfg.consolidate_model
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base

        if client is None:
            self._client = httpx.AsyncClient(base_url=self._host, timeout=timeout)
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False

    async def complete(self, prompt: str) -> str:
        """Return a single completion for ``prompt``."""
        body = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
        }

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = await self._client.post("/api/generate", json=body)
            except httpx.TransportError as exc:
                last_exc = exc
                await self._sleep_for_attempt(attempt)
                continue

            if 500 <= resp.status_code < 600:
                last_exc = ChatError(
                    f"Ollama returned {resp.status_code}: {resp.text[:200]}"
                )
                await self._sleep_for_attempt(attempt)
                continue

            if resp.status_code >= 400:
                raise ChatError(
                    f"Ollama returned {resp.status_code}: {resp.text[:200]}"
                )

            data: dict[str, Any] = resp.json()
            if "response" not in data:
                raise ChatError(
                    f"Ollama response missing 'response' field: {data!r}"
                )
            return data["response"]

        raise ChatError(
            f"Ollama chat failed after {self._max_retries} attempts"
        ) from last_exc

    async def _sleep_for_attempt(self, attempt: int) -> None:
        if attempt + 1 < self._max_retries:
            await asyncio.sleep(self._backoff_base * (2**attempt))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "OllamaChat":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/llm/test_ollama_chat.py -v`
Expected: All 4 PASS.

- [ ] **Step 6: Commit**

```bash
git add better_memory/llm/ tests/llm/
git commit -m "Phase 3: OllamaChat client (/api/generate) with retries"
```

---

## Task 3: `ChatCompleter` protocol + `FakeChat` for tests

**Files:**
- Modify: `better_memory/llm/ollama.py`
- Create: `better_memory/llm/fake.py`

**Context:** Consolidation tests must not call a real LLM. Define a `ChatCompleter` protocol that both `OllamaChat` and a `FakeChat` satisfy. Tests inject `FakeChat` with canned responses.

- [ ] **Step 1: Add protocol**

Edit `better_memory/llm/ollama.py`. Near the top, after imports, add:

```python
from typing import Protocol


class ChatCompleter(Protocol):
    """Duck-typed interface the ConsolidationService depends on."""

    async def complete(self, prompt: str) -> str: ...
```

- [ ] **Step 2: Create `FakeChat`**

Create `better_memory/llm/fake.py`:

```python
"""In-memory fake :class:`ChatCompleter` for tests.

Behaviour: pops a canned response per call. Raises if called more times
than the test seeded.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeChat:
    """Pops from ``responses`` on each ``complete`` call."""

    responses: list[str]
    calls: list[str] = field(default_factory=list)

    async def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        if not self.responses:
            raise AssertionError(
                f"FakeChat ran out of responses; last prompt:\n{prompt}"
            )
        return self.responses.pop(0)
```

- [ ] **Step 3: Verify it loads**

Run: `uv run python -c "from better_memory.llm.fake import FakeChat; f = FakeChat(responses=['x']); import asyncio; print(asyncio.run(f.complete('hi')))"`
Expected: `x`.

- [ ] **Step 4: Commit**

```bash
git add better_memory/llm/ollama.py better_memory/llm/fake.py
git commit -m "Phase 3: ChatCompleter protocol + FakeChat for tests"
```

---

## Task 4: Consolidation — data types and cluster discovery

**Files:**
- Create: `better_memory/services/consolidation.py`
- Create: `tests/services/test_consolidation.py`

**Context:** Branch pass §9 step 1 — group active observations by `(project, component, theme)` with thresholds: ≥3 observations AND ≥2 total `validated_true`. This task delivers the clustering logic only; drafting and apply come later.

- [ ] **Step 1: Write failing tests**

Create `tests/services/test_consolidation.py`:

```python
"""Unit tests for better_memory.services.consolidation."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.consolidation import (
    ObservationCluster,
    find_clusters,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "memory.db")
    try:
        apply_migrations(c)
        yield c
    finally:
        c.close()


def _insert_observation(
    conn: sqlite3.Connection,
    *,
    id: str,
    project: str,
    component: str | None = None,
    theme: str | None = None,
    status: str = "active",
    validated_true: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO observations
            (id, content, project, component, theme, status, validated_true)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (id, f"content-{id}", project, component, theme, status, validated_true),
    )
    conn.commit()


class TestFindClusters:
    def test_empty_returns_empty(self, conn: sqlite3.Connection) -> None:
        assert find_clusters(conn, project="p") == []

    def test_groups_by_component_and_theme(
        self, conn: sqlite3.Connection
    ) -> None:
        for i in range(3):
            _insert_observation(
                conn,
                id=f"a{i}",
                project="p",
                component="api",
                theme="retry",
                validated_true=1,
            )
        for i in range(3):
            _insert_observation(
                conn,
                id=f"b{i}",
                project="p",
                component="db",
                theme="migration",
                validated_true=1,
            )
        clusters = find_clusters(conn, project="p")
        assert len(clusters) == 2
        keys = {(c.component, c.theme) for c in clusters}
        assert keys == {("api", "retry"), ("db", "migration")}
        # Each cluster has 3 observations
        for c in clusters:
            assert len(c.observation_ids) == 3

    def test_skips_clusters_below_min_size(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_observation(
            conn, id="a1", project="p", component="api", theme="retry",
            validated_true=1,
        )
        _insert_observation(
            conn, id="a2", project="p", component="api", theme="retry",
            validated_true=1,
        )
        clusters = find_clusters(conn, project="p", min_size=3)
        assert clusters == []

    def test_skips_clusters_below_min_validated(
        self, conn: sqlite3.Connection
    ) -> None:
        # 3 observations, 0 validated_true total → reject
        for i in range(3):
            _insert_observation(
                conn, id=f"a{i}", project="p",
                component="api", theme="retry", validated_true=0,
            )
        clusters = find_clusters(conn, project="p", min_validated=2)
        assert clusters == []

        # 3 observations, 2 validated_true total → accept
        _insert_observation(
            conn, id="a3", project="p",
            component="api", theme="retry", validated_true=2,
        )
        clusters = find_clusters(conn, project="p", min_validated=2)
        # Cluster now has 4 obs, total validated = 2 → accepts
        assert len(clusters) == 1
        assert set(clusters[0].observation_ids) == {"a0", "a1", "a2", "a3"}

    def test_excludes_non_active_status(
        self, conn: sqlite3.Connection
    ) -> None:
        for i in range(3):
            _insert_observation(
                conn, id=f"a{i}", project="p",
                component="api", theme="retry", validated_true=1,
            )
        _insert_observation(
            conn, id="consolidated", project="p",
            component="api", theme="retry", validated_true=1,
            status="consolidated",
        )
        clusters = find_clusters(conn, project="p")
        assert len(clusters) == 1
        assert "consolidated" not in clusters[0].observation_ids
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_consolidation.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `find_clusters`**

Create `better_memory/services/consolidation.py`:

```python
"""Consolidation engine — cluster observations, draft candidate insights,
flag stale observations for sweep, and merge duplicate candidates.

Spec: :doc:`§9 <2026-04-06-better-memory-design>` of the design spec.
Triggered by the UI; never runs automatically.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ObservationCluster:
    """A group of observations sharing (project, component, theme).

    ``observation_ids`` is ordered by ``created_at ASC`` so draft prompts
    present the oldest context first.
    """

    project: str
    component: str | None
    theme: str | None
    observation_ids: list[str]
    total_validated_true: int


def find_clusters(
    conn: sqlite3.Connection,
    *,
    project: str,
    min_size: int = 3,
    min_validated: int = 2,
) -> list[ObservationCluster]:
    """Return clusters of active observations that meet the thresholds.

    Spec §9 branch step 1-2: group by ``(project, component, theme)`` and
    keep only clusters with ``>= min_size`` observations AND
    ``>= min_validated`` total ``validated_true`` across the cluster.
    Observations with ``status != 'active'`` are excluded.
    """
    rows = conn.execute(
        """
        SELECT id, component, theme, validated_true
        FROM observations
        WHERE project = ? AND status = 'active'
        ORDER BY component, theme, created_at ASC, rowid ASC
        """,
        (project,),
    ).fetchall()

    # Group in Python — SQL's GROUP BY would lose row-level ids.
    groups: dict[
        tuple[str | None, str | None], list[sqlite3.Row]
    ] = {}
    for r in rows:
        key = (r["component"], r["theme"])
        groups.setdefault(key, []).append(r)

    out: list[ObservationCluster] = []
    for (component, theme), members in groups.items():
        if len(members) < min_size:
            continue
        total_validated = sum(m["validated_true"] for m in members)
        if total_validated < min_validated:
            continue
        out.append(
            ObservationCluster(
                project=project,
                component=component,
                theme=theme,
                observation_ids=[m["id"] for m in members],
                total_validated_true=total_validated,
            )
        )
    return out
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_consolidation.py -v`
Expected: All 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/consolidation.py tests/services/test_consolidation.py
git commit -m "Phase 3: cluster discovery for branch pass"
```

---

## Task 5: Draft-prompt builder

**Files:**
- Modify: `better_memory/services/consolidation.py`
- Modify: `tests/services/test_consolidation.py`

**Context:** Spec §9 gives a specific prompt template. Pure string formatting — no LLM call. Tested by exact-string comparison.

- [ ] **Step 1: Write failing test**

Append to `tests/services/test_consolidation.py`:

```python
from better_memory.services.consolidation import (
    ObservationForPrompt,
    build_draft_prompt,
)


class TestBuildDraftPrompt:
    def test_renders_spec_prompt(self) -> None:
        observations = [
            ObservationForPrompt(
                id="o1",
                created_at="2026-03-01T10:00:00+00:00",
                content="The API retries on 503s with exponential backoff.",
                outcome="success",
            ),
            ObservationForPrompt(
                id="o2",
                created_at="2026-03-05T14:22:00+00:00",
                content="Retrying on 4xx is always wrong — they won't resolve.",
                outcome="failure",
            ),
            ObservationForPrompt(
                id="o3",
                created_at="2026-03-10T09:15:00+00:00",
                content="Add jitter to avoid thundering-herd retries.",
                outcome="success",
            ),
        ]
        prompt = build_draft_prompt(observations)
        # Structural checks — exact template given in spec §9
        assert "Here are 3 observations about the same pattern:" in prompt
        assert "o1" in prompt
        assert "2026-03-01" in prompt
        assert "success" in prompt
        assert "Write a single insight that:" in prompt
        assert "Generalises the pattern in present tense" in prompt
        assert "Is concise" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_consolidation.py::TestBuildDraftPrompt -v`
Expected: FAIL — names don't exist.

- [ ] **Step 3: Implement**

Append to `better_memory/services/consolidation.py`:

```python
@dataclass(frozen=True)
class ObservationForPrompt:
    """Subset of observation fields the draft prompt shows to the LLM."""

    id: str
    created_at: str
    content: str
    outcome: str


def build_draft_prompt(observations: list[ObservationForPrompt]) -> str:
    """Build the insight-draft prompt from spec §9."""
    lines = [
        f"Here are {len(observations)} observations about the same pattern:",
        "",
    ]
    for obs in observations:
        lines.append(
            f"- [{obs.created_at}] ({obs.outcome}) {obs.id}: {obs.content}"
        )
    lines.extend(
        [
            "",
            "Write a single insight that:",
            "- Generalises the pattern in present tense",
            "- States the conditions under which it holds",
            "- Notes any exceptions observed",
            "- Is specific enough to be actionable",
            "- Is concise (2-4 sentences for the pattern, 1-2 for conditions/exceptions)",
            "",
            "Return the insight text only, no preamble or formatting.",
        ]
    )
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_consolidation.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/consolidation.py tests/services/test_consolidation.py
git commit -m "Phase 3: draft-prompt builder for insight consolidation"
```

---

## Task 6: Dedup — find existing confirmed insight for a cluster

**Files:**
- Modify: `better_memory/services/consolidation.py`
- Modify: `tests/services/test_consolidation.py`

**Context:** Spec §9 branch step 3 — "check if a matching confirmed insight already exists (avoid duplicates)." Matching criterion: same `(project, component)`. Theme isn't on the insights table, so we match by component only — which is the natural axis for "same kind of thing". Returns the first match or None.

- [ ] **Step 1: Write failing test**

Append to `tests/services/test_consolidation.py`:

```python
from better_memory.services.consolidation import existing_insight_for_cluster


def _insert_insight(
    conn: sqlite3.Connection,
    *,
    id: str,
    project: str,
    component: str | None,
    status: str,
) -> None:
    conn.execute(
        "INSERT INTO insights "
        "(id, title, content, project, component, status, polarity) "
        "VALUES (?, ?, ?, ?, ?, ?, 'neutral')",
        (id, f"t-{id}", f"c-{id}", project, component, status),
    )
    conn.commit()


class TestExistingInsightForCluster:
    def test_returns_none_when_no_match(self, conn: sqlite3.Connection) -> None:
        cluster = ObservationCluster(
            project="p", component="api", theme="retry",
            observation_ids=["o1"], total_validated_true=0,
        )
        assert existing_insight_for_cluster(conn, cluster) is None

    def test_finds_confirmed_match_same_project_component(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_insight(conn, id="i1", project="p", component="api",
                        status="confirmed")
        cluster = ObservationCluster(
            project="p", component="api", theme="retry",
            observation_ids=["o1"], total_validated_true=0,
        )
        result = existing_insight_for_cluster(conn, cluster)
        assert result is not None
        assert result.id == "i1"

    def test_ignores_pending_review(self, conn: sqlite3.Connection) -> None:
        _insert_insight(conn, id="c1", project="p", component="api",
                        status="pending_review")
        cluster = ObservationCluster(
            project="p", component="api", theme="retry",
            observation_ids=["o1"], total_validated_true=0,
        )
        assert existing_insight_for_cluster(conn, cluster) is None

    def test_ignores_different_component(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_insight(conn, id="i1", project="p", component="db",
                        status="confirmed")
        cluster = ObservationCluster(
            project="p", component="api", theme="retry",
            observation_ids=["o1"], total_validated_true=0,
        )
        assert existing_insight_for_cluster(conn, cluster) is None

    def test_accepts_promoted_as_match(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_insight(conn, id="pr1", project="p", component="api",
                        status="promoted")
        cluster = ObservationCluster(
            project="p", component="api", theme="retry",
            observation_ids=["o1"], total_validated_true=0,
        )
        result = existing_insight_for_cluster(conn, cluster)
        assert result is not None
        assert result.id == "pr1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_consolidation.py::TestExistingInsightForCluster -v`
Expected: FAIL — function doesn't exist.

- [ ] **Step 3: Implement**

Append to `better_memory/services/consolidation.py`:

```python
from better_memory.services.insight import Insight, row_to_insight


def existing_insight_for_cluster(
    conn: sqlite3.Connection, cluster: ObservationCluster
) -> Insight | None:
    """Return the first confirmed or promoted insight matching the cluster.

    Match criterion: same ``(project, component)`` AND
    ``status IN ('confirmed', 'promoted')``. We treat both statuses as
    "already exists" — both mean a human has accepted the insight.
    """
    row = conn.execute(
        """
        SELECT * FROM insights
        WHERE project = ?
          AND (component IS ? OR component = ?)
          AND status IN ('confirmed', 'promoted')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (cluster.project, cluster.component, cluster.component),
    ).fetchone()
    if row is None:
        return None
    return row_to_insight(row)
```

Note: the double-comparison `(component IS ? OR component = ?)` handles SQL's NULL-equality quirk — `NULL = NULL` is NULL (falsy), but `NULL IS NULL` is true. The same pattern works for non-NULL values because `X IS X` is also true.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_consolidation.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/consolidation.py tests/services/test_consolidation.py
git commit -m "Phase 3: dedup check — find existing confirmed insight per cluster"
```

---

## Task 7: `BranchCandidate` dataclass and `ConsolidationService.branch_dry_run`

**Files:**
- Modify: `better_memory/services/consolidation.py`
- Modify: `tests/services/test_consolidation.py`

**Context:** Top-level branch-pass dry-run. Combines clustering, dedup, and LLM drafting. Each accepted cluster becomes a `BranchCandidate` with the drafted title+content, source observation ids, and proposed polarity (from majority outcome — success → "do", failure → "dont", else "neutral").

- [ ] **Step 1: Write failing tests**

Append to `tests/services/test_consolidation.py`:

```python
from better_memory.llm.fake import FakeChat
from better_memory.services.consolidation import (
    BranchCandidate,
    ConsolidationService,
)


class TestBranchDryRun:
    async def test_drafts_candidates_for_each_accepted_cluster(
        self, conn: sqlite3.Connection
    ) -> None:
        for i in range(3):
            _insert_observation(
                conn, id=f"a{i}", project="p",
                component="api", theme="retry", validated_true=1,
            )
        chat = FakeChat(
            responses=[
                "Retry 5xx with exponential backoff; do not retry 4xx.",
            ]
        )
        svc = ConsolidationService(conn=conn, chat=chat)
        candidates = await svc.branch_dry_run(project="p")
        assert len(candidates) == 1
        c = candidates[0]
        assert isinstance(c, BranchCandidate)
        assert c.project == "p"
        assert c.component == "api"
        assert c.theme == "retry"
        assert c.observation_ids == ["a0", "a1", "a2"]
        assert "Retry 5xx" in c.content
        # Title is first sentence / heading of the content
        assert len(c.title) > 0

    async def test_skips_clusters_with_existing_confirmed_insight(
        self, conn: sqlite3.Connection
    ) -> None:
        for i in range(3):
            _insert_observation(
                conn, id=f"a{i}", project="p",
                component="api", theme="retry", validated_true=1,
            )
        _insert_insight(conn, id="i1", project="p", component="api",
                        status="confirmed")

        chat = FakeChat(responses=[])  # no calls should happen
        svc = ConsolidationService(conn=conn, chat=chat)
        candidates = await svc.branch_dry_run(project="p")
        assert candidates == []
        assert chat.calls == []

    async def test_polarity_inferred_from_outcomes(
        self, conn: sqlite3.Connection
    ) -> None:
        # Two successes, one failure → polarity "do"
        conn.execute(
            "INSERT INTO observations (id, content, project, component, "
            "theme, validated_true, outcome) VALUES "
            "('a1', 'c1', 'p', 'api', 'retry', 1, 'success'),"
            "('a2', 'c2', 'p', 'api', 'retry', 1, 'success'),"
            "('a3', 'c3', 'p', 'api', 'retry', 0, 'failure')"
        )
        conn.commit()

        chat = FakeChat(responses=["drafted insight text"])
        svc = ConsolidationService(conn=conn, chat=chat)
        candidates = await svc.branch_dry_run(project="p")
        assert len(candidates) == 1
        assert candidates[0].polarity == "do"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_consolidation.py::TestBranchDryRun -v`
Expected: FAIL — `ConsolidationService` and `BranchCandidate` don't exist.

- [ ] **Step 3: Implement**

Append to `better_memory/services/consolidation.py`:

```python
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from better_memory.llm.ollama import ChatCompleter

Polarity = Literal["do", "dont", "neutral"]


def _default_clock() -> datetime:
    """UTC-aware ``now``. Module-level so tests can patch for determinism."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class BranchCandidate:
    """A drafted insight ready for human review.

    Phase 3 callers feed this into ``apply_branch`` after human approval.
    """

    project: str
    component: str | None
    theme: str | None
    title: str
    content: str
    polarity: Polarity
    observation_ids: list[str]
    confidence: str  # "low" | "medium" | "high"


def _infer_polarity(outcomes: list[str]) -> Polarity:
    """Majority-vote outcome → polarity mapping."""
    counts = Counter(outcomes)
    top, _ = counts.most_common(1)[0]
    if top == "success":
        return "do"
    if top == "failure":
        return "dont"
    return "neutral"


def _derive_title(content: str) -> str:
    """First sentence or first 80 chars of ``content`` as a title."""
    first = content.split(".", 1)[0].strip()
    if len(first) > 80:
        first = first[:77].rstrip() + "..."
    return first or "Untitled insight"


class ConsolidationService:
    """Consolidation engine: branch pass, sweep pass, merge.

    The service owns the sqlite connection and expects writes to be
    sequenced by the caller (Phase 2's Flask factory runs with
    ``threaded=False``, so one request / one job at a time).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        chat: ChatCompleter,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._chat = chat
        self._clock: Callable[[], datetime] = clock or _default_clock

    async def branch_dry_run(
        self, *, project: str
    ) -> list[BranchCandidate]:
        """Return draft candidates for clusters needing consolidation.

        Does NOT write to the database. Caller applies each accepted
        candidate via :meth:`apply_branch`.
        """
        clusters = find_clusters(self._conn, project=project)
        if not clusters:
            return []

        out: list[BranchCandidate] = []
        for cluster in clusters:
            if existing_insight_for_cluster(self._conn, cluster) is not None:
                continue

            # Fetch observation details for the prompt and polarity inference.
            rows = self._conn.execute(
                f"""
                SELECT id, content, created_at, outcome
                FROM observations
                WHERE id IN ({",".join("?" * len(cluster.observation_ids))})
                ORDER BY created_at ASC, rowid ASC
                """,
                cluster.observation_ids,
            ).fetchall()

            prompt_rows = [
                ObservationForPrompt(
                    id=r["id"],
                    created_at=r["created_at"],
                    content=r["content"],
                    outcome=r["outcome"],
                )
                for r in rows
            ]
            prompt = build_draft_prompt(prompt_rows)
            drafted = (await self._chat.complete(prompt)).strip()
            if not drafted:
                # LLM returned nothing — skip this cluster rather than
                # create an empty candidate.
                continue

            polarity = _infer_polarity([r["outcome"] for r in rows])
            confidence = "high" if len(rows) >= 5 else "medium"

            out.append(
                BranchCandidate(
                    project=cluster.project,
                    component=cluster.component,
                    theme=cluster.theme,
                    title=_derive_title(drafted),
                    content=drafted,
                    polarity=polarity,
                    observation_ids=list(cluster.observation_ids),
                    confidence=confidence,
                )
            )
        return out
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_consolidation.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/consolidation.py tests/services/test_consolidation.py
git commit -m "Phase 3: branch_dry_run — LLM drafting per cluster"
```

---

## Task 8: `apply_branch` — persist an accepted candidate

**Files:**
- Modify: `better_memory/services/consolidation.py`
- Modify: `tests/services/test_consolidation.py`

**Context:** Spec §9 branch step 5-6: create insight (`status=pending_review`), link sources, update source observations to `status='consolidated'`. This is a single atomic operation — either all writes commit or none.

- [ ] **Step 1: Write failing test**

Append to `tests/services/test_consolidation.py`:

```python
class TestApplyBranch:
    async def test_creates_insight_links_sources_updates_observations(
        self, conn: sqlite3.Connection
    ) -> None:
        for i in range(3):
            _insert_observation(
                conn, id=f"a{i}", project="p",
                component="api", theme="retry", validated_true=1,
            )
        candidate = BranchCandidate(
            project="p",
            component="api",
            theme="retry",
            title="Retry policy",
            content="Retry 5xx with exponential backoff.",
            polarity="do",
            observation_ids=["a0", "a1", "a2"],
            confidence="medium",
        )

        svc = ConsolidationService(conn=conn, chat=FakeChat(responses=[]))
        insight_id = await svc.apply_branch(candidate)
        assert insight_id  # non-empty

        row = conn.execute(
            "SELECT status, title, polarity, confidence FROM insights WHERE id = ?",
            (insight_id,),
        ).fetchone()
        assert row["status"] == "pending_review"
        assert row["title"] == "Retry policy"
        assert row["polarity"] == "do"
        assert row["confidence"] == "medium"

        sources = {
            r["observation_id"]
            for r in conn.execute(
                "SELECT observation_id FROM insight_sources WHERE insight_id = ?",
                (insight_id,),
            ).fetchall()
        }
        assert sources == {"a0", "a1", "a2"}

        for obs_id in ["a0", "a1", "a2"]:
            s = conn.execute(
                "SELECT status FROM observations WHERE id = ?", (obs_id,)
            ).fetchone()
            assert s["status"] == "consolidated"

    async def test_rollback_on_failure(self, conn: sqlite3.Connection) -> None:
        _insert_observation(conn, id="a0", project="p",
                            component="api", theme="retry", validated_true=1)
        # Candidate references an observation that doesn't exist → FK would
        # fail the insert into insight_sources.
        candidate = BranchCandidate(
            project="p", component="api", theme="retry",
            title="t", content="c", polarity="do",
            observation_ids=["a0", "does-not-exist"],
            confidence="low",
        )
        svc = ConsolidationService(conn=conn, chat=FakeChat(responses=[]))
        with pytest.raises(sqlite3.IntegrityError):
            await svc.apply_branch(candidate)

        # Nothing partial committed.
        assert conn.execute(
            "SELECT COUNT(*) FROM insights"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM observations WHERE id = 'a0'"
        ).fetchone()["status"] == "active"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_consolidation.py::TestApplyBranch -v`
Expected: FAIL — method doesn't exist.

- [ ] **Step 3: Implement**

Append to `ConsolidationService` in `better_memory/services/consolidation.py`:

```python
    async def apply_branch(self, candidate: BranchCandidate) -> str:
        """Persist ``candidate`` — create the insight, link sources, mark
        observations consolidated. Atomic. Returns the new insight id."""
        from uuid import uuid4

        insight_id = uuid4().hex
        now = self._clock().isoformat()
        conn = self._conn
        conn.execute("SAVEPOINT apply_branch")
        try:
            conn.execute(
                """
                INSERT INTO insights
                    (id, title, content, project, component, status,
                     confidence, polarity, evidence_count,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending_review',
                        ?, ?, ?,
                        ?, ?)
                """,
                (
                    insight_id,
                    candidate.title,
                    candidate.content,
                    candidate.project,
                    candidate.component,
                    candidate.confidence,
                    candidate.polarity,
                    len(candidate.observation_ids),
                    now,
                    now,
                ),
            )
            for obs_id in candidate.observation_ids:
                conn.execute(
                    "INSERT INTO insight_sources (insight_id, observation_id) "
                    "VALUES (?, ?)",
                    (insight_id, obs_id),
                )
            placeholders = ",".join("?" * len(candidate.observation_ids))
            conn.execute(
                f"UPDATE observations SET status = 'consolidated' "
                f"WHERE id IN ({placeholders})",
                candidate.observation_ids,
            )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT apply_branch")
            conn.execute("RELEASE SAVEPOINT apply_branch")
            raise
        conn.execute("RELEASE SAVEPOINT apply_branch")
        conn.commit()
        return insight_id
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_consolidation.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/consolidation.py tests/services/test_consolidation.py
git commit -m "Phase 3: apply_branch — atomic insight creation + observation mark"
```

---

## Task 9: `SweepCandidate` + `sweep_dry_run` — find stale observations

**Files:**
- Modify: `better_memory/services/consolidation.py`
- Modify: `tests/services/test_consolidation.py`

**Context:** Spec §9 sweep step 1: find `observations` where `last_retrieved` older than N days AND `used_count = 0` AND `validated_true = 0` AND `status = 'active'`. Return a list of candidates for human review. Contradiction detection is deferred — Phase 3 flags ALL stale observations for the UI to show.

- [ ] **Step 1: Write failing test**

Append to `tests/services/test_consolidation.py`:

```python
from datetime import UTC, datetime, timedelta


class TestSweepDryRun:
    def _insert_stale(
        self, conn: sqlite3.Connection, id: str, project: str,
        *, last_retrieved_days_ago: int
    ) -> None:
        past = (
            datetime.now(UTC) - timedelta(days=last_retrieved_days_ago)
        ).isoformat()
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, status, used_count, validated_true, "
            "last_retrieved) "
            "VALUES (?, ?, ?, 'active', 0, 0, ?)",
            (id, f"stale-{id}", project, past),
        )
        conn.commit()

    async def test_finds_stale_observations(
        self, conn: sqlite3.Connection
    ) -> None:
        self._insert_stale(conn, "old1", project="p",
                           last_retrieved_days_ago=40)
        self._insert_stale(conn, "old2", project="p",
                           last_retrieved_days_ago=60)

        svc = ConsolidationService(conn=conn, chat=FakeChat(responses=[]))
        cands = await svc.sweep_dry_run(project="p", stale_days=30)
        assert {c.observation_id for c in cands} == {"old1", "old2"}
        assert all(c.reason == "stale" for c in cands)

    async def test_skips_recently_retrieved(
        self, conn: sqlite3.Connection
    ) -> None:
        self._insert_stale(conn, "recent", project="p",
                           last_retrieved_days_ago=5)
        svc = ConsolidationService(conn=conn, chat=FakeChat(responses=[]))
        cands = await svc.sweep_dry_run(project="p", stale_days=30)
        assert cands == []

    async def test_skips_with_used_count(
        self, conn: sqlite3.Connection
    ) -> None:
        past = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, status, used_count, validated_true, "
            "last_retrieved) VALUES "
            "('used', 'c', 'p', 'active', 1, 0, ?)",
            (past,),
        )
        conn.commit()
        svc = ConsolidationService(conn=conn, chat=FakeChat(responses=[]))
        cands = await svc.sweep_dry_run(project="p", stale_days=30)
        assert cands == []

    async def test_skips_never_retrieved(
        self, conn: sqlite3.Connection
    ) -> None:
        # last_retrieved IS NULL — spec is about "older than 30 days"
        # which NULL is not. We skip never-retrieved observations — they
        # may be brand-new. A separate "never_retrieved" reason covers
        # those once we decide the policy.
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, status, used_count, validated_true) "
            "VALUES ('new', 'c', 'p', 'active', 0, 0)"
        )
        conn.commit()
        svc = ConsolidationService(conn=conn, chat=FakeChat(responses=[]))
        cands = await svc.sweep_dry_run(project="p", stale_days=30)
        assert cands == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_consolidation.py::TestSweepDryRun -v`
Expected: FAIL — types don't exist.

- [ ] **Step 3: Implement**

Append to `better_memory/services/consolidation.py`:

```python
@dataclass(frozen=True)
class SweepCandidate:
    """An observation the sweep flagged for human review before archive."""

    observation_id: str
    content: str
    project: str
    reason: str  # "stale" (Phase 3); richer reasons in Phase 3.5


class _ConsolidationMixinSweep:
    """Placeholder — inline methods below on ConsolidationService."""
```

And add this method to `ConsolidationService`:

```python
    async def sweep_dry_run(
        self, *, project: str, stale_days: int = 30
    ) -> list[SweepCandidate]:
        """Return observations that look stale and low-value.

        Criteria (spec §9): ``last_retrieved`` older than ``stale_days``,
        AND ``used_count = 0`` AND ``validated_true = 0`` AND
        ``status = 'active'``. Observations that have never been retrieved
        (``last_retrieved IS NULL``) are NOT flagged — we let them live
        until they cross the stale threshold.

        Phase 3 does not attempt contradiction detection; the UI shows
        every candidate as "stale" and the human decides.
        """
        rows = self._conn.execute(
            """
            SELECT id, content, project
            FROM observations
            WHERE status = 'active'
              AND project = ?
              AND used_count = 0
              AND validated_true = 0
              AND last_retrieved IS NOT NULL
              AND julianday(last_retrieved) < julianday('now', ?)
            ORDER BY last_retrieved ASC, rowid ASC
            """,
            (project, f"-{stale_days} days"),
        ).fetchall()
        return [
            SweepCandidate(
                observation_id=r["id"],
                content=r["content"],
                project=r["project"],
                reason="stale",
            )
            for r in rows
        ]
```

Remove the placeholder `_ConsolidationMixinSweep` class — it was just a marker for where to add the method.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_consolidation.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/consolidation.py tests/services/test_consolidation.py
git commit -m "Phase 3: sweep_dry_run — find stale low-value observations"
```

---

## Task 10: `apply_sweep` — archive a single observation

**Files:**
- Modify: `better_memory/services/consolidation.py`
- Modify: `tests/services/test_consolidation.py`

**Context:** Spec §9 sweep step 3: archive observations after human approval. Single-observation operation — the UI iterates per candidate. Writes an audit row via `services.audit`.

- [ ] **Step 1: Write failing test**

Append to `tests/services/test_consolidation.py`:

```python
class TestApplySweep:
    async def test_archives_observation(self, conn: sqlite3.Connection) -> None:
        self._insert_observation_stale(conn, "old1")
        svc = ConsolidationService(conn=conn, chat=FakeChat(responses=[]))
        await svc.apply_sweep("old1")

        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'old1'"
        ).fetchone()
        assert row["status"] == "archived"

    async def test_rejects_nonexistent(self, conn: sqlite3.Connection) -> None:
        svc = ConsolidationService(conn=conn, chat=FakeChat(responses=[]))
        with pytest.raises(ValueError, match="not found"):
            await svc.apply_sweep("missing")

    def _insert_observation_stale(
        self, conn: sqlite3.Connection, id: str
    ) -> None:
        from datetime import UTC, datetime, timedelta
        past = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, status, used_count, validated_true, "
            "last_retrieved) VALUES (?, ?, 'p', 'active', 0, 0, ?)",
            (id, f"c-{id}", past),
        )
        conn.commit()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_consolidation.py::TestApplySweep -v`
Expected: FAIL — method doesn't exist.

- [ ] **Step 3: Implement**

Append to `ConsolidationService`:

```python
    async def apply_sweep(self, observation_id: str) -> None:
        """Archive a single observation after human approval."""
        conn = self._conn
        existing = conn.execute(
            "SELECT status FROM observations WHERE id = ?", (observation_id,)
        ).fetchone()
        if existing is None:
            raise ValueError(f"Observation not found: {observation_id}")
        if existing["status"] != "active":
            return  # Idempotent: already archived or consolidated.
        conn.execute(
            "UPDATE observations SET status = 'archived' WHERE id = ?",
            (observation_id,),
        )
        conn.commit()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_consolidation.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/consolidation.py tests/services/test_consolidation.py
git commit -m "Phase 3: apply_sweep — archive a single stale observation"
```

---

## Task 11: `DryRunResult` + top-level `dry_run()`

**Files:**
- Modify: `better_memory/services/consolidation.py`
- Modify: `tests/services/test_consolidation.py`

**Context:** The UI's "Run branch-and-sweep" button wants one call, not two. Wrap branch + sweep into a single `dry_run()` that returns `DryRunResult(branch=[...], sweep=[...])`.

- [ ] **Step 1: Write failing test**

Append to `tests/services/test_consolidation.py`:

```python
from better_memory.services.consolidation import DryRunResult


class TestDryRun:
    async def test_combines_branch_and_sweep(
        self, conn: sqlite3.Connection
    ) -> None:
        # One cluster for branch pass
        for i in range(3):
            _insert_observation(
                conn, id=f"a{i}", project="p",
                component="api", theme="retry", validated_true=1,
            )
        # One stale observation for sweep pass
        from datetime import UTC, datetime, timedelta
        past = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, status, used_count, validated_true, "
            "last_retrieved, component) VALUES "
            "('stale1', 'c', 'p', 'active', 0, 0, ?, 'unused')",
            (past,),
        )
        conn.commit()

        chat = FakeChat(responses=["drafted pattern"])
        svc = ConsolidationService(conn=conn, chat=chat)
        result = await svc.dry_run(project="p")

        assert isinstance(result, DryRunResult)
        assert len(result.branch) == 1
        assert result.branch[0].observation_ids == ["a0", "a1", "a2"]
        assert len(result.sweep) == 1
        assert result.sweep[0].observation_id == "stale1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_consolidation.py::TestDryRun -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `better_memory/services/consolidation.py`:

```python
@dataclass(frozen=True)
class DryRunResult:
    """Preview of what consolidation would produce for a project."""

    branch: list[BranchCandidate]
    sweep: list[SweepCandidate]
```

Add method to `ConsolidationService`:

```python
    async def dry_run(
        self, *, project: str, stale_days: int = 30
    ) -> DryRunResult:
        """Run both branch and sweep dry-runs and return a combined result."""
        branch = await self.branch_dry_run(project=project)
        sweep = await self.sweep_dry_run(project=project, stale_days=stale_days)
        return DryRunResult(branch=branch, sweep=sweep)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_consolidation.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/consolidation.py tests/services/test_consolidation.py
git commit -m "Phase 3: DryRunResult + dry_run() — combined branch+sweep preview"
```

---

## Task 12: `merge` — combine two pending candidates

**Files:**
- Modify: `better_memory/services/consolidation.py`
- Modify: `tests/services/test_consolidation.py`

**Context:** Phase 2 stubbed `POST /candidates/<id>/merge`. The real logic: take two insights with `status='pending_review'`, combine their source observations into the target, retire the source. Evidence count updates. Validation: merging into a retired/contradicted/promoted insight is blocked.

- [ ] **Step 1: Write failing test**

Append to `tests/services/test_consolidation.py`:

```python
class TestMerge:
    async def test_merges_two_pending_candidates(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_observation(conn, id="oA", project="p")
        _insert_observation(conn, id="oB", project="p")
        _insert_insight(conn, id="src", project="p", component="api",
                        status="pending_review")
        _insert_insight(conn, id="tgt", project="p", component="api",
                        status="pending_review")
        conn.execute(
            "INSERT INTO insight_sources (insight_id, observation_id) "
            "VALUES ('src', 'oA')"
        )
        conn.execute(
            "INSERT INTO insight_sources (insight_id, observation_id) "
            "VALUES ('tgt', 'oB')"
        )
        conn.commit()

        svc = ConsolidationService(conn=conn, chat=FakeChat(responses=[]))
        await svc.merge(source_id="src", target_id="tgt")

        # Source insight is retired
        src = conn.execute(
            "SELECT status FROM insights WHERE id = 'src'"
        ).fetchone()
        assert src["status"] == "retired"

        # Target insight has both observations as sources now
        sources = {
            r["observation_id"]
            for r in conn.execute(
                "SELECT observation_id FROM insight_sources "
                "WHERE insight_id = 'tgt'"
            ).fetchall()
        }
        assert sources == {"oA", "oB"}

        # Target evidence count updated
        tgt = conn.execute(
            "SELECT evidence_count FROM insights WHERE id = 'tgt'"
        ).fetchone()
        assert tgt["evidence_count"] == 2

    async def test_merge_into_confirmed_allowed(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_observation(conn, id="oA", project="p")
        _insert_insight(conn, id="src", project="p", component="api",
                        status="pending_review")
        _insert_insight(conn, id="tgt", project="p", component="api",
                        status="confirmed")
        conn.execute(
            "INSERT INTO insight_sources VALUES ('src', 'oA')"
        )
        conn.commit()

        svc = ConsolidationService(conn=conn, chat=FakeChat(responses=[]))
        await svc.merge(source_id="src", target_id="tgt")

        src = conn.execute(
            "SELECT status FROM insights WHERE id = 'src'"
        ).fetchone()
        assert src["status"] == "retired"

    async def test_merge_into_retired_blocked(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_insight(conn, id="src", project="p", component="api",
                        status="pending_review")
        _insert_insight(conn, id="tgt", project="p", component="api",
                        status="retired")

        svc = ConsolidationService(conn=conn, chat=FakeChat(responses=[]))
        with pytest.raises(ValueError, match="retired"):
            await svc.merge(source_id="src", target_id="tgt")

    async def test_merge_unknown_source(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_insight(conn, id="tgt", project="p", component="api",
                        status="pending_review")

        svc = ConsolidationService(conn=conn, chat=FakeChat(responses=[]))
        with pytest.raises(ValueError, match="not found"):
            await svc.merge(source_id="missing", target_id="tgt")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_consolidation.py::TestMerge -v`
Expected: FAIL — method doesn't exist.

- [ ] **Step 3: Implement**

Append to `ConsolidationService`:

```python
    async def merge(self, *, source_id: str, target_id: str) -> None:
        """Merge ``source_id`` into ``target_id``.

        - Source must be ``pending_review``.
        - Target must be ``pending_review`` OR ``confirmed`` (merging into
          a live insight upgrades its evidence).
        - Target must not be ``retired``, ``contradicted``, or ``promoted``.
        """
        conn = self._conn
        src = conn.execute(
            "SELECT status FROM insights WHERE id = ?", (source_id,)
        ).fetchone()
        if src is None:
            raise ValueError(f"Source insight not found: {source_id}")
        if src["status"] != "pending_review":
            raise ValueError(
                f"Source must be pending_review, got {src['status']!r}"
            )

        tgt = conn.execute(
            "SELECT status, evidence_count FROM insights WHERE id = ?",
            (target_id,),
        ).fetchone()
        if tgt is None:
            raise ValueError(f"Target insight not found: {target_id}")
        if tgt["status"] not in ("pending_review", "confirmed"):
            raise ValueError(
                f"Cannot merge into status {tgt['status']!r}"
            )

        now = self._clock().isoformat()
        conn.execute("SAVEPOINT consolidation_merge")
        try:
            # Move source's observation links to target (dedupe via
            # INSERT OR IGNORE so duplicates are harmless).
            conn.execute(
                "INSERT OR IGNORE INTO insight_sources "
                "(insight_id, observation_id) "
                "SELECT ?, observation_id FROM insight_sources "
                "WHERE insight_id = ?",
                (target_id, source_id),
            )
            conn.execute(
                "DELETE FROM insight_sources WHERE insight_id = ?",
                (source_id,),
            )
            # Update target's evidence_count to reflect the actual
            # linked-source count.
            new_count = conn.execute(
                "SELECT COUNT(*) FROM insight_sources WHERE insight_id = ?",
                (target_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE insights SET evidence_count = ?, "
                "updated_at = ? WHERE id = ?",
                (new_count, now, target_id),
            )
            # Retire the source.
            conn.execute(
                "UPDATE insights SET status = 'retired', "
                "updated_at = ? WHERE id = ?",
                (now, source_id),
            )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT consolidation_merge")
            conn.execute("RELEASE SAVEPOINT consolidation_merge")
            raise
        conn.execute("RELEASE SAVEPOINT consolidation_merge")
        conn.commit()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_consolidation.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/consolidation.py tests/services/test_consolidation.py
git commit -m "Phase 3: ConsolidationService.merge — combine two candidates"
```

---

## Task 13: Wire `ConsolidationService` into the Flask factory

**Files:**
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/conftest.py`
- Modify: `tests/ui/test_app.py`

**Context:** Phase 2's Flask factory instantiates `InsightService` on `app.extensions`. Phase 3 adds `ConsolidationService` beside it. Chat client is configurable — tests pass a `FakeChat`; production gets an `OllamaChat`.

- [ ] **Step 1: Write failing test**

Append to `tests/ui/test_app.py`:

```python
class TestConsolidationWiring:
    def test_app_exposes_consolidation_service(
        self, client: FlaskClient
    ) -> None:
        assert "consolidation_service" in client.application.extensions
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ui/test_app.py::TestConsolidationWiring -v`
Expected: FAIL — key not set.

- [ ] **Step 3: Update the factory**

Edit `better_memory/ui/app.py`. Add import:

```python
from better_memory.llm.ollama import ChatCompleter, OllamaChat
from better_memory.services.consolidation import ConsolidationService
```

Add a `chat: ChatCompleter | None = None` kwarg to `create_app`:

```python
def create_app(
    *,
    inactivity_timeout: float = 1800.0,
    inactivity_poll_interval: float = 30.0,
    start_watchdog: bool = True,
    db_path: Path | None = None,
    chat: ChatCompleter | None = None,
) -> Flask:
```

Inside the factory, after the `insight_service` registration, add:

```python
    resolved_chat: ChatCompleter = chat if chat is not None else OllamaChat()
    app.extensions["chat"] = resolved_chat
    app.extensions["consolidation_service"] = ConsolidationService(
        conn=db_conn, chat=resolved_chat
    )
```

- [ ] **Step 4: Update the client fixture**

Edit `tests/ui/conftest.py`. Extend the `client` fixture to inject a `FakeChat`:

```python
from better_memory.llm.fake import FakeChat


@pytest.fixture
def client(tmp_db: Path) -> Iterator[FlaskClient]:
    """Yield a Flask test client backed by a migrated tmp DB."""
    fake_chat = FakeChat(responses=[])
    app = create_app(
        start_watchdog=False,
        db_path=tmp_db,
        chat=fake_chat,
    )
    app.config["TESTING"] = True
    # Expose the fake so tests can seed responses.
    app.config["_fake_chat"] = fake_chat
    with patch("better_memory.ui.app.threading.Timer"):
        with app.test_client() as c:
            yield c
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/ui/ -v`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/app.py tests/ui/conftest.py tests/ui/test_app.py
git commit -m "Phase 3: wire ConsolidationService into Flask factory"
```

---

## Task 14: Real `start_consolidation_job` in `jobs.py`

**Files:**
- Modify: `better_memory/ui/jobs.py`
- Modify: `better_memory/ui/app.py`
- Modify: `better_memory/ui/templates/fragments/consolidation_job.html`
- Modify: `tests/ui/test_pipeline.py`

**Context:** Replace `start_phase3_stub_job()` with a real threaded job that runs `ConsolidationService.dry_run()` and stores the result. The UI polls `/jobs/<id>`; when the job reports complete, the response fragment renders the branch/sweep candidates.

Because `ConsolidationService.dry_run` is async, the thread uses `asyncio.run()` to drive it. The thread owns its own `sqlite3.Connection` (spec §2) and is constructed with the same `chat` client passed to the factory.

- [ ] **Step 1: Write failing test**

Replace the existing `TestConsolidationButton` in `tests/ui/test_pipeline.py` (previously seeded against the stub) with:

```python
class TestConsolidationButton:
    def test_click_returns_running_job_fragment(
        self, client: FlaskClient
    ) -> None:
        response = client.post(
            "/pipeline/consolidate",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        # Running job fragment — no candidates yet.
        assert b'data-job-id="' in response.data

    def test_completed_job_renders_branch_candidates(
        self, client: FlaskClient
    ) -> None:
        """End-to-end: seed a cluster, run consolidate, wait for the job
        thread to finish deterministically, verify the rendered fragment
        lists the candidate."""
        import re
        import threading

        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        for i in range(3):
            conn.execute(
                "INSERT INTO observations (id, content, project, component, "
                "theme, status, validated_true, outcome) VALUES "
                "(?, ?, ?, 'api', 'retry', 'active', 1, 'success')",
                (f"obs-{i}", f"content {i}", project),
            )
        conn.commit()

        # Seed FakeChat with the drafted insight text.
        fake = client.application.config["_fake_chat"]
        fake.responses.append(
            "Always retry 5xx responses with exponential backoff; "
            "never retry 4xx."
        )

        post_resp = client.post(
            "/pipeline/consolidate",
            headers={"Origin": "http://localhost"},
        )
        match = re.search(rb'data-job-id="([a-f0-9]+)"', post_resp.data)
        assert match is not None
        job_id = match.group(1).decode()

        # Wait for the consolidation thread to finish. Deterministic —
        # no wall-clock timing reliance. Thread names start with
        # "consolidation-<job-id-prefix>".
        for t in threading.enumerate():
            if t.name.startswith("consolidation-"):
                t.join(timeout=5.0)
                assert not t.is_alive(), "consolidation thread did not exit"

        get_resp = client.get(f"/jobs/{job_id}")
        assert b"job-status-complete" in get_resp.data
        assert b"Always retry 5xx" in get_resp.data
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ui/test_pipeline.py::TestConsolidationButton -v`
Expected: FAIL — the stub returns "Phase 3" text, not candidates.

- [ ] **Step 3: Rewrite `better_memory/ui/jobs.py`**

Replace the entire contents of `better_memory/ui/jobs.py` with:

```python
"""Background-job registry for the Management UI.

Phase 3: ``start_consolidation_job`` spawns a ``threading.Thread`` that
runs ``ConsolidationService.dry_run()`` and stores the result. The UI
polls ``/jobs/<id>`` until ``state.status == 'complete'`` (or
``'failed'``).
"""

from __future__ import annotations

import asyncio
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from better_memory.db.connection import connect
from better_memory.llm.ollama import ChatCompleter
from better_memory.services.consolidation import (
    ConsolidationService,
    DryRunResult,
)

_lock = threading.Lock()
_current_job_id: str | None = None
_jobs: dict[str, "JobState"] = {}

# TODO(phase4): cap _jobs dict size once long-running jobs become common.


@dataclass
class JobState:
    id: str
    status: str  # "running" | "complete" | "failed"
    message: str = ""
    result: DryRunResult | None = None
    error: str | None = None


def get_job(job_id: str) -> JobState | None:
    return _jobs.get(job_id)


def start_consolidation_job(
    *,
    db_path: Path,
    chat: ChatCompleter,
    project: str,
    stale_days: int = 30,
) -> JobState:
    """Spawn a consolidation thread. Returns the initial ``JobState``.

    The thread owns its own ``sqlite3.Connection`` (the UI's request
    connection is single-threaded). The lock prevents concurrent jobs.
    """
    global _current_job_id
    if not _lock.acquire(blocking=False):
        # Another job is active — return the existing state if we can.
        existing_id = _current_job_id
        if existing_id is not None and existing_id in _jobs:
            return _jobs[existing_id]
        return JobState(
            id="unknown",
            status="failed",
            error="Consolidation busy but no job recorded; retry shortly.",
        )

    job_id = uuid4().hex
    state = JobState(id=job_id, status="running", message="Running consolidation…")
    _jobs[job_id] = state
    _current_job_id = job_id

    def _run() -> None:
        global _current_job_id
        try:
            conn = connect(db_path)
            try:
                svc = ConsolidationService(conn=conn, chat=chat)
                result = asyncio.run(
                    svc.dry_run(project=project, stale_days=stale_days)
                )
                state.result = result
                state.status = "complete"
                state.message = (
                    f"{len(result.branch)} candidate(s), "
                    f"{len(result.sweep)} sweep item(s)."
                )
            finally:
                conn.close()
        except Exception:
            state.status = "failed"
            state.error = traceback.format_exc()
        finally:
            # Clear current job ID BEFORE releasing the lock, so a second
            # caller can't race.
            _current_job_id = None
            _lock.release()

    t = threading.Thread(target=_run, daemon=True, name=f"consolidation-{job_id[:8]}")
    t.start()
    return state
```

- [ ] **Step 4: Update the consolidate route and job fragment**

Edit `better_memory/ui/app.py`. Replace the `pipeline_consolidate` view:

```python
    @app.post("/pipeline/consolidate")
    def pipeline_consolidate() -> tuple[str, int, dict[str, str]]:
        db_path = app.extensions.get("_db_path")
        chat = app.extensions["chat"]
        # We need the DB path. The factory stored the connection but not
        # the path — store the path too at factory time. See Task 13 note.
        state = jobs.start_consolidation_job(
            db_path=db_path, chat=chat, project=_project_name()
        )
        rendered = render_template("fragments/consolidation_job.html", job=state)
        headers = {}
        if state.status == "complete":
            headers["HX-Trigger"] = "job-complete"
        return rendered, 200, headers
```

(The `db_path` retrieval requires Task 13 to have stored `_db_path` on `app.extensions`. Add a line to Task 13's Step 3: `app.extensions["_db_path"] = resolved_db` — do that now as part of this task's edit.)

Edit `better_memory/ui/app.py` factory section, after `db_conn = connect(resolved_db)`:

```python
    app.extensions["_db_path"] = resolved_db
```

Edit `better_memory/ui/templates/fragments/consolidation_job.html` — replace the whole file so it also polls while running and renders candidates when complete:

```html
<div class="consolidation-job" data-job-id="{{ job.id }}"
     {% if job.status == 'running' %}
     hx-get="{{ url_for('jobs_get', id=job.id) }}"
     hx-trigger="every 1s"
     hx-target="this" hx-swap="outerHTML"
     {% endif %}>
  <div class="job-status job-status-{{ job.status }}">
    {% if job.status == "running" %}
      <span class="spinner">⟳</span>
    {% elif job.status == "complete" %}
      <span class="checkmark">✓</span>
    {% elif job.status == "failed" %}
      <span class="cross">✗</span>
    {% endif %}
    {{ job.status }}
  </div>
  {% if job.message %}<div class="job-message">{{ job.message }}</div>{% endif %}
  {% if job.error %}<pre class="job-error">{{ job.error }}</pre>{% endif %}
  {% if job.result %}
    {% if job.result.branch %}
      <h4>Candidate insights ({{ job.result.branch|length }})</h4>
      <ul class="branch-candidates">
        {% for c in job.result.branch %}
          <li>
            <strong>{{ c.title }}</strong>
            <div class="muted">{{ c.component or '—' }} · {{ c.polarity }} · {{ c.observation_ids|length }} source(s)</div>
            <p>{{ c.content }}</p>
          </li>
        {% endfor %}
      </ul>
    {% endif %}
    {% if job.result.sweep %}
      <h4>Sweep candidates ({{ job.result.sweep|length }})</h4>
      <ul class="sweep-candidates">
        {% for s in job.result.sweep %}
          <li class="muted">{{ s.observation_id }} — {{ s.reason }}</li>
        {% endfor %}
      </ul>
    {% endif %}
    {% if not job.result.branch and not job.result.sweep %}
      <p class="muted">Nothing to consolidate.</p>
    {% endif %}
  {% endif %}
</div>
```

When `status == 'running'`, the fragment carries its own `hx-get`+`every 1s` so clicking Run → UI swaps in a running fragment → the fragment self-polls → once the job completes the server returns the complete fragment (no polling attrs) and the panel is updated via `HX-Trigger: job-complete`.

Also update `/jobs/<id>` to set `HX-Trigger: job-complete` header when the job has completed, so the Candidates panel refetches:

```python
    @app.get("/jobs/<id>")
    def jobs_get(id: str) -> tuple[str, int, dict[str, str]]:
        state = jobs.get_job(id)
        if state is None:
            abort(404)
        rendered = render_template("fragments/consolidation_job.html", job=state)
        headers = {}
        if state.status == "complete":
            headers["HX-Trigger"] = "job-complete"
        return rendered, 200, headers
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/ui/ -v`
Expected: All PASS (including the new completed-job test).

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/jobs.py better_memory/ui/app.py better_memory/ui/templates/fragments/consolidation_job.html tests/ui/test_pipeline.py
git commit -m "Phase 3: real consolidation job — threaded dry_run + polling UI"
```

---

## Task 15: Wire `POST /candidates/<id>/merge` to `ConsolidationService.merge`

**Files:**
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/test_pipeline.py`

**Context:** Phase 2 returned a "Phase 3 ships merge" stub. Now the POST calls the real merge. On success, return an empty string (card removed from panel). On validation error (e.g., target is retired), return the error fragment so the user sees why it failed.

- [ ] **Step 1: Write failing tests**

Replace `TestMergePicker.test_merge_post_returns_phase3_stub` in `tests/ui/test_pipeline.py` with:

```python
    def test_merge_post_combines_candidates(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")
        _insert_candidate(conn, project, "c2")

        response = client.post(
            "/candidates/c1/merge?target=c2",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert response.data.strip() == b""  # empty → card removed
        row = conn.execute(
            "SELECT status FROM insights WHERE id = 'c1'"
        ).fetchone()
        assert row["status"] == "retired"

    def test_merge_post_validation_error_surfaces(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")
        # Insert a retired target so the merge should fail.
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, "
            "polarity) VALUES ('tgt', 't', 'c', ?, 'retired', 'neutral')",
            (project,),
        )
        conn.commit()

        response = client.post(
            "/candidates/c1/merge?target=tgt",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert b"Cannot merge into status 'retired'" in response.data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ui/test_pipeline.py::TestMergePicker -v`
Expected: FAIL — the POST still returns the Phase-3 stub fragment.

- [ ] **Step 3: Implement**

Edit `better_memory/ui/app.py`. Replace `candidate_merge`:

```python
    @app.post("/candidates/<id>/merge")
    def candidate_merge(id: str) -> tuple[str, int]:
        target_id = request.args.get("target", "")
        if not target_id:
            return (
                '<div class="card card-error">'
                "<p>Missing <code>target</code> query parameter.</p>"
                "</div>"
            ), 200
        svc = app.extensions["consolidation_service"]
        try:
            import asyncio
            asyncio.run(
                svc.merge(source_id=id, target_id=target_id)
            )
        except ValueError as exc:
            return (
                f'<div class="card card-error">'
                f"<p>{exc}</p>"
                "</div>"
            ), 200
        # Source retired → card removed from candidates panel.
        return "", 200
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/ui/ -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/ui/app.py tests/ui/test_pipeline.py
git commit -m "Phase 3: real merge endpoint backed by ConsolidationService.merge"
```

---

## Task 15b: Apply endpoint — persist dry-run candidates to review queue

**Files:**
- Modify: `better_memory/ui/jobs.py` (add `applied` flag + `apply_job` helper)
- Modify: `better_memory/ui/app.py` (add `POST /jobs/<id>/apply` route)
- Modify: `better_memory/ui/templates/fragments/consolidation_job.html` (add Apply button)
- Create: `tests/ui/test_apply_job.py`

**Context:** `dry_run()` is preview-only — it returns `DryRunResult` in memory and does not persist. For the Phase 2 approve/reject/retire buttons to act on real candidates, something has to move the drafts from the preview into the `insights` table (status='pending_review') and archive the swept observations. This task wires that step: after a completed dry-run, the user clicks "Apply" to commit both branch candidates (via `apply_branch`) and sweep candidates (via `apply_sweep`). Double-apply is guarded via an `applied` flag on `JobState`.

- [ ] **Step 1: Extend `JobState` and add `apply_job` helper**

In `better_memory/ui/jobs.py`:

```python
@dataclass
class JobState:
    id: str
    status: str  # "running" | "complete" | "failed"
    message: str = ""
    result: DryRunResult | None = None
    error: str | None = None
    applied: bool = False  # True after apply_job() persists the drafts.


def apply_job(
    job_id: str,
    *,
    db_path: Path,
    chat: ChatCompleter,
) -> JobState:
    """Persist a completed job's drafts: create insights (pending_review)
    and archive swept observations. Idempotent: second call is a no-op.

    Returns the updated ``JobState``. Raises ``LookupError`` if the job
    is unknown, ``ValueError`` if it is not ``complete`` or has no result.
    """
    state = _jobs.get(job_id)
    if state is None:
        raise LookupError(job_id)
    if state.status != "complete" or state.result is None:
        raise ValueError(f"Cannot apply job in status {state.status!r}")
    if state.applied:
        return state

    conn = connect(db_path)
    try:
        svc = ConsolidationService(conn=conn, chat=chat)

        async def _do_apply() -> tuple[int, int]:
            branch_count = 0
            for c in state.result.branch:  # type: ignore[union-attr]
                await svc.apply_branch(c)
                branch_count += 1
            sweep_count = 0
            for s in state.result.sweep:  # type: ignore[union-attr]
                await svc.apply_sweep(s.observation_id)
                sweep_count += 1
            return branch_count, sweep_count

        branch_count, sweep_count = asyncio.run(_do_apply())
    finally:
        conn.close()

    state.applied = True
    state.message = (
        f"Applied {branch_count} candidate(s) to review queue, "
        f"archived {sweep_count} observation(s)."
    )
    return state
```

- [ ] **Step 2: Add `POST /jobs/<id>/apply` route**

In `better_memory/ui/app.py`, register the route near `jobs_get`:

```python
@app.post("/jobs/<id>/apply")
def jobs_apply(id: str) -> tuple[str, int, dict[str, str]]:
    db_path = app.extensions["_db_path"]
    chat = app.extensions["chat"]
    try:
        state = jobs.apply_job(id, db_path=db_path, chat=chat)
    except LookupError:
        abort(404)
    except ValueError as exc:
        return (
            f'<div class="card card-error"><p>{exc}</p></div>',
            400,
            {},
        )
    rendered = render_template("fragments/consolidation_job.html", job=state)
    # Tell the pipeline badge to refresh — new pending_review items landed.
    return rendered, 200, {"HX-Trigger": "job-complete"}
```

- [ ] **Step 3: Update the fragment template**

Modify `better_memory/ui/templates/fragments/consolidation_job.html` — add an Apply form after `job.message` rendering, visible only when `status=='complete'` and `not applied`:

```html
  {% if job.status == 'complete' and not job.applied and job.result and (job.result.branch or job.result.sweep) %}
    <form hx-post="{{ url_for('jobs_apply', id=job.id) }}"
          hx-target="closest .consolidation-job" hx-swap="outerHTML">
      <button type="submit" class="btn-primary">Apply to review queue</button>
    </form>
  {% endif %}
```

- [ ] **Step 4: Write tests**

Create `tests/ui/test_apply_job.py` with at least:

- `test_apply_persists_branch_candidates` — seed cluster, POST /pipeline/consolidate, join thread, POST /jobs/<id>/apply, assert `insights` has `pending_review` rows matching the cluster
- `test_apply_archives_sweep_candidates` — seed stale observation, run consolidation, apply, assert observation status is `archived`
- `test_apply_is_idempotent` — apply twice, second call returns same state, no duplicate inserts
- `test_apply_rejects_running_job` — apply before thread joins → 400 (or the test uses a synthetic JobState)
- `test_apply_unknown_job_returns_404` — POST /jobs/nonexistent/apply → 404

Use the same `threading.enumerate()` + `join(timeout=5.0)` pattern Task 14 established for deterministic sync.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/ui -v`
Expected: All PASS (new tests + prior UI tests untouched).

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/jobs.py better_memory/ui/app.py better_memory/ui/templates/fragments/consolidation_job.html tests/ui/test_apply_job.py
git commit -m "Phase 3: /jobs/<id>/apply — persist dry-run drafts to review queue"
```

---

## Task 16: Phase-2 deferred smoke tests — Approve/Reject/Retire end-to-end

**Files:**
- Create: `tests/ui/test_consolidation_e2e.py`

**Context:** Phase 2 deferred smoke tests because the action buttons need real consolidation-generated data. Phase 3 now produces it. One end-to-end test that exercises the full flow: seed observations → run consolidation → **apply** to persist drafts → approve/reject/retire a candidate. Uses the apply endpoint added in Task 15b.

- [ ] **Step 1: Write the test**

Create `tests/ui/test_consolidation_e2e.py`:

```python
"""End-to-end Phase 2 smoke tests, executed against Phase 3's real
ConsolidationService. These were deferred in the Phase 2 plan because
Phase 2 had no way to produce real candidates; Phase 3 does.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from flask.testing import FlaskClient


def _seed_cluster(
    conn: sqlite3.Connection, project: str, component: str, n: int
) -> list[str]:
    ids = []
    for i in range(n):
        oid = f"{component}-{i}"
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, component, theme, status, "
            "validated_true, outcome) "
            "VALUES (?, ?, ?, ?, ?, 'active', 1, 'success')",
            (oid, f"observation {i} of {component}", project, component, "core"),
        )
        ids.append(oid)
    conn.commit()
    return ids


def _run_consolidation(client: FlaskClient, draft_text: str) -> str:
    """Run /pipeline/consolidate, join the worker thread, apply the
    result so candidates land in the review queue, then return job_id."""
    import threading

    fake = client.application.config["_fake_chat"]
    fake.responses.append(draft_text)

    post = client.post(
        "/pipeline/consolidate", headers={"Origin": "http://localhost"}
    )
    match = re.search(rb'data-job-id="([a-f0-9]+)"', post.data)
    assert match is not None
    job_id = match.group(1).decode()

    # Deterministic wait — join the consolidation thread by name.
    for t in threading.enumerate():
        if t.name.startswith("consolidation-"):
            t.join(timeout=5.0)
            assert not t.is_alive(), "consolidation thread did not exit"

    # Apply the dry-run result so approvable candidates exist.
    apply_resp = client.post(
        f"/jobs/{job_id}/apply", headers={"Origin": "http://localhost"}
    )
    assert apply_resp.status_code == 200, apply_resp.data
    return job_id


def test_approve_a_real_candidate(client: FlaskClient) -> None:
    conn = client.application.extensions["db_connection"]
    project = Path.cwd().name
    _seed_cluster(conn, project, "api", 3)

    _run_consolidation(client, "Drafted insight content for approve.")

    # Find the candidate id.
    cand_id = conn.execute(
        "SELECT id FROM insights WHERE project = ? AND status = 'pending_review'",
        (project,),
    ).fetchone()["id"]

    # Approve it.
    resp = client.post(
        f"/candidates/{cand_id}/approve",
        headers={"Origin": "http://localhost"},
    )
    assert resp.status_code == 200

    # Insight now confirmed.
    row = conn.execute(
        "SELECT status FROM insights WHERE id = ?", (cand_id,)
    ).fetchone()
    assert row["status"] == "confirmed"


def test_reject_a_real_candidate(client: FlaskClient) -> None:
    conn = client.application.extensions["db_connection"]
    project = Path.cwd().name
    _seed_cluster(conn, project, "db", 3)

    _run_consolidation(client, "Drafted insight content for reject.")

    cand_id = conn.execute(
        "SELECT id FROM insights WHERE project = ? AND status = 'pending_review'",
        (project,),
    ).fetchone()["id"]

    resp = client.post(
        f"/candidates/{cand_id}/reject",
        headers={"Origin": "http://localhost"},
    )
    assert resp.status_code == 200

    row = conn.execute(
        "SELECT status FROM insights WHERE id = ?", (cand_id,)
    ).fetchone()
    assert row["status"] == "retired"


def test_retire_a_confirmed_insight_end_to_end(
    client: FlaskClient,
) -> None:
    conn = client.application.extensions["db_connection"]
    project = Path.cwd().name
    _seed_cluster(conn, project, "cache", 3)

    _run_consolidation(client, "Drafted insight content for retire.")

    cand_id = conn.execute(
        "SELECT id FROM insights WHERE project = ? AND status = 'pending_review'",
        (project,),
    ).fetchone()["id"]
    # Approve (→ confirmed) then Retire (→ retired)
    client.post(
        f"/candidates/{cand_id}/approve",
        headers={"Origin": "http://localhost"},
    )
    resp = client.post(
        f"/insights/{cand_id}/retire",
        headers={"Origin": "http://localhost"},
    )
    assert resp.status_code == 200

    row = conn.execute(
        "SELECT status FROM insights WHERE id = ?", (cand_id,)
    ).fetchone()
    assert row["status"] == "retired"
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/ui/test_consolidation_e2e.py -v`
Expected: All 3 PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/ui/test_consolidation_e2e.py
git commit -m "Phase 3: E2E smoke tests — Approve/Reject/Retire real candidates"
```

---

## Task 17: Opt-in integration test — real Ollama chat

**Files:**
- Modify: `pyproject.toml` (add `addopts` so integration tests skip by default)
- Create: `tests/services/test_consolidation_integration.py`

**Context:** Every other Phase-3 test uses `FakeChat`. One integration test exercises the real `OllamaChat` end-to-end so drift between our prompt / response format and Ollama's API surface is caught. Marked `@pytest.mark.integration` — off by default; opt-in via `pytest -m integration`.

- [ ] **Step 1: Write the test**

Create `tests/services/test_consolidation_integration.py`:

```python
"""Integration tests — real Ollama, off by default.

Run with: ``uv run pytest -m integration tests/services/test_consolidation_integration.py``

Requires a running Ollama instance reachable at ``$OLLAMA_HOST`` with
the model specified in ``$CONSOLIDATE_MODEL`` (default ``llama3``)
pulled locally.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.llm.ollama import OllamaChat
from better_memory.services.consolidation import ConsolidationService


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "memory.db")
    try:
        apply_migrations(c)
        yield c
    finally:
        c.close()


@pytest.mark.integration
async def test_real_ollama_drafts_insight(conn: sqlite3.Connection) -> None:
    for i in range(3):
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, component, theme, status, "
            "validated_true, outcome) "
            "VALUES (?, ?, 'p', 'api', 'retry', 'active', 1, 'success')",
            (f"o{i}", f"Observation {i}: retry 5xx with backoff."),
        )
    conn.commit()

    chat = OllamaChat()  # real client, reads config
    try:
        svc = ConsolidationService(conn=conn, chat=chat)
        candidates = await svc.branch_dry_run(project="p")
    finally:
        await chat.aclose()

    assert len(candidates) == 1
    c = candidates[0]
    # Non-empty, non-whitespace drafted content.
    assert c.content.strip()
    assert c.polarity == "do"
    assert c.observation_ids == ["o0", "o1", "o2"]
```

- [ ] **Step 2: Add `addopts` to `pyproject.toml` so integration tests are skipped by default**

Edit `pyproject.toml`. Under `[tool.pytest.ini_options]`, add an `addopts` line:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
addopts = "-m 'not integration'"
markers = [
    "integration: marks tests as integration tests (require external services)",
]
```

**Note:** `tests/mcp/test_server_integration.py` is NOT marked with `@pytest.mark.integration` (it documents this deliberately — it wants to run by default), so this change does NOT affect it. Only tests that actively carry the mark get skipped.

- [ ] **Step 3: Verify default behavior**

Run: `uv run pytest tests/services/test_consolidation_integration.py -v`
Expected: 1 test deselected — "1 deselected in …s". The test is skipped by default.

Run: `uv run pytest -m integration tests/services/test_consolidation_integration.py -v`
Expected: 1 test runs. Passes if real Ollama + `$CONSOLIDATE_MODEL` is reachable; errors with `ChatError` or timeout otherwise.

Run the full default suite to confirm nothing else was affected: `uv run pytest -v`
Expected: All existing tests still run (MCP integration tests included — they aren't marked).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml tests/services/test_consolidation_integration.py
git commit -m "Phase 3: opt-in Ollama integration test with pytest -m integration"
```

---

## Self-Review Checklist

Spec §9 → task mapping:

| Spec item | Task |
|---|---|
| Branch: group by (project, component, theme) | 4 |
| Branch: min 3 observations, min 2 validated_true | 4 |
| Branch: dedup against confirmed/promoted insights | 6 |
| Branch: LLM draft per cluster | 5 (prompt) + 7 (drafting) |
| Branch: create pending_review insight, link sources, mark consolidated | 8 |
| Sweep: find stale observations | 9 |
| Sweep: archive on human approval | 10 |
| Sweep: contradiction detection | **Deferred** to Phase 3.5 — spec §9 explicitly calls this out as "via Ollama call", which is distinct complexity |
| Dry run always preview | 11 (DryRunResult) — only `apply_*` writes |
| Insight drafting prompt | 5 |
| UI: "Run branch-and-sweep" button wired | 14 |
| UI: merge endpoint | 15 |
| UI: Phase-2 smoke tests | 16 |
| Integration test against real Ollama | 17 |

Deferred items are scoped in the plan's Scope section and have clear Phase 3.5 follow-up.

## Notes on execution

- Task 13 creates `app.extensions["_db_path"]`. Task 14 needs it. If Task 13 is skipped or reordered, Task 14 will fail.
- `FakeChat` is seeded inside each test via `client.application.config["_fake_chat"]`. Tests that don't exercise consolidation should leave the response queue empty (default).
- Task 17 runs against real Ollama. On a dev machine with Ollama reachable this passes; on CI without Ollama it will either be skipped (if a marker filter is set) or fail. Task 17 does NOT modify the CI workflow — keep sweep-related CI policy as a separate concern.
