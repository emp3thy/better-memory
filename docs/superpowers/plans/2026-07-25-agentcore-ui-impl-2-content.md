# Phase C — PR 2: Content routing (semantic + reflections through the backend)

> **For agentic workers:** REQUIRED SUB-SKILL — use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this phase task-by-task. Each task is one commit. Steps follow strict TDD: write failing test -> run (FAIL) -> minimal impl -> run (PASS) -> commit.

**Branch:** `feat/agentcore-ui-content`
**Depends on:** PR 1 (Foundation) merged — `create_app` builds `app.extensions["backend"]` (a `StorageBackend` from `build_backend(config=get_config(), memory_conn=db_conn, sync_embedder=resolved_sync_embedder, session_id=None, project=project_name())`), keeps `app.extensions["db_connection"]` for OPERATIONAL state, and a `@app.context_processor` exposes `caps` (the six flags below). If that seam is absent, STOP and escalate — do NOT rebuild it here.
**Authoritative design:** `docs/superpowers/specs/2026-07-25-agentcore-ui-design.md`. Reconciliation + order: `docs/superpowers/plans/2026-07-25-agentcore-ui-MASTER-plan.md` (these tasks are PR 2 / master tasks B1–B8; re-numbered C1–C9 for this phase).

**Task→master map:** C1=B1, C2=B2, C3=B3, C4=B4, C5=B5, C6=B6, C7=B7, C8=B8, C9=B9.

## Canonical contract (use these EXACT names — no `*_for_ui`, no `supports_reflection_confirm/_retention/_mutation`)

Six capability flags (`@property -> bool`; `SqliteBackend` all `True`, `AgentCoreBackend` all `False` — declared in PR 1, only CONSUMED here):
`supports_episodes` (pre-existing) · `supports_observations` · `supports_provenance` · `supports_retention_runs` · `supports_reflection_review` · `supports_reflection_text_edit`

New backend methods produced by this phase (canonical signatures):
```python
def reflection_list(self, *, project: str | None = None, tech: str | None = None,
    phase: str | None = None, polarity: str | None = None, status: str | None = None,
    min_confidence: float = 0.0, useful_only: bool = False, limit: int = 200) -> list[dict]: ...
def reflection_get(self, *, reflection_id: str) -> dict | None: ...      # row only, NO provenance
def semantic_get(self, *, id: str) -> SemanticMemory | None: ...
```
Reused existing methods (do NOT redefine): `retrieve`, `semantic_observe`, `semantic_list`, `semantic_update_text`, `semantic_set_scope`, `semantic_delete`, `promote_reflection`, `retire_reflection`. (`distinct_projects` lands in PR 3 — not this phase.)

Supporting extractions produced here:
```python
# better_memory/ui/queries.py
def reflection_row(conn, *, reflection_id: str) -> ReflectionFull | None: ...
def reflection_provenance(conn, *, reflection_id: str) -> list[ReflectionSourceObservation]: ...
# reflection_detail(conn, *, reflection_id) is RECOMPOSED from the two above — output byte-identical.
# better_memory/services/semantic.py
def get(self, *, id: str) -> SemanticMemory | None: ...
```

AgentCore status vocabulary: `active` (== sqlite `confirmed`) / `promoted` / `retired`; NO `pending_review`. `reflection_list` default (status=None) admits `{active, promoted}`.

Namespaces (exact leaf; parent does NOT roll up): `projects/{project}/reflections/`, `projects/{project}/semantic/`, `projects/{project}/retired/`, `general/reflections/`, `general/semantic/`, `general/retired/`. Built via `resolve_namespace(resolve_actor_id(project), kind)` (`better_memory/storage/session.py`).

## Phase C guardrails

- **[[keep-docs-in-sync]]** (0.95) — the docs task (C8) updates `website/architecture.md` (UI reaches reflection/semantic CONTENT through `StorageBackend`, not raw SQL/services) + `website/agentcore-setup.md` (content now backend-routed; the `semantic_list` general-scope fix) + the new-method protocol docstrings. `README.md`, `website/mcp-tools.md`, `website/configuration.md` are UNAFFECTED (no new env var, no new MCP tool) — state that explicitly in the PR body.
- **[[server-boot-real-call]]** (0.65) — the agentcore-path route tests drive a REAL Flask route through a STUBBED `app.extensions["backend"]` and assert no leaked local-content read (the dead-content-table trick: seed a sentinel row in the local `reflections`/`semantic_memories` table and assert it never renders). Exercised in C1 and C6.
- **[[guard-needs-triggering-test]]** (0.8) — every named edge-guard gets a test that SEEDS a value which actually triggers it: `semantic_list(scope_filter=None)` two-namespace fan-out (C1), `reflection_list` best-effort AWS degrade + retired-namespace inclusion (C4).
- **[[brutalist-css-classes]]** (0.75) — CARRIED, not exercised: this phase makes NO template edits. `reflection_list` returns dicts and `reflection_get` feeds a `SimpleNamespace`, both consumed by the existing templates via Jinja item/attr access — zero markup change. Template gating (and its brutalist-class discipline) lands in PR 3.
- **[[playwright-domtext]]** (0.8) — the dead-content and stub-render assertions check DOM CONTENT-string presence/absence (`assert "..." in body` / `not in body`), never CSS-rendered text.

## Global constraints

- Python 3.12, Flask/Jinja/htmx, sqlite, boto3 stubbed via `MagicMock` (`tests/storage/test_agentcore_unit.py` pattern), pytest, pyright. Test command: `./.venv/Scripts/python.exe -m pytest <path> -v`. NO live AWS.
- ASCII only; ruff line length 100. One commit per task; footer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. No new env keys, no new MCP tools.
- **Sqlite behaviour BYTE-IDENTICAL.** Every `SqliteBackend` content method is a verbatim delegate to the query/service the UI calls today; `tests/ui/*` are the preservation pins and their assertions are NOT edited. Each task states its sqlite-preservation note.
- AgentCore reads best-effort: a per-namespace list failure degrades (skip that namespace / empty panel), never a 500.
- Storage→UI layering: `SqliteBackend` reaches `better_memory.ui.queries` via a LOCAL import inside the method body (mirrors agentcore's local `services.*` imports) — never a module-top import.

---

## Task C1 — Route Semantic list + CRUD through `backend.semantic_*`, incl. the `semantic_list(scope_filter=None)` two-namespace fan-out bug fix

**Files**
- Modify: `better_memory/storage/agentcore.py` (`semantic_list` — fan out over project + general when `scope_filter is None`)
- Modify: `better_memory/ui/app.py` (`semantic_panel`, `semantic_create`, `semantic_scope`, `semantic_delete`, `semantic_update` — NOT `semantic_drawer`, which is rewired in C5)
- Test: `tests/storage/test_agentcore_unit.py`, `tests/ui/test_semantic.py`

**Interfaces**
- Consumes (from PR 1): `app.extensions["backend"]: StorageBackend`, `app.extensions["db_connection"]`, the `caps` context processor.
- Consumes existing backend methods (unchanged signatures): `semantic_list(*, project=None, scope_filter=None, search=None, track_exposure=True) -> list[SemanticMemory]`, `semantic_observe(*, content, project=None, scope='project') -> str`, `semantic_update_text(*, id, content) -> None`, `semantic_set_scope(*, id, scope) -> None`, `semantic_delete(*, id) -> None`.
- Produces: fixed `AgentCoreBackend.semantic_list` (no signature change) + five backend-routed semantic routes.

**The bug:** `AgentCoreBackend.semantic_list(scope_filter=None)` queries only `projects/{actor}/semantic/`, dropping general-scope rows that sqlite's default view (`(project OR scope='general')`) includes. The UI default passes `scope_filter=None`. Fix: fan out over BOTH `projects/{actor}/semantic/` and `general/semantic/`, dedup by `memoryRecordId` (project wins).

### Steps

1. **Write the failing agentcore regression + guard tests** in `tests/storage/test_agentcore_unit.py` (append):
```python
def test_semantic_list_default_view_fans_out_over_project_and_general(
    backend, mock_data_client
) -> None:
    """[[guard-needs-triggering-test]] Bug regression: scope_filter=None (the
    UI default) must include general-scope records, mirroring sqlite's
    (project OR scope='general'). Project namespace has 0 records; general/
    semantic has 1 -> the default view must return that 1, not 0."""
    def stub(**kwargs):
        if kwargs["namespace"] == "general/semantic/":
            return {"memoryRecordSummaries": [
                {
                    "memoryRecordId": "sem-general-1",
                    "content": {"text": "prefer uv over pip"},
                    "namespaces": ["/general/semantic/"],
                    "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
                    "metadata": {"useful_count": {"numberValue": 0}},
                }
            ]}
        return {"memoryRecordSummaries": []}
    mock_data_client.list_memory_records.side_effect = stub
    result = backend.semantic_list(project="testproj", scope_filter=None)
    assert [m.id for m in result] == ["sem-general-1"]
    assert result[0].scope == "general"
    namespaces = {
        c.kwargs["namespace"]
        for c in mock_data_client.list_memory_records.call_args_list
    }
    assert namespaces == {"projects/testproj/semantic/", "general/semantic/"}


def test_semantic_list_default_view_dedups_project_wins(
    backend, mock_data_client
) -> None:
    """A record served by BOTH namespaces (lagging index) appears once."""
    rec = {
        "memoryRecordId": "sem-dup",
        "content": {"text": "dup"},
        "namespaces": ["/projects/testproj/semantic/"],
        "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
        "metadata": {"useful_count": {"numberValue": 0}},
    }
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": [rec]}
    result = backend.semantic_list(project="testproj", scope_filter=None)
    assert [m.id for m in result] == ["sem-dup"]


def test_semantic_list_project_filter_queries_only_project_namespace(
    backend, mock_data_client
) -> None:
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    backend.semantic_list(project="testproj", scope_filter="project")
    assert mock_data_client.list_memory_records.call_count == 1
    assert (
        mock_data_client.list_memory_records.call_args.kwargs["namespace"]
        == "projects/testproj/semantic/"
    )
```
   Also UPDATE the existing pin that asserts the OLD single-call behaviour — `test_semantic_list_without_search_uses_list_memory_records` calls `backend.semantic_list()` (scope_filter=None) and asserts `list_memory_records.assert_called_once()`; the fan-out makes that TWO calls. Change its body to pin the single-namespace path explicitly (its real intent is "list, not retrieve"):
```python
def test_semantic_list_without_search_uses_list_memory_records(backend, mock_data_client) -> None:
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    backend.semantic_list(scope_filter="project")
    mock_data_client.list_memory_records.assert_called_once()
    mock_data_client.retrieve_memory_records.assert_not_called()
```

2. **Run** `./.venv/Scripts/python.exe -m pytest tests/storage/test_agentcore_unit.py -v -k semantic_list` — FAIL (default view returns `[]`; one namespace queried).

3. **Implement** the fan-out in `AgentCoreBackend.semantic_list` (`better_memory/storage/agentcore.py`), replacing the single-namespace body:
```python
def semantic_list(
    self,
    *,
    project: str | None = None,
    scope_filter: str | None = None,
    search: str | None = None,
    track_exposure: bool = True,
) -> list[Any]:
    """List semantic records. With search -> retrieve_memory_records;
    without -> list_memory_records.

    scope_filter=None mirrors the sqlite default view
    (project OR scope='general'): fan out over BOTH the project/semantic and
    general/semantic namespaces and dedup by record id (project wins). A
    single-scope filter queries only that one namespace."""
    actor_id = resolve_actor_id(project or self._project)
    project_ns = resolve_namespace(actor_id, "semantic")
    general_ns = resolve_namespace("general", "semantic")
    if scope_filter == "general":
        namespaces = [general_ns]
    elif scope_filter == "project":
        namespaces = [project_ns]
    else:
        namespaces = [project_ns]
        if general_ns != project_ns:
            namespaces.append(general_ns)

    seen: set[str] = set()
    results: list[Any] = []
    for namespace in namespaces:
        if search and search.strip():
            response = self._data.retrieve_memory_records(
                memoryId=self._cfg.semantic.memory_id,
                namespace=namespace,
                searchCriteria={"searchQuery": search.strip(), "topK": 50},
            )
        else:
            response = self._data.list_memory_records(
                memoryId=self._cfg.semantic.memory_id,
                namespace=namespace,
                maxResults=100,
            )
        for rec in response.get("memoryRecordSummaries", []):
            rid = rec.get("memoryRecordId")
            if rid in seen:
                continue
            seen.add(rid)
            results.append(
                self._semantic_summary_to_model(rec, project=project or self._project)
            )
    return results
```

4. **Run** `./.venv/Scripts/python.exe -m pytest tests/storage/test_agentcore_unit.py -v` — PASS.

5. **Write the failing UI route tests** in `tests/ui/test_semantic.py` (append). These use the sqlite `client` fixture but SWAP `app.extensions["backend"]` for a stub to prove routing + no local leak. Define the stub once at module scope:
```python
from better_memory.services.semantic import SemanticMemory


class _CapsStub:
    """Exposes the six capability flags the PR1 caps context-processor reads
    at render time (all True: PR2 does no gating, values are irrelevant, but
    the attributes MUST exist or rendering KeyErrors)."""
    supports_episodes = True
    supports_observations = True
    supports_provenance = True
    supports_retention_runs = True
    supports_reflection_review = True
    supports_reflection_text_edit = True


class _SemanticStubBackend(_CapsStub):
    def __init__(self, rows):
        self._rows = rows
        self.calls: list[tuple] = []

    def semantic_list(self, *, project=None, scope_filter=None, search=None,
                      track_exposure=True):
        self.calls.append(("list", project, scope_filter, search))
        return self._rows

    def semantic_observe(self, *, content, project=None, scope="project"):
        self.calls.append(("observe", content, scope))
        return "new-id"

    def semantic_set_scope(self, *, id, scope):
        self.calls.append(("scope", id, scope))

    def semantic_delete(self, *, id):
        self.calls.append(("delete", id))

    def semantic_update_text(self, *, id, content):
        self.calls.append(("update", id, content))


def _semantic_row(id="ac-1", content="agentcore rule", scope="project"):
    return SemanticMemory(
        id=id, content=content, project="testproj", scope=scope,
        created_at="2026-06-01T00:00:00+00:00",
        updated_at="2026-06-01T00:00:00+00:00",
    )


def test_semantic_panel_lists_from_backend_not_local_sqlite(
    client, tmp_db, monkeypatch
):
    """[[server-boot-real-call]] dead-content-table: a sentinel row in the
    LOCAL semantic_memories table must NEVER render; the panel content comes
    from the stubbed backend."""
    import sqlite3
    with sqlite3.connect(tmp_db) as seed:
        seed.execute(
            "INSERT INTO semantic_memories "
            "(id, content, project, scope, created_at, updated_at) VALUES "
            "('local-sentinel','LOCAL SENTINEL ROW','testproj','project',"
            " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
        )
        seed.commit()
    stub = _SemanticStubBackend([_semantic_row(content="BACKEND ROW")])
    client.application.extensions["backend"] = stub
    body = client.get("/semantic/panel").get_data(as_text=True)
    assert "BACKEND ROW" in body
    assert "LOCAL SENTINEL ROW" not in body
    assert stub.calls and stub.calls[0][0] == "list"


def test_semantic_create_calls_backend_observe(client):
    stub = _SemanticStubBackend([])
    client.application.extensions["backend"] = stub
    resp = client.post("/semantic", data={"content": "new fact", "scope": "general"},
                       headers={"Origin": "http://localhost"})
    assert resp.status_code == 200
    assert resp.headers["HX-Trigger"] == "semantic-changed"
    assert ("observe", "new fact", "general") in stub.calls


def test_semantic_scope_and_delete_and_update_call_backend(client):
    stub = _SemanticStubBackend([])
    client.application.extensions["backend"] = stub
    h = {"Origin": "http://localhost"}
    client.post("/semantic/x1/scope", data={"scope": "general"}, headers=h)
    client.post("/semantic/x1/delete", headers=h)
    client.post("/semantic/x1/update", data={"content": "edited"}, headers=h)
    assert ("scope", "x1", "general") in stub.calls
    assert ("delete", "x1") in stub.calls
    assert ("update", "x1", "edited") in stub.calls
```

6. **Run** `./.venv/Scripts/python.exe -m pytest tests/ui/test_semantic.py -v` — FAIL (routes still build `SemanticMemoryService` on the raw conn; sentinel leaks).

7. **Rewire the five routes** in `better_memory/ui/app.py`. Drop the per-route `SemanticMemoryService(...)` construction; read `backend = app.extensions["backend"]`. Keep the `db_conn` only for `fetch_rating_evidence` (untouched here). Exact replacements:
   - `semantic_panel`: replace the service block with
     ```python
     backend = app.extensions["backend"]
     rows = backend.semantic_list(
         project=project, scope_filter=scope_filter, search=search,
     )
     ```
     (keep the existing `project`, `scope_filter`, `search` arg parsing above it verbatim.)
   - `semantic_create`: `app.extensions["backend"].semantic_observe(content=content, project=project, scope=scope)` inside the existing `try` (keep `except ValueError -> 400` card and the `HX-Trigger: semantic-changed`).
   - `semantic_scope`: `app.extensions["backend"].semantic_set_scope(id=id, scope=scope)`.
   - `semantic_delete`: `app.extensions["backend"].semantic_delete(id=id)` (idempotent — no except).
   - `semantic_update`: `app.extensions["backend"].semantic_update_text(id=id, content=content)` inside the existing `try`.
   Leave `semantic_drawer` unchanged (C5).

8. **Run** `./.venv/Scripts/python.exe -m pytest tests/ui/test_semantic.py tests/storage/test_agentcore_unit.py -v` — PASS (existing sqlite semantic route/panel pins still green).

9. **Commit** `fix(ui,storage): route semantic CRUD through StorageBackend + semantic_list general-scope fan-out`.

**Sqlite-preservation note:** in sqlite mode `app.extensions["backend"]` is a `SqliteBackend` whose `semantic_*` methods forward to the SAME `SemanticMemoryService` on the SAME `db_conn` with the SAME `sync_embedder` — identical writes, identical Wilson-ranked list with the exposure side-effect. The existing `tests/ui/test_semantic.py` panel/create/scope/delete pins are unedited. The `semantic_list` fan-out fix only touches the agentcore path; sqlite `semantic_list` is unchanged.

**Deliverable:** general-scope semantic rows appear in the agentcore default view (regression-pinned); all semantic writes + list flow through the backend with no local-content leak.

---

## Task C2 — Route reflection promote / retire through the backend

**Files**
- Modify: `better_memory/ui/app.py` (`reflection_promote`, `reflection_retire` — the MUTATION call only)
- Test: `tests/ui/test_reflections.py`

**Interfaces**
- Consumes existing backend methods: `promote_reflection(*, reflection_id) -> None`, `retire_reflection(*, reflection_id) -> None`.
- Existence check + drawer re-render stay on `queries.reflection_detail(db_conn, ...)` + `queries.fetch_rating_evidence(db_conn, ...)` for now (rewired to `reflection_get`/`reflection_provenance` in C6). Agentcore `RuntimeError` mapping is deferred to C7 — this task keeps the existing `except ValueError -> 409`.

### Steps

1. **Write the failing test** in `tests/ui/test_reflections.py` (append). Seed a real local reflection so the existing-check + re-render (still on `db_conn`) succeed, and stub the backend to prove the mutation routes through it:
```python
class _CapsStub:
    supports_episodes = True
    supports_observations = True
    supports_provenance = True
    supports_retention_runs = True
    supports_reflection_review = True
    supports_reflection_text_edit = True


class _ReflectionMutationStub(_CapsStub):
    def __init__(self):
        self.calls: list[tuple] = []

    def promote_reflection(self, *, reflection_id):
        self.calls.append(("promote", reflection_id))

    def retire_reflection(self, *, reflection_id):
        self.calls.append(("retire", reflection_id))


def _seed_reflection(tmp_db, rid="r1", status="confirmed", scope="project"):
    import sqlite3
    with sqlite3.connect(tmp_db) as seed:
        seed.execute(
            "INSERT INTO reflections (id, title, project, phase, polarity, "
            "use_cases, hints, confidence, status, evidence_count, scope, "
            "created_at, updated_at) VALUES "
            "(?, 't', 'testproj', 'general', 'do', 'uc', '[]', 0.5, ?, 0, ?, "
            "'2026-04-01T00:00:00+00:00', '2026-04-01T00:00:00+00:00')",
            (rid, status, scope),
        )
        seed.commit()


def test_reflection_promote_routes_through_backend(client, tmp_db, monkeypatch):
    from better_memory.ui import app as app_module
    monkeypatch.setattr(app_module, "project_name", lambda: "testproj")
    _seed_reflection(tmp_db, "r-promote", scope="project")
    stub = _ReflectionMutationStub()
    client.application.extensions["backend"] = stub
    # The reflection_service must NOT be the mutation path anymore.
    def _boom(**_):
        raise AssertionError("reflection_service must not be called")
    client.application.extensions["reflection_service"].promote_to_general = _boom
    resp = client.post("/reflections/r-promote/promote",
                       headers={"Origin": "http://localhost"})
    assert resp.status_code == 200
    assert resp.headers["HX-Trigger"] == "reflection-changed"
    assert ("promote", "r-promote") in stub.calls


def test_reflection_retire_routes_through_backend(client, tmp_db, monkeypatch):
    from better_memory.ui import app as app_module
    monkeypatch.setattr(app_module, "project_name", lambda: "testproj")
    _seed_reflection(tmp_db, "r-retire")
    stub = _ReflectionMutationStub()
    client.application.extensions["backend"] = stub
    resp = client.post("/reflections/r-retire/retire",
                       headers={"Origin": "http://localhost"})
    assert resp.status_code == 200
    assert ("retire", "r-retire") in stub.calls
```

2. **Run** `./.venv/Scripts/python.exe -m pytest tests/ui/test_reflections.py -v -k routes_through_backend` — FAIL (routes still call `app.extensions["reflection_service"]`).

3. **Rewire** the mutation line in each route (`better_memory/ui/app.py`):
   - `reflection_promote`: replace `app.extensions["reflection_service"].promote_to_general(reflection_id=id)` with `app.extensions["backend"].promote_reflection(reflection_id=id)`.
   - `reflection_retire`: replace `app.extensions["reflection_service"].retire(reflection_id=id)` with `app.extensions["backend"].retire_reflection(reflection_id=id)`.
   Leave the `queries.reflection_detail` existence check, the `except ValueError -> 409` card, the drawer re-render, and `reflection_confirm`/`reflection_edit_*` untouched.

4. **Run** `./.venv/Scripts/python.exe -m pytest tests/ui/test_reflections.py -v` — PASS (existing sqlite promote/retire pins still green — the `SqliteBackend` methods delegate to the same `ReflectionService`).

5. **Commit** `feat(ui): route reflection promote/retire through StorageBackend`.

**Sqlite-preservation note:** `SqliteBackend.promote_reflection`/`retire_reflection` are verbatim delegates to `ReflectionService.promote_to_general`/`retire` on the same conn — identical mutation, identical `ValueError -> 409`. The existing drawer re-render still runs `queries.reflection_detail` on `db_conn`, so rendered HTML is unchanged.

**Deliverable:** promote/retire mutate through the backend in both modes; sqlite HTML unchanged.

---

## Task C3 — `reflection_get` + split `queries.reflection_detail` into `reflection_row` / `reflection_provenance`

**Files**
- Modify: `better_memory/storage/protocol.py` (method decl + docstring), `better_memory/ui/queries.py` (extract two helpers; recompose `reflection_detail`), `better_memory/storage/sqlite.py` (impl), `better_memory/storage/agentcore.py` (impl)
- Test: `tests/ui/test_queries.py` (extraction pin), `tests/storage/test_sqlite_backend.py`, `tests/storage/test_agentcore_unit.py`

**Interfaces (Consumes / Produces)**
- Produces `queries.reflection_row(conn, *, reflection_id: str) -> ReflectionFull | None` (the row SELECT half of today's `reflection_detail`).
- Produces `queries.reflection_provenance(conn, *, reflection_id: str) -> list[ReflectionSourceObservation]` (the provenance SELECT half).
- Produces `StorageBackend.reflection_get(self, *, reflection_id: str) -> dict | None` (row dict, NO provenance; `None` when absent).
- `queries.reflection_detail` is RECOMPOSED from the two helpers so its `ReflectionDetail` output is byte-identical (composition pin).

### Steps

1. **Write the failing extraction pin** in `tests/ui/test_queries.py` (append). Seed a reflection with one source observation + episode, then assert composition:
```python
import sqlite3
from dataclasses import asdict

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.ui import queries


def _seed_refl_with_source(conn):
    conn.execute(
        "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
        "close_reason, goal) VALUES "
        "('ep1','testproj','2026-04-01T00:00:00+00:00',"
        "'2026-04-01T01:00:00+00:00','success','goal_complete','ship it')"
    )
    conn.execute(
        "INSERT INTO observations (id, content, project, outcome, status, "
        "created_at, episode_id) VALUES "
        "('o1','obs body','testproj','success','active',"
        "'2026-04-01T00:30:00+00:00','ep1')"
    )
    conn.execute(
        "INSERT INTO reflections (id, title, project, phase, polarity, "
        "use_cases, hints, confidence, status, evidence_count, scope, "
        "created_at, updated_at) VALUES "
        "('r1','t','testproj','general','do','uc','[\"h1\"]',0.5,'confirmed',"
        "1,'project','2026-04-01T00:00:00+00:00','2026-04-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO reflection_sources (reflection_id, observation_id) "
        "VALUES ('r1','o1')"
    )
    conn.commit()


def test_reflection_detail_composes_from_row_and_provenance(tmp_path):
    conn = connect(tmp_path / "m.db")
    apply_migrations(conn)
    _seed_refl_with_source(conn)
    detail = queries.reflection_detail(conn, reflection_id="r1")
    row = queries.reflection_row(conn, reflection_id="r1")
    prov = queries.reflection_provenance(conn, reflection_id="r1")
    assert detail is not None and row is not None
    assert detail.reflection == row
    assert detail.sources == prov


def test_reflection_row_none_for_missing(tmp_path):
    conn = connect(tmp_path / "m.db")
    apply_migrations(conn)
    assert queries.reflection_row(conn, reflection_id="nope") is None
    assert queries.reflection_provenance(conn, reflection_id="nope") == []
```

2. **Write the failing backend tests.**
   `tests/storage/test_sqlite_backend.py` (append):
```python
def test_reflection_get_returns_row_without_sources(backend, memory_conn):
    from dataclasses import asdict
    from better_memory.ui import queries
    memory_conn.execute(
        "INSERT INTO reflections (id, title, project, phase, polarity, "
        "use_cases, hints, confidence, status, evidence_count, scope, "
        "created_at, updated_at) VALUES "
        "('r1','t','testproj','general','do','uc','[]',0.5,'confirmed',0,"
        "'project','2026-04-01T00:00:00+00:00','2026-04-01T00:00:00+00:00')"
    )
    memory_conn.commit()
    got = backend.reflection_get(reflection_id="r1")
    detail = queries.reflection_detail(memory_conn, reflection_id="r1")
    assert got == asdict(detail.reflection)
    assert "sources" not in got


def test_reflection_get_missing_returns_none(backend):
    assert backend.reflection_get(reflection_id="nope") is None
```
   `tests/storage/test_agentcore_unit.py` (append). Body-shaped migrated record + a 404 case (monkeypatch `_ClientError` to the local fake, as the existing `record_use` 404 test does at line ~1152):
```python
import json as _json

import better_memory.storage.agentcore as ac_module


def test_reflection_get_parses_body_record(backend, mock_data_client):
    body = _json.dumps({
        "title": "Body reflection", "use_cases": "when X",
        "hints": ["h1", "h2"], "confidence": "0.8", "polarity": "do",
        "status": "active", "phase": "planning",
    })
    mock_data_client.get_memory_record.return_value = {"memoryRecord": {
        "memoryRecordId": "rec-1",
        "content": {"text": body},
        "namespaces": ["projects/testproj/reflections/"],
        "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
        "metadata": {"useful_count": {"numberValue": 4}},
    }}
    got = backend.reflection_get(reflection_id="rec-1")
    assert got["id"] == "rec-1"
    assert got["title"] == "Body reflection"
    assert got["status"] == "active"
    assert got["polarity"] == "do"
    assert got["scope"] == "project"
    assert got["useful_count"] == 4
    assert got["last_useful_at"] is None
    # hints serialized as a JSON string so the drawer's decode_hints filter
    # decodes it identically to the sqlite column shape.
    assert _json.loads(got["hints"]) == ["h1", "h2"]


def test_reflection_get_returns_none_on_404(backend, mock_data_client, monkeypatch):
    monkeypatch.setattr(ac_module, "_ClientError", _FakeClientError)
    mock_data_client.get_memory_record.side_effect = _FakeClientError(
        code="ResourceNotFoundException", message="missing"
    )
    assert backend.reflection_get(reflection_id="gone") is None
```

3. **Run** the three test files — FAIL (helpers + method absent).

4. **Implement the queries split** (`better_memory/ui/queries.py`). Extract the two SELECT blocks out of `reflection_detail` into module-level functions and recompose:
```python
def reflection_row(
    conn: sqlite3.Connection, *, reflection_id: str
) -> ReflectionFull | None:
    """The single-row half of reflection_detail: the reflections row mapped
    to ReflectionFull, or None if absent. No provenance."""
    r_row = conn.execute(
        "SELECT id, title, project, tech, phase, polarity, "
        "confidence, status, use_cases, hints, evidence_count, scope, "
        "created_at, updated_at, "
        "useful_count, last_useful_at, times_misled, last_misled_at, "
        "times_overlooked, last_overlooked_at "
        "FROM reflections WHERE id = ?",
        (reflection_id,),
    ).fetchone()
    if r_row is None:
        return None
    return ReflectionFull(
        id=r_row["id"], title=r_row["title"], project=r_row["project"],
        tech=r_row["tech"], phase=r_row["phase"], polarity=r_row["polarity"],
        confidence=r_row["confidence"], status=r_row["status"],
        use_cases=r_row["use_cases"], hints=r_row["hints"],
        evidence_count=r_row["evidence_count"], scope=r_row["scope"],
        created_at=r_row["created_at"], updated_at=r_row["updated_at"],
        useful_count=r_row["useful_count"] or 0,
        last_useful_at=r_row["last_useful_at"],
        times_misled=r_row["times_misled"] or 0,
        last_misled_at=r_row["last_misled_at"],
        times_overlooked=r_row["times_overlooked"] or 0,
        last_overlooked_at=r_row["last_overlooked_at"],
    )


def reflection_provenance(
    conn: sqlite3.Connection, *, reflection_id: str
) -> list[ReflectionSourceObservation]:
    """The source-observation half of reflection_detail. Empty list when the
    reflection has no sources (or does not exist)."""
    src_rows = conn.execute(
        """
        SELECT
            o.id AS observation_id, o.content AS content,
            o.component AS component, o.theme AS theme,
            o.outcome AS obs_outcome, o.created_at AS obs_created_at,
            e.id AS episode_id, e.goal AS episode_goal,
            e.outcome AS episode_outcome, e.close_reason AS episode_close_reason
        FROM reflection_sources rs
        JOIN observations o ON o.id = rs.observation_id
        JOIN episodes     e ON e.id = o.episode_id
        WHERE rs.reflection_id = ?
        ORDER BY o.created_at DESC, o.rowid DESC
        """,
        (reflection_id,),
    ).fetchall()
    return [
        ReflectionSourceObservation(
            observation_id=r["observation_id"], content=r["content"],
            component=r["component"], theme=r["theme"], outcome=r["obs_outcome"],
            created_at=r["obs_created_at"], episode_id=r["episode_id"],
            episode_goal=r["episode_goal"], episode_outcome=r["episode_outcome"],
            episode_close_reason=r["episode_close_reason"],
        )
        for r in src_rows
    ]


def reflection_detail(
    conn: sqlite3.Connection, *, reflection_id: str
) -> ReflectionDetail | None:
    """Return one reflection with its source observations, or None. Recomposed
    from reflection_row + reflection_provenance — output is byte-identical to
    the pre-split version (pinned by test_reflection_detail_composes_*)."""
    reflection = reflection_row(conn, reflection_id=reflection_id)
    if reflection is None:
        return None
    return ReflectionDetail(
        reflection=reflection,
        sources=reflection_provenance(conn, reflection_id=reflection_id),
    )
```

5. **Implement `reflection_get`** on the protocol + both backends.
   `better_memory/storage/protocol.py` (add after `retrieve`):
```python
def reflection_get(self, *, reflection_id: str) -> dict[str, Any] | None:
    """Single reflection row as a dict (ReflectionFull field shape), NO
    provenance; None when absent. sqlite reads its row; agentcore fetches +
    parses the record. The drawer route composes provenance separately
    (queries.reflection_provenance on the local conn, flag-gated in PR 3)."""
    ...
```
   `better_memory/storage/sqlite.py` (add in the reflection-lifecycle section):
```python
def reflection_get(self, *, reflection_id: str) -> dict[str, Any] | None:
    from dataclasses import asdict

    from better_memory.ui import queries
    row = queries.reflection_row(self._conn, reflection_id=reflection_id)
    return None if row is None else asdict(row)
```
   `better_memory/storage/agentcore.py` (add near `_parse_reflection_record`). Map the parsed internal shape to the ReflectionFull dict keys the drawer needs; derive `scope` from the record namespaces; serialise `hints` to a JSON string; `last_*_at -> None`:
```python
def reflection_get(self, *, reflection_id: str) -> dict[str, Any] | None:
    try:
        record = self._get_record(reflection_id)
    except _ClientError as exc:
        if exc.response.get("Error", {}).get("Code", "") == "ResourceNotFoundException":
            return None
        raise
    parsed = self._parse_reflection_record(record)
    if parsed is None:
        return None
    namespaces = record.get("namespaces") or []
    first_ns = next(iter(namespaces or [""]), "")
    scope = "general" if first_ns.lstrip("/").startswith("general/") else "project"
    created = record.get("createdAt")
    created_at = created.isoformat() if isinstance(created, datetime) else (created or "")
    return {
        "id": parsed["id"], "title": parsed["title"], "project": self._project,
        "tech": parsed["tech"], "phase": parsed["phase"],
        "polarity": parsed["_polarity"], "confidence": parsed["confidence"],
        "status": parsed["_status"], "use_cases": parsed["use_cases"],
        "hints": json.dumps(parsed["hints"]),
        "evidence_count": parsed["evidence_count"], "scope": scope,
        "created_at": created_at, "updated_at": parsed["updated_at"],
        "useful_count": parsed["useful_count"], "last_useful_at": None,
        "times_misled": parsed["times_misled"], "last_misled_at": None,
        "times_overlooked": parsed["times_overlooked"], "last_overlooked_at": None,
    }
```

6. **Run** `./.venv/Scripts/python.exe -m pytest tests/ui/test_queries.py tests/storage/test_sqlite_backend.py tests/storage/test_agentcore_unit.py -v` — PASS. Full `tests/ui` run green (drawer HTML unchanged).

7. **Commit** `feat(storage): reflection_get row accessor + queries reflection_row/provenance split`.

**Sqlite-preservation note:** `reflection_detail` now composes the two extracted helpers; its `ReflectionDetail` output (including the `useful_count` etc. properties) is identical, pinned by the composition test. Every existing drawer test is unedited and green.

**Deliverable:** a row-only reflection accessor on both backends + the provenance split that unblocks the flag-gated provenance path in PR 3.

---

## Task C4 — `reflection_list` flat, Wilson-ordered (trickiest — full TDD on the status remap)

**Files**
- Modify: `better_memory/storage/protocol.py` (method decl), `better_memory/storage/sqlite.py` (impl), `better_memory/storage/agentcore.py` (impl + retired-namespace fan-out + a paginated list helper)
- Test: `tests/storage/test_sqlite_backend.py`, `tests/storage/test_agentcore_unit.py`

**Interfaces (Produces)**
```python
def reflection_list(self, *, project=None, tech=None, phase=None, polarity=None,
    status=None, min_confidence=0.0, useful_only=False, limit=200) -> list[dict]: ...
```
Each dict carries exactly the keys `fragments/reflection_row.html` renders: `id, title, project, tech, phase, polarity, confidence, status, use_cases, evidence_count, updated_at, useful_count, times_misled, times_overlooked`.

- **Sqlite:** verbatim delegate to `queries.reflection_list_for_ui`, then `dataclasses.asdict` each `ReflectionListRow`.
- **Agentcore:** fan out `list_memory_records` over `projects/{actor}/reflections/` + `general/reflections/` (+ `projects/{actor}/retired/` and `general/retired/` ONLY when the resolved status set admits `retired`); parse via `_parse_reflection_record`; dedup by id (project ns first wins); filter tech/phase/polarity/min_confidence/useful_only + status-set membership client-side; order by `wilson_lower_bound(useful+overlooked, useful+overlooked+ignored)` desc, `confidence` desc, `_updated_at_ts` desc; truncate to `limit`.
- **Status remap:** agentcore vocabulary is `active`/`promoted`/`retired` (NO `pending_review`). Resolved status set: `status is None -> {"active","promoted"}`; else `-> {status}`. sqlite keeps its own default set (`{pending_review, confirmed}`) inside `reflection_list_for_ui` — the two default sets are intentionally different because the two stores use different status vocabularies (documented in the design; both map to "the active/live reflections").

### Steps

1. **Write the failing sqlite delegation test** in `tests/storage/test_sqlite_backend.py` (append):
```python
def test_reflection_list_matches_queries_for_ui(backend, memory_conn):
    from dataclasses import asdict
    from better_memory.ui import queries
    memory_conn.executemany(
        "INSERT INTO reflections (id, title, project, phase, polarity, "
        "use_cases, hints, confidence, status, evidence_count, scope, "
        "created_at, updated_at) VALUES "
        "(?, ?, 'testproj', 'general', 'do', 'uc', '[]', ?, ?, 0, 'project', "
        "'2026-04-01T00:00:00+00:00', '2026-04-01T00:00:00+00:00')",
        [("a", "A", 0.9, "confirmed"),
         ("b", "B", 0.5, "confirmed"),
         ("c", "C", 0.5, "retired")],
    )
    memory_conn.commit()
    got = backend.reflection_list(project="testproj")
    expect = [asdict(r) for r in queries.reflection_list_for_ui(
        memory_conn, project="testproj")]
    assert got == expect
    # default excludes retired; explicit status='retired' surfaces it
    assert [r["id"] for r in got] == ["a", "b"]
    retired = backend.reflection_list(project="testproj", status="retired")
    assert [r["id"] for r in retired] == ["c"]
```

2. **Write the failing agentcore tests** in `tests/storage/test_agentcore_unit.py` (append). Reuse the `_wilson_ranking_record` helper (id/counters) already in the module; add a retired-record helper and a namespace-keyed stub. Cover: flat Wilson order (same 67/125 vs 3/1 numbers as `test_wilson_ranking.py`), retired excluded-by-default / included-on-request, polarity filter, best-effort AWS degrade, row-key completeness.
```python
def _retired_record(rec_id):
    import json
    return {
        "memoryRecordId": rec_id,
        "content": {"text": json.dumps({
            "title": rec_id, "use_cases": "u", "hints": "h",
            "confidence": "0.9", "polarity": "do", "status": "retired",
        })},
        "namespaces": ["projects/testproj/retired/"],
        "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
        "metadata": {"status": {"stringValue": "retired"}},
    }


def test_reflection_list_flat_wilson_order(backend, mock_data_client):
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record("r-workhorse", useful=67, ignored=125),
                _wilson_ranking_record("r-newcomer", useful=3, ignored=1),
            ]}
        return {"memoryRecordSummaries": []}
    mock_data_client.list_memory_records.side_effect = stub
    rows = backend.reflection_list(project="testproj")
    assert [r["id"] for r in rows] == ["r-newcomer", "r-workhorse"]
    # row-key completeness against the template's field list
    assert set(rows[0]) == {
        "id", "title", "project", "tech", "phase", "polarity", "confidence",
        "status", "use_cases", "evidence_count", "updated_at",
        "useful_count", "times_misled", "times_overlooked",
    }
    assert not any(k.startswith("_") for k in rows[0])


def test_reflection_list_default_excludes_retired(backend, mock_data_client):
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record("r-active", useful=5, ignored=1),
            ]}
        return {"memoryRecordSummaries": []}
    mock_data_client.list_memory_records.side_effect = stub
    rows = backend.reflection_list(project="testproj")
    assert [r["id"] for r in rows] == ["r-active"]
    # retired namespaces are NOT queried under the default status set
    queried = {c.kwargs["namespace"] for c in mock_data_client.list_memory_records.call_args_list}
    assert "projects/testproj/retired/" not in queried


def test_reflection_list_status_retired_queries_retired_namespaces(backend, mock_data_client):
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/retired/":
            return {"memoryRecordSummaries": [_retired_record("r-old")]}
        return {"memoryRecordSummaries": []}
    mock_data_client.list_memory_records.side_effect = stub
    rows = backend.reflection_list(project="testproj", status="retired")
    assert [r["id"] for r in rows] == ["r-old"]
    assert rows[0]["status"] == "retired"
    queried = {c.kwargs["namespace"] for c in mock_data_client.list_memory_records.call_args_list}
    assert {"projects/testproj/retired/", "general/retired/"} <= queried


def test_reflection_list_polarity_filter_drops_non_matches(backend, mock_data_client):
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record("r-do", useful=5, ignored=1),  # polarity 'do'
            ]}
        return {"memoryRecordSummaries": []}
    mock_data_client.list_memory_records.side_effect = stub
    assert backend.reflection_list(project="testproj", polarity="dont") == []


def test_reflection_list_best_effort_degrades_on_namespace_error(backend, mock_data_client):
    """[[guard-needs-triggering-test]] One namespace raising must NOT 500 the
    whole list — the surviving namespace's rows come through."""
    def stub(**kwargs):
        if kwargs["namespace"] == "general/reflections/":
            raise _FakeClientError(code="ThrottlingException", message="rate exceeded")
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record("r-survivor", useful=5, ignored=1),
            ]}
        return {"memoryRecordSummaries": []}
    mock_data_client.list_memory_records.side_effect = stub
    rows = backend.reflection_list(project="testproj")
    assert [r["id"] for r in rows] == ["r-survivor"]
```

3. **Run** both files — FAIL (`reflection_list` absent).

4. **Implement** the protocol decl + both backends.
   `better_memory/storage/protocol.py`:
```python
def reflection_list(
    self, *, project: str | None = None, tech: str | None = None,
    phase: str | None = None, polarity: str | None = None,
    status: str | None = None, min_confidence: float = 0.0,
    useful_only: bool = False, limit: int = 200,
) -> list[dict[str, Any]]:
    """Flat reflection list for the UI panel, ordered by the shared Wilson
    lower bound desc / confidence desc / updated_at desc. sqlite delegates to
    queries.reflection_list_for_ui; agentcore fans out over the reflections
    (+ retired, when the status set admits it) namespaces and filters/orders
    client-side. status=None admits the live set (sqlite pending_review/
    confirmed; agentcore active/promoted)."""
    ...
```
   `better_memory/storage/sqlite.py`:
```python
def reflection_list(
    self, *, project: str | None = None, tech: str | None = None,
    phase: str | None = None, polarity: str | None = None,
    status: str | None = None, min_confidence: float = 0.0,
    useful_only: bool = False, limit: int = 200,
) -> list[dict[str, Any]]:
    from dataclasses import asdict

    from better_memory.ui import queries
    rows = queries.reflection_list_for_ui(
        self._conn, project=project or self._project, tech=tech, phase=phase,
        polarity=polarity, status=status, min_confidence=min_confidence,
        useful_only=useful_only, limit=limit,
    )
    return [asdict(r) for r in rows]
```
   `better_memory/storage/agentcore.py` — add a small paginated list helper (mirrors the inner `_fetch` in `_fetch_reflection_buckets`) and the method (`wilson_lower_bound` is already imported at module top):
```python
def _list_records_paginated(self, namespace: str, max_results: int = 200) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    token: str | None = None
    while len(summaries) < max_results:
        kwargs: dict[str, Any] = {
            "memoryId": self._cfg.episodic.memory_id,
            "namespace": namespace,
            "maxResults": min(100, max_results - len(summaries)),
        }
        if token:
            kwargs["nextToken"] = token
        response = self._data.list_memory_records(**kwargs)
        summaries.extend(response.get("memoryRecordSummaries", []))
        token = response.get("nextToken")
        if not token:
            break
    return summaries


def reflection_list(
    self, *, project: str | None = None, tech: str | None = None,
    phase: str | None = None, polarity: str | None = None,
    status: str | None = None, min_confidence: float = 0.0,
    useful_only: bool = False, limit: int = 200,
) -> list[dict[str, Any]]:
    actor_id = resolve_actor_id(project or self._project)
    wanted = {"active", "promoted"} if status is None else {status}

    refl_project = resolve_namespace(actor_id, "reflections")
    refl_general = resolve_namespace("general", "reflections")
    namespaces = [refl_project]
    if refl_general != refl_project:
        namespaces.append(refl_general)
    if "retired" in wanted:
        ret_project = resolve_namespace(actor_id, "retired")
        ret_general = resolve_namespace("general", "retired")
        namespaces.append(ret_project)
        if ret_general != ret_project:
            namespaces.append(ret_general)

    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for namespace in namespaces:
        try:
            summaries = self._list_records_paginated(namespace, max_results=limit * 2)
        except Exception:  # noqa: BLE001 - best-effort; one namespace failing must not 500
            continue
        for rec in summaries:
            parsed = self._parse_reflection_record(rec, tech_filter=tech, phase_filter=phase)
            if parsed is None or parsed["id"] in seen:
                continue
            if parsed["_status"] not in wanted:
                continue
            if polarity is not None and parsed["_polarity"] != polarity:
                continue
            if parsed["confidence"] < min_confidence:
                continue
            if useful_only and parsed["useful_count"] <= 0:
                continue
            seen.add(parsed["id"])
            rows.append(parsed)

    rows.sort(key=lambda r: (
        -wilson_lower_bound(
            r["useful_count"] + r["times_overlooked"],
            r["useful_count"] + r["times_overlooked"] + r["times_ignored"],
        ),
        -r["confidence"],
        -r["_updated_at_ts"],
    ))
    return [
        {
            "id": r["id"], "title": r["title"], "project": project or self._project,
            "tech": r["tech"], "phase": r["phase"], "polarity": r["_polarity"],
            "confidence": r["confidence"], "status": r["_status"],
            "use_cases": r["use_cases"], "evidence_count": r["evidence_count"],
            "updated_at": r["updated_at"], "useful_count": r["useful_count"],
            "times_misled": r["times_misled"], "times_overlooked": r["times_overlooked"],
        }
        for r in rows[:limit]
    ]
```

5. **Run** `./.venv/Scripts/python.exe -m pytest tests/storage -v` — PASS.

6. **Commit** `feat(storage): flat reflection_list with Wilson ordering + status remap on both backends`.

**Sqlite-preservation note:** sqlite `reflection_list` is a pure forward to `queries.reflection_list_for_ui` (same rows, same order) with `asdict`; the query itself is untouched, so its `tests/ui`/`tests/services` pins stay green. The status-default divergence (sqlite `{pending_review, confirmed}` vs agentcore `{active, promoted}`) lives entirely inside each store's own path.

**Deliverable:** a flat, Wilson-ordered reflection list on both backends, with the retired-namespace inclusion and best-effort degrade both triggering-tested.

---

## Task C5 — `semantic_get` single-record accessor + rewire the Semantic drawer route

**Files**
- Modify: `better_memory/services/semantic.py` (`SemanticMemoryService.get`), `better_memory/storage/protocol.py` (method decl), `better_memory/storage/sqlite.py` (impl), `better_memory/storage/agentcore.py` (impl), `better_memory/ui/app.py` (`semantic_drawer` route)
- Test: `tests/services/test_semantic.py` (or wherever `SemanticMemoryService` is unit-tested), `tests/storage/test_sqlite_backend.py`, `tests/storage/test_agentcore_unit.py`, `tests/ui/test_semantic.py`

**Interfaces (Produces)**
```python
# services/semantic.py
def get(self, *, id: str) -> SemanticMemory | None: ...
# protocol / both backends
def semantic_get(self, *, id: str) -> SemanticMemory | None: ...
```
- Sqlite `semantic_get` -> `self._semantic.get(id=id)` (a single-row SELECT returning the `SemanticMemory` dataclass — the drawer template reads all its attributes).
- Agentcore `semantic_get` -> `_get_semantic_record(id)` + `_semantic_summary_to_model`; `None` on 404.

### Steps

1. **Write the failing service test** in the `SemanticMemoryService` unit-test module (append):
```python
def test_semantic_service_get_returns_model_or_none(memory_conn):
    from better_memory.services.semantic import SemanticMemoryService, SemanticMemory
    svc = SemanticMemoryService(memory_conn)
    mid = svc.create(content="a rule", project="p", scope="general")
    got = svc.get(id=mid)
    assert isinstance(got, SemanticMemory)
    assert got.id == mid and got.content == "a rule" and got.scope == "general"
    assert svc.get(id="missing") is None
```

2. **Write the failing backend tests.**
   `tests/storage/test_sqlite_backend.py` (append):
```python
def test_semantic_get_returns_model(backend, memory_conn):
    from better_memory.services.semantic import SemanticMemory
    memory_conn.execute(
        "INSERT INTO semantic_memories "
        "(id, content, project, scope, created_at, updated_at) VALUES "
        "('s1','the rule','testproj','project',"
        "'2026-05-01T00:00:00+00:00','2026-05-01T00:00:00+00:00')"
    )
    memory_conn.commit()
    got = backend.semantic_get(id="s1")
    assert isinstance(got, SemanticMemory)
    assert got.content == "the rule"
    assert backend.semantic_get(id="nope") is None
```
   `tests/storage/test_agentcore_unit.py` (append):
```python
def test_semantic_get_maps_record(backend, mock_data_client):
    from better_memory.services.semantic import SemanticMemory
    mock_data_client.get_memory_record.return_value = {"memoryRecord": {
        "memoryRecordId": "sm-1",
        "content": {"text": "prefer uv"},
        "namespaces": ["/general/semantic/"],
        "createdAt": datetime(2026, 5, 25, tzinfo=UTC),
        "metadata": {"useful_count": {"numberValue": 2}},
    }}
    got = backend.semantic_get(id="sm-1")
    assert isinstance(got, SemanticMemory)
    assert got.id == "sm-1" and got.scope == "general" and got.useful_count == 2


def test_semantic_get_returns_none_on_404(backend, mock_data_client, monkeypatch):
    import better_memory.storage.agentcore as ac_module
    monkeypatch.setattr(ac_module, "_ClientError", _FakeClientError)
    mock_data_client.get_memory_record.side_effect = _FakeClientError(
        code="ResourceNotFoundException", message="missing"
    )
    assert backend.semantic_get(id="gone") is None
```

3. **Write the failing UI drawer test** in `tests/ui/test_semantic.py` (append; reuses `_SemanticStubBackend`/`_semantic_row` from C1 — extend the stub with `semantic_get`):
```python
def test_semantic_drawer_reads_from_backend_not_local(client, tmp_db):
    import sqlite3
    with sqlite3.connect(tmp_db) as seed:
        seed.execute(
            "INSERT INTO semantic_memories "
            "(id, content, project, scope, created_at, updated_at) VALUES "
            "('d1','LOCAL SENTINEL','testproj','project',"
            "'2026-05-01T00:00:00+00:00','2026-05-01T00:00:00+00:00')"
        )
        seed.commit()
    class _Stub(_SemanticStubBackend):
        def semantic_get(self, *, id):
            self.calls.append(("get", id))
            return _semantic_row(id="d1", content="BACKEND DRAWER ROW")
    stub = _Stub([])
    client.application.extensions["backend"] = stub
    body = client.get("/semantic/d1/drawer").get_data(as_text=True)
    assert "BACKEND DRAWER ROW" in body
    assert "LOCAL SENTINEL" not in body
    assert ("get", "d1") in stub.calls


def test_semantic_drawer_404_when_backend_returns_none(client):
    class _Stub(_SemanticStubBackend):
        def semantic_get(self, *, id):
            return None
    client.application.extensions["backend"] = _Stub([])
    assert client.get("/semantic/nope/drawer").status_code == 404
```

4. **Run** the four test files — FAIL.

5. **Implement.**
   `better_memory/services/semantic.py` (add to `SemanticMemoryService`):
```python
def get(self, *, id: str) -> SemanticMemory | None:
    """Single semantic memory by id, or None. Same field set as
    list_for_project's read model (drives the UI drawer)."""
    row = self._conn.execute(
        "SELECT id, content, project, scope, created_at, updated_at, "
        "useful_count, last_useful_at, times_misled, last_misled_at, "
        "times_overlooked, last_overlooked_at, times_ignored, last_ignored_at "
        "FROM semantic_memories WHERE id = ?",
        (id,),
    ).fetchone()
    if row is None:
        return None
    return SemanticMemory(
        id=row["id"], content=row["content"], project=row["project"],
        scope=row["scope"], created_at=row["created_at"], updated_at=row["updated_at"],
        useful_count=row["useful_count"] or 0, last_useful_at=row["last_useful_at"],
        times_misled=row["times_misled"] or 0, last_misled_at=row["last_misled_at"],
        times_overlooked=row["times_overlooked"] or 0,
        last_overlooked_at=row["last_overlooked_at"],
        times_ignored=row["times_ignored"] or 0, last_ignored_at=row["last_ignored_at"],
    )
```
   `better_memory/storage/protocol.py`:
```python
def semantic_get(self, *, id: str) -> Any | None:
    """Single semantic memory (SemanticMemory) by id, or None when absent."""
    ...
```
   `better_memory/storage/sqlite.py` (semantic section):
```python
def semantic_get(self, *, id: str) -> Any | None:
    return self._semantic.get(id=id)
```
   `better_memory/storage/agentcore.py` (semantic section):
```python
def semantic_get(self, *, id: str) -> Any | None:
    try:
        record = self._get_semantic_record(id)
    except _ClientError as exc:
        if exc.response.get("Error", {}).get("Code", "") == "ResourceNotFoundException":
            return None
        raise
    return self._semantic_summary_to_model(record, project=self._project)
```
   `better_memory/ui/app.py` `semantic_drawer` — replace the inline SELECT + dict build with:
```python
@app.get("/semantic/<id>/drawer")
def semantic_drawer(id: str):
    conn = app.extensions["db_connection"]
    memory = app.extensions["backend"].semantic_get(id=id)
    if memory is None:
        abort(404)
    rating_evidence = queries.fetch_rating_evidence(conn, "semantic", id)
    return render_template(
        "fragments/semantic_drawer.html",
        memory=memory, rating_evidence=rating_evidence,
    )
```

6. **Run** `./.venv/Scripts/python.exe -m pytest tests/services tests/storage tests/ui/test_semantic.py -v` — PASS.

7. **Commit** `feat(storage,ui): semantic_get accessor + backend-routed semantic drawer`.

**Sqlite-preservation note:** the drawer previously built a dict from an inline SELECT; `SemanticMemory` exposes the identical attribute set the template reads (`content/project/scope/updated_at/useful_count/last_useful_at/times_overlooked/last_overlooked_at/times_misled/last_misled_at`), so rendered HTML is byte-identical. `fetch_rating_evidence` still runs on `db_conn`.

**Deliverable:** semantic detail is backend-routed with a 404 parity path; the inline drawer SELECT is gone.

---

## Task C6 — Route the Reflections list + detail (drawer / promote / retire re-render) through the backend

**Files**
- Modify: `better_memory/ui/app.py` (`reflections_panel`, `reflections_drawer`, and the read/re-render halves of `reflection_promote`, `reflection_retire`)
- Test: `tests/ui/test_reflections.py`

**Interfaces**
- `reflections_panel` -> `backend.reflection_list(project=…, tech=…, phase=…, polarity=…, status=…, min_confidence=…, useful_only=…)` (returns `list[dict]`; the panel template consumes dicts via Jinja item access — NO template change).
- `reflections_drawer` / promote / retire re-render -> compose `detail` from `backend.reflection_get(reflection_id=id)` (row + existence; `None -> abort(404)`) and `queries.reflection_provenance(db_conn, reflection_id=id)` (still on the local conn; flag-gated in PR 3). `queries.fetch_rating_evidence(db_conn, …)` unchanged.
- No template edits ([[brutalist-css-classes]] carried, not exercised).

### Steps

1. **Write the failing tests** in `tests/ui/test_reflections.py` (append; reuses `_CapsStub`/`_seed_reflection` from C2). A combined stub returns a list row + a drawer row and records mutation calls:
```python
class _ReflectionStubBackend(_CapsStub):
    def __init__(self, list_rows, get_row):
        self._list_rows = list_rows
        self._get_row = get_row
        self.calls: list[tuple] = []

    def reflection_list(self, *, project=None, tech=None, phase=None,
                        polarity=None, status=None, min_confidence=0.0,
                        useful_only=False, limit=200):
        self.calls.append(("list", project, status))
        return self._list_rows

    def reflection_get(self, *, reflection_id):
        self.calls.append(("get", reflection_id))
        return self._get_row

    def promote_reflection(self, *, reflection_id):
        self.calls.append(("promote", reflection_id))

    def retire_reflection(self, *, reflection_id):
        self.calls.append(("retire", reflection_id))


def _row_dict(id="ac-r1", title="BACKEND REFLECTION", status="active", scope="project"):
    return {
        "id": id, "title": title, "project": "testproj", "tech": None,
        "phase": "general", "polarity": "do", "confidence": 0.5,
        "status": status, "use_cases": "uc", "evidence_count": 0,
        "updated_at": "2026-06-01T00:00:00+00:00", "useful_count": 0,
        "times_misled": 0, "times_overlooked": 0,
    }


def test_reflections_panel_lists_from_backend_not_local(client, tmp_db, monkeypatch):
    """[[server-boot-real-call]] dead-content-table: a local reflections row
    must never render; the panel comes from backend.reflection_list."""
    from better_memory.ui import app as app_module
    monkeypatch.setattr(app_module, "project_name", lambda: "testproj")
    _seed_reflection(tmp_db, "local-sentinel")  # title 't' in local table
    stub = _ReflectionStubBackend([_row_dict()], None)
    client.application.extensions["backend"] = stub
    body = client.get("/reflections/panel").get_data(as_text=True)
    assert "BACKEND REFLECTION" in body
    assert "local-sentinel" not in body
    assert stub.calls[0][0] == "list"


def test_reflections_drawer_reads_from_backend(client, tmp_db):
    drawer_row = dict(_row_dict(), hints="[]", created_at="2026-06-01T00:00:00+00:00",
                      last_useful_at=None, last_misled_at=None, last_overlooked_at=None)
    stub = _ReflectionStubBackend([], drawer_row)
    client.application.extensions["backend"] = stub
    body = client.get("/reflections/ac-r1/drawer").get_data(as_text=True)
    assert "BACKEND REFLECTION" in body
    assert ("get", "ac-r1") in stub.calls


def test_reflections_drawer_404_when_backend_none(client):
    stub = _ReflectionStubBackend([], None)
    client.application.extensions["backend"] = stub
    assert client.get("/reflections/nope/drawer").status_code == 404
```
   Note the drawer row dict must carry the extra ReflectionFull keys the drawer template reads (`hints`, `created_at`, `last_*_at`) in addition to the list keys — the real `reflection_get` returns exactly that superset.

2. **Run** `./.venv/Scripts/python.exe -m pytest tests/ui/test_reflections.py -v -k backend` — FAIL (panel/drawer still on `queries` + `db_conn`; drawer 404 path checks local table).

3. **Rewire** `better_memory/ui/app.py`. Add a tiny composition helper near the reflection routes:
```python
from types import SimpleNamespace  # add to the existing imports at top of app.py

def _reflection_drawer_detail(app, id):
    """Compose the drawer view model: row via backend.reflection_get (row +
    existence), provenance via the local conn (flag-gated in PR 3). Returns
    None when the reflection does not exist."""
    row = app.extensions["backend"].reflection_get(reflection_id=id)
    if row is None:
        return None
    sources = queries.reflection_provenance(
        app.extensions["db_connection"], reflection_id=id,
    )
    return SimpleNamespace(reflection=SimpleNamespace(**row), sources=sources)
```
   - `reflections_panel`: keep the existing arg parsing; replace the `queries.reflection_list_for_ui(conn, …)` call with `rows = app.extensions["backend"].reflection_list(project=project, tech=tech, phase=phase, polarity=polarity, status=status, min_confidence=min_confidence, useful_only=useful_only)`; render `panel_reflections.html` with `rows=rows` (unchanged).
   - `reflections_drawer`: replace the `queries.reflection_detail` block with
     ```python
     detail = _reflection_drawer_detail(app, id)
     if detail is None:
         abort(404)
     rating_evidence = queries.fetch_rating_evidence(app.extensions["db_connection"], "reflection", id)
     return render_template("fragments/reflection_drawer.html", detail=detail, rating_evidence=rating_evidence)
     ```
   - `reflection_promote` / `reflection_retire`: replace BOTH the pre-mutation existence check (`queries.reflection_detail(conn, ...) is None -> abort(404)`) and the post-mutation re-render with `_reflection_drawer_detail(app, id)`:
     ```python
     if _reflection_drawer_detail(app, id) is None:
         abort(404)
     try:
         app.extensions["backend"].promote_reflection(reflection_id=id)  # or retire_reflection
     except ValueError as exc:
         ... 409 card ...
     detail = _reflection_drawer_detail(app, id)
     rating_evidence = queries.fetch_rating_evidence(app.extensions["db_connection"], "reflection", id)
     return render_template("fragments/reflection_drawer.html", detail=detail, rating_evidence=rating_evidence), 200, {"HX-Trigger": "reflection-changed"}
     ```
   Leave `reflection_confirm` and `reflection_edit_*` on `queries.reflection_detail` (they are gated OUT in agentcore mode in PR 3; keeping them here keeps sqlite behaviour identical).

   > **Limit note:** `backend.reflection_list` defaults `limit=200` vs `reflection_list_for_ui`'s `limit=100`. The panel widens to 200; no test/behaviour pins the 100 cap, so this is an accepted, documented widening (not a regression).

4. **Run** `./.venv/Scripts/python.exe -m pytest tests/ui/test_reflections.py -v` — PASS (existing sqlite panel/drawer/promote/retire pins still green: `SqliteBackend.reflection_list`/`reflection_get` return the same data, `SimpleNamespace` composition renders identically, provenance comes from the same `reflection_provenance` query).

5. **Commit** `feat(ui): route reflections list + detail through StorageBackend`.

**Sqlite-preservation note:** the panel receives `asdict(ReflectionListRow)` dicts (Jinja item access renders identically to the attribute access it used before); the drawer receives `SimpleNamespace(reflection=SimpleNamespace(**asdict(ReflectionFull)), sources=<same reflection_provenance rows>)` — every template lookup resolves to the identical value. `reflection_detail` itself is retained for `confirm`/`edit`. No template files are touched.

**Deliverable:** the whole visible Reflections surface (list + drawer + promote/retire re-render) reads through the backend, with the local-content leak ruled out by the dead-content pin.

---

## Task C7 — 404 / error-card parity sweep for backend-routed content actions

**Files**
- Modify: `better_memory/ui/app.py` (except-clauses on `reflection_promote`, `reflection_retire`, `semantic_create`, `semantic_update`, `semantic_scope`)
- Test: `tests/ui/test_reflections.py`, `tests/ui/test_semantic.py`

**Rationale:** sqlite backends raise `ValueError` on a lifecycle/validation block; agentcore backends raise `RuntimeError` (failedRecords) for the same user-facing condition. The routes must render the SAME error card for both. Map `RuntimeError` alongside `ValueError` on the content-write routes.

### Steps

1. **Write the failing tests.**
   `tests/ui/test_reflections.py` (append; reuses `_ReflectionStubBackend` + `_seed_reflection`):
```python
def test_reflection_promote_runtimeerror_maps_to_409_card(client, tmp_db, monkeypatch):
    from better_memory.ui import app as app_module
    monkeypatch.setattr(app_module, "project_name", lambda: "testproj")
    class _Stub(_ReflectionStubBackend):
        def reflection_get(self, *, reflection_id):
            return dict(_row_dict(id=reflection_id), hints="[]",
                        created_at="2026-06-01T00:00:00+00:00",
                        last_useful_at=None, last_misled_at=None,
                        last_overlooked_at=None)
        def promote_reflection(self, *, reflection_id):
            raise RuntimeError("AgentCore promote_reflection failed: blocked")
    client.application.extensions["backend"] = _Stub([], None)
    resp = client.post("/reflections/r1/promote", headers={"Origin": "http://localhost"})
    assert resp.status_code == 409
    assert "card-error" in resp.get_data(as_text=True)


def test_reflection_promote_missing_id_404(client):
    class _Stub(_ReflectionStubBackend):
        def reflection_get(self, *, reflection_id):
            return None
    client.application.extensions["backend"] = _Stub([], None)
    resp = client.post("/reflections/gone/promote", headers={"Origin": "http://localhost"})
    assert resp.status_code == 404
```
   `tests/ui/test_semantic.py` (append; reuses `_SemanticStubBackend`):
```python
def test_semantic_create_runtimeerror_maps_to_400_card(client):
    class _Stub(_SemanticStubBackend):
        def semantic_observe(self, *, content, project=None, scope="project"):
            raise RuntimeError("AgentCore semantic_observe failed: bad")
    client.application.extensions["backend"] = _Stub([])
    resp = client.post("/semantic", data={"content": "x", "scope": "project"},
                       headers={"Origin": "http://localhost"})
    assert resp.status_code == 400
    assert "card-error" in resp.get_data(as_text=True)
```

2. **Run** — FAIL (the `RuntimeError` escapes as an unhandled 500).

3. **Widen the except-clauses** in `better_memory/ui/app.py` from `except ValueError` to `except (ValueError, RuntimeError)` on: `reflection_promote`, `reflection_retire`, `semantic_create`, `semantic_update`, `semantic_scope`. Card shape + status codes are unchanged (409 for reflections, 400 for semantic writes). The 404 path is already covered by the `reflection_get is None` / `semantic_get is None` checks from C5/C6. Leave `semantic_delete` (idempotent) and `reflection_confirm`/`edit` (unchanged, sqlite-only in PR 3) as they are.

4. **Run** `./.venv/Scripts/python.exe -m pytest tests/ui -v` — PASS (existing sqlite `ValueError -> 409/400` pins unchanged; only the `RuntimeError` branch is added).

5. **Commit** `fix(ui): map agentcore RuntimeError to the sqlite error-card contract for content writes`.

**Sqlite-preservation note:** sqlite still raises `ValueError`, still renders the same card at the same status; only the additional `RuntimeError` arm is new, and nothing in sqlite mode raises it.

**Deliverable:** uniform 404 + error-card behaviour across both backends for every backend-routed content write.

---

## Task C8 — Docs, protocol docstrings, pyright, full suite

**Files**
- Modify: `website/architecture.md`, `website/agentcore-setup.md`; confirm the new-method docstrings in `better_memory/storage/protocol.py`
- (No test file; this is the verification + docs task.)

### Steps

1. **Docs ([[keep-docs-in-sync]]).**
   - `website/architecture.md`: update the UI data-flow prose — the Reflections (list, detail, promote, retire) and Semantic (full CRUD) surfaces now reach memory CONTENT through `StorageBackend` (`app.extensions["backend"]`), not raw SQL in `queries.py` / a service on the raw conn. Operational state (hook errors, rating evidence/counters, retention runs, audit log) still reads the local `db_conn` in both modes. Grep synonyms to catch stale claims: `raw SQL`, `bypasses StorageBackend`, `SemanticMemoryService`, `reflection_detail`, `queries.py`.
   - `website/agentcore-setup.md`: note that in agentcore mode reflection + semantic content is now served from AgentCore via the backend; call out the `semantic_list` general-scope fan-out fix (default view now includes `general/semantic/` records). The full capability GATING table (which surfaces hide) lands in PR 3 — say "content routing only; surface hiding in the next PR".
   - State explicitly (in both the docs edit and the PR body): `README.md`, `website/mcp-tools.md`, `website/configuration.md` are UNAFFECTED — no new env var, no new MCP tool.
   - Confirm each new protocol method (`reflection_list`, `reflection_get`, `semantic_get`) carries a docstring stating the sqlite-delegate vs agentcore-fan-out split (added in C3/C4/C5).

2. **Pyright:** `./.venv/Scripts/python.exe -m pyright` -> 0 errors. (Watch the `list[dict]` return annotations and the `SimpleNamespace` composition in `app.py`.)

3. **Full suite:** `./.venv/Scripts/python.exe -m pytest tests -q`. Fix stragglers minimally; do not touch `tests/ui/*` assertions (preservation pins).

4. **Commit** `docs: agentcore UI content-routing data-flow + semantic general-scope fix`.

**Sqlite-preservation note:** docs + verification only; no runtime code changes.

**Deliverable:** docs match the new data flow, pyright clean, full suite green.

---

## Task C9 — PR, babysit, merge

### Steps

1. Push `feat/agentcore-ui-content`; `gh pr create`. Body: link the design spec; list the two rewired surfaces (Reflections list/detail/promote/retire, Semantic full CRUD); the three new read accessors (`reflection_list`, `reflection_get`, `semantic_get`) + the queries `reflection_row`/`reflection_provenance` split; the `semantic_list` general-scope bug fix; the sqlite-byte-identical evidence (every `SqliteBackend` content method is a verbatim delegate; `tests/ui/*` pins unedited); and an explicit "docs unaffected: README/mcp-tools/configuration (no env/MCP change)". Footer `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
2. Babysit to green + zero open threads -> squash-merge, `git checkout main`, `git pull`.
3. **Memory sweep (CLAUDE.md mandatory trigger):** before finishing the branch, record any non-obvious fix as a memory (`memory_observe`) — the `semantic_list` general-scope fan-out bug (failure), the storage->`ui.queries` local-import layering choice (decision), the agentcore hint-JSON-serialisation-for-drawer-parity gotcha, and the `RuntimeError`-> error-card mapping. One per non-obvious finding.
4. Deploy note: no env changes; agentcore live-smoke is manual (needs AWS creds) and lands with the PR 3 gating checklist — do not run it unprompted here.

**Sqlite-preservation note:** integration/merge only.

**Deliverable:** PR 2 merged to `main`; content routing complete; template gating + the `distinct_projects` dropdown remain for PR 3.
