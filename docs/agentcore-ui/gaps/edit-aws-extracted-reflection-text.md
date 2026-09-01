# GAP / DECISION — Inline editing of reflection text (use_cases / hints) in agentcore mode

**Status:** open design decision (the one genuine residual in the agentcore-UI parity work).
**Converged-design default:** disable inline reflection text-edit in agentcore mode.
**Audience:** a maintainer deciding whether to build inline reflection editing, and how far.

This is the single item in `docs/agentcore-ui/agentcore-mapping.md` (row *"Edit reflection
text (use_cases + hints) in place"*, HARD) whose resolution is a real judgement call rather
than mechanical wiring or a settled local-vs-content split. Everything else in the converged
design is decided; this is not. Do not re-litigate the surrounding design — only this call.

---

## 1. What the data is / what the UI does today (sqlite mode)

A **reflection** is a synthesised, durable memory: a title plus two human-readable text
fields — `use_cases` (a prose "when to apply this" string) and `hints` (a list of prose
bullets) — plus `confidence`, `tech`, `phase`, `polarity`, rating counters, and a status.

In sqlite mode the UI lets the maintainer edit the two **text** fields in place:

- **Pre-populate:** `GET /reflections/<id>/edit` (route `reflection_edit_form`) renders
  `fragments/reflection_edit_form.html`, loading current `use_cases` + `hints` via
  `queries.reflection_detail`.
- **Save:** `POST /reflections/<id>/edit` (route `reflection_edit_save`) calls
  `ReflectionService.update_text`, which runs
  `UPDATE reflections SET use_cases=?, hints=?, updated_at=? WHERE id=?`
  (hints stored as `json.dumps(list[str])` from newline-separated input). Single commit.
  [Correction: the re-embed step this paragraph originally described (`SyncEmbedder` →
  vec0 `reflection_embeddings`) was removed repo-wide in remove-ollama-embeddings; the
  table no longer exists (migration 0018). `update_text` today is just the `UPDATE`
  above.] Empty use_cases/hints → 400 at the route; retired/superseded → `ValueError` →
  409. On success it re-renders `fragments/reflection_drawer.html` with
  `HX-Trigger: reflection-changed`.

The mental model in sqlite mode is simple: the maintainer **owns** the reflection text.
Synthesis writes it once; nothing else rewrites it; the edit is the last word.

## 2. Where it comes from, and why agentcore cannot serve it the same way

### The sqlite source
`reflections.use_cases` and `reflections.hints` are plain columns on the local
`reflections` table. [Correction: this used to also say "with a mirrored vec0
`reflection_embeddings` row" — that table was dropped in migration 0018
(remove-ollama-embeddings); see the section-1 correction above.] `update_text` owns
those columns outright. All of this bypasses `StorageBackend` today — the UI hands
`ReflectionService` a raw `sqlite3.Connection` (`app.py:93-96`), so even the write never
touches the backend abstraction (see `docs/agentcore-ui/data-sources.md`, Reflections tab).

### Where the same data lives in agentcore
Reflections are memory **RECORDS** under namespace `projects/{project}/reflections/`
(`resolve_namespace(actor_id, "reflections")`; `actorId == project`). Records are durable
(no TTL — only *events* expire). The reflection **text** (`title`, `use_cases`, `hints`)
lives in the record's **JSON content body** (`record["content"]["text"]`), for BOTH record
origins — `_parse_reflection_record` (agentcore.py:732-885) reads `title` / `use_cases` /
`hints` straight out of `json.loads(content.text)` regardless of origin. Two origins share
that namespace, distinguished by a body marker:

- **Migrated (SQLite-origin) records** — `_record_body(record).get("source_backend") ==
  "sqlite"` (agentcore.py:1040-1053, 1526). We authored the body; status, counters, and
  timestamps all live in it (AWS silently drops the custom metadata map on client-authored
  BASE records in this namespace — design §1b, proven live). Its text is freely
  editable by a content read-modify-write.
- **AWS-EXTRACTED records** — produced and **owned by the AgentCore extraction pipeline**.
  The body has no `source_backend`; status/polarity/counters resolve from *metadata* instead
  (body-first with metadata fallback in `_parse_reflection_record`). The text body is an
  artifact of extraction/consolidation.

### Why the sqlite path does not port
1. **No reflection text-update method exists on the backend.** The protocol has
   `semantic_update_text` (semantic memories) but **no** reflection-text equivalent —
   `promote_reflection` / `retire_reflection` only mutate namespace + status
   (`_mutate_namespace_and_status`, agentcore.py:1507-1591). A text edit would be a
   brand-new body RMW: `get_memory_record` → mutate `use_cases`/`hints` in the parsed body →
   `batch_update_memory_records(records=[{memoryRecordId, timestamp, content:{text:
   json.dumps(new_body)}}])`, with the `_retry_on_transient_404` wrapper. The precedent
   already exists in `_credit_body_counter` (1055-1111) and the migrated branch of
   `_mutate_namespace_and_status` (1526-1549) — mechanically this write is not the problem.
2. **The problem is ownership, and it applies only to AWS-extracted records.** Those records
   are the extraction pipeline's output. A hand-edit to `use_cases`/`hints` is a **body RMW
   that fights the owner**: a subsequent extraction/consolidation pass can legitimately
   rewrite or supersede that same body, silently discarding the maintainer's edit. There is
   no last-writer guarantee, no field-level ownership boundary, and no "user-pinned" flag the
   extractor is contracted to respect. (The data plane also exposes no conditional/optimistic
   write — the crediting path already documents last-writer-wins, agentcore.py:1084-1088 — so
   even detecting the clobber after the fact is not free.)
3. **The vec0 re-embed leg has no agentcore counterpart** and is a design non-goal —
   embeddings are AWS-managed; vector search moves to `RetrieveMemoryRecords`. [Correction:
   this point originally framed the vec0 leg as something the sqlite `update_text` "also
   touches" and that "simply drops away in agentcore mode" — in fact `update_text` no longer
   touches any embedding table in EITHER mode; the vec0 write was removed repo-wide, not
   agentcore-specifically dropped. See the section-1 correction above.]

So: **migrated** reflection text is cleanly editable (we own the body). **AWS-extracted**
reflection text is editable *mechanically* but the edit is not durable against the extraction
pipeline that owns it. That split is the whole decision.

## 3. Options going forward

### Option A — Disable inline reflection text-edit entirely in agentcore mode  **(RECOMMENDED — matches the converged design)**
Gate the edit affordance off behind a backend capability flag (see below); the drawer shows
no "Edit text" button in agentcore mode. Retire and promote-to-general stay available (they
map cleanly to `retire_reflection` / `promote_reflection`).

- **Pros:** Zero risk of the maintainer's edit being silently clobbered by extraction. No
  new protocol surface, no new failure mode, no partial "your edit might stick" UX to
  explain. sqlite mode is untouched. Honest: the UI never promises an edit it cannot
  durably keep. Consistent with how the rest of agentcore mode hides things it cannot serve
  cleanly (Observations tab, provenance, episode lifecycle).
- **Cons:** Migrated (SQLite-origin) records — whose bodies we *do* own and *could* safely
  edit — lose an editing capability they technically could have kept. In a
  migrated-then-agentcore deployment the maintainer can no longer fix a typo in a reflection
  they wrote pre-migration. This is real but bounded: promote/retire still work, and text
  fixes were always a rare, low-value action.

### Option B — Enable edit only for migrated records; hide it for AWS-extracted
Show the edit button only when `_record_body(record).get("source_backend") == "sqlite"`;
implement the new body-RMW backend method for that case; leave AWS-extracted read-only.

- **Pros:** Preserves editing exactly where it is safe (bodies we own), blocks it exactly
  where it is unsafe (extraction-owned bodies). No clobber risk. Uses the marker the code
  already relies on everywhere else.
- **Cons:** Per-row capability, not per-mode — the drawer button appears/disappears based on
  a record's origin, which is opaque to the maintainer ("why can I edit this one and not
  that one?"). Requires the new reflection-text-update protocol method + wiring + tests now,
  for a population (migrated records) that shrinks over time as extraction takes over. More
  surface for marginal, decaying value. A reasonable future upgrade to Option A, not clearly
  worth building up front.

### Option C — Allow edit on all records, best-effort, with an explicit "may be overwritten" warning
Implement the body RMW for every reflection; on AWS-extracted records, warn in the UI that
extraction may later overwrite the edit.

- **Pros:** Uniform affordance; nothing is hidden; the maintainer decides.
- **Cons:** Ships a feature that silently loses data by design. The warning does not make the
  loss acceptable — it just moves blame. Invites confusion and bug reports ("my edit
  disappeared"). Directly contradicts the converged-design default. **Not recommended.**

### Option D — Build a durable user-override layer that extraction is contracted to respect
Add a pinned/override field (e.g. a `user_edited: true` body flag or a sidecar override
record) that the extraction pipeline must merge-preserve rather than clobber.

- **Pros:** The only option that makes editing AWS-extracted text genuinely durable and
  honest.
- **Cons:** Requires changing the **extraction pipeline's** contract — outside the UI and
  outside this codebase's control (extraction is AWS-side). No such merge-preserve contract
  exists today, and the data plane offers no conditional write to enforce it. This is a
  large, cross-team, speculative build for a rare action. **Not worth it** for the value at
  stake — this is the "we are screwed if we insist on truly-durable extracted-text edits"
  branch, and the honest answer is: don't.

### Recommended
**Option A** — disable inline reflection text-edit in agentcore mode, driven by a backend
capability flag. Rationale: it is the only option with no silent-data-loss surface and no new
protocol/cross-team dependency, and the lost capability (editing migrated-record text) is
rare and low-value. If demand for migrated-record editing materialises, **Option B** is the
clean incremental upgrade — the body-RMW method it needs is small and has direct precedent in
`_credit_body_counter` / `_mutate_namespace_and_status`.

### Implementation note (either A or B): capability-flag gating
Follow the existing `supports_episodes` pattern (protocol.py:57-64; `AgentCoreBackend`
returns `False`; sqlite returns `True`). Add an analogous mutation-capability flag — e.g.
`supports_reflection_text_edit` (or a broader `supports_reflection_mutation`) — returning
`True` for sqlite and `False` (Option A) or record-scoped (Option B) for agentcore. Thread it
into `create_app` and the reflection drawer template so the sqlite path is **completely
unchanged** and agentcore simply omits the affordance. This is the same one-boolean threading
the Episodes-tab gate needs; do them together.

---

## Summary
Reflection text (`use_cases`/`hints`) edits cleanly in sqlite mode via
`ReflectionService.update_text` (`POST /reflections/<id>/edit`), which owns the columns
outright. In agentcore the text lives in each record's JSON content body under
`projects/{project}/reflections/`, editable mechanically by a `batch_update_memory_records`
content RMW (precedent: `_credit_body_counter`) — but AWS-EXTRACTED records are owned by the
extraction pipeline, which can silently overwrite any hand-edit; migrated (`source_backend ==
"sqlite"`) bodies we own and could safely edit. No reflection-text-update method exists on the
protocol today (only `semantic_update_text`), and the data plane offers no conditional write
to detect a clobber. Recommended: **Option A** — disable inline reflection text-edit in
agentcore mode behind a new backend capability flag (per the `supports_episodes` pattern),
leaving sqlite untouched; **Option B** (edit migrated records only) is the clean later upgrade.
Options C (edit-with-warning) and D (extraction-respected override layer) are rejected as
silent-data-loss and out-of-scope-cross-team respectively.
