# AgentCore Full-Flow Journey Test Suite — Design

Feeds the implementer wave. Assumes fix-plan Tasks 1–3 have landed (idvar gate deleted,
MCP dispatch wired to `AgentCoreBackend`, `settings.json` backend activation written by
`agentcore init`, region single-sourced from `agentcore.json`, `write_backend_settings`
helper added to `tests/e2e/_agentcore_env.py`).

## 1. Purpose and the one thing that makes this suite different

The existing T2 suite (`test_agentcore_t2.py`, D1–D7) proves *individual* wire behaviors, and
almost all of it **force-activates agentcore by setting `BETTER_MEMORY_STORAGE_BACKEND=agentcore`
in the child env**. After the fix lands, that env var is no longer how a real user runs
agentcore — `agentcore init` writes `settings.json {"storage_backend":"agentcore"}` and the
backend is resolved from that file (env still wins if set). D-suite tests also carry the dead
`BETTER_MEMORY_AGENTCORE_REGION` + dummy id vars that Task 1 deletes.

The journey suite validates the **shipped onboarding path end-to-end**: `agentcore.json` +
`settings.json` present, **no** `BETTER_MEMORY_STORAGE_BACKEND`, **no** region/id env vars —
exactly the state `agentcore init` leaves behind — and walks the whole better-memory data loop
through the *real MCP dispatch layer* and the *real hooks*, asserting per step which AWS
operation fired, which memory it routed to (EPI vs SEM), the request count, and that **zero rows
leak into local sqlite**. Its distinguishing value over the D-suite is four things the focused
tests structurally cannot show:

1. **The tested config == the onboarding config** (settings.json activation, no env knobs).
2. **Sequential chaining / read-after-write** (observe's returned AWS id flows into
   retrieve_observations; a semantic id flows into rating).
3. **No-sqlite-leakage assertions co-located with each dispatch step** (the inverse half of the
   deleted D3 pin).
4. **End-to-end ordering** as one narrative: bootstrap → observe → retrieve → semantic → rate →
   inject → close.

Anything the journey does not need to re-prove (exhaustive metadata field shapes, polarity-filter
internals, env-precedence matrix, region convergence) is deferred to the owning D-suite / unit
test and cited by name, so there is no duplication.

## 2. The onboarding-config anchor (shared harness addition)

All hermetic scenarios build their env through one new module-level helper in
`tests/e2e/test_agentcore_journey.py`, composing the Task-1 `write_backend_settings` helper with
the existing `write_fake_agentcore_json`:

```python
from tests.e2e._agentcore_env import (
    agentcore_env, write_fake_agentcore_json, write_backend_settings,  # write_backend_settings ADDED by Task 1
)

def onboarding(clean_slate_home, fake, **pins):
    """The exact state `agentcore init` leaves: agentcore.json + settings.json,
    NO backend/region/id env vars. Backend resolves from settings.json."""
    bm_home = clean_slate_home / ".better-memory"
    write_fake_agentcore_json(bm_home)        # what init provisions
    write_backend_settings(bm_home)           # what init activates (settings.json)
    return agentcore_env(
        clean_slate_home, fake.port,
        BETTER_MEMORY_STORAGE_BACKEND=None,   # the onboarding state: env var ABSENT
        **pins,
    )
```

`isolated_env` pins `BETTER_MEMORY_HOME=<home>/.better-memory` (= `bm_home`), so
`resolve_storage_backend()` finds `bm_home/settings.json` and resolves `agentcore` with no env
var present. This is a **stronger** activation proof than every D-suite test. Env-precedence (env
wins over settings) is owned by Task 1's `tests/test_config.py` unit tests and is deliberately
*not* re-tested here.

Reused fixtures/helpers, all unchanged: `clean_slate_home`, `mcp_session`, `run_hook`, `text_of`
(from `tests/e2e/conftest.py`); `FakeAgentCore` + `RecordedRequest.{operation,path,body,
sigv4_region,text()}` (from `_fake_agentcore.py`); `agentcore_env` / `write_fake_agentcore_json`
(from `_agentcore_env.py`). No new endpoint or fixture machinery.

## 3. Scenario catalog

### T2 hermetic — `tests/e2e/test_agentcore_journey.py` (fake endpoint, real boto3 wire)

| ID | Scenario | Mirrors sqlite | Wired path exercised | Key wire assertion | vs D-suite |
|----|----------|----------------|----------------------|--------------------|------------|
| **J1** | Onboarding boot → wired tool surface | C1 | `create_server` backend resolve from settings.json | 0 AWS calls; wired data tools present; synthesize + 4 episode tools + `run_retention` hidden; both sqlite DBs migrated | complements D1 (activation source differs) |
| **J2** | `session_bootstrap` hook reaches AgentCore | C3 (bootstrap leg) | `session_bootstrap.py`→`build_backend`→`backend.session_bootstrap` | exactly 4 `ListMemoryRecords` (3 EPI reflections + 1 SEM); envelope carries summary; no sqlite memory-content write | NEW (zero prior coverage) |
| **J3** | `memory.observe`→`retrieve_observations` round-trip | C4 (observe+retrieve) | dispatch→`backend.observe` / `backend.list_observations` | 1 `CreateEvent` to EPI, returned id == AWS eventId (not local uuid), 0 rows in local `observations`; then 1 `ListEvents` to EPI, id-match + metadata flattened | replaces deleted D3 half; complements D4 (MCP layer vs backend-direct) |
| **J4** | `memory.retrieve` bucket fan-out | C4 (buckets) | dispatch→`backend.retrieve` | 3 `ListMemoryRecords` to EPI reflections namespace; buckets `{do:[],dont:[],neutral:[]}` | complements D2 (filters owned by D2) |
| **J5** | `semantic_observe`→`semantic_retrieve` round-trip | (no direct C) | dispatch→`backend.semantic_observe` / two `backend.semantic_list` | 1 `BatchCreateMemoryRecords` to SEM (never EPI), 0 rows in local `semantic_memories`; then **2** `ListMemoryRecords` to SEM (project + general, UD-2), merged payload with `project/created_at/updated_at` present as `None` | NEW; complements D4 |
| **J6** | Rating loop: `record_use` + short-id guard + `apply_session_ratings` + `list_session_exposures` | C4/C5 | dispatch→`backend.record_use` / `credit_one` / `list_session_exposures` | ≥40-char id → `GetMemoryRecord`+`BatchUpdateMemoryRecords` (EPI, x-amz keys stripped); <40-char id → clear error, **0 wire**; semantic-id rating routes to SEM; exposures = empty envelope, 0 wire | complements D4 (adds MCP dispatch + short-id guard + empty-exposure contract) |
| **J7** | `contextual_inject` per-prompt under activation | C5 (per-prompt) | `contextual_inject.py` resolver honours settings.json | 3 EPI + 1 SEM list calls; `<project-memory>` block injected | complements D6-A (activation source differs) |
| **J8** | `session_close` closure under activation (terminus) | C5 (close+marker) | `session_close.py` settings-aware gate | exactly 1 `CreateEvent` role=OTHER to EPI; spool marker written; no `memory.db` | complements D5 (env-gate matrix owned by D5) |

### T3 live — additions to `tests/integration/test_agentcore_live_e2e.py` (real AWS, throwaway memories)

| ID | Scenario | Proves (fix plan §4) | Read-after-write basis | vs existing |
|----|----------|----------------------|------------------------|-------------|
| **E3** | Onboarding-config MCP `observe`→`retrieve_observations` | item 2 | events are **promptly consistent** (smoke relies on it) | NEW; the non-tautological MCP write path dispatch enables |
| **E4** | MCP semantic round-trip `observe`→`retrieve`→`update`→`delete` | item 3 | semantic record readback promptly consistent | NEW |
| **E5** | `record_use`/credit on a real ≥40-char semantic record id | item 4 | uses E4's real record id | NEW (E2 only credited via backend-direct) |
| **E6** | `session_bootstrap` hook + `contextual_inject` reach AWS under onboarding config | item 7 | count/namespace validation only | NEW |
| **E7** | `session_close` Stop hook closure with settings.json only, no env | item 6 | one `CreateEvent` role=OTHER | NEW (live version of J8) |
| — | Region single-source: run E3–E7 in a **non-default** region | item 5 | indirect (cross-region factory bug 404s) | folds into E3–E7 env |

E1 (`init→status` journey) and E2(a) (smoke CLI), E2(c) (backend-direct roundtrip) stay as-is.
E2(b) (live MCP retrieve) is already slated for plumbing update by fix-plan Task 6 (drop
idvar/region env, add settings.json) — E3 builds on that same onboarding env.

## 4. Per-scenario detail

### J1 — Onboarding boot advertises the wired tool surface
**Steps:** `onboarding(clean_slate_home, fake)`; `async with mcp_session(env)` → `session.list_tools()`.
**Assertions:**
- `EXPECTED_TOOL_SUBSET <= names` (observe/retrieve/semantic_observe/record_use/session_bootstrap)
  **and** the rating/exposure tools (`memory.apply_session_ratings`, `memory.list_session_exposures`,
  `memory.credit`, `memory.semantic_retrieve`, `memory.semantic_update`, `memory.semantic_delete`)
  present.
- `SYNTHESIZE_TOOLS & names == set()` (supports_synthesis=False).
- **Episode tools + retention hidden (UD-1):** `{"memory.start_episode","memory.close_episode",
  "memory.reconcile_episodes","memory.list_episodes","memory.run_retention"} & names == set()`.
- `fake.requests == []` (boot is client-construction only).
- `{"observations","episodes","hook_errors"} <= _table_names(bm_home/"memory.db")` and
  `knowledge.db` exists (UD-4 doc-side: local DBs still migrated).

**regression_caught:** revert the `supports_episodes` gate in `tools.py`/`server.py:277` → episode
tools reappear; **or** revert `create_server`'s settings.json resolution → boot stays sqlite →
synthesize tools present and episode tools present (the sqlite surface).

### J2 — session_bootstrap hook reaches AgentCore, not sqlite
**Steps:** `onboarding(...)`; `fake.set_response("ListMemoryRecords", {"memoryRecordSummaries": []})`;
`run_hook("better_memory.hooks.session_bootstrap", {"source":"startup","session_id":"e2e-session-1","cwd":str(proj)}, env)`.
**Assertions:**
- `rc == 0`; envelope `hookSpecificOutput.hookEventName == "SessionStart"`; `additionalContext`
  non-empty and contains the reflection/semantic summary (`"Reflections"` / `"Semantic memories:"`
  per `backend.session_bootstrap`).
- `len(fake.requests_for("ListMemoryRecords")) == 4`; of those, exactly 3 carry `EPI-FAKE-0001` in
  `path` (reflections, polarity + status filters) and exactly 1 carries `SEM-FAKE-0001`.
- No memory-content sqlite write: `bm_home/"memory.db"` either absent or contains 0
  observation/semantic rows (a `hook_errors`-only migration is tolerated; assert
  `observations`/`semantic_memories` count 0 if the file exists).

**regression_caught:** revert `session_bootstrap.py` `build_backend` routing (Task 3) → hook uses
`SessionBootstrapService` on sqlite → `fake.requests == []` and sqlite rows appear.

### J3 — observe → retrieve_observations round-trip (dispatch anchor + read-after-write)
**Steps (one MCP session):** `onboarding(...)`;
`fake.set_response("CreateEvent", {"event":{"eventId":"evt-journey-1"}})`;
`fake.set_response("ListEvents", {"events":[{"eventId":"evt-journey-1","sessionId":"e2e-session-1","actorId":"e2e-project","payload":[{"conversational":{"content":{"text":"journey-obs-marker"}}}],"metadata":{"outcome":{"stringValue":"failure"},"theme":{"stringValue":"bug"}}}]})`.
- `observe = call_tool("memory.observe", {"content":"journey-obs-marker","outcome":"failure","theme":"bug"})`
- `obs = call_tool("memory.retrieve_observations", {"query":"journey-obs-marker"})`
**Assertions:**
- observe: `len(fake.requests_for("CreateEvent")) == 1`, path has `EPI-FAKE-0001`; returned
  `id == "evt-journey-1"` (the **AWS eventId**, proving dispatch switched — a local uuid means it
  hit sqlite).
- retrieve_observations: `len(fake.requests_for("ListEvents")) == 1`, path has `EPI-FAKE-0001`,
  body `sessionId == "e2e-session-1"` and `includePayloads` true; result row has
  `id == "evt-journey-1"`, `content == "journey-obs-marker"`, and **metadata flattened**
  `outcome == "failure"`, `theme == "bug"`.
- **No sqlite leakage:** post-session, `observations` table has 0 rows (or file absent).

*Note:* the fake does not persist writes, so hermetic read-after-write is a canned `ListEvents`
shaped to echo the observed id; **true persistence is E3's job**. Hermetic proves the two MCP tools
route to `backend.observe`/`backend.list_observations` and the response mapping is correct.
Exhaustive CreateEvent payload/metadata shape is owned by D4 `test_observe_create_event_wire_shape`.

**regression_caught:** revert `create_server` `remote=` wiring for
`memory.observe`/`memory.retrieve_observations` → local uuid returned, sqlite `observations` row
written, `CreateEvent`/`ListEvents` counts 0.

### J4 — memory.retrieve bucket fan-out
**Steps:** `onboarding(...)`; `fake.set_response("ListMemoryRecords", {"memoryRecordSummaries": []})`;
`call_tool("memory.retrieve", {})`.
**Assertions:** `len(fake.requests_for("ListMemoryRecords")) == 3`, all path `EPI-FAKE-0001` +
`reflections` namespace; buckets exactly `{"do":[],"dont":[],"neutral":[]}`. Polarity-filter
internals and the single-polarity restriction are owned by **D2**; J4 asserts only count + routing
+ empty-bucket shape under onboarding config.

**regression_caught:** collapse the polarity fan-out to one call, or revert dispatch → count ≠ 3.

### J5 — semantic_observe → semantic_retrieve round-trip (UD-2 merge)
**Steps (one session):** `onboarding(...)`;
`fake.set_response("BatchCreateMemoryRecords", {"successfulRecords":[{"memoryRecordId":"sem-journey-1"}],"failedRecords":[]})`;
`fake.set_response("ListMemoryRecords", {"memoryRecordSummaries": []})`.
- `sem = call_tool("memory.semantic_observe", {"content":"journey-sem-marker"})`
- `ret = call_tool("memory.semantic_retrieve", {})`
**Assertions:**
- observe: `len(fake.requests_for("BatchCreateMemoryRecords")) == 1`, path `SEM-FAKE-0001`, and
  `"EPI-FAKE-0001" not in request.text()` (never episodic); returned `id == "sem-journey-1"`;
  0 rows in local `semantic_memories`.
- retrieve: **exactly 2** `ListMemoryRecords` to `SEM-FAKE-0001` — one project namespace, one
  `general` namespace (UD-2 two-call merge). Parse the merged payload: each item carries stable
  keys with `project`, `created_at`, `updated_at` present as `None`.

**regression_caught:** drop the general-scope second call (UD-2 alternative) → 1 `ListMemoryRecords`
not 2; revert semantic dispatch → sqlite write/read, 0 SEM wire. sha256 `requestIdentifier` +
strategy-id routing owned by D4 `test_semantic_observe_sha256_request_identifier_and_routing`.

### J6 — rating loop (record_use, short-id guard, apply_session_ratings→SEM, empty exposures)
**Steps:** `onboarding(...)`. Use a ≥40-char reflection id `_REFL = "refl-journey-" + "0"*30 + "1"`
and a ≥40-char semantic id `_SEM = "sem-journey-" + "1"*30`.
- Canned `GetMemoryRecord` (EPI) returning `_REFL` with
  `{"useful_count":{"numberValue":2},"status":{"stringValue":"active"},"x-amz-agentcore-memory-recordType":{"stringValue":"EXTRACTED"}}`
  and canned `BatchUpdateMemoryRecords` success; `call_tool("memory.record_use", {"id":_REFL,"outcome":"success"})`.
- Short-id guard: `call_tool("memory.record_use", {"id":"refl-1","outcome":"success"})`.
- Canned `GetMemoryRecord`/`BatchUpdateMemoryRecords` for `_SEM`;
  `call_tool("memory.apply_session_ratings", {"ratings":[{"kind":"semantic","id":_SEM,"class":"cited"}]})`.
- `call_tool("memory.list_session_exposures", {})`.
**Assertions:**
- record_use: `GetMemoryRecord` + `BatchUpdateMemoryRecords` both to `EPI-FAKE-0001`; update
  metadata has **no** `x-amz-agentcore-memory-*` key; `useful_count.numberValue == 3`.
- short-id: result is an MCP `isError` (or a clear error payload) naming the id-length constraint,
  and **zero** additional wire requests (no `GetMemoryRecord`) — proves the Task-2 guard that avoids
  the ~20s `_retry_on_transient_404` stall inside the serialized dispatch loop.
- apply_session_ratings: lookup + update both to `SEM-FAKE-0001` (semantic-kind routing); applied
  count ≥ 1.
- list_session_exposures: payload `{"session_id": ...,"exposures": []}`; `fake.requests` unchanged
  (agentcore has no exposure log — the rating-model difference from sqlite C5).

**regression_caught:** remove the short-id guard → botocore 40-char client-side validation raises /
retry stall; revert record_use dispatch → sqlite `record_use` returns `{"ok":true}` with 0 wire;
break the semantic-kind branch in `credit_one` → lookup 404s against EPI. Full-snapshot strip
contract owned by D4 `test_record_use_strips_system_metadata_full_snapshot` /
`test_credit_one_semantic_kind_routes_to_sem_and_strips`.

### J7 — contextual_inject per-prompt honours settings.json activation
**Steps:** `onboarding(clean_slate_home, fake, BETTER_MEMORY_CONTEXT_INJECT_MODE="userprompt")`;
`fake.set_response("ListMemoryRecords", {"memoryRecordSummaries": [_DOCKER_REFLECTION_SUMMARY]})`
(reuse D6's fixture constant);
`run_hook("better_memory.hooks.contextual_inject", {"hook_event_name":"UserPromptSubmit","prompt":"how do I deploy with docker compose","session_id":"e2e-inject-session","cwd":str(proj)}, env)`.
**Assertions:** `rc == 0`, `"Traceback" not in err`; envelope `additionalContext` contains
`<project-memory` and `refl-fake-docker`; wire: `sum("EPI-FAKE-0001" in r.path) == 3` and
`sum("SEM-FAKE-0001" in r.path) == 1`.

The load-bearing difference from **D6-A** (which force-sets `BETTER_MEMORY_STORAGE_BACKEND=agentcore`):
here the per-prompt hook reaches AWS with **no backend env var**, proving `contextual_inject`'s
resolver honours settings.json.

**regression_caught:** revert `contextual_inject.py` to env-only backend resolution → settings.json
ignored → empty envelope, 0 wire.

### J8 — session_close closure under activation (sequence terminus)
**Steps:** `onboarding(...)`;
`run_hook("better_memory.hooks.session_close", {"session_id":"e2e-session-1","cwd":str(proj),"hook_event_name":"Stop"}, env)`.
**Assertions:** `rc == 0`, `out == ""`, `"Traceback" not in err`; exactly 1 `CreateEvent`,
role=OTHER, path `EPI-FAKE-0001`; one `*_session_end_*.json` spool marker with
`event_type=="session_end"`; `not (bm_home/"memory.db").exists()`.

This is the terminal narrative step under the onboarding config. The **env-gate matrix**
(env=None+settings→fires vs env=None+no-settings→silent skip, plus env-precedence + json-region
signing) is owned by **D5** post-Task-3 (`test_stop_hook_without_backend_env_skips_aws_but_writes_marker`
inverted, + the sqlite-safety sibling). J8 asserts only the closure+marker under settings.json and
defers the matrix to D5.

**regression_caught:** revert `session_close.py`'s env-first→`resolve_storage_backend()` gate →
env-absent returns before the try block → 0 wire (the exact pre-fix defect-4 behavior).

### E3 — live onboarding-config MCP observe → retrieve_observations
**Setup:** child home with `agentcore.json` (from `agentcore_throwaway_memories`) + `settings.json`
(via `write_backend_settings`); env built with `_live_env(home, CLAUDE_SESSION_ID=f"bm-int-{uuid}",
BETTER_MEMORY_PROJECT="bmintproj")` — **no** `BETTER_MEMORY_STORAGE_BACKEND`, **no** region/id env
vars (backend + region come from the files). `mcp_session(read_timeout=90s)`.
**Assertions:** `memory.observe` returns a real eventId; `memory.retrieve_observations` (current
session) returns exactly that event with `content`/`outcome`/`theme` surviving the round-trip; the
child home's local `memory.db` `observations` table has **0 rows**. Event read-after-write is
promptly consistent, so no async wait is needed.
**regression_caught:** dispatch not wired → observe returns a local uuid and writes a sqlite row;
region single-sourcing broken → cross-region 404 (only if run in a non-default region).

### E4 — live MCP semantic round-trip
`memory.semantic_observe` → `memory.semantic_retrieve` surfaces the record (project+general merge,
UD-2) → `memory.semantic_update` changes the text (re-retrieve confirms) → `memory.semantic_delete`
removes it (re-retrieve empty). All promptly consistent (record-level, not extraction). Zero local
`semantic_memories` rows.

### E5 — live record_use/credit on a real record id
Using E4's real ≥40-char semantic `memoryRecordId`, drive `memory.apply_session_ratings` (or
`memory.record_use` for episodic-shaped) with `class="cited"` → succeeds (full-snapshot update,
`x-amz-agentcore-memory-*` stripped — echoing them is a real AWS 400). This is the only live credit
against a **genuine** AWS record id.

### E6 — live bootstrap + contextual_inject reach AWS
Under the same onboarding env, run the `session_bootstrap` SessionStart hook and the
`contextual_inject` UserPromptSubmit hook; assert each returns a well-formed envelope and (via
server stderr / result shape) that they queried AWS namespaces, not sqlite. Counts are empty for
fresh memories (no reflections extracted yet — see skips).

### E7 — live session_close closure without env
Onboarding env (settings.json only, no `BETTER_MEMORY_STORAGE_BACKEND`); run the Stop hook; assert
it fires exactly one closure `CreateEvent(role=OTHER)` against the real episodic memory and writes
the spool marker. Cleanup: the closure event rides the throwaway memory's teardown.

**Region single-source (fix plan item 5):** run E3–E7 with `BETTER_MEMORY_TEST_AGENTCORE_REGION`
set to a **non-default** region (not eu-west-2). Because the region env var is deleted, the factory
and hooks read `agentcore.json`'s region; a factory regression that signs the default region
cross-regions the data plane and every call 404s — making the regression impossible to miss. Live
SigV4 region is not directly observable client-side, so this is asserted **indirectly** via request
success.

## 5. What T3 skips, and why (must be documented in the live module docstring)

- **Async reflection extraction is not promptly assertable.** After `observe`, AWS's built-in
  `episodicMemoryStrategy` extracts reflections **minutes** later. So no live scenario asserts that
  an observed fact appears in `memory.retrieve` (reflections buckets) or `session_bootstrap` counts
  within the test. E3 deliberately asserts `observe`→**`retrieve_observations`** (raw events,
  promptly consistent) — not `observe`→`retrieve`. E2(b)'s existing "buckets are exactly empty for
  fresh memories" assertion stands and is the correct contract.
- **`record_use` on an EXTRACTED reflection is not promptly testable** (no reflections exist yet).
  E5 uses a real **semantic** record id instead — created synchronously by `semantic_observe`.
- **Cross-session `list_observations` is out of scope** — `ListEvents` requires a sessionId; the
  backend only enumerates the current session (documented in `agentcore.py:319`). Each live test
  uses a unique per-run session id so readback sees exactly its own events.

## 6. Subsumes / complements matrix (no duplication)

| Existing D-suite class | Journey relation | Boundary rule |
|------------------------|------------------|---------------|
| D1 `TestServerBootToolsHidden` | **complement** (J1) | D1 keeps env-forced boot + sqlite-migration pin; J1 proves the same surface via settings.json activation + episode-tool hiding. |
| D2 `TestRetrievePolarityFanout` | **complement** (J4) | D2 owns polarity-filter internals + single-polarity restriction; J4 asserts count+routing under onboarding config only. |
| D3 `TestMcpDispatchGapPin` | **subsumed** (deleted by Task 2) | Its inverted half (dispatch reaches AWS; zero sqlite rows) lives in J3/J5/J6; Task 2's promoted per-op wire-shape tests keep the exhaustive metadata assertions. J3 asserts only id-provenance + count + no-sqlite. |
| D4 `TestBackendWireFidelity` | **complement** (J3/J5/J6) | D4 is backend-direct (no MCP); journey goes through MCP dispatch. Journey defers all exhaustive payload/metadata shape to D4. |
| D5 `TestSessionCloseClosureAndEnvGate` | **complement** (J8) | D5 owns the env-gate matrix + json-region signing; J8 is the settings.json-activation terminus (closure + marker only). |
| D6 `TestContextualInjectWireAndDegradation` | **complement** (J7) | D6-A force-sets env; J7 proves the per-prompt hook honours settings.json. D6 B/C own the missing-agentcore.json degradation (Task 1). |
| D7 `TestRegionSplitBrainPin` (→ renamed `test_server_and_hook_both_sign_json_region`, Task 1) | **defer** | Region convergence owned by renamed-D7 (hermetic) + live non-default-region run. Journey uses default region hermetically. |

**Highest duplication risk:** J3 vs Task 2's promoted D3-replacement wire tests. Rule for the
implementer: Task 2's promoted tests assert full `CreateEvent`/`BatchCreate`/`BatchUpdate`
payload+metadata shape under env-forced config; **J3/J5/J6 assert only** (a) returned id is the AWS
id not a local uuid, (b) exact operation + target-memory + count, (c) zero local sqlite rows, and
(d) cross-step id chaining. Do not restate metadata-field assertions in the journey.

## 7. File placement and gate integration

- **Hermetic:** new file `tests/e2e/test_agentcore_journey.py` (J1–J8), single-owner, mirroring
  `test_sqlite_journey.py`'s structure and helper set (`_single_json_dict`, `_table_names`,
  `_hook_envelope`). Depends on the Task-1 `write_backend_settings` helper existing in
  `_agentcore_env.py`. Auto-marked `e2e` by the conftest collection hook.
- **Live:** append E3–E7 to `tests/integration/test_agentcore_live_e2e.py` (already
  `pytest.mark.integration`, gated by `BETTER_MEMORY_TEST_AGENTCORE=1`), reusing
  `agentcore_throwaway_memories` / `agentcore_backend` / `agentcore_region` fixtures and the
  `_live_env` / `_write_throwaway_config` helpers. Add `write_backend_settings` to each child home
  so the onboarding path is the tested path.
- **Sequencing:** the journey file cannot be authored until Tasks 1–3 land (it asserts the *fixed*
  behavior). It slots into the fix plan's regression gate as an extension of the "Agentcore
  pin-flip proof" row; the live additions run in the Task-6 T3 validation phase alongside the
  existing E1/E2.
- **Isolation:** every subprocess env flows through `onboarding()`→`agentcore_env()`→`isolated_env()`,
  so the `real_home_canary` and `test_env_helper_contract` meta-tests bless the spawns unchanged.
