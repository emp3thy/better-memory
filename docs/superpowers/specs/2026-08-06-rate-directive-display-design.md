# RATE_MEMORIES directive: display snapshots + terse format — design

Date: 2026-08-06. Status: approved (user picked terse variant "minimal + 1 rule
line" and backfill option A).

## Problem

The Stop-hook RATE_MEMORIES directive renders each pending memory as an opaque
id with nothing after the colon:

```
- mem-f3ce58e6-0c5a-4a37-a87a-2c35272a8d82 [contextual]:
```

and the directive body restates ~600 chars of rating rules that already live in
the `rate-session-memories` skill. The user cannot tell what is being rated,
and the block is far larger than it needs to be.

## Root cause (verified against live DB)

`exposure_log.list_unrated` (better_memory/services/exposure_log.py) already
joins display text:

```sql
COALESCE(r.title, s.content) AS display
LEFT JOIN reflections       r ON e.memory_kind='reflection' AND e.memory_id = r.id
LEFT JOIN semantic_memories s ON e.memory_kind='semantic'   AND e.memory_id = s.id
```

That join works for the sqlite backend, whose ids are bare 32-hex. The
agentcore backend records exposures under AWS-minted public ids
(`mem-<dashed-uuid>`, server-minted per aws_record_dialect) which do not exist
in the local `reflections` / `semantic_memories` tables. The LEFT JOIN misses,
`display` is NULL, and both the Stop-hook directive and
`memory.list_session_exposures` render blank titles.

The hook's inline copy of this query (better_memory/hooks/session_close.py,
`_emit_rating_directive_if_unrated`) has the same behaviour by construction —
it is a deliberate standalone copy to avoid a service-layer import.

## Design

### Change 1 — snapshot display text at exposure-record time

Backend-agnostic fix: capture the display string when the exposure is written,
because every write site holds the full record at that moment. No network in
the Stop hook — it must stay synchronous and local
(docs/decisions/stop-hook-must-be-sync.md).

- **Migration**: `ALTER TABLE session_memory_exposure ADD COLUMN display TEXT`
  (nullable), registered via the repo's `schema_migrations` pre-claim
  convention (see #122).
- **`exposure_log.record`**: items change from `(kind, id)` tuples to
  `(kind, id, display)` triples. `display` is truncated to 120 chars at write
  time and may be `None`. First-source-wins dedup semantics unchanged.
- **Write sites** (all already hold the record content):
  - `storage/agentcore.py` — `_record_retrieve_exposures` (buckets contain
    parsed records with titles) and the bootstrap/`record_exposures` path.
  - sqlite path — `services/reflection.py` retrieve +
    `services/session_bootstrap.py`.
  - `storage/protocol.py` `record_exposures` takes the same
    `(kind, id, display)` triples as `exposure_log.record` (one shape
    everywhere, no separate display param); `hooks/contextual_inject.py`
    passes it from the `retrieve_relevant` items it already renders.
- **Read path**: `list_unrated` and the hook's inline copy select
  `COALESCE(e.display, r.title, s.content)`. Local-backend rows written before
  the migration keep resolving via the join fallback.
- `memory.list_session_exposures` picks up the fix automatically (it delegates
  to the same query shape), so the rating skill's authoritative list also
  shows titles.

### Change 2 — terse directive

Approved format ("minimal + 1 rule line"):

```
RATE_MEMORIES: 15 unrated. Invoke skill `rate-session-memories`.
Evidence line first; none possible = `ignored`.
Reflections (13):
- mem-f3ce58e6... [contextual] Keep website and README in sync with every code change
- mem-1b742a09... [retrieve] Session startup MUST call knowledge_list + memory_retrieve
Semantic (2):
- mem-5f0f9a12... [contextual] Visualiser: mermaid + full HTML doc
```

- Dropped from the directive: the "before this session ends…" preamble, the
  per-source counts line, the evidence-procedure paragraph, the class list,
  and the rejection-rule sentence. All of those live in the
  `rate-session-memories` skill, which the directive instructs the model to
  invoke.
- Kept: the unrated count, one line per memory
  (`- <id> [<source>] <display ≤80 chars>`), the single safety rule line
  `Evidence line first; none possible = `ignored`.`, and the skill pointer.
- The `reason` field (`RATE_MEMORIES — N pending rating(s) for this session`)
  is unchanged; the 8 KB cap with the truncate-and-point-at-
  `memory.list_session_exposures` fallback is unchanged.

### Backfill decision (option A — accepted)

Exposure rows written before the migration have NULL `display`; agentcore-id
rows among them still render blank. Accepted as-is: unrated rows leave the
pool at the next rating turn, so the blank lines self-clear within one rating
cycle per session. No backfill script, no purge.

## Error handling

Unchanged. `record` stays best-effort (never blocks retrieve/inject);
`_emit_rating_directive_if_unrated` keeps its swallow-everything guard and
`record_hook_error` reporting. A NULL/absent display renders as an empty
suffix, never raises.

## Testing

- `tests/hooks/test_session_close_rating_directive.py`: golden assertions move
  to the terse format; add a case proving snapshot display text appears for an
  agentcore-style `mem-` id with no local table row.
- `tests/services/test_exposure_log.py`: triple items, display persisted and
  truncated at 120, COALESCE precedence (snapshot beats join, join rescues
  NULL snapshot).
- Migration test per repo convention (pre-claim + DDL).
- `tests/e2e/test_sqlite_journey.py`: update any pinned directive text.

## Docs

`website/mcp-tools.md` (list_session_exposures output now carries display) and
any README/website text quoting the directive format get swept in the same PR;
if genuinely unaffected, the PR description states "docs unaffected".
