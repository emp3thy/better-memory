# Just-in-time serving Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve memories at the moment they apply (tool-call triggers), stop the session-start rule dump, tighten the prompt gate, and show the useful rate per channel so the 33% target is measurable.

**Architecture:** A pure trigger grammar module matches tool name plus input; a nullable `triggers` JSON column on both memory tables feeds it; the PreToolUse hook loses its once-per-session latch and injects on trigger hits with exposure source `trigger`. `retrieve_relevant` gets one document-frequency-filtered distinct-hit gate for both memory kinds and skips non-human prompts. Deferred bootstrap renders no memories. The Diagnostics page gains a useful-rate-by-channel panel.

**Tech Stack:** Python 3.12, sqlite3 (FTS5), Flask + Jinja + htmx (management UI), MCP server, pytest. Run everything through `uv run`.

**Spec:** `docs/superpowers/specs/2026-10-05-just-in-time-serving-design.md`

## Global Constraints

- Hooks never raise and always exit 0; failures go to `hook_errors` via `record_hook_error`.
- Agentcore backend keeps working with triggers absent: `supports_triggers` is `False`, `set_triggers` raises `NotImplementedError`, `triggered_candidates` returns `[]`.
- Constants from the spec: distinct-hit floor `cfg.context_min_hits` default 2 (`BETTER_MEMORY_CONTEXT_MIN_HITS`); `DF_CAP = 0.5`; `BETTER_MEMORY_TRIGGER_LARGE_WRITE_CHARS` default 8000; new exposure source string `trigger`; new diagnostics metrics `contextual_suppressed_nonhuman`, `trigger_fired`, `trigger_injected`.
- Default `BETTER_MEMORY_INJECT_MODE` becomes `deferred`; legacy mode behaviour is untouched.
- Website docs change in the same branch as the code they describe.
- Commit after every task; message style `feat:` / `test:` / `docs:` as in `git log`.
- No time or effort estimates anywhere in plan or commits.

## Review Focus

1. A `triggers` cell holding malformed JSON must count as "no triggers" in the hook and the UI, never a crash. Test in Task 2 (`test_triggered_candidates_skips_malformed_json`).
2. `path:` triggers written with forward slashes must match Windows backslash paths. Test in Task 1 (`test_path_trigger_normalises_separators`).
3. Tool input fields that are not strings (`command: None`, `content: {...}`) must not raise inside the matcher. Test in Task 1 (`test_non_string_inputs_never_match_or_raise`).
4. A prompt that is only whitespace, or that starts with `<` after leading whitespace, is non-human. Test in Task 5 (`test_nonhuman_prefix_detection` parametrised).
5. A candidate pool smaller than four memories must not have every token classed as ubiquitous by the 50% cap, otherwise nothing could ever qualify. Rule: the cap applies only when the pool has at least 4 memories. Test in Task 4 (`test_small_pool_keeps_all_tokens`).

---

### Task 1: Trigger grammar and matcher

**Confidence:** 95%

**Files:**
- Create: `better_memory/services/triggers.py`
- Test: `tests/services/test_triggers.py`

**Interfaces:**
- Produces:
  - `VALID_PREFIXES: tuple[str, ...] = ("tool:", "skill:", "bash:", "path:", "write:")`
  - `validate_triggers(raw: list[str]) -> list[str]` strips each entry, drops empties, raises `ValueError(f"invalid trigger {t!r}: expected one of tool:, skill:, bash:, path:, write:large")` for an unknown prefix or for `write:` with any operand other than `large`; returns the cleaned list (order kept, duplicates removed).
  - `parse_triggers(raw: str | None) -> list[str]` decodes the JSON column; returns `[]` for `None`, empty string, non-JSON, or JSON that is not a list of strings.
  - `match(triggers: list[str], tool_name: str, tool_input: dict, *, large_write_chars: int) -> str | None` returns the first trigger string that fires, else `None`.
  - `_PATH_TOOLS = ("Write", "Edit", "Read")`

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/test_triggers.py
import pytest
from better_memory.services.triggers import match, parse_triggers, validate_triggers

LARGE = 8000

def test_tool_trigger_exact_name():
    assert match(["tool:WebFetch"], "WebFetch", {}, large_write_chars=LARGE) == "tool:WebFetch"
    assert match(["tool:WebFetch"], "webfetch", {}, large_write_chars=LARGE) is None

def test_skill_trigger_exact_and_prefix():
    assert match(["skill:superpowers:writing-plans"], "Skill", {"skill": "superpowers:writing-plans"}, large_write_chars=LARGE)
    assert match(["skill:superpowers:*"], "Skill", {"skill": "superpowers:brainstorming"}, large_write_chars=LARGE) == "skill:superpowers:*"
    assert match(["skill:superpowers:writing-plans"], "Skill", {"skill": "superpowers:brainstorming"}, large_write_chars=LARGE) is None
    assert match(["skill:superpowers:*"], "Bash", {"command": "superpowers:x"}, large_write_chars=LARGE) is None

def test_bash_trigger_substring_case_insensitive():
    assert match(["bash:<<"], "Bash", {"command": "cat > f <<'EOF'\nx\nEOF"}, large_write_chars=LARGE) == "bash:<<"
    assert match(["bash:GIT PUSH"], "Bash", {"command": "git push origin"}, large_write_chars=LARGE) == "bash:GIT PUSH"
    assert match(["bash:<<"], "Write", {"content": "<<"}, large_write_chars=LARGE) is None

def test_path_trigger_normalises_separators():
    t = ["path:docs/superpowers/specs"]
    assert match(t, "Write", {"file_path": r"C:\Users\x\docs\superpowers\specs\a.md"}, large_write_chars=LARGE) == t[0]
    assert match(t, "Read", {"file_path": "/home/x/docs/superpowers/specs/a.md"}, large_write_chars=LARGE) == t[0]
    assert match(t, "Bash", {"command": "docs/superpowers/specs"}, large_write_chars=LARGE) is None

def test_write_large_threshold():
    assert match(["write:large"], "Write", {"content": "x" * 8001}, large_write_chars=LARGE) == "write:large"
    assert match(["write:large"], "Write", {"content": "x" * 8000}, large_write_chars=LARGE) is None
    assert match(["write:large"], "Edit", {"new_string": "x" * 9000}, large_write_chars=LARGE) is None

def test_first_matching_trigger_wins():
    assert match(["tool:Agent", "bash:<<"], "Bash", {"command": "<<"}, large_write_chars=LARGE) == "bash:<<"

def test_non_string_inputs_never_match_or_raise():
    assert match(["bash:<<"], "Bash", {"command": None}, large_write_chars=LARGE) is None
    assert match(["write:large"], "Write", {"content": {"a": 1}}, large_write_chars=LARGE) is None
    assert match(["path:x"], "Write", {}, large_write_chars=LARGE) is None
    assert match(["skill:a"], "Skill", {"skill": 5}, large_write_chars=LARGE) is None

def test_validate_accepts_grammar_and_cleans():
    assert validate_triggers([" tool:WebFetch ", "", "bash:<<", "tool:WebFetch"]) == ["tool:WebFetch", "bash:<<"]
    assert validate_triggers(["write:large"]) == ["write:large"]

@pytest.mark.parametrize("bad", ["webfetch", "tool:", "write:small", "agent:x", "skill:"])
def test_validate_rejects_unknown(bad):
    with pytest.raises(ValueError, match="invalid trigger"):
        validate_triggers([bad])

@pytest.mark.parametrize("raw", [None, "", "not json", "{}", '["ok", 3]', "[1]"])
def test_parse_triggers_tolerates_garbage(raw):
    assert parse_triggers(raw) == [] or raw == '["ok", 3]'

def test_parse_triggers_roundtrip():
    assert parse_triggers('["tool:WebFetch", "bash:<<"]') == ["tool:WebFetch", "bash:<<"]
```

Decision for `'["ok", 3]'`: a list with any non-string entry parses to `[]` (whole cell rejected). Replace the `or` clause in the test with that assertion.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_triggers.py -q`
Expected: FAIL with `ModuleNotFoundError: better_memory.services.triggers`

- [ ] **Step 3: Implement `better_memory/services/triggers.py`**

Pure module, imports only `json` and `re`. `match` lowercases both operand and haystack for `bash:` and `path:`; `path:` replaces `\\` with `/` in the file path before `in`; `tool:` compares exactly; `skill:` compares exactly, or by `startswith` when the operand ends with `*` (strip the `*`). Non-string input values are treated as absent.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/test_triggers.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/triggers.py tests/services/test_triggers.py
git commit -m "feat(triggers): pure trigger grammar, validator and matcher"
```

---

### Task 2: Storage: migration 0019, services, protocol, backends

**Confidence:** 90%

**Files:**
- Create: `better_memory/db/migrations/0019_memory_triggers.sql`
- Modify: `better_memory/services/semantic.py` (dataclass `SemanticMemory`, `create`, `get`, `list_for_project` SELECT lists, new `set_triggers`)
- Modify: `better_memory/services/reflection.py` (`_bucket_item`, `retrieve_reflections` SELECT; `ReflectionEditService.set_triggers`)
- Modify: `better_memory/storage/protocol.py`, `better_memory/storage/sqlite.py`, `better_memory/storage/agentcore.py`
- Test: `tests/db/test_migration_0019_triggers.py`, `tests/services/test_semantic.py`, `tests/services/test_reflection_writes.py`, `tests/storage/test_sqlite_backend.py`, `tests/storage/test_protocol.py`, `tests/storage/test_agentcore_unit.py`

**Interfaces:**
- Consumes: `validate_triggers`, `parse_triggers` from Task 1.
- Produces:
  - Migration adds `triggers TEXT` to `reflections` and `semantic_memories`, and inserts `rating_diagnostics` rows `contextual_suppressed_nonhuman`, `trigger_fired`, `trigger_injected` with value 0.
  - `SemanticMemory.triggers: list[str] = field(default_factory=list)` (decoded).
  - `SemanticMemoryService.create(..., triggers: list[str] | None = None) -> str` stores `json.dumps(validate_triggers(triggers))` or NULL.
  - `SemanticMemoryService.set_triggers(*, id: str, triggers: list[str]) -> None` validates, stores JSON (NULL when the cleaned list is empty), bumps `updated_at`, raises `ValueError("Semantic memory not found: <id>")` when absent.
  - `ReflectionEditService.set_triggers(*, reflection_id: str, triggers: list[str]) -> None` same contract, error `"Reflection not found: <id>"`; no status restriction.
  - `_bucket_item` dict gains `"triggers": list[str]`.
  - Protocol: `supports_triggers` property; `set_triggers(*, kind: str, id: str, triggers: list[str]) -> None`; `triggered_candidates(*, project: str) -> list[dict]` where each dict has keys `kind`, `id`, `text`, `triggers`, `polarity` (None for semantic), `confidence` (None for semantic), `useful_count`, `updated_at`. Sqlite selects reflections `WHERE (project = ? OR scope = 'general') AND status IN ('pending_review','confirmed') AND triggers IS NOT NULL` and semantics `WHERE (project = ? OR scope = 'general') AND triggers IS NOT NULL`; `text` is `f"{title}: {use_cases} {' '.join(hints)}"` for reflections and `content` for semantics; a row whose `triggers` parses to `[]` is skipped.
  - Agentcore: `supports_triggers -> False`; `set_triggers` raises `NotImplementedError("triggers are not supported on the agentcore backend")`; `triggered_candidates` returns `[]`.

- [ ] **Step 1: Write failing tests**

Migration test (`tests/db/test_migration_0019_triggers.py`), modelled on `tests/db/test_migration_0012_contextual_exposure.py`: after `apply_migrations`, `PRAGMA table_info` for both tables contains `triggers`; `rating_diagnostics` has the three new metrics at 0; applying twice is idempotent.

Service tests (append to `tests/services/test_semantic.py` and `tests/services/test_reflection_writes.py`):
```python
def test_semantic_set_triggers_roundtrip(conn):
    svc = SemanticMemoryService(conn)
    sid = svc.create(content="rule", project="p", scope="general")
    svc.set_triggers(id=sid, triggers=["tool:WebFetch", " bash:<< "])
    assert svc.get(id=sid).triggers == ["tool:WebFetch", "bash:<<"]
    svc.set_triggers(id=sid, triggers=[])
    assert svc.get(id=sid).triggers == []
    assert conn.execute("select triggers from semantic_memories where id=?", (sid,)).fetchone()[0] is None

def test_semantic_set_triggers_rejects_bad_grammar(conn): ...  # ValueError "invalid trigger"
def test_semantic_set_triggers_missing_id(conn): ...           # ValueError "Semantic memory not found"
def test_semantic_create_with_triggers(conn): ...              # create(..., triggers=[...]) persists
def test_reflection_set_triggers_roundtrip(conn): ...          # via ReflectionEditService; retrieve_reflections bucket item carries triggers
```

Backend tests (`tests/storage/test_sqlite_backend.py`):
```python
def test_triggered_candidates_returns_only_rows_with_triggers(backend, conn): ...
def test_triggered_candidates_includes_general_scope_from_other_project(backend, conn): ...
def test_triggered_candidates_skips_malformed_json(backend, conn):
    conn.execute("update semantic_memories set triggers='{not json' where id=?", (sid,))
    assert all(c["id"] != sid for c in backend.triggered_candidates(project="p"))
def test_set_triggers_dispatches_by_kind(backend): ...
```
Protocol test: extend `test_protocol_declares_all_required_methods` list with `set_triggers`, `triggered_candidates`; add `test_protocol_declares_supports_triggers_flag`. Agentcore unit test: `supports_triggers is False`, `set_triggers` raises `NotImplementedError`, `triggered_candidates` returns `[]`.

- [ ] **Step 2: Run the new tests, confirm they fail** (`uv run pytest tests/db/test_migration_0019_triggers.py tests/services/test_semantic.py tests/services/test_reflection_writes.py tests/storage -q -k triggers`)

- [ ] **Step 3: Implement** migration, service methods, protocol additions, sqlite and agentcore implementations as specified in Interfaces. Decode with `parse_triggers` everywhere a row is read; encode with `json.dumps` after `validate_triggers`.

- [ ] **Step 4: Run** `uv run pytest tests/db tests/services/test_semantic.py tests/services/test_reflection_writes.py tests/services/test_reflection.py tests/storage tests/mcp -q`
Expected: all PASS (existing SELECT-shape tests in `tests/services/test_reflection_retrieve_fields.py` may need the new `triggers` key added to expected field sets).

- [ ] **Step 5: Commit** `git commit -m "feat(storage): triggers column, services and backend surface"`

---

### Task 3: Config: large-write threshold, deferred default, min-hits un-deprecated

**Confidence:** 95%

**Files:**
- Modify: `better_memory/config.py` (`Config` dataclass, `_resolve_inject_mode`, `get_config`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config.trigger_large_write_chars: int` from `BETTER_MEMORY_TRIGGER_LARGE_WRITE_CHARS` default 8000 (via `_resolve_nonneg_int`); `_resolve_inject_mode` returns `"deferred"` unless the env value is exactly `legacy` (case-insensitive, stripped); the deprecation NOTE on `context_min_hits` is replaced by "distinct-hit floor for the prompt gate in services/relevant.py".

- [ ] **Step 1: Write failing tests**
```python
def test_trigger_large_write_default(monkeypatch): monkeypatch.delenv("BETTER_MEMORY_TRIGGER_LARGE_WRITE_CHARS", raising=False); assert get_config().trigger_large_write_chars == 8000
def test_trigger_large_write_env(monkeypatch): monkeypatch.setenv("BETTER_MEMORY_TRIGGER_LARGE_WRITE_CHARS", "100"); assert get_config().trigger_large_write_chars == 100
def test_inject_mode_defaults_to_deferred(monkeypatch): monkeypatch.delenv("BETTER_MEMORY_INJECT_MODE", raising=False); assert get_config().inject_mode == "deferred"
def test_inject_mode_legacy_opt_in(monkeypatch): monkeypatch.setenv("BETTER_MEMORY_INJECT_MODE", " Legacy "); assert get_config().inject_mode == "legacy"
```
- [ ] **Step 2: Run, confirm fail.** `uv run pytest tests/test_config.py -q`
- [ ] **Step 3: Implement.** Search tests for any that asserted the old `legacy` default (`grep -rn "legacy" tests/test_config.py tests/services/test_session_bootstrap.py tests/hooks/test_session_bootstrap.py tests/conftest.py`) and update them to set the env explicitly.
- [ ] **Step 4: Run** `uv run pytest tests/test_config.py tests/services/test_session_bootstrap.py tests/hooks/test_session_bootstrap.py -q` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(config): trigger large-write threshold; deferred injection is the default"`

---

### Task 4: Prompt gate: document-frequency filter and distinct-hit floor for both kinds

**Confidence:** 90%

**Files:**
- Modify: `better_memory/services/relevant.py` (`retrieve_relevant`, new helpers), `better_memory/hooks/contextual_inject.py` (pass `min_hits=cfg.context_min_hits`)
- Test: `tests/services/test_relevant.py` (rewrite `TestBM25Gate`, `TestSemantics`, `TestCapsAndDegradation`; keep `TestAgentCoreRelevanceGate` passing unchanged)

**Interfaces:**
- Produces:
  - `DF_CAP: float = 0.5`, `DF_MIN_POOL: int = 4`
  - `_pool_tokens(texts: list[str]) -> list[set[str]]` and `_ubiquitous(token_sets: list[set[str]]) -> set[str]`: a token is ubiquitous when `len(token_sets) >= DF_MIN_POOL` and it appears in more than `DF_CAP * len(token_sets)` of them.
  - `retrieve_relevant(backend, *, query, project, conn=None, max_items=3, include_neutral=False, now=None, min_hits: int = 2)`.
  - Sqlite gate for both kinds: `keywords = extract_keywords(query) - ubiquitous`; `hits = count_keyword_hits(text, keywords)`; qualify iff `hits >= min_hits`. Ranking: RRF of Wilson rank and hit-count rank (descending hits; ties broken by BM25 rank from `_bm25_qualifiers` when available, else id). `RelevantMemory.hits` carries the distinct count.
  - Agentcore branch (`agentcore_mode`) is unchanged, including its keyword fallback.

- [ ] **Step 1: Rewrite the gate tests**
```python
class TestDistinctHitGate:
    def test_single_shared_token_does_not_qualify(self, conn): ...   # reflection titled "Use Playwright for Amazon pages", query "look at the amazon repo" -> []
    def test_two_distinct_hits_qualify(self, conn): ...               # query "amazon playwright" -> served
    def test_min_hits_env_floor_respected(self, conn): ...            # min_hits=3 excludes a 2-hit memory
    def test_ubiquitous_tokens_dropped(self, conn): ...               # 5 memories all containing "claude code"; query "claude code research" -> none served unless another token hits
    def test_small_pool_keeps_all_tokens(self, conn): ...             # pool of 2 memories both containing "claude code"; query "claude code" -> both qualify
    def test_semantic_and_reflection_share_gate(self, conn): ...      # same query qualifies a semantic and a reflection with 2 hits each
    def test_hits_rank_then_wilson(self, conn): ...                   # 3-hit memory outranks 2-hit memory with higher Wilson
```
Delete `TestBM25Gate.test_bm25_match_qualifies` and `TestSemantics.test_semantic_fallback_keyword_when_no_embedder`; keep and re-target `test_no_evidence_no_injection`, `test_max_items_cap`, `test_no_conn_falls_back_to_keywords` (conn=None now follows the same gate), `test_blank_query_returns_empty`, `test_returns_relevantmemory`.

- [ ] **Step 2: Run** `uv run pytest tests/services/test_relevant.py -q` → new tests FAIL
- [ ] **Step 3: Implement** in `relevant.py`; update the module docstring to describe the new gate; update `contextual_inject.py` to pass `min_hits=cfg.context_min_hits`.
- [ ] **Step 4: Run** `uv run pytest tests/services/test_relevant.py tests/services/test_relevant_format.py tests/hooks/test_contextual_inject.py -q` → PASS (hook tests that seeded a one-token match, e.g. `test_injection_renders_project_memory_block`, need two matching tokens in their seeded title/query)
- [ ] **Step 5: Commit** `git commit -m "feat(relevant): one distinct-hit gate with document-frequency filter for both memory kinds"`

---

### Task 5: Prompt hook skips non-human prompts

**Confidence:** 92%

**Files:**
- Modify: `better_memory/hooks/contextual_inject.py`
- Test: `tests/hooks/test_contextual_inject.py`

**Interfaces:**
- Produces: `NON_HUMAN_PREFIXES: tuple[str, ...] = ("<", "Another Claude session sent a message")`; `is_human_prompt(text: str) -> bool` (strip, non-empty, no prefix match). On UserPromptSubmit with a non-human prompt: bump `contextual_suppressed_nonhuman`, emit empty `additionalContext`, write no exposure, do not bump the turn counter.

- [ ] **Step 1: Tests**
```python
@pytest.mark.parametrize("text,human", [("fix the bug", True), ("  <task-notification>x", False), ("Another Claude session sent a message: hi", False), ("   ", False), ("<bash-input> ls", False)])
def test_nonhuman_prefix_detection(text, human): assert hook.is_human_prompt(text) is human
def test_peer_message_is_suppressed(bm_home, monkeypatch, capsys): ...  # seeds a 2-hit memory; prompt is a peer message containing both tokens; asserts no block, no exposure row, counter == 1
```
- [ ] **Step 2: Run, FAIL.** - [ ] **Step 3: Implement.** - [ ] **Step 4: Run** `uv run pytest tests/hooks/test_contextual_inject.py -q` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(hook): skip non-human prompts in contextual injection"`

---

### Task 6: Trigger channel on PreToolUse; latch removed; reason line

**Confidence:** 90%

**Files:**
- Modify: `better_memory/hooks/contextual_inject.py`, `better_memory/services/relevant.py` (`RelevantMemory.reason: str | None = None`; `format_relevant` adds `   Triggered by: {reason}` after the text line when set), `better_memory/services/context_seen.py` (delete `try_claim_pretool_fired`, `pretool_fired`, `mark_pretool_fired`, `_sentinel`), `better_memory/setup/manifest.py` (comment on the PreToolUse entry no longer mentions the latch)
- Test: `tests/hooks/test_contextual_inject.py`, `tests/services/test_context_seen.py`, `tests/services/test_relevant_format.py`

**Interfaces:**
- Consumes: `match` (Task 1), `backend.triggered_candidates` (Task 2), `cfg.trigger_large_write_chars` (Task 3).
- Produces: `triggered_memories(backend, *, project: str, tool_name: str, tool_input: dict, large_write_chars: int, now=None) -> list[RelevantMemory]` in `relevant.py` (one entry per candidate whose triggers match, `reason` set, `hits=0`, `score` = Wilson); hook PreToolUse path: bump `contextual_fired_pretool` and `trigger_fired`, call `triggered_memories`, filter through `SeenStore`, cap to `cfg.context_max_items`, `record_exposures(source="trigger")`, `mark_seen`, bump `trigger_injected`; no latch.

- [ ] **Step 1: Tests**
```python
def test_pretool_trigger_hit_injects_with_reason(bm_home, monkeypatch, capsys): ...     # semantic with ["bash:<<"]; Bash heredoc payload -> block contains "Triggered by: bash:<<"; exposure source == "trigger"
def test_pretool_no_trigger_no_exposure(bm_home, monkeypatch, capsys): ...              # same memory, tool Read -> empty context, zero exposure rows, trigger_fired == 1, trigger_injected == 0
def test_pretool_fires_on_every_call(bm_home, monkeypatch, capsys): ...                 # replaces test_pretool_fires_once_per_session: two different triggered memories, two calls, both injected
def test_pretool_seen_store_dedups_trigger(bm_home, monkeypatch, capsys): ...           # same trigger twice -> second call empty
def test_prompt_then_trigger_same_memory_once(bm_home, monkeypatch, capsys): ...        # served by prompt channel first, trigger channel does not re-serve
def test_format_relevant_reason_line(): ...                                             # in test_relevant_format.py
```
Delete `test_pretool_fires_once_per_session`, `test_userprompt_unaffected_by_pretool_latch`, and the SeenStore latch tests in `tests/services/test_context_seen.py`.

- [ ] **Step 2: Run, FAIL.** - [ ] **Step 3: Implement.** - [ ] **Step 4: Run** `uv run pytest tests/hooks tests/services/test_context_seen.py tests/services/test_relevant_format.py tests/setup -q` → PASS (golden-parity fixtures in `tests/setup/fixtures/` are unaffected: the manifest entry's matcher and command do not change)
- [ ] **Step 5: Commit** `git commit -m "feat(hook): trigger channel on every PreToolUse; drop the per-session latch"`

---

### Task 7: Deferred bootstrap renders no memories

**Confidence:** 92%

**Files:**
- Modify: `better_memory/services/session_bootstrap.py` (deferred branch)
- Test: `tests/services/test_session_bootstrap.py`, `tests/hooks/test_session_bootstrap.py`

**Interfaces:**
- Produces: deferred output is header, index line, `---`, footer; `BootstrapResult.semantic_count` and `reflections_counts` still report the pool sizes; no `session_memory_exposure` rows are written in deferred mode.

- [ ] **Step 1: Tests** `test_deferred_renders_index_only` (no `### Semantic memories` heading even with general-scope rows present), `test_deferred_writes_no_exposures`, `test_legacy_still_dumps_general_semantics` (env `legacy`).
- [ ] **Step 2: Run, FAIL.** - [ ] **Step 3: Implement.** - [ ] **Step 4: Run** `uv run pytest tests/services/test_session_bootstrap.py tests/hooks/test_session_bootstrap.py tests/mcp/test_session_bootstrap_tool.py -q` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(bootstrap): deferred mode renders the index line only"`

---

### Task 8: MCP: `triggers` on semantic observe and update

**Confidence:** 92%

**Files:**
- Modify: `better_memory/mcp/tools.py` (schemas), `better_memory/mcp/handlers/semantics.py`
- Test: `tests/mcp/test_semantic_tools.py`

**Interfaces:**
- Consumes: `backend.semantic_observe(..., triggers=...)` (extend the protocol and both backends' `semantic_observe` with `triggers: list[str] | None = None`; agentcore raises `NotImplementedError` when non-empty), `backend.set_triggers` (Task 2).
- Produces: `memory.semantic_observe` schema property `triggers: {"type": "array", "items": {"type": "string"}, "description": "Tool-call triggers: tool:<Name>, skill:<name|prefix*>, bash:<text>, path:<text>, write:large"}`; `memory.semantic_update` gets the same optional property and `content` becomes optional (at least one of `content`, `triggers` must be present, else error text `"provide content and/or triggers"`). Handler returns the existing JSON shape plus `"triggers"` in `semantic_retrieve` rows.

- [ ] **Step 1: Tests** `test_semantic_observe_with_triggers_persists`, `test_semantic_update_triggers_only`, `test_semantic_update_rejects_bad_trigger` (error payload contains `invalid trigger`), `test_semantic_retrieve_includes_triggers`, `test_schema_declares_triggers` (both tools).
- [ ] **Step 2: Run, FAIL.** - [ ] **Step 3: Implement.** - [ ] **Step 4: Run** `uv run pytest tests/mcp -q` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(mcp): triggers parameter on semantic observe and update"`

---

### Task 9: Management UI: edit triggers; capability gate

**Confidence:** 90%

**Files:**
- Modify: `better_memory/ui/app.py` (`_inject_caps`, `reflection_edit_save`, `semantic_update`), `better_memory/ui/templates/fragments/reflection_edit_form.html`, `fragments/reflection_drawer.html`, `fragments/semantic_drawer.html`, `better_memory/ui/queries.py` (`reflection_detail` returns `triggers` decoded)
- Test: `tests/ui/test_reflections.py`, `tests/ui/test_semantic.py`, `tests/ui/test_nav_gating.py`

**Interfaces:**
- Consumes: `backend.supports_triggers`, `backend.set_triggers` (Task 2).
- Produces: `caps.supports_triggers`; both forms post a `triggers` textarea (one per line); `reflection_edit_save` and `semantic_update` call `backend.set_triggers` after the text update and return 400 with the `ValueError` text on bad grammar; when `supports_triggers` is false the textarea is not rendered and a posted `triggers` field is ignored. Drawers show current triggers as chips.

- [ ] **Step 1: Tests** `test_semantic_update_saves_triggers`, `test_semantic_update_bad_trigger_400`, `test_reflection_edit_saves_triggers`, `test_drawers_show_triggers`, `test_triggers_field_hidden_without_capability` (fake backend with `supports_triggers=False`, pattern from `tests/ui/test_nav_gating.py`).
- [ ] **Step 2: Run, FAIL.** - [ ] **Step 3: Implement.** - [ ] **Step 4: Run** `uv run pytest tests/ui -q -k "not browser"` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(ui): edit memory triggers in the drawers"`

---

### Task 10: Diagnostics: useful rate by channel

**Confidence:** 92%

**Files:**
- Modify: `better_memory/ui/queries.py`, `better_memory/ui/app.py` (`diagnostics` route), `better_memory/ui/templates/diagnostics.html`
- Test: `tests/ui/test_diagnostics_ratings.py`

**Interfaces:**
- Produces: `useful_rate_by_channel(conn, *, last_n_sessions: int | None) -> dict` with `{"rows": [{"source", "rated", "useful", "ignored", "misled", "rate"}...], "total": {...same keys...}, "rated_sessions": int, "useful_per_session": float}`; `rate` is `useful / rated` as a float (0.0 when rated is 0), `useful = cited + shaped`; an `all` row is appended as `total`. `last_n_sessions=None` means all time; otherwise restrict to the N most recent `session_id`s by `MAX(rated_at)` among sessions having any rated row. The route passes `useful_all` and `useful_recent` (N = 15); the template renders two tables under a section titled `Useful rate by channel` and a line `Useful per rated session: {{ '%.2f' % useful_all.useful_per_session }}`.

- [ ] **Step 1: Tests** seed a ledger with three sessions across sources `bootstrap`, `contextual`, `trigger`, `retrieve`; assert per-source counts and rates, the `total`, `rated_sessions == 3`, `useful_per_session`, and that `last_n_sessions=2` drops the oldest session; route test asserts the section title and the `trigger` row render.
- [ ] **Step 2: Run, FAIL.** - [ ] **Step 3: Implement.** - [ ] **Step 4: Run** `uv run pytest tests/ui/test_diagnostics_ratings.py tests/ui/test_diagnostics.py -q` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(ui): useful rate by channel on the diagnostics page"`

---

### Task 11: Documentation

**Confidence:** 95%

**Files:**
- Modify: `website/architecture.md` (Injection strategies: trigger channel, non-human skip, new gate, deferred default; Storage backends table: triggers row), `website/configuration.md` (`BETTER_MEMORY_CONTEXT_MIN_HITS` active again, `BETTER_MEMORY_TRIGGER_LARGE_WRITE_CHARS`, `BETTER_MEMORY_INJECT_MODE` default), `website/mcp-tools.md` (`triggers`), `README.md` (How it works, step 3)

- [ ] **Step 1: Edit the four files.** Each statement must match the implemented behaviour from Tasks 3 to 10; copy constant values from Global Constraints.
- [ ] **Step 2: Verify** `grep -n "latch\|once per session" website/architecture.md README.md` returns nothing describing the removed latch.
- [ ] **Step 3: Commit** `git commit -m "docs: just-in-time serving, triggers and the useful-rate panel"`

---

### Task 12: Full verification and live deployment of triggers

**Confidence:** 90%

**Files:**
- No source changes. Live data: `~/.better-memory/memory.db` (owner's database).

- [ ] **Step 1: Run the whole suite** `uv run pytest -q` → expected: all pass except `tests/setup/test_repo_hook.py::test_installs_in_worktree`, which fails on this machine for lack of a global git identity (pre-existing, unrelated).
- [ ] **Step 2: Lint and types** `uv run ruff check . && uv run pyright` → clean.
- [ ] **Step 3: Apply the deployment table** from the spec (section 2) to the live database with a one-off script using `SemanticMemoryService.set_triggers` and `ReflectionEditService.set_triggers` after `apply_migrations`; print each memory id with its stored triggers.
- [ ] **Step 4: Verify** `uv run python -m better_memory.hooks.contextual_inject` with a PreToolUse Bash heredoc payload for a fresh session id injects the heredoc pitfall with `Triggered by: bash:<<`; remove the temporary `state/context_seen_<id>.json`.
- [ ] **Step 5: Record** a better-memory observation of the deployment (component `contextual_inject`, theme `decision`).
