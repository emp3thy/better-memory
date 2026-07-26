# AgentCore-mode Management UI — Design

**Date:** 2026-07-25
**Status:** approved design (synthesis), pending implementation plan.
**Role:** the single authoritative design for running the better-memory management UI when `storage_backend = agentcore`. It synthesizes the investigation + specs + gap docs produced this cycle and is the parent of the three area sub-designs. Execution order lives in the master plan.

**Companion documents:**
- Data + mapping: `docs/agentcore-ui/data-sources.md`, `docs/agentcore-ui/agentcore-mapping.md`
- Operation specs: `docs/agentcore-ui/read-spec.md`, `docs/agentcore-ui/write-spec.md`
- Residual gaps/decisions: `docs/agentcore-ui/gaps/*.md`
- Area sub-designs (detail): `2026-07-25-agentcore-ui-{backend-wiring,capability-gating,content-wiring}-design.md`
- Execution sequence: `docs/superpowers/plans/2026-07-25-agentcore-ui-MASTER-plan.md`

## GUARDRAILS (planning + implementation memory; verified)

- **[[keep-docs-in-sync]]** (conf 0.95, ev 7) — every code phase updates `website/architecture.md` + `website/agentcore-setup.md` + protocol docstrings; `website/configuration.md`, `website/mcp-tools.md`, `README.md` tool/env tables are unaffected (no new env var, no MCP tool) — state so per PR.
- **[[surface-planning-memory]]** (conf 0.9) — satisfied.
- **[[server-boot-real-call]]** (conf 0.65) — the agentcore `create_app` test drives a REAL route through the stubbed backend and asserts no leaked local-content read; not "constructs without throwing".
- **[[guard-needs-triggering-test]]** (conf 0.8) — every named edge-guard in this design (namespace-parse rules, best-effort AWS degradation, scope_filter fan-out) MUST have a test seeding a value that actually triggers it; a described guard without a triggering test is a silent-regression hazard.
- **[[brutalist-css-classes]]** (conf 0.75) — gated template blocks wrap existing markup; no Bootstrap utility classes.
- **[[playwright-domtext]]** (conf 0.8) — nav-gating tests assert element presence/absence, not CSS-rendered text.

## 1. Problem

All 47 UI operations bypass the `StorageBackend` abstraction: reads run raw SQL in `better_memory/ui/queries.py` against a `sqlite3.Connection`; writes go through services (`SemanticMemoryService`, `ReflectionService`) bound to that same raw connection. `create_app` opens the local `memory.db` directly and never touches `storage/factory.py`.

In agentcore mode memory CONTENT (observations, reflections, semantic memories) lives in AWS AgentCore; the local `memory.db` holds only session-operational state (exposure ledger, hook errors, retention runs, migration ledger, audit log). So today the UI in agentcore mode would show EMPTY reflections/semantic and misroute content writes to the wrong store.

**Goal:** the UI serves correct data and writes in both modes, with sqlite-mode behaviour byte-identical, and hides the surfaces AgentCore cannot back.

## 2. Architecture

`create_app` builds a `StorageBackend` via `factory.build_backend(config=get_config(), memory_conn=db_conn, sync_embedder=resolved_sync_embedder, session_id=None, project=project_name())`, stored at `app.extensions["backend"]`. The raw `db_conn` stays at `app.extensions["db_connection"]` for operational-state reads/writes. A `@app.context_processor` exposes `caps` (the capability flags read off the backend) to every template.

Routing rule:
- **CONTENT** (reflections, semantic) → through backend methods.
- **OPERATIONAL STATE** (hook errors, `session_memory_exposure` / rating evidence + counters, retention runs, audit log) → stays on the local `db_conn` in BOTH modes (this is the settled content-vs-operational split from the agentcore-parity design).

`SqliteBackend`'s content methods are verbatim delegates to the same `queries.py` / services the UI calls today, so sqlite output is unchanged.

## 3. Capability model

Six `@property -> bool` on `StorageBackend`. All `True` on `SqliteBackend` (no gate ever fires in sqlite mode). AgentCore as shown.

| Flag | AgentCore | Gates |
|---|---|---|
| `supports_episodes` *(exists)* | False | Episodes tab/nav + routes |
| `supports_observations` | False | Observations tab/nav + routes; promote-obs→semantic |
| `supports_provenance` | False | reflection source-obs/episode block; observation linked-reflections; the provenance fetch |
| `supports_retention_runs` | False | Diagnostics retention-runs panel + route |
| `supports_reflection_review` | False | Confirm action + pending_review/confirmed status vocabulary |
| `supports_reflection_text_edit` | False | inline reflection Edit button + `/edit` routes |

`supports_reflection_mutation` (proposed by one sub-plan) is deliberately NOT introduced — promote/retire are supported on agentcore and must not be gated.

## 4. AgentCore-mode UI shape

- **Visible:** Reflections (list, detail *without provenance*, promote, retire) · Semantic (full CRUD: create/update-text/set-scope/delete/list/detail) · Diagnostics (hook errors + rating counters).
- **Hidden:** Observations tab · Episodes tab · provenance sections · retention-runs panel · Confirm action · inline reflection text-edit.

## 5. Data model + new backend surface

AgentCore layout (verified live 2026-07-25, `resolve_namespace`/`resolve_actor_id`, `actorId == project`):
```
projects/{project}/reflections/   reflection records (episodic memory)
projects/{project}/semantic/      semantic records (semantic memory)
projects/{project}/retired/       retired records
general/{reflections,semantic}/   cross-project bucket
```
Records are exact-leaf addressed — a parent namespace does NOT roll up (`projects/{p}/` → 0 records). Records have **no TTL** (durable); only EVENTS expire (`eventExpiryDuration`, min 3 / max 365; episodic set to 365 this cycle). Migrated reflection counters live in the JSON content body; AWS-extracted ones in declared metadata.

New backend methods (canonical names):
- `reflection_list(*, project, tech, phase, polarity, status, min_confidence, useful_only, limit) -> list[dict]` — sqlite delegates to `queries.reflection_list_for_ui`; agentcore fans out `list_memory_records` over project + general (+ retired when the status set admits it), parses via `_parse_reflection_record`, dedups (project wins), filters client-side, orders by `wilson_lower_bound` desc / confidence desc / updated_at desc. Status vocabulary maps: agentcore has only `active`(≡ sqlite `confirmed`)/`promoted`/`retired`, no `pending_review`.
- `reflection_get(*, reflection_id) -> dict | None` — row only, no provenance. sqlite delegates to an extracted `queries.reflection_row`; agentcore = `get_memory_record` + parse. `queries.reflection_detail` is refactored into `reflection_row` + `reflection_provenance` so its existing output is unchanged.
- `semantic_get(*, id) -> SemanticMemory | None` — sqlite = extracted single-row read; agentcore = `_get_semantic_record` + `_semantic_summary_to_model`.
- `distinct_projects() -> list[str]` — sqlite = `SELECT DISTINCT project`; agentcore = `ListActors(memoryId)` UNION the project set parsed from the local `agentcore_migration.namespace` column (`projects/{p}/...` → `p`, `general/...` → `general`). Best-effort: AWS error degrades to ledger-only, both empty → `[]`.

**Enumeration semantics (documented, not a bug):** `ListActors` returns EVENT-derived actors (projects with ≥1 `observe()`), not record-namespaces. It self-populates the dropdown on first `observe()` in a project; migrated-dormant projects are covered by the ledger union. `list_events` needs actorId+sessionId; `ListSessions(actorId)` works — full observation reconstruction is possible but out of scope (Observations tab hidden).

## 6. Pre-existing bug fixed inline

`AgentCoreBackend.semantic_list(scope_filter=None)` currently queries only `projects/{actor}/semantic/`, dropping general-scope records that the sqlite default view (`(project OR scope='general')`) includes — the UI default passes `scope_filter=None`. Live impact: default view shows 0 vs sqlite's 2. Fix: when `scope_filter is None`, fan out over BOTH `projects/{actor}/semantic/` and `general/semantic/` and merge/dedup. Regression test required ([[guard-needs-triggering-test]]).

## 7. Error handling

All AWS reads best-effort — reuse `_retry_on_transient_404` and the reserved-metadata-strip helper; a list failure renders an empty panel, never a 500; ranking degrades to Wilson-only, the dropdown to ledger-only. Content writes surface the same `ValueError`→400/409 error-card contract as sqlite (agentcore `RuntimeError` mapped to the same card shape). Operational-state reads are unaffected by backend selection.

## 8. Residual gaps (documented, NOT built)

Four decision docs in `docs/agentcore-ui/gaps/`:
- `edit-aws-extracted-reflection-text.md` — **decided: disable inline reflection text-edit in agentcore mode** (reflections AI-managed; AWS-extracted edits would fight extraction).
- `observation-history-absent.md` — Observations tab hidden; reconstruction path noted but not worth building.
- `distinct-project-dropdown-enumeration.md` — source = `ListActors` ∪ ledger; recent-vs-all semantics + 365-day decay caveat.
- `retention-superseded-by-event-expiry.md` — retention is sqlite-only; agentcore pruning = event expiry.

**Non-goals:** observation browsing/history, episode parity, retention content-mutation engine, ratings-event read path, embedding writes from agentcore.

## 9. Testing strategy

- **Sqlite parity** — pinned by the existing `tests/ui/*` suites: all six flags `True` so no gate fires, and every backend content method delegates verbatim to the query/service the UI calls today. Assertions in those suites are not edited.
- **Agentcore paths** — stubbed-boto unit tests (`tests/storage/test_agentcore_unit.py` pattern): counter read-back (body + metadata), flat Wilson order parity, `semantic_list` two-namespace fan-out (bug regression), `distinct_projects` union + degradation, namespace-parse edge cases.
- **Wiring proof** — `create_app` agentcore boot test drives a real route through a stubbed backend ([[server-boot-real-call]]); the dead-content-table trick (seed a sentinel row in local `reflections`/`semantic_memories` and assert it never renders) proves reads hit the backend, not local sqlite.
- **Gating** — element presence/absence assertions ([[playwright-domtext]]); every named guard gets a triggering test ([[guard-needs-triggering-test]]).

## 10. Scope boundary for the implementation plan

This design is executed as three sequential PRs per the master plan: **Foundation** (flags + backend-in-create_app + caps) → **Content routing** (semantic + reflection through the backend, incl. the bug fix and the new read accessors) → **Gating + dropdown** (hide non-agentcore surfaces, replace the dropdown). sqlite-mode is byte-identical throughout.
