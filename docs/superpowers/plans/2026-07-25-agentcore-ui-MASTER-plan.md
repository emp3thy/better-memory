# AgentCore-mode UI — MASTER Reconciled Plan

**Date:** 2026-07-25
**Status:** reconciliation of three parallel-authored sub-plans into one sequenced execution plan.
**Supersedes (for sequencing + naming):** the three sub-plans below. Their per-task TDD steps remain valid *design detail* — cite them where noted — but the canonical flag/method names and the task ORDER come from THIS doc, not from them.

Sub-plans (design references):
- `2026-07-25-agentcore-ui-backend-wiring.md` (+ its `-design.md`)
- `2026-07-25-agentcore-ui-capability-gating.md` (+ its `-design.md`)
- `2026-07-25-agentcore-ui-content-wiring.md` (+ its `-design.md`)

## Why this doc exists

The three sub-plans were authored in parallel, blind to each other. Each independently re-derived the same foundation (build a `StorageBackend` in `create_app` + a `caps` context processor) and each defined its OWN capability-flag vocabulary. Executed as-is they would collide (three conflicting flag sets, three `create_app` rewrites, semantic/reflection routing specified 2–3×). This master plan picks the canonical names, builds each thing ONCE, and orders the work so dependencies land first.

## GUARDRAILS (carried from the sub-plans; verified against planning memory)

- **[[keep-docs-in-sync]]** (conf 0.95, ev 7, useful 19) — every code phase has a docs task: `website/architecture.md` (UI now reaches content through `StorageBackend`), `website/agentcore-setup.md` (capability table: which UI surfaces hide in agentcore), protocol docstrings. `website/configuration.md`, `website/mcp-tools.md`, `README.md` tool/env tables are **unaffected** (no new env var, no MCP tool) — state that explicitly per PR.
- **[[surface-planning-memory]]** (conf 0.9) — satisfied.
- **[[server-boot-real-call]]** (conf 0.65) — the agentcore-mode `create_app` test must drive a REAL route through the stubbed backend (assert the stub is hit + no leaked local-content read), not "constructs without throwing".
- **[[brutalist-css-classes]]** (conf 0.75) — gated template blocks WRAP existing markup; introduce no Bootstrap utility classes.
- **[[playwright-domtext]]** (conf 0.8) — nav-gating tests assert element presence/absence, not CSS-rendered text.

## CANONICAL capability flags (single source of truth)

Six `@property -> bool` on `StorageBackend`. All `True` on `SqliteBackend` (so sqlite mode never gates). AgentCore values as shown.

| Flag | AgentCore | Gates (single UI surface) |
|---|---|---|
| `supports_episodes` *(exists today)* | `False` | Episodes tab/nav + routes |
| `supports_observations` | `False` | Observations tab/nav + routes; promote-obs→semantic |
| `supports_provenance` | `False` | reflection source-obs/episode block; observation linked-reflections block; the provenance fetch |
| `supports_retention_runs` | `False` | Diagnostics retention-runs panel + route (pruning = event expiry) |
| `supports_reflection_review` | `False` | Confirm action + the pending_review/confirmed status vocabulary in the filter `<select>` |
| `supports_reflection_text_edit` | `False` | inline reflection Edit button + `/edit` routes (OPEN DECISION — default OFF, see below) |

**Dropped:** `supports_reflection_mutation` (backend-wiring) — promote/retire ARE supported on agentcore (EASY); they must NOT be gated.

**Open decision (surfaced, default chosen):** `supports_reflection_text_edit=False` on agentcore (reflections are AI-managed; migrated-body edit is technically possible but AWS-extracted editability fights extraction). Overturnable — see `docs/agentcore-ui/gaps/edit-aws-extracted-reflection-text.md`.

## CANONICAL backend methods (single naming)

Existing (reused as-is): `retrieve`, `semantic_observe`, `semantic_list`, `semantic_update_text`, `semantic_set_scope`, `semantic_delete`, `promote_reflection`, `retire_reflection`.

New (canonical names — from content-wiring, NOT gating's `*_for_ui`):
- `reflection_list(*, project, tech, phase, polarity, status, min_confidence, useful_only, limit) -> list[dict]`
- `reflection_get(*, reflection_id) -> dict | None` (row only, no provenance)
- `semantic_get(*, id) -> SemanticMemory | None`
- `distinct_projects() -> list[str]` (sqlite: SELECT DISTINCT; agentcore: `ListActors` ∪ ledger-namespace-parse)

sqlite impls are verbatim wrappers over today's `queries.py` / services (byte-identical). `queries.reflection_detail` is split into `reflection_row` + `reflection_provenance` helpers so `reflection_get` reuses the row half and the provenance half stays behind the flag.

## Confirmed pre-existing bug to fix IN this work (not deferred)

`AgentCoreBackend.semantic_list(scope_filter=None)` queries only `projects/{actor}/semantic/`, dropping general-scope records the sqlite default view includes (`(project OR scope='general')`). UI default passes `scope_filter=None`. Live impact: general/semantic has 2 records, project namespaces 0 → agentcore default view shows 0 vs sqlite's 2. **Fix in Task B1:** when `scope_filter is None`, fan out over BOTH `projects/{actor}/semantic/` and `general/semantic/` and merge/dedup. Add a regression test. (Recorded: memory `agentcore semantic_list drops general-scope`.)

---

## Execution order — 3 sequential PRs (foundation first)

Use superpowers:subagent-driven-development per task (implementer + task reviewer; both verdicts pass before complete). Branch per PR at task start.

### PR 1 — Foundation  (branch `feat/agentcore-ui-foundation`)
Everything else depends on this; land + merge before PR 2.

- **A1 — Canonical capability flags** on `protocol.py` + `sqlite.py` (True×5) + `agentcore.py` (False×5). *(Detail: capability-gating T1 / content-wiring T1 — but use the CANONICAL 6-flag table above, not either plan's subset.)*
- **A2 — `create_app` builds the backend + `caps` context processor.** `build_backend(config=get_config(), memory_conn=db_conn, sync_embedder=resolved_sync_embedder, session_id=None, project=project_name())` → `app.extensions["backend"]`; keep `app.extensions["db_connection"]` for operational state; `@app.context_processor` exposes `caps` with all six flags. Agentcore real-request boot test ([[server-boot-real-call]]). *(Detail: backend-wiring T1 + T5 context-processor half, merged; single build only.)*
- **A3 — Docs + pyright + full suite + PR.** architecture.md prose (UI builds a backend); note config/mcp-tools/README unaffected.

### PR 2 — Content routing  (branch `feat/agentcore-ui-content`) — depends on PR 1
New read accessors + route every VISIBLE content surface through the backend. No template gating yet (flags all render in sqlite; agentcore content still reachable via the backend).

- **B1 — Semantic list + CRUD through `backend.semantic_*`**, INCLUDING the `semantic_list(scope_filter=None)` two-namespace bug fix + regression test. *(Detail: backend-wiring T2/T3, content-wiring T5.)*
- **B2 — Reflection promote/retire through the backend.** *(Detail: backend-wiring T4.)*
- **B3 — `reflection_get`** + split `queries.reflection_detail` into `reflection_row`/`reflection_provenance` (composition pin keeps `reflection_detail` output identical). *(Detail: content-wiring T2.)*
- **B4 — `reflection_list`** flat, Wilson-ordered on agentcore; sqlite delegates to `queries.reflection_list_for_ui`. *(Detail: content-wiring T3.)*
- **B5 — `semantic_get`** single-record accessor. *(Detail: content-wiring T4.)*
- **B6 — Route Reflections list + detail through the backend** (drawer uses `reflection_get`; provenance fetch still via `queries.reflection_provenance` on `db_conn`, to be flag-gated in PR 3). *(Detail: content-wiring T6 routes half + capability-gating T7.)*
- **B7 — 404 / error-card parity sweep** for backend-routed reflection actions. *(Detail: content-wiring T7.)*
- **B8 — Docs + pyright + full suite + PR.**

### PR 3 — Gating + dropdown  (branch `feat/agentcore-ui-gating`) — depends on PR 2
Now hide the surfaces agentcore can't back, and replace the dropdown.

- **C1 — Gate nav + Observations/Episodes** links and routes (404 in agentcore). *(Detail: capability-gating T3.)*
- **C2 — Gate provenance** sections + the provenance fetch (row-only path from B3/B6). *(Detail: capability-gating T4 + content-wiring T6 template half.)*
- **C3 — Gate retention-runs panel + Confirm + inline text-edit**; keep hook-errors + rating counters ungated (operational/local). *(Detail: capability-gating T5.)*
- **C4 — `distinct_projects` backend method + dropdown replacement** (`ListActors` ∪ `agentcore_migration.namespace`-parsed projects; best-effort degrade to ledger-only / `[]`). *(Detail: capability-gating T8.)*
- **C5 — Docs (agentcore-setup capability table) + pyright + full suite + PR** with a manual live-smoke checklist (agentcore session: Episodes+Observations hidden, Reflections/Semantic show real records, dropdown lists real projects, retention/Confirm/edit absent).

---

## Dependency graph (must-precede)
```
A1 ─▶ A2 ─▶ (all of B) ─▶ (all of C)
B3 ─▶ B6 ─▶ C2            (provenance row-only path before the provenance gate)
B1 carries the semantic_list bug fix (independent within B)
C4 needs the migration ledger present; degrades gracefully if absent
```

## What each sub-plan contributes / where it was wrong
- **backend-wiring:** correct foundation (A2) + semantic/promote-retire routing (B1/B2). Its flag set was incomplete and included the wrong `supports_reflection_mutation` — superseded by the canonical table.
- **capability-gating:** the most complete gating coverage (C1–C4) + dropdown (C4). Its `*_for_ui` method names and `supports_reflection_confirm`/`_retention_runs` naming are superseded by the canonical names.
- **content-wiring:** the read-accessor design (B3–B6) + canonical method names + the OD-1 text-edit default. Its Task 0 dependency-spike is folded into "PR 1 lands first."

## Non-goals (unchanged)
Observation browsing/history, episode parity, retention content-mutation engine, ratings-event read path, embedding writes from agentcore. See `docs/agentcore-ui/gaps/` for the four documented residual decisions.
