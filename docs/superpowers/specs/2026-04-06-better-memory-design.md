# Better Memory — Design Specification

> AI memory system with four-layer epistemic hierarchy, local-first architecture.
> Replaces memU/mem0. Single spec, two implementation plans (MCP + UI).

---

## 1. Mental Model & Data Architecture

Four layers, two stores, one shared library.

| Layer | Written by | Store | Authority |
|---|---|---|---|
| **Observations** | AI (via MCP + skills) | SQLite + sqlite-vec | Low — single event |
| **Insights** | AI (consolidation) → human confirmed | SQLite + sqlite-vec | Medium — pattern |
| **Knowledge base** | Human (via UI + direct file editing) | Markdown files on disk | High — authoritative |

The knowledge base contains both documentation (descriptive — what the system is) and standards (prescriptive — how we work). The distinction is organizational (folder structure), not architectural.

### Knowledge base scopes

| Scope | Example | When loaded |
|---|---|---|
| **Global standards** | "Always write unit tests", "Be empirical, don't guess" | Session start — always |
| **Language standards** | Python conventions, C# conventions | Session start — detected from codebase |
| **Project knowledge** | App architecture, dependencies, service descriptions | Session start — from cwd |

### Promotion path

```
Observation → (frequency + validation metadata) → Insight candidate
Insight candidate → (human review in UI) → Confirmed insight
Confirmed insight → (human review in UI) → Documentation [if fact about system]
Confirmed insight → (human review in UI) → Standard [if rule about behavior]
```

The AI has write access only to observations and usage metadata. Everything upward requires a human gate.

---

## 2. Observation Store

### Schema

```sql
CREATE TABLE observations (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    project TEXT NOT NULL,
    component TEXT,
    theme TEXT,
    session_id TEXT,
    trigger_type TEXT,          -- what caused this to be written
    status TEXT DEFAULT 'active', -- active | consolidated | archived
    retrieved_count INTEGER DEFAULT 0,
    used_count INTEGER DEFAULT 0,
    validated_true INTEGER DEFAULT 0,
    validated_false INTEGER DEFAULT 0,
    last_retrieved TIMESTAMP,
    last_used TIMESTAMP,
    last_validated TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE VIRTUAL TABLE observation_fts USING fts5(
    content, component, theme,
    content='observations', content_rowid='rowid'
);

CREATE VIRTUAL TABLE observation_embeddings USING vec0(
    embedding FLOAT[768]        -- nomic-embed-text dimensions
);
```

### Write pattern

Append-only. The AI calls `memory.observe()` at decision points — design finalised, implementation complete, bug fixed, approach abandoned. The skill defines the trigger points and required format.

### Observation format (enforced by skill)

```markdown
## Observation: [Short title]
Component: [specific system area]
Trigger: [what caused this]

### What happened
[Past tense, specific]

### What worked / What didn't
[Outcome and why]

### Watch out for
[Gotchas, edge cases — mandatory field]

### Conditions
[Circumstances that produced this]
```

### Retrieval pattern

Filter first (project, component, status, recency window), then hybrid search (BM25 via FTS5 + vector via sqlite-vec), merged with Reciprocal Rank Fusion. Newer observations rank higher than older ones at equivalent similarity.

### Spool drain

Every `memory.retrieve()` call first drains the spool directory — reads pending hook event JSON files, writes them into the database as raw events, deletes the processed files. This ensures hook data is available before search runs.

---

## 3. Insight Store

### Schema

```sql
CREATE TABLE insights (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    project TEXT,
    component TEXT,
    status TEXT DEFAULT 'pending_review', -- pending_review | confirmed | contradicted | promoted | retired
    confidence TEXT DEFAULT 'low',        -- low | medium | high
    evidence_count INTEGER DEFAULT 0,
    last_validated TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE insight_sources (
    insight_id TEXT REFERENCES insights(id),
    observation_id TEXT REFERENCES observations(id),
    PRIMARY KEY (insight_id, observation_id)
);

CREATE TABLE insight_relations (
    from_insight_id TEXT REFERENCES insights(id),
    to_insight_id TEXT REFERENCES insights(id),
    relation_type TEXT,    -- related | contradicts | supersedes
    PRIMARY KEY (from_insight_id, to_insight_id)
);

CREATE VIRTUAL TABLE insight_fts USING fts5(
    title, content, component,
    content='insights', content_rowid='rowid'
);

CREATE VIRTUAL TABLE insight_embeddings USING vec0(
    embedding FLOAT[768]
);
```

### Key differences from observations

- **Mutable.** Confidence scores, status, evidence count, and content all update as evidence accumulates.
- **Relationship-aware.** `insight_sources` links back to the observations that produced it. `insight_relations` links insights to each other (related, contradicts, supersedes).
- **Not AI-writable directly.** The AI never calls a "create insight" tool. Insights are created by the consolidation process and confirmed by a human in the UI.

### Retrieval pattern

Semantic search weighted by confidence — confirmed insights rank above pending ones. When an insight matches, its related insights and source observations are available for traversal.

### Lifecycle

```
Observation cluster crosses threshold
  → ConsolidationService drafts insight candidate (LLM call)
  → status: pending_review
  → Human reviews in UI: approve → confirmed, reject → retired
  → Confirmed insight accumulates more evidence over time
  → High-confidence confirmed insight → human promotes to knowledge base
  → status: promoted (original stays as record, content lives in KB now)
```

---

## 4. Knowledge Base

### Structure

```
~/knowledge-base/
  standards/              ← global rules, always loaded
    golden-rules.md
    testing.md
    debugging.md
  languages/
    python/               ← loaded when working in Python
      conventions.md
      patterns.md
    csharp/               ← loaded when working in C#
      conventions.md
    typescript/           ← loaded when working in TypeScript
      conventions.md
  projects/
    auth-service/         ← loaded when cwd maps to this project
      architecture.md
      dependencies.md
      failure-modes.md
    payment-service/
      architecture.md
```

### Storage

Plain markdown files. The source of truth is the file on disk. The system indexes them for search but never modifies them autonomously.

### Indexing

FTS5 in a separate SQLite database (`knowledge.db` — not `memory.db`, maintaining the architectural boundary).

```sql
-- knowledge.db
CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,    -- relative to knowledge-base root
    scope TEXT NOT NULL,          -- 'standard' | 'language' | 'project'
    project TEXT,                 -- null for standards/languages
    language TEXT,                -- null for standards/projects
    content TEXT NOT NULL,
    last_indexed TIMESTAMP,
    file_mtime TIMESTAMP
);

CREATE VIRTUAL TABLE document_fts USING fts5(
    content, path,
    content='documents', content_rowid='rowid'
);
```

Rebuilt on demand or when the MCP server detects file changes (mtime check at session start).

### Session-start loading

1. **Always:** Load all files from `standards/`
2. **Language detection:** Scan cwd for language signals (file extensions, pyproject.toml, package.json, *.csproj, etc.). Load matching `languages/` folders.
3. **Project mapping:** Map cwd to a project name using the leaf directory name (e.g., cwd `/home/dev/source/auth-service` → project `auth-service`). Override via a `.better-memory` file in the project root if the folder name doesn't match the knowledge base folder name. Load matching `projects/` folder.
4. **On demand:** During a session, if the AI references another project, `knowledge.search()` with a `project` parameter pulls from that project's docs.

### Editing

Humans edit markdown files directly (any editor) or through the management UI. The files are the source of truth.

### No embeddings

Keyword search via FTS5 is sufficient. These are human-authored documents with controlled vocabulary. Adding semantic search later is a schema change, not an architectural one.

---

## 5. MCP Server

### Transport

stdio, registered globally in `~/.claude/settings.json`.

### Registration

```json
{
  "mcpServers": {
    "memory": {
      "command": "python",
      "args": ["-m", "better_memory.mcp"],
      "env": {
        "MEMORY_DB": "~/.better-memory/memory.db",
        "KNOWLEDGE_BASE": "~/knowledge-base",
        "SPOOL_DIR": "~/.better-memory/spool",
        "OLLAMA_HOST": "http://localhost:11434",
        "EMBED_MODEL": "nomic-embed-text"
      }
    }
  }
}
```

### Tools

| Tool | Purpose | Details |
|---|---|---|
| `memory.observe(content, component?, theme?, trigger_type?)` | Write an observation | Generates embedding, writes to DB. Project inferred from cwd. Returns observation ID. |
| `memory.retrieve(query?, component?, type?, window?)` | Search memories | Drains spool first. Type: `observation` \| `insight` \| `all` (default). Window: time range (default 30d for observations, none for insights). Returns ranked results with metadata. |
| `memory.record_use(id, validated?)` | Record usage/validation | Increments `used_count`, updates `last_used`. If `validated` is true/false, updates validation counters. Lightweight — single UPDATE. |
| `knowledge.search(query, project?)` | Search knowledge base docs | FTS5 keyword search. Project defaults to current. |
| `knowledge.list(project?)` | List available documents | Returns file paths and scopes. |
| `memory.start_ui()` | Spawn management UI | Starts the web UI as a separate process, returns the URL. |

### Session-start behavior

When the MCP server initializes:

1. Resolve project from cwd
2. Check knowledge base index freshness (mtime comparison), re-index if stale
3. Load `standards/` docs into context
4. Detect languages in cwd, load matching language docs
5. Load project-specific docs

### Spool drain on retrieve

Before executing any search, `memory.retrieve()`:

1. Reads all JSON files in `SPOOL_DIR`
2. Parses each as a hook event
3. Writes event records to a `hook_events` table
4. Deletes processed spool files
5. Proceeds with search

---

## 6. Hook System

Dumb event loggers. No LLM calls, no DB writes. Write JSON to the spool directory and exit.

### Claude Code hooks (global `~/.claude/settings.json`)

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write|Edit|Bash",
        "hooks": [{
          "type": "command",
          "command": "python -m better_memory.hooks.observer"
        }]
      }
    ],
    "Stop": [
      {
        "hooks": [{
          "type": "command",
          "command": "python -m better_memory.hooks.session_close"
        }]
      }
    ]
  }
}
```

### Observer hook

Receives tool call context via stdin. Writes a JSON file to the spool directory:

```json
{
  "event_type": "tool_use",
  "tool": "Edit",
  "file": "auth/connection.py",
  "content_snippet": "first 500 chars of output",
  "cwd": "/path/to/project",
  "session_id": "from env or generated",
  "timestamp": "2026-04-06T14:23:00Z"
}
```

File naming: `{timestamp}_{tool}_{hash}.json` — avoids collisions, naturally orders by time.

### Session-close hook

Fires on Claude Code's Stop event. Writes a session-end marker to the spool:

```json
{
  "event_type": "session_end",
  "cwd": "/path/to/project",
  "session_id": "...",
  "timestamp": "2026-04-06T15:30:00Z"
}
```

The consolidation service uses session-end markers to understand session boundaries when reasoning about observation clusters.

### Cursor hooks (`~/.cursor/hooks.json`)

```json
{
  "afterFileEdit": [{
    "command": "python -m better_memory.hooks.observer"
  }],
  "stop": [{
    "command": "python -m better_memory.hooks.session_close"
  }]
}
```

### What hooks don't do

- No LLM calls
- No database writes
- No network requests
- No blocking operations

They write a file and exit. Everything else happens when the MCP drains the spool.

---

## 7. Core Library Structure

Shared Python package consumed by both MCP and UI.

```
better_memory/
├── __init__.py
├── config.py              ← Settings, paths, env resolution
├── db/
│   ├── __init__.py
│   ├── connection.py      ← SQLite connection manager (WAL mode)
│   ├── schema.py          ← Table definitions, migrations
│   └── migrations/        ← Versioned schema changes
├── services/
│   ├── __init__.py
│   ├── observation.py     ← ObservationService: create, search, update metadata
│   ├── insight.py         ← InsightService: CRUD, relationship management
│   ├── knowledge.py       ← KnowledgeService: index, search, language detection
│   ├── consolidation.py   ← ConsolidationService: branch-and-sweep, contradiction checks
│   └── spool.py           ← SpoolService: read/drain spool directory
├── embeddings/
│   ├── __init__.py
│   └── ollama.py          ← Ollama client, embed text, batch embedding
├── search/
│   ├── __init__.py
│   ├── hybrid.py          ← BM25 + vector fusion (RRF)
│   └── fts.py             ← FTS5 keyword search for knowledge base
├── mcp/
│   ├── __init__.py
│   ├── __main__.py        ← Entry point (python -m better_memory.mcp)
│   └── server.py          ← MCP stdio server, tool definitions
├── ui/
│   ├── __init__.py
│   ├── __main__.py        ← Entry point (python -m better_memory.ui)
│   ├── app.py             ← Web app (Flask or FastAPI)
│   ├── static/
│   └── templates/
├── hooks/
│   ├── __init__.py
│   ├── observer.py        ← PostToolUse hook script
│   └── session_close.py   ← Stop hook script
└── skills/
    ├── memory-write.md
    ├── memory-retrieve.md
    ├── memory-feedback.md
    └── session-close.md
```

### Key design decisions

- **Services are stateless.** They receive a database connection and operate on it. No singletons, no global state.
- **One connection manager.** WAL mode. Connection pooling for the UI (concurrent requests), single-connection for MCP (sequential tool calls).
- **Embeddings are isolated.** `embeddings/ollama.py` is the only module that talks to Ollama. If the embedding model changes, only this module changes.
- **Search is its own layer.** Hybrid search (RRF merging BM25 + vector) is reusable. Services call into `search/` rather than implementing search themselves.
- **Skills ship with the package.** Markdown files distributed with `better_memory`. CLAUDE.md tells the AI where to find them.

---

## 8. Audit Trail

Every state transition is an immutable append. No audit records are ever updated.

```sql
CREATE TABLE audit_log (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,    -- observation | insight | document
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,         -- created | retrieved | used | validated |
                                 -- consolidated | promoted | archived |
                                 -- retired | contradicted | status_changed
    from_status TEXT,
    to_status TEXT,
    triggered_by TEXT,            -- skill | hook | consolidation | human
    actor TEXT,                   -- 'ai' | 'system' | developer identifier
    detail TEXT,                  -- JSON blob: LLM draft, validation note, etc.
    session_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_audit_entity ON audit_log(entity_type, entity_id);
CREATE INDEX idx_audit_action ON audit_log(action);
CREATE INDEX idx_audit_created ON audit_log(created_at);

CREATE TABLE hook_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,     -- tool_use | session_end
    tool TEXT,
    file TEXT,
    content_snippet TEXT,
    cwd TEXT,
    session_id TEXT,
    processed INTEGER DEFAULT 0,  -- 0 = pending, 1 = correlated by consolidation
    event_timestamp TIMESTAMP,
    drained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_hook_events_session ON hook_events(session_id);
CREATE INDEX idx_hook_events_processed ON hook_events(processed);
```

### Tuning point

The audit trail may generate high row volume, particularly from `retrieved` events (every search logs every result). If this becomes a performance or storage issue, the first lever is dropping `retrieved` events (lowest value, highest volume) and keeping only state changes and usage signals.

### Full lineage

From any knowledge base document, trace back: which insight was it promoted from → which observations produced that insight → which sessions generated those observations.

---

## 9. Consolidation Engine

Triggered by a human through the UI. Never runs automatically.

### Branch pass — observation clusters to insight candidates

1. Group active observations by `project` + `component` + `theme`
2. Check thresholds per group:
   - Minimum 3 observations in the cluster
   - Minimum 2 total `validated_true` across the cluster
3. Check if a matching confirmed insight already exists (avoid duplicates)
4. For new clusters: call Ollama to draft an insight from source observations
5. Create insight with `status: pending_review`, link sources via `insight_sources`
6. Mark source observations as `status: consolidated`

### Sweep pass — retire low-value observations

1. Find observations where:
   - `last_retrieved` older than 30 days (configurable)
   - `used_count = 0`
   - `validated_true = 0`
   - `status = 'active'`
2. Contradiction check before archiving:
   - Semantic similarity between candidate and confirmed insights
   - If high similarity but contradicting content (detected via Ollama call): flag for human review, do not archive
   - If no contradiction: archive (`status: archived`)
3. All sweep candidates presented in UI before execution — nothing archived without human approval

### Dry run

Consolidation always previews results. The UI shows what would be promoted, swept, and flagged. The human approves, rejects, or edits per item.

### Insight drafting prompt

```
Here are N observations about the same pattern:
[observations with dates and contexts]

Write a single insight that:
- Generalises the pattern in present tense
- States the conditions under which it holds
- Notes any exceptions observed
- Is specific enough to be actionable
- Is concise (2-4 sentences for the pattern, 1-2 for conditions/exceptions)
```

---

## 10. Management UI

Spawned on demand via `memory.start_ui()`. Separate process. Killed when done.

### Technology

Python web app (Flask or FastAPI), server-rendered with HTMX for interactivity. No SPA framework.

### Views (build order)

**View 1: Pipeline Kanban** — primary daily surface

```
[Observations: 247]  →  [Candidates: 12]  →  [Insights: 34]  →  [Promoted: 8]
```

- Click into any column to see items
- Candidate actions: Approve | Reject | Edit | Merge
- Insight actions: Edit | Promote to KB | Retire | View source observations
- Trigger consolidation: "Run branch-and-sweep" button → dry run → approve/reject per item

**View 2: Sweep Review Queue**

- Shows what sweep pass would archive
- Each candidate: content, age, usage stats, reason for sweep
- Actions: Approve archive | Retain | Flag for investigation

**View 3: Knowledge Base Editor**

- Browse `~/knowledge-base/` tree by scope
- Edit markdown in-browser (files remain source of truth)
- Re-index button for FTS5 refresh
- "Promote insight here" workflow: select insight → choose destination → system drafts markdown → human edits and saves

**View 4: Audit Timeline**

- Filterable log of state transitions
- Filter by: entity type, action, actor, date range, project
- Trace lineage: click insight → source observations → original sessions

**View 5: Graph View** (last to build)

- Force-directed graph: observation → insight → document nodes
- Derivation edges and relationship edges
- Dense clusters without insights = consolidation backlog
- Only meaningful once dataset is populated

---

## 11. Skills

Four skill files ship with the package.

### memory-retrieve.md — session start

1. Identify component(s) to work on
2. Call `memory.retrieve(component=..., type='all', window='30d')`
3. Call `knowledge.search()` for reference material
4. Read results as prior art
5. Do not re-explore documented dead ends without reason to believe conditions changed

### memory-write.md — decision points

Trigger points:
- Design decision finalised
- Implementation complete
- Bug identified and root-caused
- Test suite passing after fix
- Approach tried and abandoned
- Unexpected behaviour observed

Uses the observation template from Section 2. "Watch out for" field is mandatory.
Write immediately at the trigger point. Do not defer.

### memory-feedback.md — inline during work

- Memory influences decision → `memory.record_use(id)` immediately
- Work confirms memory → `memory.record_use(id, validated=true)`
- Work contradicts memory → `memory.record_use(id, validated=false)`
- Counter increments, not prose. Two seconds, move on.

### session-close.md — session end

1. Were all decision-point observations written? If missed, write now.
2. Did any retrieved memories get used without `record_use`? Update now.
3. Safety net. If point-of-use discipline was good, this adds little.

### CLAUDE.md entry

```markdown
## Memory System

This project uses better-memory for persistent AI knowledge.

### Skills
- Starting work → read skills/memory-retrieve.md
- Decision point reached → read skills/memory-write.md
- Using a retrieved memory → read skills/memory-feedback.md
- Session ending → read skills/session-close.md
```

---

## 12. Build Order

One spec, two implementation plans. Each phase delivers value independently.

### Implementation Plan 1: MCP Server + Core

| Phase | What | Delivers |
|---|---|---|
| 1 | SQLite schema, connection manager, migrations, config | Foundation |
| 2 | Ollama embedding client, batch embedding | Embedding pipeline |
| 3 | ObservationService + write path | AI can write observations |
| 4 | Hybrid search (FTS5 + sqlite-vec + RRF) | AI can retrieve observations |
| 5 | KnowledgeService — file indexing, FTS5, language detection | AI gets standards and project docs |
| 6 | InsightService — CRUD, relationships | Insights queryable |
| 7 | SpoolService + hook scripts | Hook events captured |
| 8 | MCP server — stdio, all 6 tools | Full MCP operational |
| 9 | Skills — all 4 markdown files | AI knows how to use the system |
| 10 | Audit trail | State transitions logged |

### Implementation Plan 2: Management UI

| Phase | What | Delivers |
|---|---|---|
| 1 | Web app skeleton, HTMX setup | Server runs |
| 2 | Pipeline kanban | Human can see system contents |
| 3 | ConsolidationService — branch pass | Consolidation + candidate review |
| 4 | ConsolidationService — sweep pass, contradiction detection | Archival review |
| 5 | Sweep review queue view | Dedicated sweep surface |
| 6 | Knowledge base editor | In-browser doc management |
| 7 | Promotion workflow | Full promotion pipeline |
| 8 | Audit timeline view | Lineage tracing |
| 9 | Graph view | Visual exploration |
| 10 | `memory.start_ui()` MCP tool | AI can spawn UI |

Plan 1 is priority. Plan 2 can start once Plan 1 phases 1-6 are complete.

---

## 13. Key Decisions & Rationale

| Decision | Rationale |
|---|---|
| **Local-first (Ollama + SQLite + sqlite-vec)** | No cloud API in retrieval path. Embedding model consistency is a hard constraint. |
| **Single SQLite file for observations + insights** | Simplifies operations, consolidation queries can join directly. Conceptual separation lives in schema and MCP tools. |
| **Separate knowledge.db for knowledge base** | Different store, different search strategy (FTS5 only), different write permissions. Architectural boundary matches conceptual boundary. |
| **Knowledge base as markdown files** | Source of truth is the file. Index is derived. Humans can edit with any tool. |
| **Spool directory for hooks** | Hooks are decoupled from MCP and DB. Fire-and-forget. Drained on retrieve — data arrives when it matters. |
| **No LLM in hooks** | Latency cost on every tool call not justified. AI reports usage inline via skill. Hook events are backup signal for consolidation. |
| **Human gate on all upward promotions** | Automated promotion without review launders wrong memories into authoritative documentation. |
| **Consolidation triggered by human, not automatic** | Trust must be established first. Automate later if thresholds prove reliable. |
| **Global MCP registration** | One config, works in every project. Project identity inferred from cwd. |
| **Python** | Existing ecosystem (uv, pytest), mature Ollama/SQLite libraries, sqlite-vec has pip package. |
| **Audit trail with tuning point** | Log everything initially. Drop `retrieved` events first if volume becomes a problem. |
| **Replaces memU/mem0** | Clean break. New architecture informed by prior work but not constrained by it. |
