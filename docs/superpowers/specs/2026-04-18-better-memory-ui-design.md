# better-memory Management UI — design

**Status:** draft · **Date:** 2026-04-18 · **Covers:** Plan 2, phases 1–10 (per `2026-04-06-better-memory-design.md` §12)

---

## 1. Scope, goals, non-goals

### Scope

A single Python web app, `better_memory.ui`, that covers all 10 Management UI phases from the parent spec §12. Ships as part of the `better-memory` package; spawned on demand via `memory.start_ui()` (Plan 2 Phase 10).

### Goals

1. A human surface for consolidation — approve, reject, edit, and merge insight candidates; promote insights to the knowledge base; retire dead wood.
2. A traceability surface — audit timeline plus source lineage from insight to observations to the originating session.
3. An exploration surface — a graph view of observations, insights, and knowledge documents for spotting consolidation backlogs.
4. A knowledge-base editor that respects "files are source of truth" — atomic writes, reindex on save.

### Non-goals

- Multi-user auth, sharing, or remote access. Loopback only.
- Mobile or responsive design. Desktop browser only.
- Offline-capable PWA. The UI runs only while the server process is up.
- Cloud APIs anywhere. Consolidation uses local Ollama, same as the retrieval path.
- Editing observations after creation. Observations are episodic facts from a moment in time. Candidates and insights are editable.

---

## 2. Technology and process model

### Stack

- **Flask** (+ Jinja2, bundled) for routing and templates.
- **HTMX** (vendored, ~14 KB) for server-pushed fragment updates.
- **Cytoscape.js** (vendored, ~500 KB) loaded only on the Graph route.
- New dependency in `pyproject.toml`: `flask`. Nothing else.

### Process model

- `memory.start_ui()` (MCP tool) spawns the UI via `subprocess.Popen(["python", "-m", "better_memory.ui"], env=os.environ.copy())`. Env vars (`MEMORY_DB`, `KNOWLEDGE_BASE`, `SPOOL_DIR`, `OLLAMA_HOST`, `EMBED_MODEL`, `CONSOLIDATE_MODEL`) propagate.
- Child binds `127.0.0.1:0` (random free port).
- **URL signaling.** Child writes `~/.better-memory/ui.url` atomically on bind, deletes it on clean exit. Parent polls that file for up to 5 s after spawn. Avoids Windows stdout-buffering quirks.
- **Log file.** Child redirects stdout and stderr to `~/.better-memory/ui.log` (rotated manually out-of-band; not in scope here).
- **Single-instance guard.** Before spawning, `memory.start_ui()` checks `~/.better-memory/ui.pid`. If the PID is alive and `GET /healthz` on its port returns `200`, return that URL instead of spawning a second. Stale entries are cleared.
- **Lifecycle.**
  - 30-minute inactivity timeout (reset on every non-`/healthz` request). On expiry, server calls `os._exit(0)`.
  - `POST /shutdown` endpoint hit by the "Close UI" button. Responds, then `os._exit(0)`.
- **Origin check.** Flask `before_request` rejects non-GET requests unless `Origin` or `Referer` matches the bound host:port. Browsers always send these; a cross-origin tab cannot forge them. Combined with `127.0.0.1`-only binding and random port, this is adequate for a single-user local tool.

### Data access

- UI instantiates `ObservationService`, `InsightService`, `KnowledgeService` with a single `sqlite3.Connection` opened at startup (`check_same_thread=False`). Flask runs with `threaded=False`, so exactly one request is in flight at a time and the shared connection is safe.
- All mutations go through the service layer. Audit-log entries are produced by the services.
- Read-only aggregate queries (kanban counts, audit pagination, graph-data shaping) live in `better_memory/ui/queries.py` — a small module of parameterised SQL helpers. They never mutate.
- Background jobs (Section 4 — consolidation) open their **own** `sqlite3.Connection`. They do not share the request connection.

---

## 3. Information architecture and routes

### Layout shell

`templates/base.html` renders:

- Header: app name · project name · five nav tabs (`Pipeline` · `Sweep` · `Knowledge` · `Audit` · `Graph`) · "Close UI" button.
- The `Pipeline` tab shows a candidate-count badge. The badge fragment polls `/pipeline/badge` every 10 s, so the count stays fresh on every page.
- `<main>` — per-view content; HTMX swaps fragments into named targets.

### Routes

Grouped by view. HTMX fragment routes live under their parent view's path.

| Path | Method | Purpose |
|---|---|---|
| `/` | GET | Redirect to `/pipeline` |
| `/healthz` | GET | Liveness probe — `200 ok` |
| `/shutdown` | POST | Stops the server |
| `/pipeline` | GET | Kanban: summary bar + drill-in panel (defaults to Candidates) |
| `/pipeline/panel/<stage>` | GET | HTMX fragment — drill-in panel for `observations` \| `candidates` \| `insights` \| `promoted` |
| `/pipeline/badge` | GET | HTMX fragment — candidate-count pill for the nav |
| `/pipeline/consolidate` | POST | Start a branch-and-sweep job, returns job fragment |
| `/candidates/<id>/card` | GET | HTMX fragment — expanded card (progressive disclosure) |
| `/candidates/<id>/approve` \| `/reject` \| `/merge` | POST | State transitions; return refreshed card fragment |
| `/candidates/<id>/edit` | GET | Inline edit form fragment |
| `/candidates/<id>/edit` | POST | Save edits, return refreshed card |
| `/insights/<id>/card` | GET | Expanded card fragment |
| `/insights/<id>/sources` | GET | Source-observation list fragment |
| `/insights/<id>/edit` | GET | Inline edit form fragment |
| `/insights/<id>/edit` | POST | Save edits, return refreshed card |
| `/insights/<id>/retire` \| `/demote` | POST | State transitions (retire → `retired`; demote → `confirmed`, used from Promoted stage) |
| `/insights/<id>/promote` | GET/POST | Promotion workflow modal |
| `/sweep` | GET | Sweep review queue |
| `/sweep/<id>/archive` \| `/retain` \| `/flag` | POST | Sweep actions |
| `/knowledge` | GET | KB tree view by scope |
| `/knowledge/edit?path=...` | GET | Editor form |
| `/knowledge/save` | POST | Atomic write + reindex |
| `/knowledge/reindex` | POST | Manual reindex for external edits |
| `/audit` | GET | Timeline page with filter form |
| `/audit/rows` | GET | HTMX fragment — paginated rows |
| `/graph` | GET | Graph view shell (Cytoscape) |
| `/graph/data` | GET | JSON — nodes + edges for current filter |
| `/jobs/<id>` | GET | HTMX fragment — consolidation job progress |

### Directory layout

```
better_memory/ui/
  __main__.py        # python -m better_memory.ui — bind, write ui.url, run Flask
  app.py             # Flask app factory, routes
  queries.py         # aggregate SQL helpers (read-only)
  jobs.py            # background-thread job registry
  static/
    htmx.min.js
    cytoscape.min.js
    app.css
  templates/
    base.html
    pipeline.html, sweep.html, knowledge.html, audit.html, graph.html
    fragments/
      candidate_card.html, insight_card.html, panel.html, job.html, ...
```

---

## 4. Pipeline Kanban (Plan 2 Phase 2)

Primary daily surface.

### Summary bar

Four pills in a row: Observations · Candidates · Insights · Promoted. Each shows a count above a stage name. Clicking a pill loads that stage's items into the panel below via HTMX swap. The active pill is highlighted. Candidates is amber-tinted to signal "needs attention". A "Run branch-and-sweep" button and "Close UI" button sit on the far right of the header.

### Panel

On `GET /pipeline`, the panel renders Candidates by default. Subsequent pill clicks fire `hx-get="/pipeline/panel/<stage>"` targeting `#panel`. Both summary bar (counts) and active panel (rows) poll every 10 s via `hx-trigger="every 10s"`.

### Card rendering

Compact row by default:

```
[polarity badge] [title]                                   [actions]
                 [1-line snippet, truncated]
                 [component · evidence · confidence]
```

Click the row to expand — hits `/candidates/<id>/card` (or `/insights/<id>/card`), swaps the row in place for the expanded version showing title, full content, sources link, and all four actions. Click again to collapse. Only one card expanded at a time; opening a second collapses the first.

### Per-stage actions

| Stage | Compact actions | Expanded actions | State transition |
|---|---|---|---|
| Observations | none | none | read-only; linked to from insight source lists |
| Candidates | Approve, Reject | + Edit, Merge | `insights.status`: `pending_review` → `confirmed` (Approve) or `retired` (Reject); Edit opens inline form; Merge picks a target from other Candidates |
| Insights | Promote, Retire | + Edit, View sources | Promote opens the promotion workflow (Section 5); Retire → `retired`; Edit inline |
| Promoted | View doc, Demote | + View sources, Retire | View doc opens the KB editor at the promoted file; Demote → `confirmed` (insight stays live, KB doc no longer tracks it); Retire → `retired` |

### Merge flow

Clicking Merge on a candidate swaps the action bar for an inline picker listing other pending candidates in the same project. Selecting one posts `/candidates/<id>/merge?target=<other_id>`. The ConsolidationService merge logic combines evidence, keeps the target, retires the source. Audit-log records both transitions. Merging into a retired or contradicted insight returns a validation error rendered back in the fragment.

### Consolidation button

`POST /pipeline/consolidate` registers a background job via `jobs.py` (an in-memory `dict[job_id, JobState]` plus a `threading.Thread` running `ConsolidationService.dry_run()`). The POST response is an HTMX fragment showing a progress bar with `hx-get="/jobs/<id>"` polling every 2 s. On completion, the fragment triggers a refresh of the Candidates panel (`hx-trigger="load from:(job-complete)"`). Only one consolidation job runs at a time; the button is disabled while one is active.

### Empty states

Every stage renders an explicit empty state. Examples: "No candidates pending. Run branch-and-sweep to produce new ones." · "No observations yet. Memories appear here once the MCP writes them."

---

## 5. Sweep Review, Knowledge Editor, Promotion Workflow

### Sweep Review Queue (Phase 5)

Dedicated top-nav tab. Rendered only after a consolidation run produces a sweep list; otherwise: "No sweep candidates. Run branch-and-sweep from the Pipeline."

Each sweep row shows: content snippet · age · `retrieved_count` · `used_count` · `reinforcement_score` · reason. Reason is one of `age`, `never_retrieved`, `superseded_by_insight:<id>`, `low_reinforcement`.

Row actions: **Archive** (`observations.status='archived'`), **Retain** (clears sweep flag, bumps reinforcement slightly), **Flag** (adds `flagged_for_investigation` marker, keeps in queue).

A bulk-action bar supports select-all within the current filter, then Archive/Retain the selection. Filters: project · component · reason. Ordering: oldest-first. No date picker in the first cut.

### Knowledge Base Editor (Phase 6)

Landing: tree of `~/knowledge-base/` bucketed by scope (`standards/`, `languages/`, `projects/`, `ad-hoc/`). Each node shows file name plus last-indexed timestamp.

Clicking a file opens the editor — a textarea with the raw markdown, plus a read-only preview pane rendered server-side with a minimal markdown parser (choice of `markdown` vs `mistune` deferred to implementation; isolated to one helper module so it can be swapped later).

**Save flow.**

1. UI posts path + content to `/knowledge/save`.
2. Server writes to `<path>.tmp`, `os.replace()` to final path (atomic on POSIX and Windows).
3. Server calls `KnowledgeService.reindex(path)`.
4. Return refreshed file card fragment.

A "Reindex all" button posts `/knowledge/reindex` for when external edits need to be picked up without waiting for the session-start mtime sweep.

No soft lock. If concurrent-edit protection matters later, add it then. Residual risk: the in-browser editor can overwrite an external edit silently. Git covers recovery.

### Promotion Workflow (Phase 7)

Triggered by an insight's "Promote" action. Opens a modal via `hx-get="/insights/<id>/promote"` into `#modal`.

1. **Choose destination.** Tree picker of `~/knowledge-base/` folders. System suggests one based on the insight's `component` and project.
2. **Draft.** Server generates a markdown draft (title + content + source observation IDs as a `<!-- Sources: ... -->` footer). Renders in an editable textarea.
3. **Save.** Creates a new markdown file at `<scope>/<slug>.md`, atomic-writes, calls `KnowledgeService.reindex(path)`, sets `insights.status='promoted'`, stores the destination path in a new column `insights.promoted_to`.

**Schema migration** `0002_insights_promoted_to.sql`:

```sql
ALTER TABLE insights ADD COLUMN promoted_to TEXT;
```

Kanban Promoted cards link back via `promoted_to`; "View doc" opens the KB editor at that path.

---

## 6. Audit Timeline, Graph View, `memory.start_ui()`

### Audit Timeline (Phase 8)

Reads the existing `audit_log` table (wired in Plan 1 Phase 10).

- **Filter form** at top: `entity_type`, `action`, `actor` (session_id), `project`, date range. Submits via HTMX; `#rows` target swaps in the new page.
- **Body:** paginated table. Columns: timestamp, entity type, entity id (linked), action, `from_status → to_status`, actor, details. 50 rows per page. "Load more" at the bottom fires `hx-get="/audit/rows?offset=..."` and appends.
- **Lineage drawer.** Clicking an entity id opens a side drawer with the entity's full lineage. For an insight, the chain is source observations → insight → (if promoted) knowledge doc. Rendered as a vertical list, not a graph.
- **Volume control.** `retrieved` events can flood the log (parent spec §8 calls them out as the first thing to drop). The timeline filters `retrieved` out by default; users opt in.

### Graph View (Phase 9)

- Route renders a minimal shell with a single `#cy` div. `/graph/data` returns `{nodes: [...], edges: [...]}`.
- **Node types.** observation (small, neutral) · insight (medium, colored by polarity: green = do, red = dont, grey = neutral) · knowledge doc (large, distinct shape).
- **Edge types.** `derived_from` (observation → insight) · `related` / `contradicts` / `supersedes` (insight → insight) · `promoted_to` (insight → doc).
- **Layout.** Cytoscape's `cose` force layout. Default. No dagre. No manual positions.
- **Filters** (top bar): project · component · date range · include-archived checkbox. Changes re-fetch `/graph/data` and re-run the layout.
- **Interactions.** Click node → side panel with that entity's details (reuses the card fragments). Shift-click to multi-select. Pan/zoom free from Cytoscape.
- **Performance cap.** Graph data is capped at 500 nodes. If the query matches more, show "N more hidden — narrow the filter" as an overlay. The cap is a simple `LIMIT 500` per node-type bucket in `/graph/data`.

### `memory.start_ui()` (Phase 10)

New MCP tool. No parameters.

Behavior:

1. Read `~/.better-memory/ui.pid`. If PID alive and `GET /healthz` on its port returns `200`, return the stored URL.
2. Else spawn `subprocess.Popen(["python", "-m", "better_memory.ui"], env=os.environ.copy(), stdout=logfile, stderr=subprocess.STDOUT)` where `logfile` is `~/.better-memory/ui.log` opened in append mode.
3. Poll `~/.better-memory/ui.url` for up to 5 s. When present, read the URL.
4. Write `<pid>\t<url>` to `~/.better-memory/ui.pid`.
5. Return `{url: "http://127.0.0.1:<port>"}`.

On clean shutdown the UI deletes `~/.better-memory/ui.url` and `~/.better-memory/ui.pid`.

---

## 7. Testing, build order, risks

### Testing strategy

- **Unit tests** for `ui/queries.py` (kanban counts, audit pagination, graph-data shaping) against an in-memory sqlite fixture with representative rows.
- **Integration tests** for every route using Flask's test client and `pytest`. Each test spins up the app with a temp DB, hits the route, asserts status plus HTML fragment shape (use `lxml` or `pyquery` — assert on structure, not string matching).
- **Service-layer tests already exist** — UI tests do not re-test them. They assert the UI calls the right service method with the right arguments.
- **No browser or E2E tests.** Cost/benefit isn't there for a single-user local tool. Visual QA is manual.
- **Consolidation job tests** use a fake ConsolidationService that returns fixture results. Real LLM calls are only exercised under `pytest -m integration` and are off by default in CI.

### Build order

Design sections map to Plan 2 phases as follows. Each phase is its own implementation plan.

| Plan 2 Phase | Design coverage |
|---|---|
| 1 — Web app skeleton | §2 (process model, stack) + §3 (layout shell, routes, `/healthz`, empty views) |
| 2 — Pipeline Kanban | §4 |
| 3 — ConsolidationService branch | Out of UI scope; the kanban's consolidation button becomes functional here |
| 4 — ConsolidationService sweep | Out of UI scope; enables the sweep queue's content |
| 5 — Sweep Review Queue | §5 sweep |
| 6 — Knowledge Base Editor | §5 editor |
| 7 — Promotion Workflow | §5 promotion + migration `0002_insights_promoted_to.sql` |
| 8 — Audit Timeline | §6 audit |
| 9 — Graph View | §6 graph |
| 10 — `memory.start_ui()` | §6 MCP tool |

### Risks and mitigations

- **Flask + sqlite threading.** `threaded=False` keeps exactly one request in flight; the shared request-scoped connection is safe. The consolidation background job opens its own connection, so it cannot race the request path.
- **LLM availability for consolidation.** Consolidation uses Ollama with a configurable chat model (`CONSOLIDATE_MODEL`, default `llama3`). If the model is missing, the dry-run fails with a clear error surfaced in the job fragment. Not a UI bug — surfaced by the UI.
- **Merge semantics.** Merging two unreviewed candidates: combine evidence, keep target, retire source. Merging into a confirmed insight: allowed, upgrades target's evidence count. Merging into a retired or contradicted insight: blocked with validation error.
- **KB concurrent external edits.** Atomic write prevents torn files; mtime-based reindex picks up external changes eventually. Residual risk: in-browser editor can overwrite an external edit silently. Accepted — git covers recovery.
- **Cytoscape bundle size.** ~500 KB minified. Loaded only on `/graph`. Served with `Cache-Control: public, max-age=86400`.
- **Windows subprocess quirks.** Child does not rely on stdout for URL signaling. It writes `~/.better-memory/ui.url` and deletes on exit. Parent polls that file for up to 5 s.

---
