# Evidence-Anchored Ratings (PR-B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Non-ignored ratings require a one-line evidence statement, stored on the exposure row and surfaced in the UI drawers.

**Architecture:** Migration 0016 adds nullable `evidence TEXT` to `session_memory_exposure`. `MemoryRatingService` validates evidence in its existing pre-savepoint pass (non-ignored ⇒ required, trimmed, ≤500 chars) and stamps it alongside `classification`. Tool schemas, the rating skill (evidence-first ordering), the Stop-hook directive, and the UI drawers/diagnostics follow. Audit-only: no scoring use of evidence.

**Tech Stack:** Python 3.12, sqlite, Flask/htmx UI (project-native CSS), pytest.

**Spec:** `docs/superpowers/specs/2026-07-23-retrieval-quality-design.md` §4, plus post-PR-D deltas (rendered summary in the design review).

## Global Constraints

- New branch `feat/evidence-ratings` from current main (create at Task 1 start).
- Test command: `./.venv/Scripts/python.exe -m pytest <path> -v`; pyright stays 0 errors.
- **Naming collision guard:** `evidence_count` already exists on reflections meaning *count of source observations from synthesis*. The new artefact is always the column `evidence` (exposures) / field `rating_evidence` (UI read-models) / section title "Rating evidence". Never mix them.
- Evidence rule (exact): classes `cited/shaped/misled/overlooked` REQUIRE evidence — a string that is non-empty after `.strip()`, max 500 chars post-trim; violation ⇒ `ValueError`, whole batch rejected (matches existing validate-before-savepoint behaviour). Class `ignored`: evidence optional; stored if provided (same trim/length rule when present).
- UI uses project-native classes (`outcome-badge`/`chip` family in `ui/static/app.css:556-585`) — Bootstrap classes are NOT available (recorded gotcha).
- ASCII only. Ruff line 100. Website-sync guardrail applies. Stage exact paths, never `git add -A`. Commit per task, footer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Test fixtures constructing a BETTER_MEMORY_HOME or app factory pin `BETTER_MEMORY_EMBEDDINGS_BACKEND=sqlite` (recorded recurring gotcha).

## Verified-against-source facts (do not re-derive)

| Fact | Where verified |
|---|---|
| Validation loop shape: `apply_session_ratings` raises per-item `ValueError`s BEFORE the savepoint (~memory_rating.py:263-297); `_VALID_CLASSES`/`_CREDIT_CLASSES` at :61-62 | read 2026-07-24 |
| `_apply_one(*, session_id, kind, memory_id, classification, now)` stamps ALL unrated rows: `UPDATE session_memory_exposure SET rated_at = ?, classification = ? WHERE ... AND rated_at IS NULL` (~:226) | read |
| `credit_one` validates kind (:33) and class ∈ `_CREDIT_CLASSES` (:121), wraps `SAVEPOINT memory_credit` | read |
| `memory.credit` schema: `required: [kind, id, class]`, `additionalProperties: False` (tools.py ~:510-531); `apply_session_ratings` items schema nearby (~:474) | read |
| Handlers: `mcp/handlers/sessions.py` — credit ~:136, apply ~:114; both pass args straight to the service | PR-A map, unchanged |
| Migrations: latest is `0015_via_exploration.sql` ⇒ this PR is **0016**; the exact-column-set tests (test_migration_0009/0010/0012 + schema tests) will need the new column added | read + Task-2-PR-D precedent |
| UI badges: `.outcome-badge`, `.chip`, variants `.outcome-success` etc. (app.css:556-585); diagnostics.html renders `<span class="badge">` (unstyled — pre-existing) | read |
| Drawer read-models: `ui/queries.py` `ReflectionDetail` (~:324, has `evidence_count` = SOURCE-observation count) built ~:392-460; semantic drawer route app.py ~:523, template `semantic.html` + `fragments/` | read |
| Rating skill: `.claude/skills/rate-session-memories/SKILL.md`, STEP 2 at line 17, current rule line 38 "Default is `ignored`..." — user-level copy is a junction to this file (auto-updates) | read + deploy history |
| Stop-hook directive text: `hooks/session_close.py::_emit_rating_directive_if_unrated` (~:200-233), tested by `tests/hooks/test_session_close_rating_directive.py` | PR-#81 work |
| Contextual footer nudge: `services/relevant.py::_BLOCK_FOOTER` (~:140) says "credit it now: memory_credit(kind, id, 'cited'|'shaped'|'misled')" | read (PR-D) |

---

### Task 1: Migration 0016 — evidence column

**Files:**
- Create: `better_memory/db/migrations/0016_rating_evidence.sql`
- Modify: exact-column-set tests (grep `via_exploration` in tests/db and tests/services — every list that enumerates session_memory_exposure columns gains `evidence`)
- Test: `tests/db/test_schema.py` (append)

**Interfaces:**
- Produces: nullable `evidence TEXT` on `session_memory_exposure`. Consumed by Tasks 2, 5.

- [ ] **Step 1: Write the failing test** (mirror the 0015 test):

```python
def test_0016_rating_evidence_column(tmp_memory_db):
    conn = connect(tmp_memory_db)
    apply_migrations(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(session_memory_exposure)")]
    assert "evidence" in cols
    conn.close()
```

Also: create the branch first — `git checkout -b feat/evidence-ratings` from main.

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/db -q -k 0016`
Expected: FAIL — column missing.

- [ ] **Step 3: Write the migration**

```sql
-- Migration 0016: evidence line on rated exposures.
--
-- Rating variance is the noise floor under every ranking signal: identical
-- memory sets were rated `shaped` in one session and `ignored` in another
-- (2026-07 A/B runs). Non-ignored ratings now carry a one-line evidence
-- statement (what the memory changed, or a quote), enforced by
-- MemoryRatingService and surfaced in the UI drawers. Audit-only: no
-- scoring reads this column.
--
-- Distinct from reflections.evidence_count, which counts synthesis source
-- observations and has nothing to do with rating evidence.

ALTER TABLE session_memory_exposure ADD COLUMN evidence TEXT;
```

Update every exact-column-set assertion found by:
`grep -rn "via_exploration" tests/ --include="*.py" -l` — add `"evidence"` beside it in each enumerated set.

- [ ] **Step 4: Run** `./.venv/Scripts/python.exe -m pytest tests/db tests/services/test_exploration_tagging.py -q` — all pass.

- [ ] **Step 5: Commit**

```bash
git add better_memory/db/migrations/0016_rating_evidence.sql tests/db <touched test files>
git commit -m "feat(db): rating-evidence column on exposures (migration 0016)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Evidence validation + storage in MemoryRatingService

**Files:**
- Modify: `better_memory/services/memory_rating.py`
- Test: `tests/services/test_memory_rating.py` (append class), `tests/services/test_rating_evidence.py` (new)

**Interfaces:**
- Consumes: migration 0016.
- Produces:
  - `credit_one(*, session_id, kind, id, classification, evidence: str)` — evidence REQUIRED (all credit classes are non-ignored).
  - `apply_session_ratings(session_id, ratings)` — each rating dict may carry `"evidence"`; validation per the Global Constraints rule; the trimmed value passes to `_apply_one`.
  - `_apply_one(..., evidence: str | None)` — stamp SQL becomes `SET rated_at = ?, classification = ?, evidence = ?`.
  - Module constant `EVIDENCE_MAX_CHARS = 500`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/test_rating_evidence.py
"""Non-ignored ratings must carry a one-line evidence statement.

The ordering is the variance killer: the rater writes the evidence line
BEFORE choosing the class; nothing to point at means the class is
`ignored`. The server enforces the contract loudly - a violating batch is
rejected whole, before the savepoint, like every other validation error.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.memory_rating import (
    EVIDENCE_MAX_CHARS,
    MemoryRatingService,
)


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed(conn, rid="r1", session="s1"):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at)
           VALUES (?, ?, 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')""", (rid, rid))
    conn.execute(
        """INSERT INTO session_memory_exposure
           (session_id, memory_kind, memory_id, exposed_at, source)
           VALUES (?, 'reflection', ?, '2026-01-01', 'retrieve')""",
        (session, rid))
    conn.commit()


def _stored_evidence(conn, rid="r1"):
    return conn.execute(
        "SELECT evidence FROM session_memory_exposure WHERE memory_id = ?",
        (rid,)).fetchone()[0]


class TestApplyBatchEvidence:
    def test_shaped_without_evidence_rejected(self, conn):
        _seed(conn)
        svc = MemoryRatingService(conn)
        with pytest.raises(ValueError, match="evidence"):
            svc.apply_session_ratings(
                session_id="s1",
                ratings=[{"kind": "reflection", "id": "r1", "class": "shaped"}])

    def test_blank_evidence_rejected(self, conn):
        _seed(conn)
        svc = MemoryRatingService(conn)
        with pytest.raises(ValueError, match="evidence"):
            svc.apply_session_ratings(
                session_id="s1",
                ratings=[{"kind": "reflection", "id": "r1",
                          "class": "cited", "evidence": "   "}])

    def test_overlong_evidence_rejected(self, conn):
        _seed(conn)
        svc = MemoryRatingService(conn)
        with pytest.raises(ValueError, match="500"):
            svc.apply_session_ratings(
                session_id="s1",
                ratings=[{"kind": "reflection", "id": "r1", "class": "shaped",
                          "evidence": "x" * (EVIDENCE_MAX_CHARS + 1)}])

    def test_ignored_needs_no_evidence(self, conn):
        _seed(conn)
        svc = MemoryRatingService(conn)
        out = svc.apply_session_ratings(
            session_id="s1",
            ratings=[{"kind": "reflection", "id": "r1", "class": "ignored"}])
        assert out["applied"]["ignored"] == 1
        assert _stored_evidence(conn) is None

    def test_valid_evidence_stored_trimmed(self, conn):
        _seed(conn)
        svc = MemoryRatingService(conn)
        svc.apply_session_ratings(
            session_id="s1",
            ratings=[{"kind": "reflection", "id": "r1", "class": "shaped",
                      "evidence": "  guided the retention fix approach  "}])
        assert _stored_evidence(conn) == "guided the retention fix approach"

    def test_batch_atomic_on_evidence_violation(self, conn):
        # One bad item rejects the WHOLE batch before anything applies.
        _seed(conn, "r1")
        _seed(conn, "r2")
        svc = MemoryRatingService(conn)
        with pytest.raises(ValueError):
            svc.apply_session_ratings(
                session_id="s1",
                ratings=[
                    {"kind": "reflection", "id": "r1", "class": "ignored"},
                    {"kind": "reflection", "id": "r2", "class": "shaped"},
                ])
        rows = conn.execute(
            "SELECT rated_at FROM session_memory_exposure").fetchall()
        assert all(r[0] is None for r in rows)

    def test_ignored_with_evidence_stores_it(self, conn):
        _seed(conn)
        svc = MemoryRatingService(conn)
        svc.apply_session_ratings(
            session_id="s1",
            ratings=[{"kind": "reflection", "id": "r1", "class": "ignored",
                      "evidence": "checked but task was unrelated"}])
        assert _stored_evidence(conn) == "checked but task was unrelated"


class TestCreditEvidence:
    def test_credit_requires_evidence(self, conn):
        _seed(conn)
        svc = MemoryRatingService(conn)
        with pytest.raises(TypeError):
            svc.credit_one(session_id="s1", kind="reflection", id="r1",
                           classification="cited")     # no evidence kwarg

    def test_credit_blank_evidence_rejected(self, conn):
        _seed(conn)
        svc = MemoryRatingService(conn)
        with pytest.raises(ValueError, match="evidence"):
            svc.credit_one(session_id="s1", kind="reflection", id="r1",
                           classification="cited", evidence="")

    def test_credit_stores_evidence(self, conn):
        _seed(conn)
        svc = MemoryRatingService(conn)
        out = svc.credit_one(session_id="s1", kind="reflection", id="r1",
                             classification="shaped",
                             evidence="applied its retry guidance")
        assert out == {"applied": "shaped", "skipped": None}
        assert _stored_evidence(conn) == "applied its retry guidance"
```

(Read `credit_one`'s exact parameter name for the id — it is `id` per the
current signature; keep it.)

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_rating_evidence.py -v`
Expected: FAIL — `EVIDENCE_MAX_CHARS` import error.

- [ ] **Step 3: Implement**

In `memory_rating.py`:

```python
EVIDENCE_MAX_CHARS = 500


def _validate_evidence(cls: str, evidence, *, where: str) -> str | None:
    """Trim + enforce the evidence contract for one rating.

    Non-ignored classes require a non-empty line; `ignored` may carry one.
    Returns the trimmed value (or None). Raises ValueError with the
    caller-supplied position prefix on violation.
    """
    trimmed = evidence.strip() if isinstance(evidence, str) else None
    if cls != "ignored" and not trimmed:
        raise ValueError(
            f"{where}: class {cls!r} requires a non-empty evidence line "
            "(what the memory changed, or a quote); if there is nothing "
            "to point at, the class is 'ignored'")
    if trimmed and len(trimmed) > EVIDENCE_MAX_CHARS:
        raise ValueError(
            f"{where}: evidence exceeds {EVIDENCE_MAX_CHARS} chars "
            f"({len(trimmed)})")
    return trimmed or None
```

- `apply_session_ratings` validation loop: after the class check, add
  `r["_evidence"] = _validate_evidence(cls, r.get("evidence"), where=f"ratings[{i}]")`
  (store the trimmed value on a scratch key; the apply loop below passes
  `evidence=r["_evidence"]` into `_apply_one`).
- `credit_one`: signature gains `evidence: str` (keyword-only, required);
  validate via `_validate_evidence(classification, evidence, where="credit")`
  before the savepoint; pass trimmed value down.
- `_apply_one`: gains `evidence: str | None`; stamp SQL:

```python
        self._conn.execute(
            "UPDATE session_memory_exposure "
            "SET rated_at = ?, classification = ?, evidence = ? "
            "WHERE session_id = ? AND memory_kind = ? AND memory_id = ?"
            "  AND rated_at IS NULL",
            (now, classification, evidence, session_id, kind, memory_id),
        )
```

Existing tests in `test_memory_rating.py` that call these APIs without
evidence: update each non-ignored call site to pass a short evidence string
(mechanical; do not weaken assertions).

- [ ] **Step 4: Run**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_rating_evidence.py tests/services/test_memory_rating.py tests/integration/test_memory_rating_e2e.py -q`
Expected: all pass (the e2e file's non-ignored ratings also gain evidence strings).

- [ ] **Step 5: Commit**

```bash
git add -A -- better_memory/services/memory_rating.py tests/services tests/integration
git commit -m "feat(rating): non-ignored ratings require a one-line evidence statement

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Tool schemas + handlers

**Files:**
- Modify: `better_memory/mcp/tools.py` (memory.credit ~:510; apply_session_ratings items ~:474)
- Modify: `better_memory/mcp/handlers/sessions.py` (credit ~:136 passes `evidence=args["evidence"]`; apply passes rating dicts through unchanged — service reads `evidence` key)
- Test: `tests/mcp/test_rating_tools.py` (extend)

**Interfaces:**
- Consumes: Task 2 service signatures.
- Produces: `memory.credit` schema `required: [kind, id, class, evidence]`, `evidence: {type: string, maxLength: 500}`, description gains "always include a one-line evidence statement: what the memory changed, or a quote". `apply_session_ratings` rating-item schema gains optional `evidence` (same type), description states the non-ignored requirement and the evidence-first rule.

- [ ] **Step 1: Write the failing tests** — extend `tests/mcp/test_rating_tools.py` (read its dispatch style first):
  - schema test: credit requires `evidence`; items schema has `evidence` property.
  - dispatch test: credit without evidence ⇒ isError (validation); credit with evidence ⇒ applied and stored (query the DB); apply batch with shaped+evidence ⇒ applied; shaped without ⇒ isError mentioning "evidence".

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — schema edits per Interfaces; handler: `sessions.py` credit call gains `evidence=str(args.get("evidence", ""))` (service validates); apply handler already forwards dicts.

- [ ] **Step 4: Run** `./.venv/Scripts/python.exe -m pytest tests/mcp -q` — all pass.

- [ ] **Step 5: Commit**

```bash
git add better_memory/mcp/tools.py better_memory/mcp/handlers/sessions.py tests/mcp
git commit -m "feat(mcp): evidence on credit + apply_session_ratings schemas

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Skill, Stop-hook directive, contextual footer

**Files:**
- Modify: `.claude/skills/rate-session-memories/SKILL.md` (STEP 2 + STEP 3 payload example)
- Modify: `better_memory/hooks/session_close.py` (directive text ~:200-233)
- Modify: `better_memory/services/relevant.py::_BLOCK_FOOTER` (~:140)
- Test: `tests/hooks/test_session_close_rating_directive.py` (extend), `tests/services/test_relevant_format.py` (footer assertion update)

**Interfaces:** none new — text contracts, test-pinned.

- [ ] **Step 1: Failing tests** — directive test asserts the emitted text contains `evidence` and the evidence-first sentence; footer test asserts the nudge mentions evidence.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

Skill STEP 2 becomes (replace the class list intro + rules):

```markdown
## STEP 2 — Evidence first, then classify

For each `(kind, id)`: FIRST try to write ONE line of evidence — what the
memory changed in this session, or a quote of where you used it. Then:

- Evidence line written → choose `cited` (quoted/directly referenced),
  `shaped` (guided a decision), `misled` (sent you wrong), or `overlooked`
  (user had to point you back to it). Include the evidence line in the
  rating.
- No evidence line possible → the class is `ignored`. Full stop. Do not
  reverse the order; choosing a class first and rationalising evidence
  after is how ratings drift.
```

STEP 3 payload example gains `"evidence"` on the non-ignored item. Directive
text in `session_close.py` (keep under the existing 8KiB cap):

```python
            "For each id: FIRST write one line of evidence (what the memory "
            "changed, or a quote) - if you cannot, the class is `ignored`.\n"
            "Classes: cited / shaped / ignored / misled / overlooked.\n"
            "Non-ignored ratings without an evidence line are rejected. "
            "Invoke the skill `rate-session-memories`."
```

`_BLOCK_FOOTER`: "credit it now: memory_credit(kind, id, class, evidence) -
include a one-line evidence statement."

- [ ] **Step 4: Run** `./.venv/Scripts/python.exe -m pytest tests/hooks tests/services/test_relevant_format.py -q` — all pass.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/rate-session-memories/SKILL.md better_memory/hooks/session_close.py better_memory/services/relevant.py tests/hooks tests/services/test_relevant_format.py
git commit -m "feat(rating): evidence-first skill, directive, and credit nudge

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: UI — rating-evidence history + diagnostics column

**Files:**
- Modify: `better_memory/ui/queries.py` (reflection detail ~:392-460; semantic drawer query; new `RatingEvidenceRow` dataclass + fetch helper), `better_memory/ui/app.py` (drawer routes pass the rows), templates `reflections.html`/`semantic.html` (or their `fragments/`) + `diagnostics.html` (+ evidence column ~:32)
- Test: `tests/ui/test_reflections.py`, `tests/ui/test_semantic.py`, `tests/ui/test_app.py` (extend, following each file's client-fixture style)

**Interfaces:**
- Produces: `RatingEvidenceRow(classification: str, evidence: str, rated_at: str)`; `fetch_rating_evidence(conn, kind: str, memory_id: str, limit: int = 10) -> list[RatingEvidenceRow]` — rows with `evidence IS NOT NULL`, newest `rated_at` first. Drawer templates render a "Rating evidence" section using `chip`/`outcome-badge` classes (map: cited/shaped → `outcome-success`, misled → `outcome-failure`, overlooked → `outcome-partial`, ignored → `outcome-no_outcome`).

- [ ] **Step 1: Failing tests** — seed exposures with evidence via SQL; assert drawer HTML contains the section title, the evidence text, and the class-mapped chip class; diagnostics page shows the evidence cell; memories without evidence rows show no section.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — query helper + dataclass in queries.py; wire into both drawer routes; template section (cap 10 enforced in SQL `LIMIT`); diagnostics `<td>` with truncation at 120 chars + title attr.

- [ ] **Step 4: Run** `./.venv/Scripts/python.exe -m pytest tests/ui -q` — all pass.

- [ ] **Step 5: Commit**

```bash
git add better_memory/ui tests/ui
git commit -m "feat(ui): rating-evidence history on drawers + diagnostics column

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Website sync, pyright, full suite

- [ ] **Step 1:** `grep -rn "rating\|credit\|classify\|RATE_MEMORIES\|evidence" website/ | head -30` — update the rating-loop prose (architecture.md feedback section, mcp-tools.md credit/apply docs) for the evidence requirement; note the audit-only stance and the evidence_count naming distinction where useful. Synonym-widen greps.
- [ ] **Step 2:** `./.venv/Scripts/python.exe -m pyright` → 0 errors.
- [ ] **Step 3:** `./.venv/Scripts/python.exe -m pytest tests -q --junitxml=suiteB.xml > suiteB.txt 2>&1` ONCE; read tail; fix stragglers minimally (expect: any test calling rating APIs non-ignored without evidence); delete suiteB.* before staging.
- [ ] **Step 4:** Commit `docs(website): evidence-anchored rating prose`.

---

### Task 7: PR, babysit, merge, deploy

- [ ] Push `feat/evidence-ratings`; `gh pr create` (body: spec §4 link, evidence-first mechanism, migration 0016, UI drawers, audit-only stance; footer `🤖 Generated with [Claude Code](https://claude.com/claude-code)`).
- [ ] Babysit: checks green + threads resolved (fix findings via subagents) → squash-merge + delete branch.
- [ ] Deploy: `git checkout main && git pull`; migration 0016 applies on next MCP server restart; the user-level skill junction picks up the SKILL.md rewrite automatically. No env changes.
- [ ] Post-merge note: this session's own next RATE_MEMORIES sweep exercises the new contract live — verify one evidence line lands in the DB (`SELECT evidence FROM session_memory_exposure WHERE evidence IS NOT NULL LIMIT 3`).

---

## Self-review notes

- Spec §4 fully covered (schema, validation, tools, skill, UI drawers incl. the user's explicit per-reflection ask, diagnostics, compat nudge); post-PR-D deltas honoured (0016 renumber; smaller-sweep note is prose only).
- Collision guard threaded through every task (column `evidence`, UI `rating_evidence`, never `evidence_count`).
- Straggler expectation named in Task 6 (existing tests rating non-ignored without evidence) so the full-suite fix wave is anticipated, not surprising.
- AgentCore: rating tools already no-op there (no exposure table); no changes needed — consistent with prior PRs' treatment.
