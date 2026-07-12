# Final Fix Plan — agentcore mode usable end-to-end

Branch: `fix/agentcore-onboarding` (off `main` @ ba2c824, includes merged e2e clean-slate suite PR #78).
Design doc: `C:/Users/gethi/source/better-memory/docs/superpowers/specs/2026-07-12-e2e-clean-slate-smoke-design.md` (section 4 defect pins).

---

## 0. USER-DECISIONS (confirm before Task 2 dispatch; recommended defaults will be used if unchallenged)

| # | Decision | Recommended default | Alternative |
|---|----------|--------------------|-------------|
| UD-1 | Episode tools (`memory.start_episode/close_episode/reconcile_episodes/list_episodes`) + `memory.run_retention` in agentcore mode | **Hide from tool list** via new `supports_episodes` param on `tool_definitions` (default `True`), mirroring the existing `supports_synthesis` gate. Handlers stay registered defensively. | Leave advertised as backend no-ops (returns empty ids/`[]`). |
| UD-2 | `memory.semantic_retrieve` in agentcore mode | **Two backend calls** (`scope_filter=None` project + `scope_filter='general'`) merged — sqlite parity. Payload keeps stable keys with `project: None, created_at: None, updated_at: None`. | One project-namespace call (cheaper, drops general-scope memories sqlite would return). |
| UD-3 | `agentcore init` activation | **Activates by default**: init writes `settings.json {"storage_backend": "agentcore"}` after `save_agentcore_config` succeeds; `--no-activate` flag for provision-only scripting. Running init expresses intent; the goal is init → next-steps → working. | Opt-in `--activate` flag (preserves init's provision-only contract, worse onboarding). |
| UD-4 | Defect 12 ("No SQLite traffic" claim) | **Doc-side**: rewrite the false claim; boot-time memory.db/knowledge.db migration stays (needed for hook_errors + knowledge tools; pinned by D1). Pin assertions do NOT flip, only docstrings. | Product-side (skip memory.db in agentcore boot) — high-regression, owned by nobody in this wave, rejected. |
| UD-5 | Region env var `BETTER_MEMORY_AGENTCORE_REGION` | **Delete** (Option C, json-authoritative). A differing env value never produced a working setup (cross-region requests against ids created elsewhere fail at AWS), and Option D would force session_close changes to avoid re-introducing the pinned split-brain. Not treated as blocking — but stated here for visibility since it removes a documented knob. | Demote to explicit override (Option D) — only if env-var compat is insisted on. |

Note: the previously flagged "should init write the ~/.claude.json env block" question is **mooted** by the settings.json mechanism — the MCP server and all hooks resolve the backend through `get_config()`/the shared resolver, so no `~/.claude.json` env edit is needed at all.

## Decisions taken per area

1. **Config surface (defects 1, 5, 6, 3-config):** Option **A + C + E**. Delete the vestigial idvar gate and both dead `Config` fields; factory signs with `agentcore.json`'s region (`ac_cfg.region`) and `Config.agentcore_region` + the env var are deleted; boto3 import wrapped with a `better-memory[agentcore]` hint (class stays `ModuleNotFoundError`, chained `from exc`); every `AgentCoreConfigError` gains remediation text. Option B (repoint gate at file existence) rejected — duplicates factory's check, adds I/O to `get_config()`.
2. **MCP dispatch (defect 2):** Option **A** — additive keyword-only `remote: StorageBackend | None = None` per handler, branch predicate `config.storage_backend == "agentcore"` at `create_server` (never truthiness/isinstance). Sqlite else-branch is the current code verbatim. Plus: lazy session-id re-resolution in `AgentCoreBackend.observe/list_observations`; `session_bootstrap` dict-unwrap shim; `semantic_retrieve` shape shim; `memory.retrieve` pre-hooks (spool drain + RetentionScheduler) gated on `remote is None`; episode/retention tool hiding per UD-1. Options B (all-through-backend) and C (adapters) rejected — B violates byte-identical sqlite, C is strictly more code.
3. **Hook env propagation (defect 4) + session_close region half of defect 3:** Option **A, variant A2** — dedicated `$BETTER_MEMORY_HOME/settings.json` with `storage_backend`, env always wins, written by init (UD-3). `session_close.py:94` region code is **unchanged** — it already reads `agentcore.json`'s region, which is the convergence target. Options B (infer from agentcore.json existence — silent backend switch, existence is not consent), C (document-only — pin cannot flip), D (bake env into hook commands — stale/quoting hell) all rejected.
4. **Docs + CLI UX (defect 7):** Option **A** truth-first sweep, executed **last** so every sentence is verified against landed code (sync-rule reflection 98056ebc: verify every factual token). Doc-side resolution of defect 12 per UD-4.

---

## 1. Ordered implementation tasks

**Execution model:** tasks run **strictly sequentially** (Task N+1 dispatches only after Task N's gate is green). File ownership is exclusive per task. Three e2e files are legitimately touched by more than one task; the handoff schedule below makes ownership unambiguous per task — no task may touch a class outside its row:

| File | Task 1 | Task 2 | Task 3 | Task 5 |
|------|--------|--------|--------|--------|
| `tests/e2e/_agentcore_env.py` | DUMMY_ID_VARS machinery deletion, region pin (:91) removal, ADD `write_backend_settings(bm_home)` helper | — | consumes helper only | — |
| `tests/e2e/test_agentcore_t2.py` | `TestRegionSplitBrainPin`, `TestContextualInjectWireAndDegradation` Cases B/C | `TestMcpDispatchGapPin`, `TestBackendWireFidelity`, `TestServerBootToolsHidden` | `TestSessionCloseClosureAndEnvGate` | docstrings only |
| `tests/e2e/test_agentcore_neg.py` | `TestPrehandshakeConfigErrors[idvar-gate]`, `TestCorruptAgentCoreJson`, `TestBoto3MissingImportSurface` | — | — | `TestMissingAgentCoreJson` docstring only |

The suite must be green after **every** task (pin flips are paired with the code change that causes them).

---

### Task 1 — Config surface: gate deletion, settings.json backend resolution, region single-source, friendly errors

**Confidence: 92%.** Deletion-heavy against wire-tested targets; the settings loader is new surface, mitigated by tests-first + conftest isolation guard below.

**Owns:** `better_memory/config.py`, `better_memory/storage/factory.py`, `better_memory/storage/agentcore_persistence.py`, `tests/conftest.py`, `tests/test_config.py`, `tests/storage/test_factory.py`, `tests/storage/test_agentcore_persistence.py`, `tests/hooks/test_contextual_inject.py` (lines 197-198 only), `tests/e2e/_agentcore_env.py`, `tests/e2e/test_tripwires_aws.py`, `tests/e2e/test_agentcore_neg.py` (three classes per table), `tests/e2e/test_agentcore_t2.py` (two classes per table).

**Regression tests to ADD FIRST (red before the change where applicable):**
- Settings loader unit tests in `tests/test_config.py`: (a) no env + no settings.json → `"sqlite"` (byte-identical default); (b) env `BETTER_MEMORY_STORAGE_BACKEND` wins over settings.json in both directions; (c) settings.json `{"storage_backend": "agentcore"}` + no env → `"agentcore"`; (d) malformed JSON / invalid value → `ValueError` naming the file with remediation, symmetric to the invalid-env error.
- **Conftest isolation guard** (`tests/conftest.py`): autouse fixture pinning `BETTER_MEMORY_HOME` to a tmp dir (the existing autouse strips only `CLAUDE_*`). Without this, any of the ~1300 unit tests calling `get_config()` reads the developer's real `~/.better-memory/settings.json` and flips to agentcore. Verified by the meta canary/env-bleed runs in the gate.
- Factory region unit test: mock `boto3.client`, assert `BotoConfig.region_name == agentcore.json's region` (closes the gap where only the e2e split-brain pin covers it).
- boto3-missing unit test around the factory ImportError wrap (hint text + `ModuleNotFoundError` class + chaining).
- Extend `tests/storage/test_agentcore_persistence.py` corrupt/schema/missing-field tests to assert remediation text present.

**Code changes:**
- `config.py`: delete lines 286-301 (idvar resolution + gate), fields :229-230, kwargs :317-318; delete `agentcore_region` (:37, :228, :283-284, :316). `_resolve_storage_backend()` becomes: env var if set (validated as today) → else read `$BETTER_MEMORY_HOME/settings.json` (missing → `"sqlite"`; invalid → `ValueError` with remediation) → export public `resolve_storage_backend()` for hooks. No memoization (docstring contract: per-call re-resolution).
- `storage/factory.py`: `:74` → `BotoConfig(region_name=ac_cfg.region)` (ac_cfg loaded 3 lines up); `_ConfigLike` Protocol shrinks to `storage_backend` only (:23-27 property deleted); wrap `:57-58` boto3 imports: `except ImportError as exc: raise ModuleNotFoundError("boto3 is required for the agentcore storage backend. Install it with: pip install 'better-memory[agentcore]'") from exc`. Mirror the hint in `hooks/session_close.py:50`'s lazy import — **deferred to Task 3** (that file's owner).
- `storage/agentcore_persistence.py`: append one shared remediation sentence to every `AgentCoreConfigError` raise (:72-75, :77-78, :80-85, :87-91, :100-103): *"Delete the file and re-run `better-memory agentcore init` (or use `--force`); existing AWS memories can be re-linked by hand-editing the file."* — the delete-then-init / `--force` wording is mandatory (init without `--force` refuses when the file exists, cli/agentcore.py:201-209). Schema-version case adds "this file may have been written by a newer better-memory".

**Pin inversions (node id → new assertion):**
- `tests/e2e/test_agentcore_neg.py::TestPrehandshakeConfigErrors::test_config_error_kills_server_prehandshake_before_any_disk_write[idvar-gate]` → **DELETE param** per its docstring's deletion contract. `[backend-typo]` stays and must remain green (backend-name validation config.py:264-271 untouched).
- `tests/e2e/test_tripwires_aws.py::TestDummyIdVarContainment` (both params) → **DELETE class** (its anti-vacuity half fails by construction once the literals go); drop lines 84-87 of `TestAgentcoreEnvLockdownContract::test_lockdown_contract_under_hostile_outer_shell` (region-env + dummy-var presence asserts).
- `tests/e2e/test_agentcore_neg.py::TestCorruptAgentCoreJson::test_corrupt_json_dies_loudly_naming_the_file[truncated]` → line 264 flips: `assert "agentcore init" in err`; keep AgentCoreConfigError / file-named / "failed to parse" asserts. `[schema-v2]` same inversion; keep `'unsupported schema_version=2'` / `'expected 1'`.
- `tests/e2e/test_agentcore_neg.py::TestBoto3MissingImportSurface::test_shadowed_boto3_yields_raw_traceback_without_install_hint` → lines 315-316 invert: `assert "better-memory[agentcore]" in err` and `assert "pip install" in err`; existing `ModuleNotFoundError` + `"No module named 'boto3'"` asserts stay green via the chained traceback; control-run assertions unchanged.
- `tests/e2e/test_agentcore_t2.py::TestRegionSplitBrainPin::test_server_signs_env_default_while_hook_signs_json_region` → rename `test_server_and_hook_both_sign_json_region`; all 3 server ListMemoryRecords requests assert `sigv4_region == "us-east-1"` (json region); hook CreateEvent half unchanged; drop the `DUMMY_EPISODIC_MEMORY_ID` wire-absence assert (:841, symbol deleted); keep `"MEM-EPI-JSON" in path` as id-provenance proof.
- `tests/e2e/test_agentcore_t2.py::TestContextualInjectWireAndDegradation::test_case_b_misconfig_clean_slate_silent_noop_with_stray_db` and `::test_case_c_misconfig_premigrated_db_records_one_hook_error_row` → misconfig re-expressed as **missing agentcore.json** (settings.json written via new helper, json never written); Case C hook_errors row: `exception_type` `'ValueError'` → `'FileNotFoundError'`, message asserts `'agentcore.json'` instead of the two dead var names; drop `DUMMY_ID_VARS` import (:790).
- Unit co-deletions: `tests/test_config.py::test_agentcore_mode_without_memory_ids_raises`, `::test_agentcore_mode_with_only_one_memory_id_raises[both params]`, `::test_agentcore_region_override` DELETE; `::test_storage_backend_agentcore_when_env_set` drops setenvs + region/id field asserts; `::test_storage_backend_defaults_to_sqlite` untouched green. `tests/storage/test_factory.py` FakeConfig loses region/id fields (:23-25, :103-104, :123-124). `tests/hooks/test_contextual_inject.py:197-198` setenvs dropped.
- `tests/e2e_meta/test_env_bleed.py` poison entries for the deleted vars: **do NOT delete** — they now prove the removed knobs are truly inert.

---

### Task 2 — MCP dispatch wiring (defect 2)

**Confidence: 90%** with embedded mitigations: (a) branch predicate keyed on `config.storage_backend == "agentcore"` string at `create_server`, `remote=None` otherwise — never backend truthiness/isinstance (sqlite session-id/project resolution semantics must not change); (b) sqlite else-branches are current code line-for-line; (c) `tests/e2e/test_sqlite_journey.py` + `tests/mcp/test_server_integration.py` (14 stdio roundtrips) run untouched as the byte-identical oracle; (d) mutation patches M1-M3 rebased and re-verified.

**Owns:** `better_memory/mcp/server.py`, `better_memory/mcp/tools.py`, `better_memory/mcp/handlers/observations.py`, `better_memory/mcp/handlers/semantics.py`, `better_memory/mcp/handlers/sessions.py`, `better_memory/mcp/handlers/reflections.py`, `better_memory/storage/agentcore.py`, `tests/storage/test_agentcore_unit.py`, `tests/mcp/` (new/extended files), `tests/e2e/test_agentcore_t2.py` (three classes per table), `tests/e2e_meta/mutations/*.patch` (rebase only).

**Regression tests to ADD FIRST:**
- Handler-level stub-backend tests in `tests/mcp/`: `semantic_retrieve` agentcore branch emits stable keys (`project/created_at/updated_at` present as `None`) and merges project+general (UD-2); `session_bootstrap` agentcore branch unwraps the backend **dict** (not dataclass) without AttributeError; `record_use` short-circuits ids shorter than 40 chars with a clear error (avoids the ~20s `_retry_on_transient_404` stall on observe-returned EVENT ids inside the serialized dispatch loop).
- `tool_definitions` gating tests: `supports_episodes` defaults `True` (all existing registration tests — test_episode_tools, test_retention_tool, test_semantic_tools, test_rating_tools, test_session_bootstrap_tool — call with defaults and must stay green); `False` hides the 4 episode tools + `memory.run_retention`.
- `tests/mcp/test_server_backend_dispatch.py`: agentcore-mode wiring test (handlers receive `remote`; sqlite mode receives `None`).

**Code changes:**
- Each affected handler gains keyword-only `remote: StorageBackend | None = None`; `create_server` passes `remote=backend if config.storage_backend == "agentcore" else None`. Routes: `memory.observe`→`remote.observe`; `memory.retrieve_observations`→`remote.list_observations`; `memory.record_use`→`remote.record_use` (with the short-id guard); `memory.semantic_observe`→`remote.semantic_observe`; `memory.semantic_retrieve`→two `remote.semantic_list` calls merged (UD-2); `memory.semantic_update`→`remote.semantic_update_text`; `memory.semantic_delete`→`remote.semantic_delete`; `memory.credit`→`remote.credit_one` (keep no-session guard); `memory.apply_session_ratings`→`remote.apply_session_ratings`; `memory.list_session_exposures`→`remote.list_session_exposures`; `memory.session_bootstrap`→`remote.session_bootstrap` with dict key access (`additional_context/project/source/episode_id/episode_action/semantic_count/reflections_counts`), pending_synthesis-adjacent fields omitted/None.
- `tools.py`/`server.py:277`: new `supports_episodes` param (UD-1); agentcore mode passes `False`. `reconcile_episodes` returns `[]` when remote is set.
- `reflections.py:81-97`: gate spool.drain + RetentionScheduler pre-hooks on `remote is None` (agentcore mode stops mutating local episode/retention rows; sqlite path lines move under a guard unchanged — sqlite journey oracle covers drain behavior). Documented residue: spool markers accumulate un-drained in agentcore homes (files, not sqlite; no correctness impact).
- `storage/agentcore.py`: lazy session-id re-resolution — when `_session_id is None`, re-resolve via env/marker (`mcp/_util.py` resolver) at first `observe`/`list_observations` call; still raise (with **zero wire requests**) when both resolve to nothing.
- Serialized-dispatch invariant (server.py:186-195) preserved: no handler branch mixes a backend call with a sqlite service write in one invocation.

**Pin inversions:**
- `tests/e2e/test_agentcore_t2.py::TestMcpDispatchGapPin::test_observe_semantic_record_use_never_reach_the_wire` → **DELETE**, replaced (per its docstring) by promoted MCP-level wire tests asserting: `memory.observe` → exactly 1 CreateEvent to EPI-FAKE-0001, sessionId `e2e-session-1`, USER role, content marker in payload, returned id == fake eventId; `memory.semantic_observe` → exactly 1 BatchCreateMemoryRecords to SEM-FAKE-0001 (never EPI), `sha256(content)[:80]` requestIdentifier; `memory.record_use(<40-char record id>, outcome=success)` → GetMemoryRecord + BatchUpdateMemoryRecords on EPI with `x-amz-agentcore-memory-*` keys stripped; **AND zero rows** in local `memory.db` observations/semantic_memories after the session (the inverted half of today's pin). Record ids in tests must be ≥40 chars (botocore client-side validation).
- `tests/e2e/test_agentcore_t2.py::TestServerBootToolsHidden::test_boot_hides_synthesize_tools_and_still_migrates_sqlite` → stays green (subset-based); **EXTEND**: episode tools + `memory.run_retention` absent in agentcore mode, present in the sqlite journey's tool list. Sqlite-migration assertions unchanged (UD-4 doc-side).
- `tests/e2e/test_agentcore_t2.py::TestBackendWireFidelity` (5 tests) → keep; update the class docstring ("unreachable over MCP today" no longer true). `::test_observe_without_session_id_raises_before_wire` → rewrite for lazy re-resolution: no env AND no marker (e2e homes have none) → still raises with ZERO wire requests.
- `tests/storage/test_agentcore_unit.py::test_observe_raises_value_error_when_session_id_is_none` (:207) + `::test_list_observations_raises_when_session_id_is_none` (:280) → rewrite: re-resolution from env/marker attempted first; raise only when both absent.
- `tests/e2e_meta/mutations/M1.patch` (targets mcp/server.py): rebase if hunks drift; confirm sentinel still flips red via `scripts/e2e_mutation_smoke.py`.

---

### Task 3 — Hooks: session_close settings-aware gate; session_bootstrap → build_backend

**Confidence: 91%.** Mitigations: env-first fast path preserves the zero-import sqlite exit; `tests/test_common.py::test_import_is_lightweight` is a hard constraint — the resolver import stays lazy inside the function body; new sqlite-safety sibling e2e test proves a once-provisioned sqlite user is not silently switched.

**Owns:** `better_memory/hooks/session_close.py`, `better_memory/hooks/session_bootstrap.py`, `tests/hooks/test_session_close_agentcore.py`, `tests/hooks/test_session_bootstrap.py`, `tests/e2e/test_agentcore_t2.py` (`TestSessionCloseClosureAndEnvGate` only).

**Regression tests to ADD FIRST:**
- `tests/hooks/test_session_bootstrap.py`: agentcore-mode test mirroring `test_contextual_inject.py::test_agentcore_mode_does_not_open_sqlite_connection` — the SessionStart hook must not open sqlite in agentcore mode (currently zero coverage anywhere).
- `tests/hooks/test_session_close_agentcore.py`: settings.json-resolved closure fires without env; corrupt settings.json → `record_hook_error` + `return False` + marker still written.

**Code changes:**
- `session_close.py:73`: replace raw `os.environ.get` with: env check first (any explicit env value keeps today's semantics, zero file I/O) → else `resolve_storage_backend()` (Task 1's public helper), wrapped in try/except → on any resolver error `record_hook_error` + `return False` (hooks never fail; marker still written). Region code `:89-94` **unchanged** (already json-authoritative — the defect-3 convergence target). Add the `better-memory[agentcore]` hint to the lazy boto3 import at `:50` for consistency with factory.
- `session_bootstrap.py:22/:70-71`: route through `build_backend` like `contextual_inject.py:110` when resolved backend is agentcore; sqlite path (direct `SessionBootstrapService`) byte-identical. Preserves C2's known-defect pin behavior (schema-less db degradation) untouched.

**Pin inversions:**
- `tests/e2e/test_agentcore_t2.py::TestSessionCloseClosureAndEnvGate::test_stop_hook_without_backend_env_skips_aws_but_writes_marker` → **INVERT** (the defect-4 pin, design §4 item 5): env `BETTER_MEMORY_STORAGE_BACKEND=None` but bm_home carries settings.json (via `write_backend_settings` helper from Task 1) → exactly ONE CreateEvent (role=OTHER, EPI-FAKE-0001 in path, SigV4-signed with agentcore.json's region) AND spool marker written AND no memory.db created.
- **ADD sibling** (sqlite-safety oracle): env absent AND no settings.json (agentcore.json may exist) → ZERO wire requests + marker written — existence is not consent.
- `::test_stop_hook_fires_one_closure_event_signed_with_json_region` → stays green unchanged (env-precedence + json-region regression guard).
- `tests/hooks/test_session_close_agentcore.py::test_env_guard_short_circuits_before_any_import` (:126) → rewrite: sqlite fast-exit (env=sqlite, or env unset + no settings.json) must not import boto3/`better_memory.config`; settings-file path resolves agentcore.
- M2/M3 mutation patches (session_bootstrap.py, contextual_inject.py targets): rebase check in the gate.

---

### Task 4 — CLI: `agentcore init` activation + next-steps rewrite; `status` effective backend

**Confidence: 93%.** No existing test pins next-steps text or init side effects beyond memory-ids/config (verified: `tests/cli/test_agentcore_init.py` asserts only memory-ids in stdout).

**Owns:** `better_memory/cli/agentcore.py`, `tests/cli/test_agentcore_init.py`, `tests/cli/test_agentcore_status.py`.

**Regression tests to ADD FIRST:**
- init writes `settings.json {"storage_backend": "agentcore"}` atomically (tmp + `os.replace`, same pattern as `save_agentcore_config`) after config save succeeds; `--no-activate` skips it; failure-path (memory creation fails) does not write it.
- Next-steps stdout pin: names settings.json + env precedence; asserts the text does **NOT** contain a bare `"Export BETTER_MEMORY_STORAGE_BACKEND"` step (the onboarding trap).
- `status` reports the EFFECTIVE backend and its source (`env` / `settings` / `default`) — makes the env-precedence surprise diagnosable.

**Code changes:**
- `_handle_init`: settings.json write per UD-3 (+ `--no-activate` argparse flag).
- Next-steps block (:312-315) rewritten: (1) "agentcore is now the default backend for `<home>` (persisted in settings.json; the `BETTER_MEMORY_STORAGE_BACKEND` env var still overrides — unset it or set it to agentcore). To revert: remove `storage_backend` from settings.json or set the env var to `sqlite`."; (2) restart Claude Code; (3) `better-memory agentcore status` then `smoke`, with a caveat line that smoke validates AWS credentials/wire, not MCP registration.
- `_handle_status`: effective-backend + source line.
- Custom-home caveat (installer injects `BETTER_MEMORY_HOME` only into the MCP server env; hooks fall back to `~/.better-memory`) — unchanged behavior, documented in Task 5.

**`better_memory/cli/install_hooks.py` is UNTOUCHED** (variant A2 requires zero installer changes); `tests/cli/test_install_hooks.py` (~40), `tests/e2e/test_install_hooks.py` (8), `tests/e2e/test_setup_sh.py` passing untouched is the proof.

---

### Task 5 — Docs sweep (LAST, verified against landed code)

See section 4 below. **Confidence: 95%** (zero code risk; single risk is factual drift, mitigated by running last + token-by-token verification per the standing 0.95-confidence sync rule).

### Task 6 — Validation phase

Owns `tests/integration/test_agentcore_live_e2e.py` (drop idvar setenvs :327-328 and the split-brain-dodging `BETTER_MEMORY_AGENTCORE_REGION` :315-319; add settings.json to the child home). Runs the full gate (section 3) + mutation smoke + T3 live run (section 5).

---

## 2. Regression gate definition

Run after **every** task (inner loop) and in full at validation:

| Gate | Command | Purpose |
|------|---------|---------|
| Inner loop (per fix commit) | `pytest tests/test_config.py tests/storage tests/mcp tests/hooks tests/cli -q` | config gate, factory, dispatch, hooks, CLI fast feedback |
| Full default suite | `pytest -q` (~1300 tests: unit + mcp + storage + hooks + cli + e2e T1/T2 hermetic + e2e_meta fast incl. real_home_canary) | **primary gate — 100% green with pins flipped** |
| Agentcore pin-flip proof | `pytest tests/e2e/test_agentcore_t2.py tests/e2e/test_agentcore_neg.py tests/e2e/test_tripwires_aws.py tests/e2e/test_hooks_contracts.py -q` | D1-D7 + negatives + lockdown tripwires pass in INVERTED form |
| Sqlite byte-identical proof | `pytest tests/e2e/test_sqlite_journey.py tests/e2e/test_sqlite_negative.py tests/e2e/test_install_hooks.py -q` | default path unchanged, installer untouched — **these files ship with ZERO edits** |
| Isolation proofs (MANDATORY this wave) | `pytest tests/e2e_meta/test_canary_home.py tests/e2e_meta/test_env_bleed.py -q` | settings.json adds a real-home file dependency; poisoned idvar/region vars proven inert post-removal |
| Suite-power validation (REQUIRED) | `python scripts/e2e_mutation_smoke.py` | M1/M2/M3 target files all edited this wave — rebase drifted patches, confirm each sentinel flips red |
| T3 live AWS (validation phase only) | `BETTER_MEMORY_TEST_AGENTCORE=1 pytest -m integration tests/integration/test_agentcore_live_e2e.py` | section 5; ~5-8 min, real cost, run ONCE after T3 plumbing update |

---

## 3. Docs task — per-file edit list (Task 5)

- **website/agentcore-setup.md**: reorder Initialise — `better-memory agentcore init --region <r>` FIRST, no export step; explain settings.json activation + env-var precedence/override + revert path; rewrite `:70` → "Memory data lives in AWS — observations, reflections, semantic memories, and reinforcement all go to Bedrock AgentCore. A local memory.db is still created for hook-error logging and knowledge.db for knowledge tools; no memory content is stored in them."; `:73` closure bullet names the settings.json mechanism; add at `:50` that agentcore.json's region is the single source of truth for all clients; custom-`BETTER_MEMORY_HOME` caveat (hooks fall back to `~/.better-memory`); keep anchor/heading text stable (inbound links from configuration.md).
- **website/troubleshooting/agentcore.md**: `:51` region section — now TRUE; drop the env-var mid-flight paragraph → "region changes require re-init (or hand-edit agentcore.json)"; extend `:39-47` closure checklist: effective backend via `agentcore status`, env-var-overrides-settings pitfall; add two new sections quoting the exact landed error strings: boto3 missing → `pip install 'better-memory[agentcore]'`; corrupt agentcore.json → delete + re-init / `--force`.
- **website/configuration.md**: `:16` `BETTER_MEMORY_STORAGE_BACKEND` row — note settings.json fallback and that env wins; **delete `:17` `BETTER_MEMORY_AGENTCORE_REGION` row** (var removed) + one-line migration note; `:72` session_close description gains the agentcore closure event; add settings.json to any file-layout section.
- **website/architecture.md**: `:3` → "backed by a pluggable storage backend — a single SQLite database by default"; `:30`/`:35` hardcoded `eu-west-2` → "region from agentcore.json"; add comparison-table row "Local files | memory.db + knowledge.db | hook-error log + knowledge index only (no memory content)".
- **website/mcp-tools.md**: episode no-op notes (`:73,:92,:99,:106`) → update per UD-1 (tools hidden in agentcore mode, like the synthesize notes `:177,:192`); one-line note that observation/semantic/rating tools dispatch to AgentCore in agentcore mode.
- **README.md**: `:5` → minimal qualifier "(with the default sqlite backend)"; `:39` → describes settings.json activation via init, env var as override; add `BETTER_MEMORY_STORAGE_BACKEND` row to the `:163` env table; re-verify the "22 tools" count at `:214` + `website/index.md:192` (this wave adds/removes no Tool registration — count-neutral, but counts drifted twice historically).
- **docs/superpowers/specs/2026-05-24-agentcore-storage-backend-design.md:433**: SUPERSEDED annotation on the idvar-env-var row (per reflection bd0dadda — only place the ID vars were ever documented).
- **docs/superpowers/specs/2026-07-12-e2e-clean-slate-smoke-design.md §4**: per-item "FIXED in <PR>" annotations so future agents don't re-pin old behavior.
- **e2e docstrings** (docstring-only edits): `tests/e2e/test_agentcore_neg.py::TestMissingAgentCoreJson` ("false 'No SQLite traffic' docs claim" clause → documented behavior), `tests/e2e/test_agentcore_t2.py:98` equivalent.

Coordinate merge risk: `cli/agentcore.py` is owned by Task 4, not this task — next-steps text lands there; docs quote it verbatim after it lands.

---

## 4. T3 live run — what it must prove (Task 6, after all fixes land)

Update `tests/integration/test_agentcore_live_e2e.py` first (drop dead idvar/region env plumbing; child home gets settings.json), then one run must demonstrate against **real AWS**:

1. **Onboarding path is the tested path**: child process configured exactly as the printed next-steps produce — agentcore.json + settings.json from `agentcore init`, **no** `BETTER_MEMORY_STORAGE_BACKEND` and **no** id/region env vars in the child env.
2. **observe → AWS round-trip over MCP** (new — non-tautological now that dispatch is wired): `memory.observe` via MCP stdio returns a real eventId; the content is subsequently retrievable via `memory.retrieve`/`memory.retrieve_observations` from AgentCore (allowing extraction latency), and **zero rows** land in the child home's local `memory.db` observations table.
3. **semantic round-trip over MCP**: `memory.semantic_observe` → record visible via `memory.semantic_retrieve` (project+general merge per UD-2); `semantic_update`/`semantic_delete` take effect on AWS.
4. **record_use on a real record id** succeeds (full-snapshot update, system keys stripped) — using a genuine ≥40-char AgentCore record id.
5. **Region single-source**: all requests (server plane and hook plane) hit the region written in agentcore.json — run in a non-default region (not eu-west-2) to make a factory regression impossible to miss.
6. **Hook closure without env**: Stop hook fires exactly one CreateEvent(role=OTHER) closure with only settings.json driving the backend choice; spool marker written.
7. **session_bootstrap / contextual_inject** reach AgentCore, not sqlite, in the same configuration.
8. Cleanup: created events/records deleted or the test memories torn down (existing orphan-cleanup patterns in the T3 suite).

---

## Cross-cutting sequencing invariants (hard rules)

- Task 1 (gate deletion) MUST land before or with anything that activates settings.json — a surviving gate + file-resolved agentcore = ValueError in every hook and at server boot (user worse off than before).
- Tasks 1-4 ship in **one PR/release** — settings.json without dispatch wiring makes the split-brain more visible (hooks on AWS, observe on sqlite).
- Docs task runs strictly last; every rewritten sentence verified against landed code.
- `tests/e2e/test_sqlite_journey.py`, `tests/e2e/test_sqlite_negative.py`, `tests/cli/test_install_hooks.py`, `tests/e2e/test_install_hooks.py`, `tests/e2e/test_setup_sh.py` ship with zero edits — they are the backward-compat proof.
- After merge, record the wave's non-obvious findings to better-memory per the mandatory record triggers (idvar gate vestigiality, settings.json precedence design, record_use id-domain stall).