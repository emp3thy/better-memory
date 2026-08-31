# RATE_MEMORIES Session-Id Pass-Through Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The RATE_MEMORIES flow becomes deterministic end-to-end: the Stop-hook directive names the session id it counted, and the rating tools accept that id explicitly instead of guessing from env/marker.

**Architecture:** Three small changes along one data path: `hooks/session_close.py` (directive text), `mcp/tools.py` + `mcp/handlers/sessions.py` (optional `session_id` param, explicit > env > marker), `.claude/skills/rate-session-memories/SKILL.md` (pass it through). Docs ship in the same PR.

**Tech Stack:** Python 3.12 stdlib; pytest; ruff.

**Spec:** Root-cause record: observation `0000001788170115314#bf82ddc6` (2026-08-31). Summary: hook-side writers key exposures to the stdin-payload session id; the MCP server resolves env-first from its spawn env (which can hold a different id after an MCP respawn), and the per-project marker file loses last-writer-wins races between concurrent sessions. Result: `list_session_exposures` returns empty while the Stop hook counts N pending, forever.

## Global Constraints

- Hooks never raise and never block beyond the existing decision:block contract.
- Backward compatible: both tools keep working with NO `session_id` argument (existing resolution unchanged, fallback path).
- MCP tool COUNT unchanged (schema edits only) — do not touch tool-count claims in docs.
- Lint/test gates per touched dirs: `uv run ruff check <touched>` clean (pre-existing E501 in tests/hooks/test_post_commit.py:169 is not yours); focused pytest per task; full `uv run pytest -q` once before the branch review.
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 1: Directive names its session id (confidence 95%)

**Files:**
- Modify: `better_memory/hooks/session_close.py` (in `_emit_rating_directive_if_unrated`, sections list ~line 111)
- Test: extend `tests/hooks/test_session_close_rating_directive.py`

**Interfaces:**
- Consumes: existing `_emit_rating_directive_if_unrated(session_id)`.
- Produces: directive `additionalContext` whose SECOND line is exactly `Session: <session_id>` (after the `RATE_MEMORIES: N unrated...` header line); the truncation suffix message becomes `(list truncated; call memory.list_session_exposures with this session id for the full set)`.

- [ ] **Step 1: Failing tests** — extend the existing directive tests: (a) directive contains line `Session: abc-123` when called with that id; (b) the `Session:` line survives truncation (build >8KB of rows, assert the line is still present in the truncated output — it is in the header section before the cap slice). Follow the file's existing seeding/capture style.
- [ ] **Step 2: Run to verify fail** — `uv run pytest tests/hooks/test_session_close_rating_directive.py -q` → new tests FAIL.
- [ ] **Step 3: Implement** — insert `f"Session: {session_id}"` as the second element of `sections` (between the header string and the evidence-first line); update the truncation suffix text.
- [ ] **Step 4: Pass + lint** — same pytest command PASS; `uv run ruff check better_memory/hooks tests/hooks`.
- [ ] **Step 5: Commit** — `fix(hooks): RATE_MEMORIES directive names the session id it counted`

### Task 2: Rating tools accept explicit session_id (confidence 92%)

**Files:**
- Modify: `better_memory/mcp/tools.py` (~:459 `memory.list_session_exposures`, ~:474 `memory.apply_session_ratings` inputSchemas)
- Modify: `better_memory/mcp/handlers/sessions.py` (the two handler methods; read their current session-resolution first)
- Test: extend the existing handler tests (locate via `grep -rl list_session_exposures tests/`)

**Interfaces:**
- Consumes: existing handler resolution (env/marker via `_common`/`session_marker`).
- Produces: both tools accept optional `session_id: string` (schema: `{"type": "string"}`, NOT required). Resolution order in BOTH handlers: explicit argument (non-empty, stripped) > existing env/marker resolution. Empty/whitespace argument = absent.

- [ ] **Step 1: Failing tests** — for each handler: seed exposures under session `S1`; call handler with `session_id="S1"` while the ambient resolution points elsewhere (monkeypatch env/marker per the existing tests' style) → rows returned / ratings applied; and one test that omitting the param preserves current behavior (regression guard). Follow existing handler-test fixtures.
- [ ] **Step 2: Run to verify fail** — focused pytest on the touched test files → FAIL (unknown argument or empty result).
- [ ] **Step 3: Implement** — schema: add `session_id` property + description ("Explicit session id from the RATE_MEMORIES directive; overrides env/marker resolution"). Handlers: `explicit = str(arguments.get("session_id") or "").strip()` then `session_id = explicit or <existing resolution>`.
- [ ] **Step 4: Pass + lint** — focused pytest PASS; `uv run ruff check better_memory/mcp tests`.
- [ ] **Step 5: Commit** — `fix(mcp): list/apply session-rating tools accept explicit session_id`

### Task 3: Skill pass-through + docs sweep (confidence 93%)

**Files:**
- Modify: `.claude/skills/rate-session-memories/SKILL.md` (repo copy — user-scope is a link to it)
- Modify: `website/mcp-tools.md` (the two tools' schema sections), `README.md` ONLY if it details these two tools' parameters (grep first; tool count untouched)

**Interfaces:** none (prose), but every parameter name copied verbatim from the Task 2 schemas in `mcp/tools.py`.

- [ ] **Step 1: Skill update** — STEP 1: "Read the `Session:` line from the RATE_MEMORIES directive; call `memory.list_session_exposures` with `{"session_id": "<that id>"}`. If the directive has no Session line (older hook), call with no arguments as before." STEP 3: apply call includes the same `session_id`. Keep the anti-hallucination rule (authoritative list) and evidence-first rules intact.
- [ ] **Step 2: Docs** — update the two tools' parameter tables/schemas in `website/mcp-tools.md`, copying names from `mcp/tools.py`. Verify README mentions (grep `list_session_exposures` in README.md) and update only if parameters are shown.
- [ ] **Step 3: Gates + full suite** — `uv run pytest -q` full run green (exact counts in report); `uv run ruff check better_memory` clean for touched dirs.
- [ ] **Step 4: Commit** — `docs(skill): rate-session-memories passes the directive session id`

## Self-review record

- Coverage: root cause has three legs (env drift, marker race, no explicit channel); the fix adds the explicit channel end-to-end (T1 producer → T2 consumer → T3 instructions) and leaves fallbacks untouched (constraint). Docs-sync guardrail satisfied by T3.
- Placeholders: none — exact line texts, resolution order, and schema shapes specified; handler internals deliberately referenced by grep because current signatures are read at implementation time (both tasks instruct reading first).
- Type consistency: `session_id` string param named identically in schema, handler, skill, and directive line.
