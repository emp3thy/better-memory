# Attention-First Injection Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make injected memories get the LLM's attention (slim bootstrap, relevance-floored + deduped + XML-formatted contextual injection) and make every injected memory rateable (exposure tracking + rating affordances).

**Architecture:** Backend-agnostic scoring stays pure-Python over `StorageBackend.retrieve()` / `.semantic_list()`. Contextual injections gain a score floor, per-session seen-file dedup, `<project-memory>` XML rendering with full ids/ages, and exposure rows via a new `record_exposures` protocol method (sqlite insert / agentcore no-op). Bootstrap renders top-N in full + one-line index for the rest. Stop-hook rating directive gains per-source labels.

**Tech Stack:** Python 3.12, sqlite3, pytest (`asyncio_mode=auto`), ruff preset E,F,I,B,UP,SIM.

**Spec:** `docs/superpowers/specs/2026-07-11-attention-first-injection-design.md` (approved; R1=A, R2=A).

## Guardrails (from project memory — apply throughout)

- **[[98056ebc]] Docs sync (conf 0.95):** every env var / behaviour change lands in README + website/* in the SAME PR — Task 12 enumerates the files; module docstrings that enumerate config knobs update in the same task as the code.
- **[[2dcd790a]] No tautological tests (conf 0.9):** every new test must fail before the change. For formatting tests, anchor on specific markup (e.g. `<project-memory` and the exact id string), not bare substrings.
- **[[59c00a80]] ASCII only in printed output (conf 0.9):** hook stdout is JSON (safe), but keep all new literals ASCII. No `…`, `—`, `•` in NEW code paths' output text; use `...`, `--`, `-`.
- **[[7c9968e2]] Timestamp math in Python (conf 0.85):** never compare SQLite `datetime('now')` to Python isoformat strings; compute cutoffs/ages in Python from `datetime.now(UTC)`.
- **[[62f66888]] `args.get(k) or default` for JSON-sourced kwargs (conf 0.85).**
- **[[35651681]] Every described guard gets a triggering test (conf 0.85):** corrupt seen-file, exposure-write failure, empty-below-floor, TOP_N=0 all have explicit tests below.
- **[[96936ffc]] Table-recreation migration needs a data-preservation round-trip test (2 evidence):** Task 2 includes it.
- **[[1ad537fd]] Extend seed helpers with kwargs, no inline INSERT in new tests (conf 0.7).**
- **ruff UP017:** use `from datetime import UTC`; never `timezone.utc`.
- Dismissed as not applicable: [[88572c3e]] config-merger (no installer changes), [[23a255a5]] surgical removal (no bulk deletions), [[b017b510]] ralph-queue (not queue work).

## Global Constraints

- Hooks NEVER raise and always exit 0; failure paths degrade to empty context + `record_hook_error`.
- Relevance path must work on sqlite AND agentcore backends: only `backend.retrieve(...)` / `backend.semantic_list(...)` / `backend.record_exposures(...)`; no direct SQL, no embeddings, no FTS5.
- New env vars (exact names): `BETTER_MEMORY_BOOTSTRAP_TOP_N` (default 5), `BETTER_MEMORY_CONTEXT_MIN_HITS` (default 2), `BETTER_MEMORY_CONTEXT_MAX_ITEMS` (default 3), `BETTER_MEMORY_CONTEXT_REINJECT_TURNS` (default 0 = never re-inject).
- Rating classes offered inline: `cited`, `shaped`, `misled` only (`credit_one` rejects `ignored`; `overlooked` is user-intervention-anchored).
- All new text literals ASCII.
- Run `uv run ruff check` and `uv run pytest -q` before every commit. Capture pytest with `> file 2>&1` redirect order or `--junit-xml` on Windows.

---

### Task 1: Config — four new env vars

**Files:**
- Modify: `better_memory/config.py` (Config dataclass ~line 196; `get_config()` ~line 254; module docstring if it enumerates knobs)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config.bootstrap_top_n: int`, `Config.context_min_hits: int`, `Config.context_max_items: int`, `Config.context_reinject_turns: int` — consumed by Tasks 4, 6, 8, 9.

**Confidence: 95%**

- [ ] **Step 1: Write failing tests** (append to `tests/test_config.py`, follow the file's existing monkeypatch style):

```python
def test_injection_knobs_defaults(monkeypatch):
    for var in (
        "BETTER_MEMORY_BOOTSTRAP_TOP_N",
        "BETTER_MEMORY_CONTEXT_MIN_HITS",
        "BETTER_MEMORY_CONTEXT_MAX_ITEMS",
        "BETTER_MEMORY_CONTEXT_REINJECT_TURNS",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = get_config()
    assert cfg.bootstrap_top_n == 5
    assert cfg.context_min_hits == 2
    assert cfg.context_max_items == 3
    assert cfg.context_reinject_turns == 0


def test_injection_knobs_env_override(monkeypatch):
    monkeypatch.setenv("BETTER_MEMORY_BOOTSTRAP_TOP_N", "0")
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_MIN_HITS", "1")
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_MAX_ITEMS", "5")
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_REINJECT_TURNS", "20")
    cfg = get_config()
    assert cfg.bootstrap_top_n == 0
    assert cfg.context_min_hits == 1
    assert cfg.context_max_items == 5
    assert cfg.context_reinject_turns == 20


def test_injection_knobs_invalid_raises(monkeypatch):
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_MIN_HITS", "banana")
    with pytest.raises(ValueError):
        get_config()
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_MIN_HITS", "-1")
    with pytest.raises(ValueError):
        get_config()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'bootstrap_top_n'` (or TypeError on construction).

- [ ] **Step 3: Implement** in `better_memory/config.py`:

Add resolver next to `_resolve_bool`:

```python
def _resolve_nonneg_int(env_var: str, default: int) -> int:
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{env_var} must be a non-negative integer, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{env_var} must be a non-negative integer, got {raw!r}")
    return value
```

Add fields to the frozen `Config` dataclass (defaults on the field are unnecessary — `get_config` always passes them — but keep dataclass field order: append after `context_inject_mode`):

```python
    bootstrap_top_n: int
    context_min_hits: int
    context_max_items: int
    context_reinject_turns: int
```

In `get_config()` return, append:

```python
        bootstrap_top_n=_resolve_nonneg_int("BETTER_MEMORY_BOOTSTRAP_TOP_N", 5),
        context_min_hits=_resolve_nonneg_int("BETTER_MEMORY_CONTEXT_MIN_HITS", 2),
        context_max_items=_resolve_nonneg_int("BETTER_MEMORY_CONTEXT_MAX_ITEMS", 3),
        context_reinject_turns=_resolve_nonneg_int("BETTER_MEMORY_CONTEXT_REINJECT_TURNS", 0),
```

If `config.py`'s module docstring enumerates env vars, add the four new ones there in this task.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_config.py -q` — Expected: PASS.
Run: `uv run ruff check better_memory/config.py tests/test_config.py` — Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add better_memory/config.py tests/test_config.py
git commit -m "feat(config): injection tuning knobs (bootstrap_top_n, context_min_hits/max_items/reinject_turns)"
```

---

### Task 2: Migration 0011 — 'contextual' exposure source + diagnostics metrics

**Files:**
- Create: `better_memory/db/migrations/0011_contextual_exposure.sql`
- Test: `tests/db/test_migration_0011.py`

**Interfaces:**
- Produces: `session_memory_exposure.source` accepts `'contextual'`; `rating_diagnostics` rows `contextual_fired_userprompt`, `contextual_fired_pretool`, `contextual_injected`, `contextual_suppressed_floor`, `contextual_suppressed_dedup`. Consumed by Tasks 7, 8.

**Confidence: 93%** (table-recreation risk mitigated by round-trip test below)

- [ ] **Step 1: Write failing test** `tests/db/test_migration_0011.py`. Mirror the structure of `tests/db/test_migration_0009.py` / existing migration tests: use `apply_migrations(conn, migrations_dir=<dir>)` with a tempdir holding 0001..0010 to reach the pre-state, then a tempdir holding only 0011 (this two-tempdir pattern is required because `apply_migrations` skips versions already in `schema_migrations`):

```python
"""Migration 0011: widen exposure source CHECK to include 'contextual';
seed contextual diagnostics counters. Data-preservation round-trip per
project reflection 96936ffc."""
import shutil
from pathlib import Path

import pytest

from better_memory.db.schema import apply_migrations

MIGRATIONS = Path("better_memory/db/migrations")


@pytest.fixture
def pre_0011_conn(tmp_path, fresh_conn):
    """Connection with 0001..0010 applied (fresh_conn fixture from tests/db
    conftest; if the conftest instead provides a raw connection factory,
    open an in-memory conn with row_factory=sqlite3.Row the same way the
    other migration tests do)."""
    pre_dir = tmp_path / "pre"
    pre_dir.mkdir()
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name < "0011":
            shutil.copy(f, pre_dir / f.name)
    apply_migrations(fresh_conn, migrations_dir=pre_dir)
    return fresh_conn


def _apply_0011(conn, tmp_path):
    post_dir = tmp_path / "post"
    post_dir.mkdir()
    shutil.copy(MIGRATIONS / "0011_contextual_exposure.sql", post_dir)
    apply_migrations(conn, migrations_dir=post_dir)


def test_0011_accepts_contextual_source(pre_0011_conn, tmp_path):
    conn = pre_0011_conn
    _apply_0011(conn, tmp_path)
    conn.execute(
        "INSERT INTO session_memory_exposure "
        "(session_id, memory_kind, memory_id, exposed_at, source) "
        "VALUES ('s1', 'reflection', 'r1', '2026-07-11T00:00:00+00:00', 'contextual')"
    )
    row = conn.execute(
        "SELECT source FROM session_memory_exposure WHERE session_id='s1'"
    ).fetchone()
    assert row["source"] == "contextual"


def test_0011_round_trip_preserves_rows(pre_0011_conn, tmp_path):
    conn = pre_0011_conn
    conn.execute(
        "INSERT INTO session_memory_exposure "
        "(session_id, memory_kind, memory_id, exposed_at, source, rated_at, classification) "
        "VALUES ('s0', 'semantic', 'm0', '2026-07-10T09:00:00+00:00', "
        "'bootstrap', '2026-07-10T10:00:00+00:00', 'cited')"
    )
    conn.commit()
    _apply_0011(conn, tmp_path)
    row = conn.execute(
        "SELECT * FROM session_memory_exposure WHERE session_id='s0'"
    ).fetchone()
    assert row["memory_kind"] == "semantic"
    assert row["memory_id"] == "m0"
    assert row["exposed_at"] == "2026-07-10T09:00:00+00:00"
    assert row["source"] == "bootstrap"
    assert row["rated_at"] == "2026-07-10T10:00:00+00:00"
    assert row["classification"] == "cited"


def test_0011_seeds_contextual_diagnostics(pre_0011_conn, tmp_path):
    conn = pre_0011_conn
    _apply_0011(conn, tmp_path)
    metrics = {
        r["metric"]
        for r in conn.execute("SELECT metric FROM rating_diagnostics").fetchall()
    }
    assert {
        "contextual_fired_userprompt",
        "contextual_fired_pretool",
        "contextual_injected",
        "contextual_suppressed_floor",
        "contextual_suppressed_dedup",
    } <= metrics


def test_0011_indexes_recreated(pre_0011_conn, tmp_path):
    conn = pre_0011_conn
    _apply_0011(conn, tmp_path)
    names = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='session_memory_exposure'"
        ).fetchall()
    }
    assert {"idx_sme_session_unrated", "idx_sme_memory"} <= names
```

Adjust the fixture to the actual conftest in `tests/db/` (read `tests/db/test_migration_0009.py` first and copy its connection-setup idiom exactly).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/db/test_migration_0011.py -q`
Expected: FAIL — 0011 file not found.

- [ ] **Step 3: Write migration** `better_memory/db/migrations/0011_contextual_exposure.sql` (same recreation pattern as 0010):

```sql
-- Migration 0011: contextual exposure source + contextual diagnostics.
--
-- The contextual_inject hook (UserPromptSubmit / PreToolUse) now records
-- exposures so contextually-injected memories are rateable. SQLite cannot
-- ALTER a CHECK constraint, so session_memory_exposure is recreated to
-- widen source IN ('bootstrap','retrieve') to include 'contextual'.
-- No table holds a foreign key into session_memory_exposure.

CREATE TABLE session_memory_exposure_new (
    session_id     TEXT NOT NULL,
    memory_kind    TEXT NOT NULL CHECK(memory_kind IN ('reflection', 'semantic')),
    memory_id      TEXT NOT NULL,
    exposed_at     TEXT NOT NULL,
    source         TEXT NOT NULL CHECK(source IN ('bootstrap', 'retrieve', 'contextual')),
    rated_at       TEXT,
    classification TEXT CHECK(classification IN
                     ('cited', 'shaped', 'ignored', 'misled', 'overlooked')),
    PRIMARY KEY (session_id, memory_kind, memory_id, exposed_at)
);

INSERT INTO session_memory_exposure_new
    (session_id, memory_kind, memory_id, exposed_at, source, rated_at, classification)
SELECT
    session_id, memory_kind, memory_id, exposed_at, source, rated_at, classification
FROM session_memory_exposure;

DROP TABLE session_memory_exposure;
ALTER TABLE session_memory_exposure_new RENAME TO session_memory_exposure;

CREATE INDEX idx_sme_session_unrated
    ON session_memory_exposure(session_id) WHERE rated_at IS NULL;
CREATE INDEX idx_sme_memory
    ON session_memory_exposure(memory_kind, memory_id);

-- Contextual-injection observability counters (R1=A in the spec).

INSERT INTO rating_diagnostics (metric, value) VALUES ('contextual_fired_userprompt', 0);
INSERT INTO rating_diagnostics (metric, value) VALUES ('contextual_fired_pretool', 0);
INSERT INTO rating_diagnostics (metric, value) VALUES ('contextual_injected', 0);
INSERT INTO rating_diagnostics (metric, value) VALUES ('contextual_suppressed_floor', 0);
INSERT INTO rating_diagnostics (metric, value) VALUES ('contextual_suppressed_dedup', 0);
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/db/ -q` — Expected: PASS (including older migration tests; if any test hardcodes "10 migrations" or exact schema state, update it — known anti-pattern [[76cc650c]]).

- [ ] **Step 5: Commit**

```bash
git add better_memory/db/migrations/0011_contextual_exposure.sql tests/db/test_migration_0011.py
git commit -m "feat(db): migration 0011 - contextual exposure source + diagnostics counters"
```

---

### Task 3: Reflection dicts gain `times_misled` + `updated_at` (both backends)

**Files:**
- Modify: `better_memory/services/reflection.py:1225-1255` (SELECT + bucket dict)
- Modify: `better_memory/storage/agentcore.py:286-302` (`_parse_reflection_record` return dict)
- Modify: `better_memory/storage/protocol.py:94-105` (`retrieve` docstring key list)
- Test: `tests/services/test_reflection_retrieve_fields.py` (new), `tests/storage/test_agentcore_unit.py` (extend)

**Interfaces:**
- Produces: every reflection dict from `backend.retrieve()` additionally carries `times_misled: int` and `updated_at: str | None` (ISO-8601). Consumed by Task 4 scoring.

**Confidence: 92%**

- [ ] **Step 1: Write failing sqlite test** `tests/services/test_reflection_retrieve_fields.py`. Use the existing reflection seed helper if `tests/services/` has one (grep for how `tests/services/test_useful_count_ranking.py` seeds reflections and reuse that helper/idiom — per guardrail [[1ad537fd]] extend helpers rather than writing new inline INSERT):

```python
def test_retrieve_reflections_includes_misled_and_updated_at(reflection_conn_with_one_row):
    """Every bucket dict must carry times_misled and updated_at so the
    contextual relevance scorer can apply the misled penalty and age."""
    svc = ReflectionSynthesisService(reflection_conn_with_one_row)
    buckets = svc.retrieve_reflections(project="proj", track_exposure=False)
    item = (buckets["do"] + buckets["dont"] + buckets["neutral"])[0]
    assert item["times_misled"] == 0
    assert isinstance(item["updated_at"], str) and item["updated_at"]
```

(Fixture: whatever existing helper seeds one reflection for project "proj" — copy from `test_useful_count_ranking.py`.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/services/test_reflection_retrieve_fields.py -q`
Expected: FAIL — `KeyError: 'times_misled'`.

- [ ] **Step 3: Implement sqlite side** — in `reflection.py` `retrieve_reflections`:
  - SELECT list becomes: `id, title, phase, polarity, use_cases, hints, confidence, tech, evidence_count, useful_count, times_misled, updated_at`
  - bucket dict gains: `"times_misled": r["times_misled"], "updated_at": r["updated_at"],`

- [ ] **Step 4: Implement agentcore side** — in `_parse_reflection_record`'s return dict add:

```python
            "times_misled": int(_num("times_misled")),
            "updated_at": (
                updated_at.isoformat() if isinstance(updated_at, datetime) else None
            ),
```

(`updated_at` local already exists at agentcore.py:283. Metadata key `times_misled` is seeded at agentcore.py:495; records missing it yield 0 via `_num`.)

- [ ] **Step 5: Extend agentcore unit test** — in `tests/storage/test_agentcore_unit.py`, find the existing `_parse_reflection_record` shape test and extend its assertions:

```python
    assert parsed["times_misled"] == 0
    assert parsed["updated_at"] is None or isinstance(parsed["updated_at"], str)
```

Also update the protocol docstring key list at `protocol.py:94-105` to read `{id, title, phase, use_cases, hints (list[str]), confidence (float), tech, evidence_count, useful_count, times_misled, updated_at}`.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/services/test_reflection_retrieve_fields.py tests/storage/test_agentcore_unit.py tests/services/ -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add better_memory/services/reflection.py better_memory/storage/agentcore.py better_memory/storage/protocol.py tests/services/test_reflection_retrieve_fields.py tests/storage/test_agentcore_unit.py
git commit -m "feat(retrieve): reflection dicts carry times_misled + updated_at on both backends"
```

---

### Task 4: Relevance scoring v2 (`relevant.py`)

**Files:**
- Modify: `better_memory/services/relevant.py` (replace `RelevantMemory` + `retrieve_relevant`; `format_relevant` is replaced in Task 5)
- Test: `tests/services/test_relevant.py` (extend existing file)

**Interfaces:**
- Consumes: Task 3's `times_misled`/`updated_at` keys; `SemanticMemory` fields (`useful_count`, `times_misled`, `updated_at`, `scope`) — all existing.
- Produces:
  `RelevantMemory(kind, id, text, polarity, confidence, useful_count, age_days, hits, score)` and
  `retrieve_relevant(backend, *, query, project, min_hits=2, max_items=3, include_neutral=False, now=None) -> list[RelevantMemory]`.
  Consumed by Tasks 5 and 8.

**Confidence: 91%**

- [ ] **Step 1: Write failing tests** (extend `tests/services/test_relevant.py`; reuse its existing fake-backend idiom — read the file first and copy its fake `retrieve`/`semantic_list` stub shape):

```python
FIXED_NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)


def _reflection(id="r1", title="pytest windows redirect", hints=None,
                useful_count=0, times_misled=0, confidence=0.8,
                updated_at="2026-07-01T00:00:00+00:00", polarity="do"):
    return {
        "id": id, "title": title, "phase": "general", "use_cases": "",
        "hints": hints or [], "confidence": confidence, "tech": None,
        "evidence_count": 1, "useful_count": useful_count,
        "times_misled": times_misled, "updated_at": updated_at,
        "_polarity": polarity,  # test helper only; buckets carry polarity
    }


def test_floor_rejects_single_hit_by_default(fake_backend):
    fake_backend.reflections = {"do": [_reflection(title="pytest only-one")], "dont": [], "neutral": []}
    out = retrieve_relevant(fake_backend, query="pytest something unrelated",
                            project="p", now=lambda: FIXED_NOW)
    assert out == []  # 1 distinct hit < min_hits=2


def test_floor_admits_two_hits(fake_backend):
    fake_backend.reflections = {"do": [_reflection(title="pytest windows redirect")], "dont": [], "neutral": []}
    out = retrieve_relevant(fake_backend, query="run pytest on windows",
                            project="p", now=lambda: FIXED_NOW)
    assert [m.id for m in out] == ["r1"]
    assert out[0].hits == 2
    assert out[0].age_days == 10
    assert out[0].kind == "reflection"


def test_title_hits_count_double_in_score(fake_backend):
    title_match = _reflection(id="rt", title="alpha beta", updated_at="2026-07-01T00:00:00+00:00")
    hint_match = _reflection(id="rh", title="zzz yyy", hints=["alpha beta"], updated_at="2026-07-01T00:00:00+00:00")
    fake_backend.reflections = {"do": [hint_match, title_match], "dont": [], "neutral": []}
    out = retrieve_relevant(fake_backend, query="alpha beta", project="p", now=lambda: FIXED_NOW)
    assert out[0].id == "rt"  # same hits, but title-weighted score wins


def test_misled_penalty_halves_score(fake_backend):
    clean = _reflection(id="rc", title="alpha beta", useful_count=0, times_misled=0)
    burned = _reflection(id="rb", title="alpha beta", useful_count=0, times_misled=3)
    fake_backend.reflections = {"do": [burned, clean], "dont": [], "neutral": []}
    out = retrieve_relevant(fake_backend, query="alpha beta", project="p", now=lambda: FIXED_NOW)
    assert out[0].id == "rc"
    assert out[1].score < out[0].score


def test_max_items_cap(fake_backend):
    fake_backend.reflections = {"do": [
        _reflection(id=f"r{i}", title="alpha beta") for i in range(6)
    ], "dont": [], "neutral": []}
    out = retrieve_relevant(fake_backend, query="alpha beta", project="p",
                            max_items=3, now=lambda: FIXED_NOW)
    assert len(out) == 3


def test_missing_metadata_is_neutral(fake_backend):
    r = _reflection(id="rm", title="alpha beta")
    del r["times_misled"]; del r["updated_at"]  # older backend shape
    fake_backend.reflections = {"do": [r], "dont": [], "neutral": []}
    out = retrieve_relevant(fake_backend, query="alpha beta", project="p", now=lambda: FIXED_NOW)
    assert out[0].age_days is None
    assert out[0].score > 0
```

Also keep/adapt the file's existing tests: backend-error-degrades-to-empty, empty-keyword-query returns [] (these already exist — update signatures only).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/services/test_relevant.py -q`
Expected: FAIL — new kwargs/fields missing.

- [ ] **Step 3: Implement** — replace `RelevantMemory` and `retrieve_relevant` in `relevant.py`:

```python
@dataclass
class RelevantMemory:
    kind: str                 # "reflection" | "semantic"
    id: str
    text: str                 # full display text (renderer truncates)
    polarity: str | None      # "do" | "dont" | None for semantic
    confidence: float | None
    useful_count: int
    age_days: int | None
    hits: int
    score: float


def _age_days(iso_ts: str | None, now: datetime) -> int | None:
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return max(0, (now - ts).days)


def _activation(*, useful_count: int, times_misled: int, confidence: float | None) -> float:
    act = (1.0 + 0.2 * math.log1p(max(0, useful_count)))
    if confidence is not None:
        act *= max(0.1, float(confidence))
    if times_misled > useful_count:
        act *= 0.5
    return act


def retrieve_relevant(
    backend: Any,
    *,
    query: str,
    project: str,
    min_hits: int = 2,
    max_items: int = 3,
    include_neutral: bool = False,
    now: Callable[[], datetime] | None = None,
) -> list[RelevantMemory]:
    """Score curated memories against the query; return top max_items whose
    distinct-keyword hits >= min_hits, ordered by score desc. Never raises."""
    keywords = extract_keywords(query)
    if not keywords:
        return []
    _now = (now or (lambda: datetime.now(UTC)))()

    out: list[RelevantMemory] = []

    try:
        buckets = backend.retrieve(project=project, track_exposure=False)
    except Exception:  # noqa: BLE001 - degrade to no reflections
        buckets = {}
    order = ["do", "dont"] + (["neutral"] if include_neutral else [])
    for bucket in order:
        for r in buckets.get(bucket, []) or []:
            title = str(r.get("title") or "")
            body = " ".join(
                [str(r.get("use_cases") or "")]
                + [str(h) for h in (r.get("hints") or [])]
            )
            title_hits = count_keyword_hits(title, keywords)
            total_hits = count_keyword_hits(f"{title} {body}", keywords)
            if total_hits < min_hits:
                continue
            act = _activation(
                useful_count=int(r.get("useful_count") or 0),
                times_misled=int(r.get("times_misled") or 0),
                confidence=r.get("confidence"),
            )
            score = (total_hits + title_hits) * act  # title hits count double
            out.append(RelevantMemory(
                kind="reflection", id=str(r.get("id")),
                text=f"{title}: {body}".strip(": "),
                polarity=bucket if bucket in ("do", "dont") else None,
                confidence=r.get("confidence"),
                useful_count=int(r.get("useful_count") or 0),
                age_days=_age_days(r.get("updated_at"), _now),
                hits=total_hits, score=score,
            ))

    try:
        semantic = backend.semantic_list(project=project, track_exposure=False)
    except Exception:  # noqa: BLE001 - degrade to no semantic
        semantic = []
    for s in semantic or []:
        content = getattr(s, "content", "") or ""
        hits = count_keyword_hits(content, keywords)
        if hits < min_hits:
            continue
        act = _activation(
            useful_count=int(getattr(s, "useful_count", 0) or 0),
            times_misled=int(getattr(s, "times_misled", 0) or 0),
            confidence=None,
        )
        out.append(RelevantMemory(
            kind="semantic", id=str(getattr(s, "id", "")),
            text=content, polarity=None, confidence=None,
            useful_count=int(getattr(s, "useful_count", 0) or 0),
            age_days=_age_days(getattr(s, "updated_at", None), _now),
            hits=hits, score=hits * act,
        ))

    out.sort(key=lambda m: (-m.score, m.id))
    return out[:max_items]
```

Imports to add at top: `import math`, `from collections.abc import Callable`, `from datetime import UTC, datetime`. Module docstring: update "whole-word keyword-filters" sentence to describe hits x activation scoring with a min-hits floor.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_relevant.py tests/hooks/test_contextual_inject.py -q`
Expected: relevant tests PASS. If `test_contextual_inject.py` breaks on `format_relevant`/`retrieve_relevant` signatures, patch minimal call-site compatibility in the test only if trivial — otherwise note it and fix fully in Task 8 (hook rewiring), keeping this commit green by adjusting the hook's `retrieve_relevant(...)` call: `retrieve_relevant(backend, query=query, project=project)` already matches the new defaults; `limit=5` kwarg must be dropped from `contextual_inject.py:77`.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/relevant.py tests/services/test_relevant.py better_memory/hooks/contextual_inject.py
git commit -m "feat(relevant): hits-x-activation scoring with min-hits floor and max-items cap"
```

---

### Task 5: Injection renderer v2 — `<project-memory>` block

**Files:**
- Modify: `better_memory/services/relevant.py` (replace `format_relevant`)
- Test: `tests/services/test_relevant_format.py` (new)

**Interfaces:**
- Consumes: `RelevantMemory` from Task 4.
- Produces: `format_relevant(items: list[RelevantMemory]) -> str` (no `max_items` kwarg — capping happens in retrieve). Consumed by Task 8.

**Confidence: 93%**

- [ ] **Step 1: Write failing tests** `tests/services/test_relevant_format.py`:

```python
from better_memory.services.relevant import RelevantMemory, format_relevant


def _mem(**kw):
    base = dict(kind="reflection", id="a" * 32, text="Use junit-xml on windows",
                polarity="do", confidence=0.9, useful_count=15, age_days=34,
                hits=3, score=5.0)
    base.update(kw)
    return RelevantMemory(**base)


def test_empty_items_renders_empty():
    assert format_relevant([]) == ""


def test_block_structure_and_full_id():
    out = format_relevant([_mem()])
    assert out.startswith('<project-memory source="better-memory">')
    assert out.rstrip().endswith("</project-memory>")
    assert "a" * 32 in out                       # FULL id present
    assert "conf 0.9" in out
    assert "used 15x" in out
    assert "34d old" in out
    assert "memory_credit" in out                 # rating affordance line
    assert "'cited'|'shaped'|'misled'" in out


def test_dont_polarity_rendered_as_corrective():
    out = format_relevant([_mem(polarity="dont", text="inline INSERT SQL in tests drifts")])
    assert "Known pitfall -- do this instead:" in out


def test_semantic_item_without_confidence():
    out = format_relevant([_mem(kind="semantic", polarity=None, confidence=None,
                                useful_count=0, text="repo uses uv run pytest")])
    assert "conf" not in out.split("\n")[2]       # no conf tag on the semantic line
    assert "semantic" in out


def test_missing_age_omitted():
    out = format_relevant([_mem(age_days=None)])
    assert "d old" not in out


def test_output_is_ascii():
    out = format_relevant([_mem()])
    out.encode("ascii")  # raises if any non-ASCII slipped in
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/services/test_relevant_format.py -q`
Expected: FAIL — old format has no XML tag / different signature.

- [ ] **Step 3: Implement** — replace `format_relevant` in `relevant.py`:

```python
_TEXT_MAX_CHARS = 400

_BLOCK_HEADER = (
    '<project-memory source="better-memory">\n'
    "Prior knowledge from past sessions in this project "
    "(factual records; verify if stale):"
)
_BLOCK_FOOTER = (
    "If any entry above materially helps or misleads this task, credit it now: "
    "memory_credit(kind, id, 'cited'|'shaped'|'misled').\n"
    "</project-memory>"
)


def _meta_tag(m: RelevantMemory) -> str:
    parts = [f"{m.kind} {m.id}"]
    if m.confidence is not None:
        parts.append(f"conf {m.confidence:.1f}")
    if m.useful_count:
        parts.append(f"used {m.useful_count}x")
    if m.age_days is not None:
        parts.append(f"{m.age_days}d old")
    return "[" + " | ".join(parts) + "]"


def format_relevant(items: list[RelevantMemory]) -> str:
    """Render the additionalContext block. Empty string if no items."""
    if not items:
        return ""
    lines = [_BLOCK_HEADER]
    for i, m in enumerate(items, start=1):
        text = m.text if len(m.text) <= _TEXT_MAX_CHARS else m.text[: _TEXT_MAX_CHARS - 3] + "..."
        if m.polarity == "dont":
            text = f"Known pitfall -- do this instead: {text}"
        lines.append(f"{i}. {_meta_tag(m)}")
        lines.append(f"   {text}")
    lines.append(_BLOCK_FOOTER)
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_relevant_format.py tests/services/test_relevant.py -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/relevant.py tests/services/test_relevant_format.py
git commit -m "feat(relevant): project-memory XML rendering with full ids, ages, dont-flip, rating affordance"
```

---

### Task 6: Per-session seen-file dedup module

**Files:**
- Create: `better_memory/services/context_seen.py`
- Test: `tests/services/test_context_seen.py`

**Interfaces:**
- Produces:
  - `SeenStore(state_dir: Path, session_id: str)` with methods
    `bump_turn() -> int` (increments + persists turn counter, returns new turn),
    `filter_unseen(ids: list[tuple[str, str]], *, reinject_turns: int) -> list[tuple[str, str]]` ((kind,id) pairs),
    `mark_seen(ids: list[tuple[str, str]]) -> None`,
  - module function `prune_stale(state_dir: Path, *, now: datetime, max_age_days: int = 7) -> None`.
  Consumed by Task 8.
- File format: `context_seen_<session_id>.json` = `{"turn": int, "seen": {"<kind>:<id>": last_injected_turn}}`.

**Confidence: 92%**

- [ ] **Step 1: Write failing tests** `tests/services/test_context_seen.py`:

```python
from datetime import UTC, datetime
from pathlib import Path

from better_memory.services.context_seen import SeenStore, prune_stale


def _store(tmp_path):
    return SeenStore(tmp_path, "sess-1")


def test_first_exposure_passes_then_suppressed(tmp_path):
    s = _store(tmp_path)
    s.bump_turn()
    ids = [("reflection", "r1"), ("semantic", "m1")]
    assert s.filter_unseen(ids, reinject_turns=0) == ids
    s.mark_seen(ids)
    s2 = _store(tmp_path)  # fresh instance = fresh hook process
    s2.bump_turn()
    assert s2.filter_unseen(ids, reinject_turns=0) == []


def test_reinject_after_n_turns(tmp_path):
    s = _store(tmp_path)
    s.bump_turn()
    s.mark_seen([("reflection", "r1")])
    for _ in range(3):
        s2 = _store(tmp_path)
        s2.bump_turn()
    s3 = _store(tmp_path)
    s3.bump_turn()  # turn 5; injected at turn 1
    assert s3.filter_unseen([("reflection", "r1")], reinject_turns=3) == [("reflection", "r1")]
    assert s3.filter_unseen([("reflection", "r1")], reinject_turns=10) == []


def test_corrupt_file_treated_as_empty(tmp_path):
    (tmp_path / "context_seen_sess-1.json").write_text("{not json", encoding="utf-8")
    s = _store(tmp_path)
    assert s.bump_turn() == 1
    assert s.filter_unseen([("reflection", "r1")], reinject_turns=0) == [("reflection", "r1")]


def test_sessions_are_isolated(tmp_path):
    a = SeenStore(tmp_path, "sess-a")
    a.bump_turn()
    a.mark_seen([("reflection", "r1")])
    b = SeenStore(tmp_path, "sess-b")
    b.bump_turn()
    assert b.filter_unseen([("reflection", "r1")], reinject_turns=0) == [("reflection", "r1")]


def test_prune_stale_removes_old_files_only(tmp_path):
    import os
    old = tmp_path / "context_seen_old.json"
    new = tmp_path / "context_seen_new.json"
    old.write_text("{}", encoding="utf-8")
    new.write_text("{}", encoding="utf-8")
    ten_days_ago = datetime(2026, 7, 1, tzinfo=UTC).timestamp()
    os.utime(old, (ten_days_ago, ten_days_ago))
    prune_stale(tmp_path, now=datetime(2026, 7, 11, tzinfo=UTC))
    assert not old.exists()
    assert new.exists()


def test_missing_state_dir_never_raises(tmp_path):
    s = SeenStore(tmp_path / "does-not-exist-yet", "sess-1")
    assert s.bump_turn() == 1  # creates the dir
    s.mark_seen([("reflection", "r1")])
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/services/test_context_seen.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement** `better_memory/services/context_seen.py`:

```python
"""Per-session seen-store for contextual memory injection dedup.

Backend-independent (works in agentcore mode where there is no exposure
table) and cheap: one small JSON file per session under
``<better-memory home>/state``. Never raises: corrupt or unwritable state
degrades to "nothing seen".

File format: ``context_seen_<session_id>.json`` ->
``{"turn": int, "seen": {"<kind>:<id>": last_injected_turn}}``.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

_FILE_RE = re.compile(r"^context_seen_.+\.json$")
_SAFE_SESSION_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _key(kind: str, id_: str) -> str:
    return f"{kind}:{id_}"


class SeenStore:
    def __init__(self, state_dir: Path, session_id: str) -> None:
        self._dir = state_dir
        safe = _SAFE_SESSION_RE.sub("_", session_id or "unknown")
        self._path = state_dir / f"context_seen_{safe}.json"
        self._data = self._load()

    def _load(self) -> dict:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("seen"), dict):
                return {"turn": int(raw.get("turn") or 0), "seen": raw["seen"]}
        except BaseException:  # noqa: BLE001 - corrupt/missing -> empty
            pass
        return {"turn": 0, "seen": {}}

    def _save(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data), encoding="utf-8")
        except BaseException:  # noqa: BLE001 - best-effort
            pass

    def bump_turn(self) -> int:
        self._data["turn"] = int(self._data.get("turn") or 0) + 1
        self._save()
        return self._data["turn"]

    def filter_unseen(
        self, ids: list[tuple[str, str]], *, reinject_turns: int,
    ) -> list[tuple[str, str]]:
        turn = int(self._data.get("turn") or 0)
        out: list[tuple[str, str]] = []
        for kind, id_ in ids:
            last = self._data["seen"].get(_key(kind, id_))
            if last is None:
                out.append((kind, id_))
            elif reinject_turns > 0 and (turn - int(last)) > reinject_turns:
                out.append((kind, id_))
        return out

    def mark_seen(self, ids: list[tuple[str, str]]) -> None:
        turn = int(self._data.get("turn") or 0)
        for kind, id_ in ids:
            self._data["seen"][_key(kind, id_)] = turn
        self._save()


def prune_stale(state_dir: Path, *, now: datetime, max_age_days: int = 7) -> None:
    """Delete context_seen files older than max_age_days. Never raises."""
    try:
        cutoff = now.timestamp() - max_age_days * 86400
        for f in state_dir.iterdir():
            if _FILE_RE.match(f.name) and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except BaseException:  # noqa: BLE001
        pass
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_context_seen.py -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/context_seen.py tests/services/test_context_seen.py
git commit -m "feat(context): per-session seen-store for contextual injection dedup"
```

---

### Task 7: `record_exposures` protocol method (sqlite insert / agentcore no-op)

**Files:**
- Modify: `better_memory/storage/protocol.py` (new method after `session_bootstrap`, ~line 241)
- Modify: `better_memory/services/session_bootstrap.py` (generalise `_record_exposure` into public `record_exposures`)
- Modify: `better_memory/storage/sqlite.py` (delegate)
- Modify: `better_memory/storage/agentcore.py` (no-op)
- Test: `tests/services/test_exposure_tracking.py` (extend), `tests/storage/test_protocol.py` (extend), `tests/storage/test_agentcore_unit.py` (extend)

**Interfaces:**
- Produces: `record_exposures(*, session_id: str, items: list[tuple[str, str]], source: str) -> None` on the protocol and both backends. `items` = `(kind, id)` pairs, kind in `{"reflection","semantic"}`. Consumed by Task 8.

**Confidence: 92%**

- [ ] **Step 1: Write failing tests.** In `tests/services/test_exposure_tracking.py` (reuse its existing seeded-conn fixture idiom):

```python
def test_record_exposures_contextual_source(seeded_conn):
    svc = SessionBootstrapService(seeded_conn)
    svc.record_exposures(
        session_id="s-ctx",
        items=[("reflection", "r1"), ("semantic", "m1")],
        source="contextual",
    )
    rows = seeded_conn.execute(
        "SELECT memory_kind, memory_id, source FROM session_memory_exposure "
        "WHERE session_id='s-ctx' ORDER BY memory_kind"
    ).fetchall()
    assert [(r["memory_kind"], r["memory_id"], r["source"]) for r in rows] == [
        ("reflection", "r1", "contextual"),
        ("semantic", "m1", "contextual"),
    ]


def test_record_exposures_empty_session_id_is_noop(seeded_conn):
    svc = SessionBootstrapService(seeded_conn)
    svc.record_exposures(session_id="", items=[("reflection", "r1")], source="contextual")
    n = seeded_conn.execute(
        "SELECT COUNT(*) AS n FROM session_memory_exposure"
    ).fetchone()["n"]
    assert n == 0
```

In `tests/storage/test_protocol.py`, extend the conformance check so both backends expose `record_exposures` (follow the file's existing method-presence pattern). In `tests/storage/test_agentcore_unit.py`:

```python
def test_record_exposures_is_noop(agentcore_backend):
    agentcore_backend.record_exposures(
        session_id="s", items=[("reflection", "r1")], source="contextual",
    )  # must not raise, must not call any client API (assert no boto calls if the fixture records them)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/services/test_exposure_tracking.py tests/storage/ -q`
Expected: FAIL — `record_exposures` missing.

- [ ] **Step 3: Implement service method** in `session_bootstrap.py` — replace the body of `_record_exposure` with a call to the new public method:

```python
    def record_exposures(
        self,
        *,
        session_id: str,
        items: list[tuple[str, str]],
        source: str,
    ) -> None:
        """Write one session_memory_exposure row per (kind, id) item.

        Best-effort: skips entirely when session_id is empty. Own commit
        (see module docstring on connection ownership).
        """
        if not session_id or not items:
            return
        now = self._clock().isoformat()
        self._conn.executemany(
            "INSERT OR IGNORE INTO session_memory_exposure "
            "(session_id, memory_kind, memory_id, exposed_at, source) "
            "VALUES (?, ?, ?, ?, ?)",
            [(session_id, kind, mid, now, source) for kind, mid in items],
        )
        self._conn.commit()

    def _record_exposure(
        self,
        *,
        session_id: str,
        reflection_ids: list[str],
        semantic_ids: list[str],
    ) -> None:
        self.record_exposures(
            session_id=session_id,
            items=[("reflection", rid) for rid in reflection_ids]
            + [("semantic", sid) for sid in semantic_ids],
            source="bootstrap",
        )
```

- [ ] **Step 4: Protocol + backends.** `protocol.py` (after `session_bootstrap`):

```python
    def record_exposures(
        self,
        *,
        session_id: str,
        items: list[tuple[str, str]],
        source: str,
    ) -> None:
        """Record (kind, id) memory exposures for later rating. Sqlite writes
        session_memory_exposure rows; agentcore is a documented no-op (it has
        no exposure log — rating flows through credit_one)."""
        ...
```

`sqlite.py` (after `session_bootstrap`):

```python
    def record_exposures(
        self,
        *,
        session_id: str,
        items: list[tuple[str, str]],
        source: str,
    ) -> None:
        self._session_bootstrap.record_exposures(
            session_id=session_id, items=items, source=source,
        )
```

`agentcore.py` (after `session_bootstrap`, before `list_session_exposures`):

```python
    def record_exposures(
        self,
        *,
        session_id: str,
        items: list[tuple[str, str]],
        source: str,
    ) -> None:
        """No-op: agentcore mode has no exposure log (see list_session_exposures)."""
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/services/test_exposure_tracking.py tests/storage/ tests/services/test_session_bootstrap.py tests/hooks/test_session_bootstrap.py -q`
Expected: PASS (bootstrap exposure behaviour unchanged via delegation).

- [ ] **Step 6: Commit**

```bash
git add better_memory/storage/protocol.py better_memory/storage/sqlite.py better_memory/storage/agentcore.py better_memory/services/session_bootstrap.py tests/services/test_exposure_tracking.py tests/storage/test_protocol.py tests/storage/test_agentcore_unit.py
git commit -m "feat(storage): record_exposures protocol method (sqlite insert, agentcore no-op)"
```

---

### Task 8: Rewire `contextual_inject` hook — session_id, floor/cap config, dedup, exposures, diagnostics

**Files:**
- Modify: `better_memory/hooks/contextual_inject.py`
- Test: `tests/hooks/test_contextual_inject.py` (extend)

**Interfaces:**
- Consumes: Tasks 1 (config), 4/5 (retrieve/format), 6 (SeenStore), 7 (record_exposures).
- Produces: final hook behaviour; envelope shape unchanged (`hookSpecificOutput.additionalContext`).

**Confidence: 90%**

- [ ] **Step 1: Write failing tests** (extend `tests/hooks/test_contextual_inject.py`; follow its existing run-hook-via-monkeypatched-stdin idiom — read the file first and reuse its helpers; seed memories through the same fixtures the existing tests use):

Test list (each is a distinct test function):
1. `test_injection_renders_project_memory_block` — seeded matching reflection (two-keyword title), prompt hits it, stdout JSON `additionalContext` starts with `<project-memory` and contains the reflection's full id.
2. `test_exposure_row_written_with_contextual_source` — after hook run with `session_id` in the payload, `session_memory_exposure` has the row with `source='contextual'`.
3. `test_second_run_suppressed_by_seen_store` — run hook twice with same payload + same session_id (point `BETTER_MEMORY_HOME` at tmp_path so the state dir is isolated); second run's `additionalContext` == `""` and diagnostics `contextual_suppressed_dedup` == 1.
4. `test_below_floor_injects_nothing` — prompt sharing only ONE keyword with the seeded memory: `additionalContext` == `""`, `contextual_suppressed_floor` bumped.
5. `test_fired_counters` — UserPromptSubmit run bumps `contextual_fired_userprompt`; PreToolUse run bumps `contextual_fired_pretool`.
6. `test_exposure_write_failure_does_not_block_injection` — monkeypatch `SessionBootstrapService.record_exposures` (or backend method) to raise; `additionalContext` still contains the block; `hook_errors` gets a row OR the error is swallowed silently (assert non-empty context is the load-bearing claim).
7. Keep all existing envelope/mode/garbage-stdin tests passing unchanged.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/hooks/test_contextual_inject.py -q` — Expected: new tests FAIL.

- [ ] **Step 3: Implement** — replace the `try` body in `main()` of `contextual_inject.py`:

```python
    event = str(payload.get("hook_event_name") or "UserPromptSubmit")
    rendered = ""
    try:
        cfg = get_config()
        if _enabled(event, cfg.context_inject_mode):
            query = _query_from(payload, event)
            session_id = str(payload.get("session_id") or "")
            cwd = str(payload.get("cwd") or os.getcwd())
            project = project_name(Path(cwd))
            state_dir = cfg.home / "state"
            prune_stale(state_dir, now=datetime.now(UTC))
            seen = SeenStore(state_dir, session_id)
            seen.bump_turn()
            with closing(connect(cfg.memory_db)) as conn:
                _bump_diagnostic(
                    conn, cfg,
                    "contextual_fired_userprompt" if event == "UserPromptSubmit"
                    else "contextual_fired_pretool",
                )
                backend = build_backend(
                    config=cfg,
                    memory_conn=conn,
                    embedder=None,
                    session_id=session_id or None,
                    project=project,
                )
                items = retrieve_relevant(
                    backend, query=query, project=project,
                    min_hits=cfg.context_min_hits,
                    max_items=cfg.context_max_items,
                )
                had_candidates = bool(items)
                pairs = [(m.kind, m.id) for m in items]
                unseen = set(seen.filter_unseen(
                    pairs, reinject_turns=cfg.context_reinject_turns,
                ))
                items = [m for m in items if (m.kind, m.id) in unseen]
                if items:
                    rendered = format_relevant(items)
                    survivors = [(m.kind, m.id) for m in items]
                    try:
                        backend.record_exposures(
                            session_id=session_id,
                            items=survivors,
                            source="contextual",
                        )
                    except BaseException as exc:  # noqa: BLE001 - never block injection
                        try:
                            record_hook_error(hook_name="contextual_inject_exposure", exc=exc)
                        except BaseException:  # noqa: BLE001
                            pass
                    seen.mark_seen(survivors)
                    _bump_diagnostic(conn, cfg, "contextual_injected")
                elif had_candidates:
                    _bump_diagnostic(conn, cfg, "contextual_suppressed_dedup")
                else:
                    _bump_diagnostic(conn, cfg, "contextual_suppressed_floor")
    except BaseException as exc:  # noqa: BLE001
        ...  # existing error handling unchanged
```

Module-level helper (sqlite-only; agentcore mode skips silently):

```python
def _bump_diagnostic(conn, cfg, metric: str) -> None:
    """Best-effort observability counter. Sqlite mode only; never raises."""
    if cfg.storage_backend != "sqlite":
        return
    try:
        conn.execute(
            "UPDATE rating_diagnostics SET value = value + 1, updated_at = ? "
            "WHERE metric = ?",
            (datetime.now(UTC).isoformat(), metric),
        )
        conn.commit()
    except BaseException:  # noqa: BLE001
        pass
```

New imports: `from datetime import UTC, datetime`, `from better_memory.services.context_seen import SeenStore, prune_stale`. Update the module docstring (mode gating + new floor/dedup/exposure behaviour, PreToolUse reliability note stays).

Note: `contextual_suppressed_floor` also increments when the query had no keywords or nothing matched at all — that is fine (it means "no injection because nothing cleared the relevance bar").

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/hooks/test_contextual_inject.py tests/hooks/ -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/hooks/contextual_inject.py tests/hooks/test_contextual_inject.py
git commit -m "feat(hooks): contextual_inject floor + dedup + exposure tracking + diagnostics"
```

---

### Task 9: Bootstrap slimming — top-N full render + index + affordance + full ids + ages

**Files:**
- Modify: `better_memory/services/session_bootstrap.py` (render helpers + `bootstrap()`)
- Modify: `better_memory/hooks/session_bootstrap.py` ONLY if it passes render kwargs (it does not — no change expected)
- Test: `tests/services/test_session_bootstrap.py` (extend), `tests/hooks/test_session_bootstrap.py` (spot-check envelope unchanged)

**Interfaces:**
- Consumes: `Config.bootstrap_top_n` (Task 1). `SessionBootstrapService.__init__` gains `top_n: int | None = None` kwarg (None -> read from `get_config().bootstrap_top_n` at bootstrap() time so tests can inject).
- Produces: new rendering; `BootstrapResult` unchanged; exposure rows only for fully-rendered items.

**Confidence: 90%**

- [ ] **Step 1: Write failing tests** (extend `tests/services/test_session_bootstrap.py`, reusing its seed helpers; add kwargs to helpers if a scope/updated_at knob is missing per [[1ad537fd]]):

Test list:
1. `test_top_n_limits_full_renders` — seed 8 project semantic + 8 do-reflections, `top_n=2`: exactly 2 semantic full lines (`- [<full 32-char id>]`), 2 reflection blocks (`_id: ...`), and the rest appear as index lines under an `### Index` heading (assert on a seeded title appearing exactly once, in the index section, without its id).
2. `test_general_semantic_always_full` — seed 3 general-scope + 3 project-scope semantic, `top_n=1`: all 3 general render full; only 1 project-scope renders full.
3. `test_full_ids_never_truncated` — full 32-char semantic id present in output; the 8-char prefix-only form (`[abcd1234]` for a longer id) absent.
4. `test_age_stamp_present` — seeded `updated_at` 10 days before injected clock: `(10d old)` in the full-render line.
5. `test_affordance_footer_counts` — with 6 indexed items: footer contains `6 more memories are indexed above` and names `memory_retrieve`.
6. `test_top_n_zero_is_legacy_full_dump` — `top_n=0`: every seeded memory fully rendered, no `### Index` section.
7. `test_exposures_only_for_full_renders` — with `top_n=1`: `session_memory_exposure` contains rows only for the fully-rendered ids.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/services/test_session_bootstrap.py -q` — Expected: FAIL.

- [ ] **Step 3: Implement.** In `services/session_bootstrap.py`:

Constructor + age helper:

```python
    def __init__(
        self,
        conn,
        *,
        clock: Callable[[], datetime] | None = None,
        top_n: int | None = None,
    ) -> None:
        self._conn = conn
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._episodes = EpisodeService(conn)
        self._top_n = top_n
```

```python
def _age_suffix(iso_ts: str | None, now: datetime) -> str:
    if not iso_ts:
        return ""
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return f" ({max(0, (now - ts).days)}d old)"
```

Render changes inside `bootstrap()` (after the buckets/semantic fetch, replacing the current section assembly):

```python
        if self._top_n is not None:
            top_n = self._top_n
        else:
            from better_memory.config import get_config
            top_n = get_config().bootstrap_top_n

        now = self._clock()

        if top_n == 0:
            sem_full, sem_index = list(semantic), []
        else:
            general = [m for m in semantic if m.scope == "general"]
            project_scoped = [m for m in semantic if m.scope != "general"]
            sem_full = general + project_scoped[:top_n]
            sem_index = project_scoped[top_n:]

        flat_reflections = (
            [("do", r) for r in buckets["do"]]
            + [("dont", r) for r in buckets["dont"]]
            + [("neutral", r) for r in buckets["neutral"]]
        )
        if top_n == 0:
            refl_full, refl_index = flat_reflections, []
        else:
            refl_full = flat_reflections[:top_n]
            refl_index = flat_reflections[top_n:]
```

New render helpers (replace `_render_semantic` / keep `_render_reflection_bucket` for the full items, grouped by polarity of the survivors):

```python
def _render_semantic_full(items, now: datetime) -> tuple[str, list[str]]:
    if not items:
        return "", []
    lines = [f"### Semantic memories ({len(items)} shown in full)"]
    ids: list[str] = []
    for m in items:
        lines.append(f"- [{m.id}]{_age_suffix(m.updated_at, now)} {_truncate(m.content)}")
        ids.append(m.id)
    return "\n".join(lines), ids


def _render_reflections_full(pairs, now: datetime) -> tuple[str, list[str]]:
    """pairs: list of (polarity, reflection-dict) already capped to top-N."""
    if not pairs:
        return "", []
    blocks: list[str] = []
    ids: list[str] = []
    for polarity, item in pairs:
        label = {"do": "do", "dont": "dont (pitfall - do the corrective action)",
                 "neutral": "neutral"}[polarity]
        lines = [
            f"**{item['title']}** [{label}]"
            f"{_age_suffix(item.get('updated_at'), now)}",
            f"_{item['use_cases']}_",
        ]
        for hint in item.get("hints", []):
            lines.append(f"- {_truncate(hint)}")
        lines.append(f"_id: {item['id']}_")
        blocks.append("\n".join(lines))
        ids.append(item["id"])
    return "### Reflections (shown in full)\n" + "\n\n".join(blocks), ids


def _render_index(sem_index, refl_index) -> tuple[str, int]:
    n = len(sem_index) + len(refl_index)
    if n == 0:
        return "", 0
    lines = ["### Index (not expanded - retrieve on demand)"]
    for polarity, item in refl_index:
        conf = item.get("confidence")
        conf_s = f", conf {conf:.1f}" if isinstance(conf, (int, float)) else ""
        lines.append(f"- {item['title']} ({polarity}{conf_s})")
    for m in sem_index:
        first_line = (m.content or "").splitlines()[0][:100]
        lines.append(f"- {first_line} (semantic)")
    return "\n".join(lines), n
```

Section assembly + footer:

```python
        sections = [_render_header(...)]  # unchanged header call
        sem_section, semantic_ids = _render_semantic_full(sem_full, now)
        if sem_section:
            sections.append(sem_section)
        refl_section, reflection_ids = _render_reflections_full(refl_full, now)
        if refl_section:
            sections.append(refl_section)
        index_section, index_count = _render_index(sem_index, refl_index)
        if index_section:
            sections.append(index_section)
        sections.append("---")
        footer = _FOOTER
        if index_count:
            footer = (
                f"{index_count} more memories are indexed above - call "
                "mcp__better-memory__memory_retrieve or "
                "mcp__better-memory__memory_retrieve_observations when a task "
                "touches them.\n" + _FOOTER
            )
        sections.append(footer)
        rendered = "\n\n".join(sections)

        self._record_exposure(
            session_id=session_id,
            reflection_ids=reflection_ids,
            semantic_ids=semantic_ids,
        )
```

Notes: reflection dicts carry `updated_at` after Task 3. Semantic full-render fixes the 8-char truncation bug (`m.id`, not `m.id[:8]`). `BootstrapResult` counts stay as the RETRIEVED counts (unchanged semantics: what exists, not what rendered) — do not change count fields.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_session_bootstrap.py tests/hooks/test_session_bootstrap.py tests/mcp/ -q`
Expected: PASS. Fix any MCP bootstrap handler test that asserted the old semantic `[:8]` rendering.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/session_bootstrap.py tests/services/test_session_bootstrap.py tests/hooks/test_session_bootstrap.py
git commit -m "feat(bootstrap): top-N full render + index + retrieve affordance; full ids and age stamps"
```

---

### Task 10: Stop-hook directive — per-source labels and counts

**Files:**
- Modify: `better_memory/hooks/session_close.py:138-184` (`_emit_rating_directive_if_unrated`)
- Test: `tests/hooks/test_session_close_rating_directive.py` (extend)

**Interfaces:**
- Consumes: `session_memory_exposure.source` values including `'contextual'` (Task 2).
- Produces: directive lines formatted `- <id> [<source>]: <display>`; header gains per-source counts.

**Confidence: 93%**

- [ ] **Step 1: Write failing test** (extend the existing directive test file, reusing its seeding helpers; add a `source=` kwarg to the seed helper if absent):

```python
def test_directive_shows_source_labels_and_counts(seeded_session):
    # seed: one bootstrap reflection exposure, one contextual semantic exposure
    out = run_stop_hook(seeded_session)  # existing helper idiom
    directive = out["hookSpecificOutput"]["additionalContext"]
    assert "[bootstrap]" in directive
    assert "[contextual]" in directive
    assert "sources: bootstrap 1, contextual 1" in directive
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/hooks/test_session_close_rating_directive.py -q` — Expected: FAIL.

- [ ] **Step 3: Implement.** In `_emit_rating_directive_if_unrated`:
  - Add `MIN(e.source) AS source` to the SELECT column list (after `MIN(e.exposed_at) AS exposed_at`).
  - Line rendering becomes:

```python
        source_counts: dict[str, int] = {}
        for r in rows:
            display = (r["display"] or "")[:TRUNC]
            source = r["source"] or "bootstrap"
            source_counts[source] = source_counts.get(source, 0) + 1
            line = f"- {r['memory_id']} [{source}]: {display}"
            if r["memory_kind"] == "reflection":
                refl_lines.append(line)
            else:
                sem_lines.append(line)
        counts_line = "sources: " + ", ".join(
            f"{k} {v}" for k, v in sorted(source_counts.items())
        )
```

  - Insert `counts_line` into the directive right after the first sentence:

```python
        directive = (
            "RATE_MEMORIES - before this session ends, classify the "
            "memories that were exposed during this session and that you "
            "did NOT already credit via memory.credit.\n"
            f"({counts_line})\n\n"
            ...  # remainder unchanged
        )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/hooks/test_session_close_rating_directive.py tests/hooks/test_session_close_agentcore.py -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/hooks/session_close.py tests/hooks/test_session_close_rating_directive.py
git commit -m "feat(hooks): rating directive shows per-source labels and counts"
```

---

### Task 11: E2E integration test — contextual injection to rating credit

**Files:**
- Test: `tests/integration/test_contextual_rating_e2e.py` (new; marked `integration` per the pattern in `tests/integration/test_memory_rating_e2e.py` — read that file first and mirror its fixtures/marks)

**Interfaces:** consumes everything above; no production code.

**Confidence: 90%**

- [ ] **Step 1: Write the test** (single test, full loop, sqlite backend):

```python
@pytest.mark.integration
def test_contextual_injection_full_rating_loop(migrated_conn, monkeypatch, tmp_path):
    """contextual exposure -> list_session_exposures includes it ->
    apply_session_ratings('cited') bumps useful_count -> next retrieval ranks it up."""
    # 1. Seed one reflection whose title shares >= 2 keywords with the query.
    # 2. Build SqliteBackend(session_id='e2e-sess', project='proj').
    # 3. items = retrieve_relevant(backend, query='pytest windows redirect', project='proj')
    #    assert the seeded reflection is in items.
    # 4. backend.record_exposures(session_id='e2e-sess',
    #        items=[(m.kind, m.id) for m in items], source='contextual')
    # 5. exposures = backend.list_session_exposures(session_id='e2e-sess')
    #    assert the id present with source == 'contextual'.
    # 6. backend.apply_session_ratings(session_id='e2e-sess',
    #        ratings=[{'kind': 'reflection', 'id': <id>, 'class': 'cited'}])
    # 7. assert reflections.useful_count == 1 for that id (direct SELECT).
```

Write the real code following `test_memory_rating_e2e.py`'s seeding idiom — every numbered step above becomes real statements, no placeholders in the committed test.

- [ ] **Step 2: Run**

Run: `uv run pytest tests/integration/test_contextual_rating_e2e.py -m integration -q`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_contextual_rating_e2e.py
git commit -m "test(integration): contextual injection to rating credit e2e loop"
```

---

### Task 12: Docs sweep

**Files:**
- Modify: `README.md`, `website/configuration.md`, `website/architecture.md`, `website/observation-lifecycle.md`, `website/mcp-tools.md` (only if tool docs mention exposure sources), `website/index.md` (only if counts/claims drift)

**Confidence: 95%**

- [ ] **Step 1: Update docs.**
  - `website/configuration.md` env-var table: add the four new vars with defaults + one-line meanings (exact names from Global Constraints).
  - `website/architecture.md`: contextual injection section — describe floor, dedup seen-file, `<project-memory>` block, `source='contextual'` exposures; bootstrap section — top-N + index + affordance.
  - `website/observation-lifecycle.md`: rating loop now covers contextual exposures; directive shows per-source labels.
  - `README.md`: env-var mentions + any description of the contextual hook / bootstrap dump; verify no stale claims about "all semantic memories injected".
  - Verify every factual token (env var names, defaults, table/column names, metric names) against the code written in Tasks 1-10 — do not carry forward from this plan without checking.
- [ ] **Step 2: Full suite + lint**

Run: `uv run pytest -q > pytest_final.txt 2>&1` then read the tail; and `uv run ruff check .`
Expected: all green.

- [ ] **Step 3: Commit**

```bash
git add README.md website/
git commit -m "docs: attention-first injection - env vars, contextual flow, bootstrap slimming"
```

---

## Self-review notes

- Spec coverage: C1=Task 9, C2=Tasks 3+4, C3=Task 5, C4=Task 6, C5=Tasks 2+7+8, C6=Tasks 8 (diagnostics) + 10 (directive) + 5 (affordance line), C7=Tasks 1+12, E2E=Task 11. Out-of-scope items untouched.
- Type consistency: `record_exposures(*, session_id, items: list[tuple[str, str]], source)` identical in Tasks 7 and 8; `retrieve_relevant(backend, *, query, project, min_hits, max_items, include_neutral, now)` identical in Tasks 4, 8, 11; `format_relevant(items)` (no max_items) in Tasks 5 and 8; `SeenStore(state_dir, session_id)` in Tasks 6 and 8; config field names in Tasks 1, 8, 9.
- Known judgment calls implementers must NOT re-litigate: floor counts DISTINCT keyword hits; title hits add (not multiply) into score; `dont` items render with the literal prefix `Known pitfall -- do this instead: `; index items get no ids; diagnostics are sqlite-only.
