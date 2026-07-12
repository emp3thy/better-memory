# better-memory E2E Smoke-Test Design — Final (Phase 2 Synthesis)

Goal: prove a **brand-new user** (no `~/.better-memory`, no MCP registration, no hooks, no DB) can set up and use better-memory in both sqlite and agentcore modes — without any test ever touching real user data, real AWS, or the network.

Tiers: **T1** hermetic sqlite (default run) · **T2** hermetic agentcore via `AWS_ENDPOINT_URL` fake (default run) · **T3** live AWS (gated: `integration` marker + `BETTER_MEMORY_TEST_AGENTCORE=1`).

All scenario edits from the judge round are applied; scenarios killed by both judges are dropped; single-judge kills are folded/trimmed per the surviving judge's critique; all judge-proposed gap scenarios are incorporated; cross-lens overlaps are deduplicated to the stronger variant.

---

## 1. Final Scenario Catalog

### A. Installer surface (`install_hooks`) — T1

| id | tier | mode | purpose | regression caught |
|---|---|---|---|---|
| `e2e-install-fresh-clean-slate` | T1 | both | Truly clean slate (no `.claude/`, no `.claude.json`): subprocess install writes both targets with exactly 5 hook entries, quoted interpreters (paths **with embedded space**), correct py/pyw split, matchers, async flags, `mcpServers` env block; zero DB/side writes; symlinks capability-probed. | needs_stdout interpreter swap (pythonw silently kills additionalContext); dropped interpreter quoting; missing UserPromptSubmit/PreToolUse merge branches; installer growing a DB-creating side effect. |
| `e2e-install-idempotent-rerun` | T1 | both | Runs 2 and 3 are **byte-identical** for both targets; better-memory entry count stays exactly 5. | REMOVE-pass predicate drift (exact-match vs substring) causing duplicate hook groups appended per re-run; serialization drift. |
| `e2e-install-foreign-config-preserved` | T1 | both | Seeded foreign servers/hooks/top-level keys + user's custom `BETTER_MEMORY_HOME` survive; command path refreshed; **legacy `session_start`/`session_retrieve` entries scrubbed** while co-resident user hooks in the same group survive (legacy seeds folded in per judges — no standalone legacy scenario). | `env.setdefault` → unconditional assignment (repoints user data dir); dict rebuild dropping `model`/foreign events; `_LEGACY_HOOK_MODULES` deletion; group-level (vs entry-level) removal. |
| `e2e-install-malformed-claude-json-refused` | T1 | both | Malformed `~/.claude.json` (first target — direction existing tests don't cover): exit 1, path+lineno+'Fix the file then re-run' on stderr, **both** files byte-unchanged, no backups, no `*.tmp` litter. (Reframed per judge: the per-target-loop ordering trap is owned by the existing malformed-settings test; this pins the swallow-and-treat-as-`{}` hazard for `.claude.json`, whose loss would destroy the user's whole MCP config.) | catch-JSONDecodeError-continue-with-`{}` change atomically replacing the user's `.claude.json`. |
| `e2e-install-backup-before-overwrite` | T1 | both | Backups land in `$BETTER_MEMORY_HOME/install-backups/` with timestamped names containing the **pre-run bytes**; dir auto-created. | `_backup` call moved after `_atomic_write` (backup-of-result destroys the only rollback artifact); backup location/name-format drift docs point users at. |
| `e2e-install-symlink-oserror-fallback` | T1 | both | Driver patches `Path.symlink_to` to raise `OSError(1314)`: exit 0, ≥2 `WARN skill symlink skipped` + `Developer Mode` on stderr, both configs still fully written. | Narrowing/removing the `except OSError` — total install failure for non-Developer-Mode Windows (symlinks run *before* JSON writes). |
| `e2e-install-symlink-replacement-ladder` (gap) | T1 | both | Capability-probed: pre-existing wrong symlink / plain file / real dir with sentinel at the skill path each get replaced by a correct symlink; sentinel gone (pins the documented no-backup destructive contract); no WARN; already-correct link is a no-op (lstat compare). | Removing pre-clearing → `FileExistsError` (an OSError) silently converts every upgrade into perpetual "WARN skipped" — skills never update again. |

### B. `setup.sh` surface — T1 (skipif bash absent; containment per harness gap-1)

| id | tier | mode | purpose | regression caught |
|---|---|---|---|---|
| `e2e-setup-headless-decline-completes` | T1 | both | `printf 'n\n' \| bash scripts/setup.sh` runs to completion: layout dirs (`spool/`, `knowledge-base/{standards,languages,projects}`), install_hooks invoked, `win_path`-correct command in `.claude.json` (regex on decoded backslash/drive form), py/pyw split survives bash→argparse. **Fixes applied:** fake-`curl`-exits-1 shim (+ no-op `ollama`) forces the daemon-unreachable warn-and-continue branch on every host — no network pull ever; env carries SYSTEMROOT/COMSPEC/TEMP/TMP/UV_CACHE_DIR(or LOCALAPPDATA); bash-version probe for patsub_replacement backslash semantics; assert `Dependencies installed.` before `Runtime layout ready`. | `win_path` regressions (MSYS `/c/...` handed to Claude Code); mkdir-layout/install stage removal or reordering; `set -e`-unsafe edit to the decline branch. |
| `e2e-setup-eof-aborts-all-or-nothing` | T1 | both | `bash scripts/setup.sh < /dev/null`: exit 1 at the Ollama `read -rp`, **before** layout and install_hooks — no `.claude.json`, no settings, no spool. Skip-guard probes both ollama-detection paths (self-skips on this dev machine; runs on ollama-free CI — documented). Asserts `Dependencies installed.` + `Ollama not found` so an earlier uv-stage death cannot false-pass. | "Do the important stuff first" reordering → headless failure leaves hooks registered against a setup the user believes failed; prompt gaining an EOF-safe default (deliberate contract flip). |
| `e2e-setup-default-home-derivation` (gap) | T1 | both | Decline flow with `BETTER_MEMORY_HOME` **absent**: default `$HOME/.better-memory` derived, propagated via `--home` into `mcpServers` env and backup location. | Dropping the `${VAR:-default}` fallback / default dir rename / wrong `--home` plumbing — invisible while every other test exports the var. |
| `e2e-setup-install-hooks-failure-propagates` (gap) | T1 | both | Malformed seeded `.claude.json` + decline flow: exit 1, `install_hooks failed` + `aborting` + underlying `<path>:<lineno>` in output, no final `Done.`; `spool/` EXISTS (layout precedes install — stage-order pin); settings.json absent, `.claude.json` bytes unchanged. | `\|\| true` / dropped subshell status masking an install failure as "Done." |

Containment (harness gap-1, embedded): setup.sh tests run against a **copy of the repo** (or `UV_PROJECT_ENVIRONMENT`+`UV_CACHE_DIR` redirect) with a warm host uv cache; assert the real repo `.venv/pyvenv.cfg` hash unchanged and all writes landed under tmp.

### C. sqlite journey — T1

| id | tier | mode | purpose | regression caught |
|---|---|---|---|---|
| `e2e-sqlite-first-boot-migrates-tools-knowledge` | T1 | sqlite | First `python -m better_memory.mcp` on a **nonexistent** home: initialize succeeds offline (poisoned OLLAMA_HOST + `EMBEDDINGS_BACKEND=sqlite`), full tool subset incl. both synthesize tools, both DBs created+migrated (table superset incl. trigram FTS), `knowledge-base/` NOT auto-created (defect pin). **Folded gap-3:** `knowledge.search` + `knowledge.list` return well-formed empty results, not errors, on the unindexed corpus. | Clean-slate-breaking migration; `apply_migrations` moved out of `create_server`; `supports_synthesis` gating regression; knowledge handlers raising on empty index (breaks every fresh install's mandated startup `knowledge_search`). |
| `e2e-sqlite-hook-before-server-degraded` (merged set1+set3) | T1 | sqlite | KNOWN-DEFECT PIN: SessionStart hook on a virgin home → exit 0, empty stderr, single fallback-directive envelope (prefix-match `OperationalError: no such table:`), schema-less `memory.db` (0 tables), **no** `runtime/sessions` marker, no other dirs, silent `hook_errors` no-op. | Flips red on the intended fix (hook-side migrations / hoisted marker write) forcing a deliberate contract update; never-fail wrapper regressions that would break every new user's session start. |
| `e2e-sqlite-hook-first-then-server-heals` (gap-1; absorbs marker-bridge scenario) | T1 | sqlite | The real new-user **sequence**: degraded hook → server boot onto the pre-existing 0-table WAL file heals it (migrations apply) → `memory.session_bootstrap` MCP tool works (`episode.action == 'opened'`) → re-run hook: `Episode: reused`, marker written with content `e2e-session-1`, exactly 1 open episode row, full table superset. | Migrations failing on an already-existing empty DB; fallback directive pointing at a drifted tool name; episode-dedup and marker-bridge regressions (folded from the split-verdict marker-bridge scenario). |
| `e2e-sqlite-observe-retrieve-record-use-offline` (absorbs record_use scenario per both judges) | T1 | sqlite | Full offline data loop in one server session: observe → trigram retrieve (obs_id surfaces) → bucket shape `{do,dont,neutral}` → `record_use(success)` on obs A **and** `record_use(failure)` on obs B → not-found id returns isError → post-exit re-open DB: A has used_count=1/validated_true=1/score≈+1.0, B has validated_false=1/score≈−1.0, timestamps non-NULL. Observe wall time < 30s. | sqlite-embeddings bypass loss (Ollama path reintroduced → hangs/EmbeddingError for every Ollama-less user); migration-0011 trigger drop; success/failure branch swap (failure leg per judge); dropped commit losing ratings at process exit (post-exit read). |
| `e2e-sqlite-session-close-rate-then-marker` | T1 | sqlite | Two-fire Stop sequence with a real rating turn: fire 1 blocks with RATE_MEMORIES (memory id + `rate-session-memories` in directive), **zero** session_end markers; server spawned with `cwd=proj_dir` (or `CLAUDE_PROJECT_DIR` on both sides) and `CLAUDE_SESSION_ID` deleted → session resolved via marker file; ratings payload exactly `[{'kind','id','class':'ignored'}]`; assert `sum(result['applied'].values()) >= 1`; exposures asserted on real wire shape (no `rated_at` field); fire 2: **empty stdout**, exactly one session_end marker. | Marker-written-while-blocking (double session_end / premature synthesis); unrated-dedupe query silently matching nothing; `rated_at` never stamped → infinite Stop-block loop; marker-bridge break in `apply_session_ratings`. |
| `e2e-sqlite-spool-drain-synthesize-loop` | T1 | sqlite | session_end marker + next-session `memory.retrieve` drains spool (top-level non-recursive glob), closes background episode as `no_outcome`; `start_episode` payload `pending_synthesis['pending'] >= 1`; get_context carries obs_id; apply asserts `payload['ok'] is True`, `payload['counts']['created'] == 1`, `queue` keys present; second get_context excludes E; final retrieve surfaces the reflection in `dont`. Session B env carries `CLAUDE_SESSION_ID=e2e-session-2` explicitly. | Drain no longer closing episodes (learning loop strands forever); queue-count drift zeroing the CLAUDE.md synthesize trigger; lost already-synthesized guard duplicating reflections; `pending_synthesis` field dropped from serializer. |
| `e2e-sqlite-contextual-inject-contract` (gap-2 set 1) | T1 | sqlite | (a) virgin home + **default mode** (var unset = 'both'): exit 0, never a traceback; (b) migrated home + seeded matching memory: injection envelope + `session_memory_exposure` row `source='contextual'`; (c) `mode=off`: exit 0, exact empty envelope, `BETTER_MEMORY_HOME` **not created** (verified zero side effects). | Never-fail wrapper regression breaking *every prompt* for every new user; off-mode gaining side effects; injection/exposure recording break. |
| `e2e-sqlite-ollama-absent-default-backend` | T1 | sqlite | Default (ollama) embeddings, `OLLAMA_HOST=127.0.0.1:9`: boot succeeds with stderr warning; `memory.observe` → in-band isError EmbeddingError (timed **only around the call**, budget **< 30s**, measured baseline 7.7s noted); `knowledge.list` still works; KNOWN-DEFECT PIN: orphan episode row committed (episodes=1, observations=0). | Fatal startup probe (bricks Ollama-less installs); EmbeddingError escaping as server crash; retry/timeout inflation (~90s stalls); silent trigram auto-fallback (deliberate change flag); orphan-episode fix lands. |

### D. agentcore hermetic — T2 (fake `AWS_ENDPOINT_URL` endpoint; verified boto3 1.43.14 seam)

| id | tier | mode | purpose | regression caught |
|---|---|---|---|---|
| `e2e-ac-server-boot-tools-hidden` | T2 | agentcore | Real server boots from fabricated `agentcore.json` against the fake endpoint; synthesize tools hidden; sqlite DBs still migrated (docs-contradiction pin). Fake routing derived from **gzipped** `service-2.json.gz` botocore models (judge fix). | `supports_synthesis` flip re-exposing no-op tools; broken factory agentcore branch; boot growing an AWS call. |
| `e2e-ac-retrieve-polarity-fanout` | T2 | agentcore | The **flagship wired-path** test: MCP `memory.retrieve` → exactly 3 `ListMemoryRecords` (episodic memory, `reflections` namespace), order-insensitive polarity set `{do,dont,neutral}` + `status=active` filter each; buckets are exact empty lists; optional single-polarity leg (`polarity='do'` → exactly 1 request). Semantic-search leg **deleted** (unwired at MCP layer, per both judges). | Fan-out collapsed into one unfiltered list; dropped status filter resurfacing retired reflections; ReflectionToolHandlers rewired off the backend. |
| `e2e-ac-mcp-dispatch-gap-pin` (new, per judges) | T2 | agentcore | KNOWN-DEFECT PIN of the **dispatch gap**: in agentcore mode, `memory.observe` / `memory.semantic_observe` / `memory.record_use` produce **zero** wire requests and read/write local `memory.db` (row lands in sqlite; observe returns a local uuid). Docstring cites `server.py:250`; delete when handlers are wired to the backend. | Flips loudly the day dispatch wiring lands, forcing wire tests to be promoted; guards against anyone claiming AWS coverage from these tools. |
| `e2e-ac-backend-wire-fidelity` (re-homed, per judges) | T2 | agentcore | In-process `AgentCoreBackend` with **real boto3 clients** against the fake (no mocks — real botocore serialization): CreateEvent shape (EPI memory routing, sessionId/actorId, USER role, stringValue-only metadata, None-dropped keys, dummy env IDs never on the wire); semantic BatchCreate (`requestIdentifier == sha256(content)[:80]` computed independently, SEM routing, strategy id, initial counters); `record_use` + `credit_one` full-snapshot updates with `x-amz-agentcore-memory-*` keys **stripped** (currently untested anywhere; live-AWS-400 class); observe with no session id → ValueError, zero wire requests (set-3 gap-2 folded here). | Dedup-identifier scheme change; semantic/episodic routing swap; reserved-prefix strip removal (real-AWS 400); env-ID consumption (would put dummies on the wire); uuid4 session fallback fabricating identities. |
| `e2e-ac-session-close-closure-and-env-gate` (merged + set-3 gap-1) | T2 | agentcore | Stop hook fires exactly one role=OTHER closure CreateEvent (real client construction — zero coverage today), SigV4 region == **agentcore.json's** region, spool marker still written; Case B (`BETTER_MEMORY_STORAGE_BACKEND` absent — exactly what the installer produces): exit 0, zero wire requests, marker **still written**, no `hook_errors` row (guard short-circuits pre-try). | Closure reorder/removal (silent AWS extraction latency loss); boto exception killing the marker; env-propagation fix landing (deliberate flip); guard condition change. |
| `e2e-ac-contextual-inject-wire-and-degradation` (merged set-2 gap-1 + set-3 inject) | T2 | agentcore | The only shipped per-prompt backend path: Case A happy — hook subprocess hits SEM/EPI on the fake, envelope contains injection; Case B misconfig (ID vars unset), clean slate — exit 0, empty envelope, zero wire, **stray schema-less `memory.db` created** (defect pin), no state/; Case C misconfig, pre-migrated DB — exactly one `hook_errors` row (`hook_name='contextual_inject'`, `exception_type='ValueError'`, column **`exception_message`** per migration 0005 — judge fix) containing both var names. | Per-prompt never-fail regression; SeenStore hoisted above `get_config` (state litter); gate removal flipping Case B/C (rewrite alongside fix); silent-degradation contract drift. |
| `e2e-ac-region-split-brain-pin` (merged set-2 gap-2 + set-3) | T2 | agentcore | KNOWN-DEFECT PIN: `agentcore.json` region=`us-east-1`, env region unset → (a) server `memory.retrieve` (the **wired** trigger — corrected from observe per dispatch-gap finding) signs SigV4 scope `eu-west-2` (env default) with `MEM-EPI-JSON` in path and `DUMMY-EPI` absent; (b) session_close hook signs `us-east-1` (json-derived). One test proves the two planes disagree. | Single-source-of-truth fix flips both halves visibly; factory consuming dummy env IDs; hardcoded endpoint_url breaking the env seam. |
| `e2e-ac-neg-prehandshake-config-errors` (parametrized: idvar-gate + backend typo; absorbs set-3 typo scenario per judge) | T2 | agentcore | Raw spawn, two cases sharing one helper: (1) KNOWN-DEFECT idvar gate — valid `agentcore.json`, ID vars unset → rc 1, stdout `''` (pre-handshake), ValueError text + unsatisfiable `agentcore init` remediation; docstring: delete with the dummy-var fixture when gate is fixed. (2) `BETTER_MEMORY_STORAGE_BACKEND=AgentCore` typo → rc 1, stdout `''`, `is not one of` + repr'd `'AgentCore'`. Both cases: **no memory.db created** (config validated before any disk write). | Gate fix/removal (inverted trap forcing workaround cleanup); silent sqlite fallback/coercion on invalid values; `get_config` moved past the handshake. |
| `e2e-ac-neg-missing-agentcore-json` (absorbs the SDK-client-experience scenario) | T2 | agentcore | The **enduring** negative (survives the gate fix), two levels: raw — rc 1, stdout `''`, `agentcore.json not found` + `better-memory agentcore init`, and `memory.db` migrated-before-backend-failure (sqlite_master > 0; ordering pin + docs-claim pin); SDK — `pytest.raises(McpError)` tight around `initialize()`, `error.code == -32000`, `Connection closed`, error text recovered via real-file `errlog`; contexts exit cleanly; never assert exit code via SDK. | Silent sqlite fallback on missing json (worst case); remediation text loss; half-alive server advertising tools with no backend; SDK upgrade changing the client-visible death shape. |
| `e2e-ac-neg-corrupt-agentcore-json` | T2 | agentcore | Truncated JSON and `schema_version: 2` both → rc 1, stdout `''`, `AgentCoreConfigError` naming the file; `failed to parse` / `unsupported schema_version=2; expected 1`; negative pin: **no** `agentcore init` remediation (product gap flag). | `load_agentcore_config` swallowing parse errors into None (fail-loud contract destroyed); unshipped schema bumps. |
| `e2e-ac-neg-boto3-missing` | T2 | agentcore | PYTHONPATH-shadow `boto3.py` raising ModuleNotFoundError: rc 1, stdout `''`, raw traceback, PRODUCT-GAP PIN: no `better-memory[agentcore]` / `pip install` hint (inverted assertion comment). Control run assertion is **content-based** (`ModuleNotFoundError` absent from control stderr — judge fix; never rc comparison). | Friendly-message fix landing (flip); boto3 becoming a top-level import breaking sqlite-mode plain installs; lazy-import contract break. |

### E. live AWS — T3 (integration marker + `BETTER_MEMORY_TEST_AGENTCORE=1`)

| id | tier | mode | purpose | regression caught |
|---|---|---|---|---|
| `e2e-live-init-status-journey` | T3 | agentcore | `agentcore init` (monkeypatched `bm_int_*` DEFAULT names, cleanup pre-registered) provisions two ACTIVE memories, writes schema-1 `agentcore.json` with distinct ids + non-empty strategy ids; re-init refuses (rc 1, `already exists`/`force`, file unchanged — money-safety pin); `status` subprocess reports both ids + ACTIVE. | Real-AWS strategy/indexedKeys shape drift; poll loop returning pre-ACTIVE; init writing json before confirmation; silent-clobber orphaning billable memories. |
| `e2e-live-smoke-retrieve-backend-roundtrip` (rebuilt per both judges) | T3 | agentcore | (a) `agentcore smoke` CLI passes against session-scoped throwaway memories (6-step data-plane loop vs real AWS wire validation — mocked-only today); (b) real MCP server boot with real creds + **explicit** `BETTER_MEMORY_AGENTCORE_REGION` (split-brain workaround, commented) → `memory.retrieve` live (wired path; empty `{do,dont,neutral}` — live filter/namespace validation); (c) direct `AgentCoreBackend.observe` → `list_observations` with unique marker: read-after-write + metadata survival (`outcome`/`theme` through the flattening). MCP observe round-trip **removed** (tautological-sqlite false positive per both judges). | AWS-side payload/enum/metadata validation drift the fakes structurally cannot see; ListEvents mapping regressions; live metadataFilters rejection. |

### F. Harness safety & suite meaningfulness (tests/e2e_meta + fixtures + scripts)

| id | tier | mode | purpose | regression caught |
|---|---|---|---|---|
| `meta-canary-home-run` | meta | both | Whole e2e suite executed under a seeded hostile canary home (invalid-sqlite `memory.db` sentinel); outer env strips all `BETTER_MEMORY_*/CLAUDE_*/AWS_*/OLLAMA_*` **case-insensitively** (judge fix) so an un-redirected fixture falls back into the canary; sha256 + file-set diff + mcpServers subset unchanged; explicit exit-5 vacuity assertion. | Any fixture forgetting USERPROFILE/HOME/BETTER_MEMORY_HOME — install_hooks would corrupt real `~/.claude.json` (no `--target` flag exists). |
| `real_home_canary` fixture | meta | both | Session-scoped autouse tripwire on every run: additive dot-canaries (xdist worker-id suffixed, stale-overwrite on setup), **semantic subtree comparison** of the better-memory mcpServers/hooks entries (not byte hash — live-session-safe, per judges), tmp-path smoking-gun check, install-backups/skills-set tell-tales; hash mismatch demoted to warning unless corroborated; env snapshot at module-import time covering both session-id var names. | A single new test with a hand-rolled env dict — immediate local failure instead of corrupted personal config days later. |
| `meta-env-bleed-poisoned-shell` | meta | both | Re-run T1 slice under a maximally hostile outer shell (backend=agentcore, POISON ids, decoy home, aggressive inject, poisoned AWS/Ollama): inner suite still green, decoy untouched, no poison strings in output. | Reintroduction of `{**os.environ, ...}` spawn patterns; unpinned `BETTER_MEMORY_PROJECT`/backend leaking into outcomes. |
| `e2e-ollama-zero-traffic-tripwire` | T1 | sqlite | Recorder server on OLLAMA_HOST across the full journey + both sync hooks: **zero requests**, with positive journey assertions (anti-vacuity). | Ollama probe/embed path becoming unconditional again — stronger than the `.invalid` poison (proves no attempt, not just survivable failure). |
| `e2e-aws-lockdown-tripwire` | T2 | agentcore | Helper-contract + round-trip: every request Host `127.0.0.1:*`, every SigV4 scope `Credential=bm-e2e-fake/`, credentials-file lockdown, IMDS disabled; dummy MEMORY_ID vars grep-pinned to the single helper + the one defect-pin test. | Dropping `AWS_ENDPOINT_URL` (real endpoint hit) or credential lockdown (real AKIA key signed) — red **before** anything is billed; workaround metastasis. |
| `meta-marker-tier-wiring` | meta | both | Default collection = all T1+T2, zero T3; `e2e` marker declared (no unknown-mark warning); `-m integration` without the env gate → green-**skipped**, never red/exit-5. | Hermetic test mismarked `integration` (silent coverage rot); marker declaration loss; skip-gate refactored to assert. |
| `meta-env-helper-contract` | meta | both | `isolated_env()` contract: HOME+USERPROFILE both set on every OS, case-insensitive key hygiene, SYSTEMROOT/COMSPEC/TEMP/TMP allowlist, child-process ground truth (`Path.home()`/`expanduser` == tmp in a real spawn); AST/grep check that every spawn site uses the helper; setup.sh/symlink skip-decoration checks. | POSIX contributor deleting the "redundant" USERPROFILE line → every Windows run torches the real profile; helper bypass via hand-rolled env dicts. |
| mutation smoke (M1–M4) | meta | both | See §5. Runs in a throwaway **git worktree** (judge fix: current tree has ~70 untracked scratch files; gate on `--untracked-files=no`); expected-node-id matching; one-other-test-passes; unpatched control run. | The suite itself decaying into a green-always ritual — including the safety harness (M4). |

Dropped (both-judge or fold-resolved): `t1-observer-hook-spools-tool-event` (redundant with `tests/hooks/test_observer.py`; three one-line additions go there instead — see task 11), `t1-install-legacy-entry-migration` (folded into foreign-config), `t1-bootstrap-first-opened-second-reused` (folded into heal sequence), MCP-level agentcore observe/semantic/record_use wire scenarios (false premise — re-homed to backend wire-fidelity + dispatch-gap pin), `neg-agentcore-observe-no-session-id` at MCP level (unreachable via MCP today — folded into backend wire-fidelity), MCP semantic_retrieve search leg (schema rejects `search`; unwired).

---

## 2. Harness Architecture

```
tests/e2e/
  conftest.py            # clean_slate_home, real_home_canary (autouse, session), mcp_session, run_hook
  _env.py                # isolated_env(tmp_home, **pins)  — THE single env choke point
  _agentcore_env.py      # agentcore_env(tmp, port) — extends isolated_env; ONLY location of dummy ID vars (FIXME)
  _fake_agentcore.py     # ThreadingHTTPServer fake; routing from botocore service-2.json.gz (gzip-decoded)
  test_install_hooks.py  # scenarios A1-A7
  test_setup_sh.py       # scenarios B1-B4 (skipif no bash; repo-copy containment)
  test_sqlite_journey.py # C1-C6
  test_hooks_contracts.py# C2, C7 (inject), degraded/heal
  test_sqlite_negative.py# C8 (ollama-absent)
  test_agentcore_t2.py   # D1-D7
  test_agentcore_neg.py  # D8-D11
  test_tripwires.py      # ollama zero-traffic, aws lockdown
tests/e2e_meta/
  test_canary_home.py    # meta-run
  test_env_bleed.py
  test_marker_wiring.py
  test_env_helper_contract.py
  mutations/M1..M4.patch
tests/integration/
  test_agentcore_live_e2e.py  # T3 (integration marker; reuses conftest throwaway memories)
scripts/e2e_mutation_smoke.py
```

**Key fixtures / helpers**

- `isolated_env(tmp_home, **pins)` — built from an **allowlist**, never `os.environ.copy()`: sets `HOME`, `USERPROFILE` (both, unconditionally, every OS), `HOMEDRIVE`/`HOMEPATH` (win), `BETTER_MEMORY_HOME=<tmp>/.better-memory`, `BETTER_MEMORY_PROJECT`, `CLAUDE_SESSION_ID`, `BETTER_MEMORY_EMBEDDINGS_BACKEND=sqlite`, `OLLAMA_HOST=http://does-not-exist.invalid:1`; preserves only `{PATH, SYSTEMROOT, COMSPEC, PATHEXT, TEMP, TMP, WINDIR, LANG, LC_ALL, PYTHONIOENCODING}`; drops all `CLAUDE_*/BETTER_MEMORY_*/AWS_*/OLLAMA_*` **case-insensitively**.
- `agentcore_env(tmp, fake_port)` — adds `BETTER_MEMORY_STORAGE_BACKEND=agentcore`, `BETTER_MEMORY_AGENTCORE_REGION`, `AWS_ENDPOINT_URL=http://127.0.0.1:<port>`, `AWS_ACCESS_KEY_ID/SECRET=bm-e2e-fake*`, `AWS_SHARED_CREDENTIALS_FILE`/`AWS_CONFIG_FILE` → nonexistent tmp paths, `AWS_EC2_METADATA_DISABLED=true`, removes `AWS_IGNORE_CONFIGURED_ENDPOINT_URLS`, and the two dummy `BETTER_MEMORY_AGENTCORE_*_MEMORY_ID` vars **in this one place only**, with a `# FIXME(idvar-gate)` comment: workaround for the dead gate at `config.py:293-301`; delete together with `e2e-ac-neg-prehandshake-config-errors[idvar]`.
- `fake_agentcore_endpoint` — local ThreadingHTTPServer, ephemeral 127.0.0.1 port bound before env construction; records `(method, path, headers, body)`; canned rest-json responses per operation; routing derived from the installed `botocore/data/bedrock-agentcore*/**/service-2.json.gz` (gzip-decoded). Plain HTTP; verified end-to-end at boto3 1.43.14.
- `mcp_session(params_env, errlog=None)` — `stdio_client` + `ClientSession(read_timeout_seconds=60)`; **always** includes `USERPROFILE`/`HOME` explicitly in `StdioServerParameters.env` (the SDK force-inherits them on Windows underneath `server.env`); `errlog` must be a real file with `fileno()` (never StringIO); supports `cwd=` for marker-bridge tests.
- `run_hook(module, payload, env)` — `subprocess.run([sys.executable, '-m', module], input=json.dumps(payload), ...)`, returns (rc, stdout, stderr).
- `real_home_canary` — autouse session tripwire (spec in §1F).
- Live-AWS: reuse `tests/integration/conftest.py` (`agentcore_throwaway_memories`, `bm_int_*` names, stale sweep, `BETTER_MEMORY_TEST_AGENTCORE_KEEP`).

**pyproject additions**

```toml
[tool.pytest.ini_options]
markers = [
  "integration: ...",                                  # existing
  "e2e: end-to-end clean-slate smoke tests (hermetic)",# new
]
# addopts unchanged: -m 'not integration'
```

---

## 3. Execution Model

| invocation | runs |
|---|---|
| plain `pytest` | T1 + T2 (all `tests/e2e`, hermetic, offline) + `real_home_canary` tripwire + `tests/e2e_meta` fast checks (marker wiring, env-helper contract). setup.sh tests self-skip without bash; symlink asserts capability-probed; `e2e-setup-eof-aborts` self-skips on hosts with Ollama installed (runs on CI). |
| `pytest tests/e2e_meta/test_canary_home.py` / `test_env_bleed.py` | CI isolation-proof jobs (each re-runs the suite in a subprocess — too slow for every local run). |
| `BETTER_MEMORY_TEST_AGENTCORE=1 pytest -m integration tests/integration/test_agentcore_live_e2e.py` | T3 live AWS (~5–8 min, real cost; opt-in only). Without the env var: green-skipped. |
| `python scripts/e2e_mutation_smoke.py` | Manual/nightly suite-power validation (§5); never in default runs. |

Offline guarantees on the default run: no DNS (`.invalid` / loopback literals only), no Ollama (zero-traffic tripwire), no AWS (endpoint + credential lockdown tripwire), no uv/network (setup.sh curl shim + primed/warm-cache preconditions).

---

## 4. Product Bugs Discovered — Pinned as Current Behavior (FIXME markers)

Each gets a test whose docstring names the defect and states the deletion/inversion condition. **These need product decisions/fixes; the tests will fail loudly when fixed:**

1. **Vestigial ID-var gate** (`config.py:293-301`) — `BETTER_MEMORY_AGENTCORE_{SEMANTIC,EPISODIC}_MEMORY_ID` are required but their values are consumed by nothing (IDs come from `agentcore.json`); the error's own remediation (`agentcore init`) cannot clear it. Every documented agentcore setup dies pre-handshake. → `e2e-ac-neg-prehandshake-config-errors[idvar]` + delete-together dummy-var fixture.
2. **MCP dispatch gap in agentcore mode** (judge-discovered, empirically verified): `memory.observe`, `memory.semantic_observe`, `memory.record_use`, `memory.retrieve_observations` write/read **local sqlite**, never AWS; only `memory.retrieve` (and hooks) reach `AgentCoreBackend`. Agentcore users' memories silently land in a local file. → `e2e-ac-mcp-dispatch-gap-pin`. *This contradicts the phase-1 journey map and is the highest-priority product finding.*
3. **Hook-before-server ordering** — first SessionStart on a clean slate creates a schema-less `memory.db`, never writes the session marker, error-logging silently no-ops. → `e2e-sqlite-hook-before-server-degraded`.
4. **Region split-brain** — runtime factory signs with env `BETTER_MEMORY_AGENTCORE_REGION` (default eu-west-2); CLI/session_close use `agentcore.json`'s region. Silent cross-region divergence. → `e2e-ac-region-split-brain-pin`.
5. **Hook env-propagation gap** — installer writes no env into hook commands; `session_close` agentcore closure silently never fires unless the user exports the backend process-wide. → `e2e-ac-session-close-closure-and-env-gate` Case B.
6. **contextual_inject misconfig degradation** — `record_hook_error` creates a stray schema-less `memory.db` on clean slate; misconfigured agentcore users get a fully silent no-op on every prompt. → `e2e-ac-contextual-inject-wire-and-degradation` Cases B/C.
7. **boto3 missing = raw ImportError** — plain pip install + backend=agentcore gives no `better-memory[agentcore]` hint. → `e2e-ac-neg-boto3-missing` (inverted assertion).
8. **Corrupt `agentcore.json` error lacks remediation** (missing-file error has one; corrupt-file doesn't). → `e2e-ac-neg-corrupt-agentcore-json` negative pin.
9. **`knowledge-base/` never auto-created by the server** — pip installs get silently-empty knowledge search forever. → pinned in `e2e-sqlite-first-boot`.
10. **Orphan episode on failed observe** — Ollama-down observe commits a background episode then fails. → pinned in `e2e-sqlite-ollama-absent-default-backend`.
11. **setup.sh aborts headless at the Ollama prompt** (EOF under `set -euo pipefail`) before layout/install — partial install with zero diagnostics. → `e2e-setup-eof-aborts-all-or-nothing`.
12. **"No SQLite traffic" doc claim is false** — agentcore mode still creates+migrates both sqlite DBs. → pinned in `e2e-ac-server-boot-tools-hidden` and `e2e-ac-neg-missing-agentcore-json`.

---

## 5. Mutation Smoke Checklist (validation phase)

Driver `scripts/e2e_mutation_smoke.py`: runs inside a throwaway `git worktree add <scratch>/mutsmoke HEAD` (never touches the live tree — required: repo currently has ~70 untracked scratch files); per mutation: apply patch → `pytest tests/e2e -q --tb=line -p no:cacheprovider` → assert red **with the expected node id in the failure output** and ≥1 other test still passing → revert via `git checkout --` on the target files; final unpatched control run must be green.

| # | mutation (patch target) | must-flip sentinel |
|---|---|---|
| M1 | `mcp/server.py`: skip `apply_migrations` for memory.db at boot | `e2e-sqlite-first-boot-migrates-tools-knowledge` / observe journey (`no such table` via MCP tool error) |
| M2 | `hooks/session_bootstrap.py`: hoist `write_session_id()` above `service.bootstrap()` | `e2e-sqlite-hook-before-server-degraded` (`runtime/sessions` absent assertion) |
| M3 | `hooks/contextual_inject.py`: except path exits without printing the envelope | `e2e-sqlite-contextual-inject-contract` (exactly-one-JSON-line stdout) |
| M4 | drop hostile `tests/e2e/test_zz_seeded_breach.py` writing to `Path.home()/.claude.json` + `.better-memory/leak.txt` | `meta-canary-home-run` must FAIL with the isolation-breach/hash assertion naming `<canary>/.claude.json` (who-watches-the-watchers) — then green again after removal |

CI adds a cheap "patches still apply cleanly" check without executing the suite.

---

## 6. Ordered Implementation Task List

Every task ≥90% confidence; where confidence rests on a mitigation, it is embedded.

| # | task | deliverables | confidence |
|---|---|---|---|
| 1 | **Harness foundation**: `tests/e2e/_env.py` (`isolated_env` allowlist contract), `pyproject` `e2e` marker, `meta-env-helper-contract` tests (A/B/C/D), `meta-marker-tier-wiring`. | `_env.py`, `tests/e2e_meta/test_env_helper_contract.py`, `test_marker_wiring.py`, pyproject edit | 96% — pure-python fixtures mirroring the proven `mock_home` precedent; all assertions pre-verified by judges. |
| 2 | **Subprocess helpers**: `run_hook`, `mcp_session` (explicit USERPROFILE/HOME, errlog real-file, `cwd=` support), `clean_slate_home`. | `tests/e2e/conftest.py` (partial) | 95% — direct lift of `tests/mcp/test_server_integration.py` precedent + empirically verified SDK behaviors (McpError -32000, errlog fileno). |
| 3 | **Installer scenarios A1–A7**. | `test_install_hooks.py` | 95% — every assertion verified line-by-line against `install_hooks.py` by judges; symlink OSError via driver-script patch works on all hosts. |
| 4 | **sqlite journey C1–C6** (first-boot, degraded, heal, observe/record_use, session-close two-fire, spool-drain/synthesize) with all judge fixes (counts['created'], marker-key pinning via `cwd`, ratings payload shape, non-recursive glob). | `test_sqlite_journey.py`, `test_hooks_contracts.py` (partial) | 92% — long multi-step flows; mitigations embedded: exact wire shapes pinned in the design (no implementer guessing), each step's contract empirically verified in the verifier round. |
| 5 | **sqlite contracts C7–C8** (contextual_inject sqlite contract incl. default-mode; ollama-absent with <30s budget timed only around the call) + `e2e-ollama-zero-traffic-tripwire`. | `test_hooks_contracts.py`, `test_sqlite_negative.py`, `test_tripwires.py` (partial) | 93% — Case A/C empirically verified; Case B (injection fires) needs one seeding recipe — mitigation: seed via `memory.semantic_observe` through the server exactly as the session-close scenario does. |
| 6 | **Fake agentcore endpoint + env**: `_fake_agentcore.py` (gzip model routing), `_agentcore_env.py` (single dummy-ID location + FIXME), `e2e-aws-lockdown-tripwire`. | `_fake_agentcore.py`, `_agentcore_env.py`, `test_tripwires.py` | 91% — endpoint seam empirically proven (signed round-trip captured); residual risk is rest-json routing breadth — mitigation: route only the 8 operations the suite uses, with a fallback 200 `{}` default handler and per-test canned overrides. |
| 7 | **T2 agentcore D1–D7** (boot, retrieve fan-out, dispatch-gap pin, backend wire-fidelity, session_close, contextual_inject, region split-brain). | `test_agentcore_t2.py` | 90% — the dispatch-gap correction removes the false-premise scenarios; remaining wired paths (retrieve, hooks, direct backend) all empirically verified; mitigation for fan-out flake: order-insensitive set assertions, canned empty summaries. |
| 8 | **T2 negatives D8–D11** (parametrized pre-handshake, missing json + SDK level, corrupt json, boto3 shadow with content-based control). | `test_agentcore_neg.py` | 95% — every case empirically run during verification (3/3 stable); exact stderr substrings pinned to source. |
| 9 | **setup.sh B1–B4 + containment**: repo-copy execution, curl/ollama shims, env fixes (SYSTEMROOT/COMSPEC/TEMP/UV_CACHE_DIR), bash-version probe, real-`.venv`-unchanged assertion. | `test_setup_sh.py` | 90% — bash/uv host variability is the risk; mitigations embedded: skip-guards (no bash / host-ollama for the EOF test), shim-forced deterministic branches, warm-cache precondition, progress-marker assertions to localize failures. |
| 10 | **Harness meta**: `meta-canary-home-run` (case-insensitive outer strip, exit-5 vacuity), `real_home_canary` fixture (semantic subtree compare, worker-id canaries, import-time env snapshot), `meta-env-bleed-poisoned-shell`. | `tests/e2e_meta/test_canary_home.py`, `test_env_bleed.py`, conftest fixture | 92% — all judge fixes specified concretely; live-session false-alarm risk removed by the semantic-compare redesign. |
| 11 | **Existing-test additions** (from folded/killed scenarios): `tests/hooks/test_observer.py` — add `stdout == ''`, `no *.db under home`, full filename regex; `tests/cli/test_install_hooks.py::test_malformed_settings_leaves_claude_json_untouched` — add no-backup + no-`.tmp` assertions; strengthen `test_agentcore_unit` requestIdentifier to sha256 equality. | 3 small edits | 98% — one-line assertions into passing tests. |
| 12 | **T3 live E1–E2** (init/status journey; smoke + live retrieve + backend round-trip). | `tests/integration/test_agentcore_live_e2e.py` | 90% — reuses the proven throwaway-memories conftest; risks (cost, slowness, name collisions) mitigated by `bm_int_*` uuid names, pre-registered cleanup, stale sweep, opt-in gating; region set explicitly to dodge the split-brain. |
| 13 | **Mutation smoke** (`scripts/e2e_mutation_smoke.py` + M1–M4 patches, worktree-based) + CI patch-apply check. Run once as the validation gate for the whole phase. | script + 4 patches | 92% — worktree approach removes the dirty-tree blocker; patches anchored to verified line ranges; sentinel node ids fixed by tasks 4–5 and 10. |
| 14 | **Memory sweep + findings report**: record remaining product defects (§4) to better-memory (items 1–2 already recorded: obs `a912351f…`, `971703a0…`), file the dispatch-gap and idvar-gate as product issues. | observations + issue notes | 97%. |

Sequencing constraint: tasks 1–2 block everything; 6 blocks 7–8; 13 runs last (its sentinels must exist). Tasks 3, 4–5, 9 are parallelizable after task 2.