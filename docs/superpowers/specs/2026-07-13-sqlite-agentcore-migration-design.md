# Design: `better-memory agentcore migrate` — Idempotent SQLite → AgentCore Migration

## 1. Overview

`better-memory agentcore migrate` promotes an existing local SQLite knowledge base into an already-provisioned (or freshly provisioned) AWS Bedrock AgentCore memory, so that a user who has been running the default `sqlite` backend can switch `storage_backend` to `agentcore` without losing curated reflections and semantic memories.

The command replaces the current stub. Today `migrate-from-sqlite` is registered in `add_subparsers` (`cli/agentcore.py:75-78`) but `_handle_migrate` (`cli/agentcore.py:614-621`) raises `NotImplementedError` and takes no arguments.

The hard problem is **not** the write — it is guaranteeing that after the write, the *existing* AgentCore read path (`_fetch_reflection_buckets` → `_parse_reflection_record`, `agentcore.py:235-442`) surfaces each migrated reflection byte-for-byte the way `ReflectionSynthesisService.retrieve_reflections` (`reflection.py:1225-1258`) surfaces the SQLite original: same 11-key dict, same bucketing, same ranking, same counters. Two required fields (`evidence_count`, `updated_at`) do **not** round-trip through the current reader and force concrete backend edits (Section 7).

A second hard constraint: **better-memory has no code path that creates a reflection record.** Reflections are produced only by AWS's `episodicMemoryStrategy` extraction (`supports_synthesis=False`, `agentcore.py:80-81`); better-memory only *mutates* their metadata after the fact (`credit_one`, `record_use`, `_mutate_namespace_and_status`). The migrator is therefore the first-ever writer of reflection records and must build them directly via `batch_create_memory_records` into the reflections namespace the reader scans.

Design principle for idempotency: **stable source id + deterministic requestIdentifier + local ledger + client-side reconcile-by-source-row-id**, never rollback. This is the opposite of `init`'s failure model, which rolls back all `created_ids` via `delete_memory` on any exception (`agentcore.py:317-360`).

---

## 1b. REVISION 2026-07-13 — validated architecture (SUPERSEDES the metadata plane in §3/§6)

Three live probes against real AWS (acct 708306701628, eu-west-2, throwaway memories, torn down) invalidated the original assumption that client-authored records can use the AgentCore **metadata** plane. Corrected facts:

1. **Client BASE writes are accepted and the JSON content body round-trips perfectly**, but on the **episodic/reflections** namespace the **entire custom metadata map is silently dropped** (only `x-amz-*` system keys survive). Metadata retention is **schema-gated by the strategy owning the namespace**: episodic declares its schema under `reflectionConfiguration.memoryRecordSchema`, which governs only AWS-**extracted** records — client BASE writes get nothing. `batch_update_memory_records` to add metadata is a silent no-op.
2. **Content-BODY updates on a client-authored BASE record DO persist durably** (confirmed by successive updates via read-your-write GET).
3. **userPreference (semantic) declares a top-level `memoryRecordSchema`** that DOES govern client BASE writes: keys **declared** in it are retained on create, survive updates, and are listable. **Undeclared** keys (e.g. `source_row_id`) are silently dropped.
4. A **non-strategy namespace** (no `memoryStrategyId`) retains full client metadata but records are **not listable** (`list_memory_records` → `[]`) → dead end for retrieval. `customMemoryStrategy` needs `memoryExecutionRoleArn` + SNS + S3 → too heavy.

**Resulting architecture (hybrid):**

- **Reflections → Option A: all state in the JSON content body.** The body carries `title, use_cases, hints, confidence, tech, phase, evidence_count, updated_at` **and** the fields §3 put in metadata: `polarity, status, useful_count, times_misled, times_overlooked, source_row_id, source_backend`. `_parse_reflection_record` reads **body-first with metadata fallback** — AWS-extracted records keep working via metadata; migrated records resolve from the body. No metadata map is written for migrated reflections.
- **Reflection rating on migrated records rewrites the BODY, not metadata.** `credit_one` / `record_use` / `_mutate_namespace_and_status` currently write metadata (a no-op on client records). For records carrying `source_backend='sqlite'` in the body, the mutate path must read-modify-write the content body instead. This changes existing agentcore rating behavior for migrated records only (extracted records unchanged) — a backend edit beyond §6.
- **Semantic → declared metadata.** Extend the userPreference `memoryRecordSchema` to **declare** every migration field (notably `source_row_id`; counters `useful_count/times_misled/overlooked_count` are already declared). Semantic content stays the user's text (not JSON), so the idempotency key MUST be declared metadata. Open item: whether the already-provisioned real semantic memory's strategy schema can be widened via `update_memory_strategy` or requires re-provision — the `migrate`/`--provision` path must guarantee the target schema declares the needed keys before writing.
- **Idempotency reconcile:** reflections by `body.source_row_id` (client-side scan of the listable reflections namespace); semantic by declared-metadata `source_row_id`. Both listable, so §5.3 reconcile holds with the key relocated.
- **Rejected:** non-strategy namespace (not listable); `customMemoryStrategy` (heavy infra, no gain).

§3's mapping table and §6's reader edits below remain the reference for field names and read-path citations, but read "metadata" as "content body" for every reflection field marked metadata there. Evidence: `scratchpad/probe*_out.txt`; memory ids `c588ca1c…`, `9bd09ba6…`.

---

## 2. Scope

### In scope (default)

- **Reflections** — the primary target. All rows in `reflections` with `status IN ('pending_review','confirmed')` and (`project = <db project>` OR `scope = 'general'`), matching the retrieval admission rule (`reflection.py:1208-1209`). Full metadata: `confidence, evidence_count, useful_count, times_misled, times_overlooked, polarity, phase, use_cases, hints, updated_at, title, tech` (Section 4).
- **Semantic memories** — all rows in `semantic_memories` (`0008_semantic_memories.sql:10-18`), with rating counters `useful_count, times_misled, times_overlooked` preserved. `semantic_observe` already exists (`agentcore.py:683-695`) but its `_semantic_initial_metadata` seeds every counter to 0 (`agentcore.py:646-655`); the migrator writes the real values.

### Out of scope (default; `--include=observations` opt-in, flagged lossy)

- **Observations.** In AgentCore an observation is an **episodic event** (`observe()` → `create_event`, `agentcore.py:181-196`), not a record. Three facts make migrating them near-useless:
  1. `list_observations` only reads *current-session* events via `list_events` and **cross-session enumeration is unsupported** (`agentcore.py:444-527`) — migrated events, written under a synthetic migration session id, would never be re-read.
  2. `reinforcement_score` has **no event-plane home** and is returned as a constant `None` (`agentcore.py:506`); SQLite's `reinforcement_score REAL NOT NULL` and all `validated_*`/`used_count`/`retrieved_count` counters are unrepresentable.
  3. AWS's `episodicMemoryStrategy` **re-extracts** reflections from events internally, so replaying raw observations risks duplicate/uncontrolled reflection extraction that collides with our explicitly-migrated reflections.
  We therefore treat observations as source material already distilled into reflections. `--include=observations` remains available as a best-effort episodic replay but prints a loud "non-retrievable, lossy" warning.

### Explicitly not migrated (SQLite-only structural columns, no AgentCore home)

Named for completeness (Section 4 mapping table lists each): `reflections.project` (encoded via namespace/actor), `superseded_by`, `created_at`, `status_changed_at`, `scope` (encoded via namespace); the per-class rating timestamps `last_useful_at/last_misled_at/last_overlooked_at` (collapsed — see below); observation-only columns `session_id, trigger_type, scope_path, validated_true/false, retrieved_count, used_count, last_retrieved/used/validated`. `retired` and `superseded` reflections are skipped on first migration (they are excluded from retrieval anyway, `reflection.py:1208`); status *transitions* to retired on later runs are handled by the ledger diff (Section 6).

---

## 3. Data-model mapping — the metadata round-trip (the crux)

AgentCore stores two planes per reflection **record**: the **content body** (`record.content.text`, a JSON string) and the **metadata map** (`{key: {numberValue|stringValue|dateTimeValue}}`). Which plane a field lands in is dictated entirely by where the *reader* looks — parity is defined by `_parse_reflection_record` (`agentcore.py:356-442`), not by preference.

Key reader facts that pin the mapping:
- `confidence` is read from `json.loads(rec['content']['text'])['confidence']` — **content body only** (`agentcore.py:396-399`). There is no `confidence` key in `EPISODIC_METADATA_SCHEMA` (`_agentcore_strategies.py:11-40`). Putting confidence in metadata would be invisible.
- `title, use_cases, hints, phase, tech` are all parsed from the content body (`agentcore.py:381-397`).
- `polarity` is read from **metadata** `stringValue 'polarity'` into internal `_polarity`, used only as the bucket selector and stripped from the public dict (`agentcore.py:414, 332, 350-353`). `status` is **metadata** `stringValue 'status'` (`agentcore.py:417`).
- `useful_count`, `times_misled` are read from **metadata** `numberValue` (`agentcore.py:428-429`) — these two round-trip cleanly.
- `times_overlooked` has a **name mismatch**: SQLite column `times_overlooked` (`0010_overlooked_rating.sql:42`) ↔ AgentCore metadata key `overlooked_count` (`agentcore.py:438`, `_agentcore_strategies.py:37`). Read into `_overlooked_count`, ranking-only, stripped from the dict.
- `evidence_count` is **computed** `int(useful_count)+int(missed_count)` (`agentcore.py:430`), NOT stored — a parity gap (Section 7).
- `updated_at` is **derived** from system metadata `x-amz-agentcore-memory-updatedAt` (dateTimeValue), falling back to `createdAt` (`agentcore.py:401-408`) — better-memory cannot set the system key, a parity gap (Section 7).
- Per-class rating timestamps collapse to one metadata `stringValue 'last_credited_at'` (`agentcore.py:619, 1087`). **It MUST be `stringValue`, not `dateTimeValue`** — it is a declared STRING indexed key; a `dateTimeValue` fails the whole record update with "value type does not match declared indexed key type" (`agentcore.py:616-621`, the live root cause of the `applied:0` bug).

### 3.1 Reflection mapping table

| SQLite field (source) | AgentCore location | Wire type | Read-path surfacing (public dict key) |
|---|---|---|---|
| `title` | content body `.title` | JSON string | `title` (`agentcore.py:381-424`) |
| `use_cases` | content body `.use_cases` | JSON string | `use_cases` |
| `hints` (JSON-encoded list) | content body `.hints` | JSON list | `hints` (list, `agentcore.py:428`) |
| `confidence` | content body `.confidence` | JSON number | `confidence` (`agentcore.py:396-399`) |
| `tech` | content body `.tech` | JSON string | `tech` |
| `phase` | content body `.phase` | JSON string | `phase` |
| `evidence_count` | content body `.evidence_count` **(NEW field, requires reader edit)** | JSON number | `evidence_count` (after edit at `agentcore.py:430`) |
| `updated_at` | content body `.updated_at` **(NEW field, requires reader edit)** | JSON string | `updated_at` (after edit at `agentcore.py:401-408`) |
| `polarity` | metadata `polarity` | stringValue | bucket key `do`/`dont`/`neutral` (`agentcore.py:414, 332`) |
| `status` (remapped) | metadata `status` | stringValue (INDEXED) | filter, default active (`agentcore.py:417`) |
| `useful_count` | metadata `useful_count` | numberValue | `useful_count` (`agentcore.py:428`) |
| `times_misled` | metadata `times_misled` | numberValue | `times_misled` (`agentcore.py:429`) |
| `times_overlooked` | metadata `overlooked_count` **(renamed)** | numberValue | `_overlooked_count` → ranking only (`agentcore.py:438`) |
| — (derive `0`) | metadata `missed_count` | numberValue | feeds evidence_count fallback only (`agentcore.py:430`) |
| — (derive `0`) | metadata `ignored_count` | numberValue | stored, read nowhere |
| `max(last_useful_at,last_misled_at,last_overlooked_at)` | metadata `last_credited_at` | **stringValue** (INDEXED) | not surfaced; MUST be stringValue (`agentcore.py:616-621`) |
| `id` (sqlite PK) | metadata `source_row_id` **(NEW)** + `requestIdentifier` | stringValue | reconcile/idempotency (client-side scan) |
| — | metadata `source_backend='sqlite'` **(NEW)** | stringValue | reconcile filter |
| `project` | encoded in namespace/actor (`resolve_actor_id`) | — | not a field |
| `scope` | encoded in namespace choice | — | not a field |
| `superseded_by`, `created_at`, `status_changed_at` | — | — | not migrated (Section 2) |

**Namespace + strategy:** project-scoped reflections → `projects/{actor}/reflections/` (`resolve_actor_id(project)`); `scope='general'` → `general/reflections/`. These are exactly the two namespaces `_fetch_reflection_buckets` lists (`agentcore.py:235-354`). Each record supplies `memoryStrategyId` = the episodic strategy id from `agentcore.json`.

**Status remap** (SQLite `{pending_review,confirmed,retired,superseded}` → AgentCore `{active,promoted,retired}`, `agentcore.py:273-276,417`): `pending_review → active`, `confirmed → promoted`, `retired → retired`, `superseded → skip`.

### 3.2 Semantic mapping table

| SQLite field | AgentCore location | Read surfacing |
|---|---|---|
| `content` | content `.text` | `content` |
| `scope` | namespace (`projects/{actor}/...` vs `general/...`) | — |
| `useful_count` | metadata `useful_count` numberValue | (reader edit — Section 7.3) |
| `times_misled` | metadata `times_misled` numberValue | (reader edit) |
| `times_overlooked` | metadata `overlooked_count` numberValue (renamed) | ranking |
| `last_*_at` (3) | metadata `last_credited_at` stringValue | — |
| `created_at`/`updated_at` | content body `.created_at`/`.updated_at` | (reader edit) |
| `id` | metadata `source_row_id` + `requestIdentifier` | idempotency |

---

## 4. CLI surface

```
better-memory agentcore migrate [flags]
```

| Flag | Default | Behavior |
|---|---|---|
| `--dry-run` | off | Read SQLite + ledger, compute the plan (create / update / skip / delete-transition counts per kind and namespace), print it, make **zero** AWS calls. |
| `--include=<kinds>` | `reflections,semantic` | Comma list of `reflections`, `semantic`, `observations`. `observations` prints the lossy/non-retrievable warning. |
| `--restart` | off | Ignore the ledger's "migrated" state and re-verify/upsert every eligible row (still idempotent via `source_row_id` reconcile). Default (no flag) is resume-safe. |
| `--provision` | off | If target memories are missing/not ACTIVE, create them by reusing `init`'s `_create_one_memory` + `_poll_until_active` path; else abort with guidance. Without the flag, missing memories → hard error. |
| `--db <path>` | `<home>/memory.db` | Source SQLite path. |
| `--project <name>` | all projects in db | Limit migration to one project. |
| `--region <r>` | `cfg.region` from `agentcore.json` else `--region` | Same resolution as `status`/`smoke` (`agentcore.py:442,485`); region lives in `agentcore.json`, not env (`factory.py:76-82`). |
| `--home <path>` | `--home` else `$BETTER_MEMORY_HOME` else `~/.better-memory` | Same `_resolve_home` as init (`agentcore.py:94-98`). |
| `--batch-size <n>` | `25` (conservative; API limit undocumented in-repo — Section 10) | Records per `batch_create_memory_records` call. |
| `--verify` | off | After writes, read a sample (or all) back via `get_memory_record` using smoke's read-your-write retry (`ResourceNotFoundException` ×5 / 2s, `agentcore.py` smoke path) and diff against the SQLite source. |

Exit codes: `0` full convergence; `2` partial (some records failed, ledger records them for resume); `1` fatal (no config, not provisioned, auth failure). Auth failures surface as `1` by building the data client inside the try, mirroring smoke (`agentcore.py:473-611`).

---

## 5. Idempotency & resumability

### 5.1 Stable id → deterministic requestIdentifier

Each record's `requestIdentifier = f"bm-{kind}-{sqlite_id}"` truncated to the 80-char AWS limit (mirrors the existing content-hash dedup at `agentcore.py:679-681`, but keyed on the **row id** so the same row always maps to the same identifier). `requestIdentifier` is correlation/dedup only and the **server mints its own `mem-<uuid>` id** (`agentcore.py`, smoke; `memoryRecordId` is not a valid input key). AWS dedup on `requestIdentifier` is short-window only, so it is a *first line* of defense, not the source of truth.

### 5.2 Local ledger (source of truth)

A new SQLite table in the **source** db (created lazily by migrate; never touched by the normal backends):

```sql
CREATE TABLE IF NOT EXISTS agentcore_migration (
  source_kind        TEXT NOT NULL,          -- 'reflection' | 'semantic'
  source_id          TEXT NOT NULL,          -- sqlite row id (stable)
  namespace          TEXT NOT NULL,
  target_record_id   TEXT,                   -- server-minted mem-<uuid>, NULL until first success
  content_hash       TEXT NOT NULL,          -- sha256 of the canonical (body+metadata) payload
  status             TEXT NOT NULL,          -- 'pending' | 'migrated' | 'failed' | 'retired'
  last_error         TEXT,
  migrated_at        TEXT,
  PRIMARY KEY (source_kind, source_id)
);
```

Per-row decision on every run:
- **No ledger entry** → CREATE via `batch_create_memory_records`; on success record `target_record_id` + `content_hash` + `status='migrated'`.
- **Entry exists, `content_hash` unchanged, `status='migrated'`** → SKIP (converged).
- **Entry exists, `content_hash` changed** → UPDATE via `batch_update_memory_records` (`agentcore.py:625-636` shape: `{memoryRecordId, timestamp, metadata:full_snapshot, [content], [namespaces]}`, `_full_metadata_snapshot` strips `x-amz-*` keys, `agentcore.py:569-595`). Refresh hash.
- **SQLite status now `retired`/`superseded` but ledger `migrated`** → status-transition UPDATE to `status='retired'` metadata (converges retirements), mark ledger `retired`.
- **`status='failed'`** → retry (same as create/update).

This makes re-runs idempotent and convergent regardless of AWS dedup behavior.

### 5.3 Reconcile-by-metadata (ledger-loss safety net)

Because `source_row_id` is written into metadata, a `--restart` (or a lost ledger) can rebuild state: list the two reflection namespaces client-side, index existing records by `metadata.source_row_id`, and reattach `target_record_id`. `source_row_id` is **not** a server-indexed key (indexed keys are fixed at provision time to `status, last_credited_at, overlooked_count`, `_agentcore_strategies.py:52-56` — cannot be extended post-hoc), so this reconcile is a client-side scan, acceptable for a maintenance command.

### 5.4 Resume & dry-run

- **Resume** is the default: the ledger already encodes progress; a crashed run leaves `migrated` rows skipped and `pending`/`failed` rows retried. No cursor needed beyond the ledger.
- **Dry-run** walks the same decision logic and prints the create/update/skip/retire tallies without any AWS call.
- A single-process **advisory lock** (a `agentcore_migration_lock` row / file in `<home>`) prevents concurrent runs from double-creating within the AWS dedup blind spot.

---

## 6. Backend changes required for read parity

Migration is not just a writer; it must guarantee the reader surfaces every field. Concrete edits:

### 6.1 `storage/agentcore.py :: _parse_reflection_record` — `evidence_count` (`agentcore.py:430`)

Currently: `evidence_count = int(useful_count) + int(missed_count)`.
Change to prefer the stored value when present:

```python
body_ec = body.get("evidence_count")
evidence_count = int(body_ec) if body_ec is not None else int(useful_count) + int(missed_count)
```

Locks parity with SQLite's stored, synthesis-recomputed `evidence_count` (`reflection.py:858,1254`), which otherwise diverges whenever `evidence_base != useful+missed` (scout gap). AWS-native extracted records (no `evidence_count` in body) keep the old computed fallback — no regression.

### 6.2 `storage/agentcore.py :: _parse_reflection_record` — `updated_at` (`agentcore.py:401-408`)

Prefer the migrated body value before the system-metadata fallback:

```python
updated_at = body.get("updated_at") or _from_system_updated_at(rec) or rec.get("createdAt")
```

Without this, migrated reflections report **creation** time as update time, corrupting `age_days` in `relevant.py:103` / bootstrap and drifting the ranking tiebreak (`ORDER BY ... updated_at DESC`). Native records (no body `updated_at`) keep today's behavior.

### 6.3 Semantic read parity — `semantic_list` (`agentcore.py:742-757`) and/or `relevant.py:112-125`

`semantic_list` returns plain dicts `{id, content, namespaces, scope}` with **no counters**, but `relevant.py:112-125` uses attribute access (`getattr(s,'useful_count'...)`) that only works on SQLite's `SemanticMemory` dataclass — on dicts it silently returns defaults, so `content=''` and counters `0`, making semantic injection dead in agentcore mode (scout gap). Fix: have `semantic_list` return objects matching the `SemanticMemory` shape (`semantic.py:22-37`) populated from the metadata counters the migrator now writes, and surface `useful_count/times_misled/times_overlooked/updated_at`. (Required only if `semantic` is in `--include`; guard the change with a semantic round-trip test.)

### 6.4 New writer module `better_memory/storage/agentcore_migrate.py`

- `build_reflection_record(row) -> dict` — assembles content-body JSON (`title, use_cases, hints, confidence, tech, phase, evidence_count, updated_at`) + metadata map (polarity/status/counters/`last_credited_at` stringValue/`source_row_id`/`source_backend`) + `requestIdentifier`. Reuses the `_full_metadata_snapshot` coercion + `x-amz-*` stripping pattern (`agentcore.py:569-595`).
- `build_semantic_record(row)` — analogous, reusing `semantic_observe`'s per-record shape (`agentcore.py:683-695`) but with real counters instead of `_semantic_initial_metadata` zeros.
- `chunk(records, batch_size)` + `push_batch(data_client, records)` — the missing batching helper; existing call sites all pass `records=[<single>]` (`agentcore.py:685,545`).

### 6.5 CLI `_handle_migrate` (`cli/agentcore.py:614-621`) + argparse

Replace `NotImplementedError`; add the Section-4 flags to the `add_subparsers` registration (`agentcore.py:75-78`). Reuse `load_agentcore_config`, `_build_data_client`/`_build_control_client` (`agentcore.py:154-170`), `_resolve_home`, and `_find_existing_memory` (`agentcore.py:201-214`).

---

## 7. Auth & provisioning reuse

**Auth is entirely ambient** — nothing to add. `_build_data_client`/`_build_control_client` (`agentcore.py:154-170`) and the runtime factory (`factory.py:79-84`) call `boto3.client(...)` with only `BotoConfig(region_name=..., retries={mode:standard, max_attempts:5})` and no credential args. Credentials come from boto3's default provider chain (env `AWS_ACCESS_KEY_ID/SECRET`, shared profile, IMDS/role). The IAM-user model the requirement assumes is exactly what already works; migrate reuses the same clients and the same retries config.

**Provisioning:** migrate must verify or create the targets, not assume them.
1. Load `agentcore.json` via `load_agentcore_config` (schema-pinned to `schema_version==1`, `persistence.py:91-97`); if absent → abort unless `--provision`.
2. For each of episodic + semantic memory, call `_find_existing_memory` (`agentcore.py:201-214`, paginates `list_memories`, skips DELETING) and confirm ACTIVE via `get_memory` / the `_poll_until_active` readiness rule (`agentcore.py:173-198`: memory ACTIVE **and** every strategy ACTIVE).
3. If missing/not-ACTIVE and `--provision`: run init's `_create_one_memory` + `_poll_until_active` for the missing memory only, then persist via `save_agentcore_config` (atomic tmp+rename, `agentcore_persistence.py:59-69`). Do **not** touch `settings.json` `storage_backend` — flipping the backend is init/`--activate`'s job, not migrate's.
4. Region/home resolution identical to init (`agentcore.py:94-98`).

---

## 8. Error handling & partial-failure

- **No rollback.** Unlike init (delete all `created_ids` on any error, `agentcore.py:317-360`), migrate never deletes on failure — it records per-row `failed` in the ledger and continues, so re-run resumes.
- **Batch partial failure.** `batch_create_memory_records` returns both `successfulRecords` and `failedRecords`; update the ledger per successful record (capture `memoryRecordId`), leave failed rows `failed` with `last_error`, continue to the next batch.
- **The `last_credited_at` landmine.** Always write it as `stringValue`; a `dateTimeValue` rejects the entire record update with "value type does not match declared indexed key type" (`agentcore.py:616-621`). A unit test asserts the wire type.
- **Throttling / bulk backoff.** Rely on botocore `standard/5` retries; add jittered exponential backoff on `ThrottlingException` around each batch since bulk load is heavier than any existing call site.
- **Read-your-write lag.** `--verify` uses smoke's pattern: `get_memory_record` is read-your-write (~1s) with `ResourceNotFoundException` retried ×5/2s, while `list_memory_records` lags ~60s (`agentcore.py` smoke) — verification must not use list for immediate readback.
- **Auth/region misconfig** → data client built inside the try → clean `rc=1` (smoke pattern, `agentcore.py:473-611`).

---

## 9. Test plan

### T1 — unit, hermetic (no AWS)

- **Reflection mapping invariants** (`build_reflection_record`): `confidence`/`evidence_count`/`updated_at` land in the content body; `polarity`/`status`/counters land in metadata; `times_overlooked`→`overlooked_count` rename; the three `last_*_at` collapse to a single `last_credited_at` emitted as **stringValue**; `requestIdentifier` deterministic from row id; status remap `pending_review→active`, `confirmed→promoted`; `retired`/`superseded` skipped on create.
- **Round-trip parity (locks Requirement 4):** feed a record built by `build_reflection_record` into the *edited* `_parse_reflection_record` and assert the resulting 11-key dict `{id,title,phase,use_cases,hints,confidence,tech,evidence_count,useful_count,times_misled,updated_at}` **equals** `retrieve_reflections`' dict for the same source row (`reflection.py:1246-1258`). Assert bucket = polarity, and that the ranking key `(useful_count + 3*overlooked_count, confidence, updated_at)` matches SQLite's `ORDER BY` (`reflection.py:1232`, weight 3, `memory_rating.py:71`).
- **Reader edits:** body `evidence_count` preferred over `useful+missed`; body `updated_at` preferred over system metadata; native records (no body keys) keep legacy fallbacks (no regression).
- **Ledger idempotency:** run 2 with unchanged `content_hash` ⇒ 0 create/update calls; changed hash ⇒ exactly 1 update; SQLite-retire-after-migrate ⇒ 1 status-transition update.
- **Semantic parity:** `semantic_list` returns counter-bearing objects; `relevant.py` sees non-zero `useful_count`.

### T2 — fake-endpoint (stubbed bedrock-agentcore data plane)

Reuse `tests/e2e/_agentcore_env.py` scaffolding with a stub/`moto`-style data client.
- Full migrate run: assert `batch_create_memory_records` called with the expected chunked payloads (namespaces, `requestIdentifier`, body JSON, metadata types).
- Idempotent re-run: ledger short-circuits, zero creates.
- `--dry-run`: zero AWS calls, correct printed tallies.
- Partial failure: injected `failedRecords` ⇒ ledger marks them `failed`, re-run retries only those.
- `--verify`: readback diff passes.

### T3 — live, env-gated (real AWS, behind `BETTER_MEMORY_E2E`)

- Provision (or reuse) memories, migrate a small fixture db, then call `backend.retrieve(project, tech)` and assert buckets/order/counters/`confidence`/`evidence_count`/`updated_at` match the SQLite `retrieve_reflections` output for the same fixture — the end-to-end parity invariant.
- Re-run and assert `list_memory_records` count is stable (no duplicates) — the end-to-end idempotency invariant.
- **Landmine validation:** confirm client-authored records are *accepted* into the `episodicMemoryStrategy` reflections namespace and returned by `_fetch_reflection_buckets` (Section 10 risk).

---

## 10. Open questions / risks

1. **Can we write records into the extraction strategy's namespace?** The reader lists `projects/{actor}/reflections/` and `general/reflections/` (`agentcore.py:235-354`), namespaces owned by AWS's `episodicMemoryStrategy`. If `batch_create_memory_records` rejects client-authored records there (or AWS re-extraction later mutates/evicts them), we need a dedicated manual/custom strategy namespace *and* a corresponding second list namespace added to `_fetch_reflection_buckets`. **Highest-risk unknown; T3 must validate before build sign-off.**
2. **`batch_create_memory_records` per-call record limit** is undocumented in-repo (scout gap). `--batch-size` defaults conservative (25); T3 probes the real ceiling.
3. **`requestIdentifier` dedup horizon.** If AWS enforces dedup only briefly, the ledger + single-run lock are the sole guard against concurrent double-writes.
4. **`evidence_count` semantic drift.** SQLite `evidence_count` is a synthesis-recomputed source count (`reflection.py:858`); we migrate the stored value into the body (fixed by 6.1). AWS-native reflections still compute it — mixed populations are expected and acceptable.
5. **Per-class recency loss.** `last_useful_at/last_misled_at/last_overlooked_at` collapse into one `last_credited_at` (`agentcore.py:619`) — one-way, irreversible; documented.
6. **Native vs migrated duplication.** If AWS extracts a reflection semantically identical to a migrated one, dedup fails (native records carry no `source_row_id`). Mitigation: reconcile only owns `source_backend='sqlite'` records; native duplicates are out of migrate's authority.
7. **Indexed keys are frozen at provision.** `source_row_id` cannot be server-indexed post-hoc, so reconcile is a client-side scan — fine for a maintenance command, not for hot-path reads.
8. **Retirement convergence.** A reflection retired in SQLite *after* migration converges only if the user re-runs migrate; there is no push. Acceptable for a manual command; note in help text.