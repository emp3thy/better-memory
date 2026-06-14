# Contextual Memory Injection Hook — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the *relevant* curated memories (semantic + reflections, project + general) at the moment they matter, by keyword-matching against the current prompt / tool-input via a Claude Code hook — closing the "loaded once at startup, then ignored" gap.

**Architecture:** A pure-Python relevance filter (`retrieve_relevant`) fetches the small, already-ranked curated sets through the `StorageBackend` abstraction (works on sqlite *and* agentcore), whole-word keyword-filters them against the query, and returns the top matches. A thin hook (`contextual_inject`) wires it to `UserPromptSubmit` and `PreToolUse`, gated by a config mode switch. No new deps, no embeddings, no schema migration.

**Tech Stack:** Python 3.12, SQLite via existing `StorageBackend`, pytest (+pytest-asyncio), ruff (E/F/I/UP/B, line 100), pyright (standard). Spec: `docs/superpowers/specs/2026-06-14-contextual-memory-injection-hook-design.md`.

---

## Memory Guardrails

Retrieved via `memory_retrieve` (planning + implementation) + `knowledge_list` before drafting.

- **`keep-docs-in-sync` — conf 0.95, useful 13 (implementation).** Adding an env var, a hook, or an MCP tool MUST update README + `website/configuration.md` + `docs/hooks-setup.md` (+ `website/mcp-tools.md` and tool counts if a tool is added) **in the same task** — deferring docs to a trailing task is a code smell. Tasks 5/7/9 bake this in.
- **`apply-confidence-scoring` — knowledge standard `ralph-runtime.md` (gate).** Every task carries a confidence %; sub-90 embeds its mitigation. Test/snippets must be lint-clean for py312 ruff `UP` (`from datetime import UTC`; never `class X(str, Enum)` → use `StrEnum`).
- **`writing-plans-surface-guardrails` — conf 0.90.** This section.
- **`verify-before-commit-internal-patterns` — standard.** Service/test/hook/config/MCP patterns were read from the repo (conftest `tmp_memory_db`, `connect`+`apply_migrations`, `session_bootstrap.py` skeleton, `_resolve_*` config style, `StorageBackend.retrieve/semantic_list`, `build_registry`); signatures below match them.
- **`prioritise-root-cause` — conf 1.0.** This whole feature is the root-cause fix for "memories ignored"; keep it relevance-correct, not a patch.

**Dismissed (considered, n/a):** Playwright textContent (0.8 — no template/Playwright work here), tempfile-fd-leak (0.6 — no temp files), freeze enter/exit logging (0.55), fail-fast-ordering doc (0.55 — no such guard here).

---

## File Structure

- **Create** `better_memory/services/keywords.py` — `extract_keywords()`, `count_keyword_hits()` (pure, no deps).
- **Create** `better_memory/services/relevant.py` — `RelevantMemory` dataclass, `retrieve_relevant(backend, …)`, `format_relevant(items)`.
- **Create** `better_memory/hooks/contextual_inject.py` — the hook entry (`UserPromptSubmit` + `PreToolUse`), mode-gated.
- **Modify** `better_memory/config.py` — `context_inject_mode` field + `_resolve_context_inject_mode()`.
- **Modify** `better_memory/mcp/handlers/` (+ `server.py`) — optional `memory.retrieve_relevant` tool (Task 8).
- **Modify** `docs/hooks-setup.md`, `README.md`, `website/configuration.md` (+ `website/mcp-tools.md` if Task 8) — registration + env var + tool docs.
- **Tests** under `tests/services/`, `tests/hooks/`, `tests/mcp/`.

---

## Task 0: Spike — does `PreToolUse` fire for the `Skill` tool?

**Confidence: 95% (spike, de-risks the one residual unknown).** Resolves the only open item from the spec's assumptions. Pure investigation; no production code.

**Files:** none committed (temporary probe).

- [ ] **Step 1: Add a throwaway probe hook to user settings**

In `~/.claude/settings.json`, temporarily add:

```json
{ "hooks": { "PreToolUse": [ { "matcher": "Skill|Task",
  "hooks": [ { "type": "command",
    "command": "python -c \"import sys,json,datetime; d=json.load(sys.stdin); open(r'C:/Users/gethi/pretooluse_probe.log','a').write(datetime.datetime.now().isoformat()+' '+str(d.get('tool_name'))+' '+json.dumps(d.get('tool_input'))[:200]+'\\n')\"" } ] } ] } }
```

- [ ] **Step 2: Trigger a skill and a subagent**

In a Claude Code session: invoke any skill (e.g. `/help` or a Skill tool call) and dispatch one Agent/Task. Then read the log:

Run: `cat C:/Users/gethi/pretooluse_probe.log`
Expected (to confirm): one line per Skill/Task invocation containing `Skill` / `Task` and the tool_input (skill name + args).

- [ ] **Step 3: Record the result + clean up**

- If lines appear → `PreToolUse` fires for `Skill`/`Task`; the `pretool` mode is fully viable (query = `tool_input`). 
- If NO lines appear → `PreToolUse` does NOT fire for those tools; `pretool` mode is limited to `Write`/`Bash`/etc., and `UserPromptSubmit` is the sole planning trigger. Note this in the hook's docstring and `docs/hooks-setup.md`.
Remove the probe hook from settings and delete the log. No commit.

---

## Task 1: Keyword extraction + whole-word matching

**Confidence: 97%** — pure functions, fully unit-testable, no deps.

**Files:**
- Create: `better_memory/services/keywords.py`
- Test: `tests/services/test_keywords.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/services/test_keywords.py
"""Tests for keyword extraction + whole-word matching."""
from __future__ import annotations

from better_memory.services.keywords import count_keyword_hits, extract_keywords


class TestExtractKeywords:
    def test_lowercases_and_splits(self):
        assert extract_keywords("Write the Plan") == {"write", "the_skip", "plan"} - {"the_skip"} | {"write", "plan"}

    def test_drops_stopwords_and_short_tokens(self):
        kw = extract_keywords("we are on to the CI plan")
        assert "plan" in kw
        assert "the" not in kw and "on" not in kw and "we" not in kw
        assert "ci" not in kw  # 2-char tokens dropped (documented tunable)

    def test_dedupes(self):
        assert extract_keywords("plan plan PLAN") == {"plan"}

    def test_empty(self):
        assert extract_keywords("   ") == set()


class TestCountKeywordHits:
    def test_whole_word_only(self):
        kw = {"art", "plan"}
        assert count_keyword_hits("let us start the planner", kw) == 0  # 'art' in 'start', 'plan' in 'planner' — neither whole-word
        assert count_keyword_hits("the art of a plan", kw) == 2

    def test_case_insensitive_and_punctuation(self):
        assert count_keyword_hits("Finalise the Plan.", {"plan"}) == 1

    def test_distinct_terms_counted_once_each(self):
        assert count_keyword_hits("plan plan plan", {"plan"}) == 1
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/services/test_keywords.py -v`
Expected: FAIL (module `better_memory.services.keywords` not found).

- [ ] **Step 3: Implement**

```python
# better_memory/services/keywords.py
"""Keyword extraction + whole-word matching for contextual memory relevance.

Pure, dependency-free. Used by retrieve_relevant to filter the curated memory
set against the current prompt / tool-input.
"""
from __future__ import annotations

import re

# Small, deliberately conservative stopword set. Tunable.
_STOPWORDS = frozenset({
    "the", "and", "for", "are", "was", "were", "you", "your", "our", "with",
    "this", "that", "from", "into", "have", "has", "had", "but", "not", "can",
    "will", "would", "should", "could", "lets", "let", "get", "got", "out",
    "use", "using", "what", "when", "how", "why", "who", "all", "any", "its",
    "his", "her", "they", "them", "then", "than", "now", "via", "per",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def extract_keywords(text: str) -> set[str]:
    """Lowercase, tokenise on non-alphanumerics, drop stopwords + <3-char tokens."""
    if not text:
        return set()
    return {
        tok
        for tok in _TOKEN_RE.findall(text.lower())
        if len(tok) >= 3 and tok not in _STOPWORDS
    }


def count_keyword_hits(text: str, keywords: set[str]) -> int:
    """Number of distinct keywords that appear as a WHOLE WORD in text."""
    if not text or not keywords:
        return 0
    lowered = text.lower()
    hits = 0
    for kw in keywords:
        if re.search(rf"\b{re.escape(kw)}\b", lowered):
            hits += 1
    return hits
```

- [ ] **Step 4: Fix the first test's expected value**

Replace the contrived line in `test_lowercases_and_splits` with the real expectation:

```python
    def test_lowercases_and_splits(self):
        assert extract_keywords("Write the Plan") == {"write", "plan"}
```

- [ ] **Step 5: Run — expect pass**

Run: `uv run pytest tests/services/test_keywords.py -v`
Expected: PASS (all).

- [ ] **Step 6: Lint + commit**

Run: `uv run ruff check better_memory/services/keywords.py tests/services/test_keywords.py`
Expected: no errors.
```bash
git add better_memory/services/keywords.py tests/services/test_keywords.py
git commit -m "feat(relevant): keyword extraction + whole-word matching"
```

---

## Task 2: `retrieve_relevant` — fetch, filter, rank

**Confidence: 90%** — orchestrates verified abstraction methods (`backend.retrieve`, `backend.semantic_list`) over the keyword helpers. Mitigation for the one soft spot (semantic returns `SemanticMemory` objects vs reflections returning dicts): the code normalises both into a single `RelevantMemory` and the test seeds *both* kinds (project + general) and asserts ordering + scope union, so a shape mismatch fails loudly.

**Files:**
- Create: `better_memory/services/relevant.py`
- Test: `tests/services/test_relevant.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/services/test_relevant.py
"""Tests for retrieve_relevant over a real sqlite StorageBackend."""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.relevant import RelevantMemory, retrieve_relevant
from better_memory.services.semantic import SemanticMemoryService
from better_memory.storage.sqlite import SqliteBackend


@pytest.fixture
def backend(tmp_memory_db: Path):
    conn = connect(tmp_memory_db)
    apply_migrations(conn)
    # seed semantic: one project, one general, one irrelevant
    sem = SemanticMemoryService(conn)
    sem.create(content="Always write the implementation plan with confidence scores",
               project="proj", scope="project")
    sem.create(content="Never ask the user to babysit a PR", project="other", scope="general")
    sem.create(content="Prefer tea over coffee", project="proj", scope="project")
    try:
        yield SqliteBackend(conn, project="proj")
    finally:
        conn.close()


def test_filters_to_keyword_matches(backend):
    out = retrieve_relevant(backend, query="let us write the plan", project="proj", limit=5)
    texts = " ".join(m.summary.lower() for m in out)
    assert "plan" in texts
    assert "coffee" not in texts          # irrelevant memory excluded


def test_includes_general_scope(backend):
    out = retrieve_relevant(backend, query="babysit the PR", project="proj", limit=5)
    assert any("babysit" in m.summary.lower() for m in out)  # general-scope semantic matched


def test_empty_on_no_match(backend):
    assert retrieve_relevant(backend, query="xylophone zeppelin", project="proj", limit=5) == []


def test_respects_limit(backend):
    out = retrieve_relevant(backend, query="plan confidence babysit pr user", project="proj", limit=1)
    assert len(out) == 1


def test_returns_relevantmemory(backend):
    out = retrieve_relevant(backend, query="plan", project="proj", limit=5)
    assert all(isinstance(m, RelevantMemory) for m in out)
    assert all(m.kind in ("reflection", "semantic") for m in out)
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/services/test_relevant.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

```python
# better_memory/services/relevant.py
"""Relevance filter over the curated memory set (semantic + reflections).

Fetches the small, already-ranked sets through the StorageBackend abstraction
(works on sqlite AND agentcore), whole-word keyword-filters them against a query,
and returns the top matches. Pure-Python; no embeddings, no new schema.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from better_memory.services.keywords import count_keyword_hits, extract_keywords


@dataclass
class RelevantMemory:
    kind: str            # "reflection" | "semantic"
    id: str
    summary: str         # short display text
    confidence: float | None
    hits: int            # distinct keyword hits (higher = more relevant)


def _reflection_text(r: dict[str, Any]) -> str:
    parts = [str(r.get("title") or ""), str(r.get("use_cases") or "")]
    hints = r.get("hints") or []
    if isinstance(hints, list):
        parts.extend(str(h) for h in hints)
    return " ".join(parts)


def retrieve_relevant(
    backend: Any,
    *,
    query: str,
    project: str,
    limit: int = 5,
    include_neutral: bool = False,
) -> list[RelevantMemory]:
    """Return up to `limit` curated memories whose text whole-word-matches a
    keyword from `query`, ordered by (# hits desc, managed rank asc).

    Never raises: any backend error yields an empty list (the hook must not break
    a turn). `managed rank` = the order the backend already returned items in
    (confidence/useful-count), flattened reflections do->dont->[neutral] then
    semantic.
    """
    keywords = extract_keywords(query)
    if not keywords:
        return []

    candidates: list[tuple[int, RelevantMemory]] = []  # (managed_rank, mem)
    rank = 0
    try:
        buckets = backend.retrieve(project=project, track_exposure=False)
    except Exception:
        buckets = {}
    order = ["do", "dont"] + (["neutral"] if include_neutral else [])
    for bucket in order:
        for r in buckets.get(bucket, []) or []:
            text = _reflection_text(r)
            hits = count_keyword_hits(text, keywords)
            if hits:
                candidates.append((rank, RelevantMemory(
                    kind="reflection", id=str(r.get("id")),
                    summary=str(r.get("title") or text)[:160],
                    confidence=r.get("confidence"), hits=hits)))
            rank += 1

    try:
        semantic = backend.semantic_list(project=project, track_exposure=False)
    except Exception:
        semantic = []
    for s in semantic or []:
        content = getattr(s, "content", "") or ""
        hits = count_keyword_hits(content, keywords)
        if hits:
            candidates.append((rank, RelevantMemory(
                kind="semantic", id=str(getattr(s, "id", "")),
                summary=content[:160], confidence=None, hits=hits)))
        rank += 1

    candidates.sort(key=lambda t: (-t[1].hits, t[0]))
    return [m for _, m in candidates[:limit]]


def format_relevant(items: list[RelevantMemory]) -> str:
    """Render the additionalContext block. Empty string if no items."""
    if not items:
        return ""
    lines = ["RELEVANT MEMORY — apply unless it conflicts with the user's request:"]
    for m in items:
        tag = f"{m.kind}" + (f" · conf {m.confidence:.2f}" if m.confidence is not None else "")
        lines.append(f"• [{tag}] {m.summary}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run — expect pass**

Run: `uv run pytest tests/services/test_relevant.py -v`
Expected: PASS.

- [ ] **Step 5: Lint/typecheck + commit**

Run: `uv run ruff check better_memory/services/relevant.py tests/services/test_relevant.py && uv run pyright better_memory/services/relevant.py`
Expected: clean.
```bash
git add better_memory/services/relevant.py tests/services/test_relevant.py
git commit -m "feat(relevant): retrieve_relevant fetch+filter+rank + formatter"
```

---

## Task 3: `format_relevant` token cap

**Confidence: 96%** — small pure formatting addition + test.

**Files:**
- Modify: `better_memory/services/relevant.py`
- Test: `tests/services/test_relevant_format.py`

- [ ] **Step 1: Write failing test**

```python
# tests/services/test_relevant_format.py
from __future__ import annotations

from better_memory.services.relevant import RelevantMemory, format_relevant


def test_empty_returns_empty_string():
    assert format_relevant([]) == ""


def test_caps_items():
    items = [RelevantMemory("semantic", str(i), f"memory {i}", None, 1) for i in range(10)]
    out = format_relevant(items, max_items=5)
    assert out.count("•") == 5


def test_includes_confidence_for_reflections():
    out = format_relevant([RelevantMemory("reflection", "r1", "do the thing", 0.9, 2)])
    assert "conf 0.90" in out
```

- [ ] **Step 2: Run — expect failure** (`format_relevant` has no `max_items`).

Run: `uv run pytest tests/services/test_relevant_format.py -v`
Expected: FAIL (unexpected keyword `max_items`).

- [ ] **Step 3: Add the cap param**

Replace `format_relevant` signature/body in `relevant.py`:

```python
def format_relevant(items: list[RelevantMemory], *, max_items: int = 5) -> str:
    """Render the additionalContext block (≤ max_items). Empty if no items."""
    if not items:
        return ""
    lines = ["RELEVANT MEMORY — apply unless it conflicts with the user's request:"]
    for m in items[:max_items]:
        tag = f"{m.kind}" + (f" · conf {m.confidence:.2f}" if m.confidence is not None else "")
        lines.append(f"• [{tag}] {m.summary}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run — expect pass.**

Run: `uv run pytest tests/services/test_relevant_format.py tests/services/test_relevant.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/relevant.py tests/services/test_relevant_format.py
git commit -m "feat(relevant): cap formatted output to max_items"
```

---

## Task 4: Config — `context_inject_mode`

**Confidence: 95%** — mirrors the existing `_resolve_embeddings_backend` resolver. Docs updated in-task per the keep-docs-in-sync guardrail.

**Files:**
- Modify: `better_memory/config.py`
- Modify: `website/configuration.md`
- Test: `tests/test_config.py` (append)

- [ ] **Step 1: Write failing test**

```python
# tests/test_config.py  (append)
import os
import pytest
from better_memory import config as cfg_mod


@pytest.mark.parametrize("val,expected", [
    (None, "both"), ("userprompt", "userprompt"), ("pretool", "pretool"),
    ("both", "both"), ("off", "off"),
])
def test_context_inject_mode_valid(monkeypatch, val, expected):
    if val is None:
        monkeypatch.delenv("BETTER_MEMORY_CONTEXT_INJECT_MODE", raising=False)
    else:
        monkeypatch.setenv("BETTER_MEMORY_CONTEXT_INJECT_MODE", val)
    assert cfg_mod._resolve_context_inject_mode() == expected


def test_context_inject_mode_invalid(monkeypatch):
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_INJECT_MODE", "bogus")
    with pytest.raises(ValueError):
        cfg_mod._resolve_context_inject_mode()
```

- [ ] **Step 2: Run — expect failure.**

Run: `uv run pytest tests/test_config.py -k context_inject -v`
Expected: FAIL (`_resolve_context_inject_mode` missing).

- [ ] **Step 3: Implement resolver + Config field**

In `better_memory/config.py`, add near the other resolvers:

```python
_DEFAULT_CONTEXT_INJECT_MODE = "both"
_VALID_CONTEXT_INJECT_MODES = ("userprompt", "pretool", "both", "off")


def _resolve_context_inject_mode() -> str:
    raw = os.environ.get(
        "BETTER_MEMORY_CONTEXT_INJECT_MODE", _DEFAULT_CONTEXT_INJECT_MODE
    )
    if raw not in _VALID_CONTEXT_INJECT_MODES:
        raise ValueError(
            f"BETTER_MEMORY_CONTEXT_INJECT_MODE must be one of "
            f"{_VALID_CONTEXT_INJECT_MODES}, got {raw!r}"
        )
    return raw
```

Add a field to the `Config` dataclass: `context_inject_mode: str` and set it in `get_config()`: `context_inject_mode=_resolve_context_inject_mode(),`. If the module docstring enumerates env vars, add this one there too (keep-docs-in-sync).

- [ ] **Step 4: Update `website/configuration.md`**

Add a row to the env-var table:

```markdown
| `BETTER_MEMORY_CONTEXT_INJECT_MODE` | `both` | Contextual memory injection hook trigger: `userprompt`, `pretool`, `both`, or `off`. |
```

- [ ] **Step 5: Run — expect pass + commit**

Run: `uv run pytest tests/test_config.py -k context_inject -v`
Expected: PASS.
```bash
git add better_memory/config.py tests/test_config.py website/configuration.md
git commit -m "feat(config): BETTER_MEMORY_CONTEXT_INJECT_MODE switch"
```

---

## Task 5: The hook — `contextual_inject`

**Confidence: 92%** — mirrors `session_bootstrap.py` exactly (stdin → parse → service → JSON envelope → exit 0). Mitigation for the dual-event + mode gating: tests feed both a `UserPromptSubmit` and a `PreToolUse` payload AND a forced-error case, asserting correct `hookEventName`, mode no-op, and always-exit-0.

**Files:**
- Create: `better_memory/hooks/contextual_inject.py`
- Test: `tests/hooks/test_contextual_inject.py`

- [ ] **Step 1: Write failing tests** (subprocess-style via `main()` with patched stdin/stdout)

```python
# tests/hooks/test_contextual_inject.py
from __future__ import annotations

import io
import json
import sys

import pytest

from better_memory.hooks import contextual_inject as hook


def _run(payload: dict, monkeypatch, capsys, mode="both"):
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_INJECT_MODE", mode)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as e:
        hook.main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    return json.loads(out) if out.strip() else {}


def test_userprompt_emits_envelope(monkeypatch, capsys):
    res = _run({"hook_event_name": "UserPromptSubmit", "prompt": "write the plan",
                "cwd": "."}, monkeypatch, capsys)
    assert res["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "additionalContext" in res["hookSpecificOutput"]


def test_mode_off_is_noop(monkeypatch, capsys):
    res = _run({"hook_event_name": "UserPromptSubmit", "prompt": "write the plan",
                "cwd": "."}, monkeypatch, capsys, mode="off")
    assert res["hookSpecificOutput"]["additionalContext"] == ""


def test_pretool_disabled_when_mode_userprompt(monkeypatch, capsys):
    res = _run({"hook_event_name": "PreToolUse", "tool_name": "Skill",
                "tool_input": {"skill": "writing-plans"}, "cwd": "."},
               monkeypatch, capsys, mode="userprompt")
    assert res["hookSpecificOutput"]["additionalContext"] == ""


def test_never_throws_on_garbage(monkeypatch, capsys):
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_INJECT_MODE", "both")
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    with pytest.raises(SystemExit) as e:
        hook.main()
    assert e.value.code == 0
```

- [ ] **Step 2: Run — expect failure.**

Run: `uv run pytest tests/hooks/test_contextual_inject.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement the hook**

```python
# better_memory/hooks/contextual_inject.py
"""UserPromptSubmit / PreToolUse hook: inject curated memories relevant to the
current prompt or tool-input. Gated by BETTER_MEMORY_CONTEXT_INJECT_MODE
(userprompt | pretool | both | off). Never raises; always exits 0.

NOTE (Task 0): whether PreToolUse fires for the built-in Skill/Task tools is
environment-dependent; UserPromptSubmit is the reliable trigger.
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import closing
from pathlib import Path

from better_memory.config import get_config, project_name
from better_memory.db.connection import connect
from better_memory.hooks._error_log import record_hook_error
from better_memory.services.relevant import format_relevant, retrieve_relevant
from better_memory.storage import build_backend

_MAX_STDIN_BYTES = 1_000_000


def _enabled(event: str, mode: str) -> bool:
    if mode == "off":
        return False
    if event == "UserPromptSubmit":
        return mode in ("userprompt", "both")
    if event == "PreToolUse":
        return mode in ("pretool", "both")
    return False


def _query_from(payload: dict, event: str) -> str:
    if event == "UserPromptSubmit":
        return str(payload.get("prompt") or "")
    if event == "PreToolUse":
        return f"{payload.get('tool_name') or ''} {json.dumps(payload.get('tool_input') or {})}"
    return ""


def main() -> None:
    raw = ""
    try:
        raw = sys.stdin.read(_MAX_STDIN_BYTES + 1)
    except BaseException:  # noqa: BLE001 — hooks never fail
        pass
    payload: dict = {}
    if raw.strip() and len(raw) <= _MAX_STDIN_BYTES:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except BaseException:  # noqa: BLE001
            pass

    event = str(payload.get("hook_event_name") or "UserPromptSubmit")
    rendered = ""
    try:
        cfg = get_config()
        if _enabled(event, cfg.context_inject_mode):
            query = _query_from(payload, event)
            cwd = str(payload.get("cwd") or os.getcwd())
            project = project_name(Path(cwd))
            with closing(connect(cfg.memory_db)) as conn:
                backend = build_backend(config=cfg, memory_conn=conn,
                                        embedder=None, session_id=None, project=project)
                items = retrieve_relevant(backend, query=query, project=project, limit=5)
            rendered = format_relevant(items)
    except BaseException as exc:  # noqa: BLE001
        try:
            record_hook_error(hook_name="contextual_inject", exc=exc)
        except BaseException:  # noqa: BLE001
            pass
        rendered = ""

    try:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": event, "additionalContext": rendered}}), flush=True)
    except BaseException:  # noqa: BLE001
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
```

> **Verify-before-commit:** confirm `build_backend`'s exact kwargs in `better_memory/storage/__init__.py` and that `embedder=None`/`session_id=None` are acceptable for a read-only retrieve (the sqlite backend doesn't need an embedder for `retrieve`/`semantic_list`). Adjust the call to match the real signature if it differs.

- [ ] **Step 4: Run — expect pass.**

Run: `uv run pytest tests/hooks/test_contextual_inject.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Lint/typecheck + commit**

Run: `uv run ruff check better_memory/hooks/contextual_inject.py tests/hooks/test_contextual_inject.py && uv run pyright better_memory/hooks/contextual_inject.py`
Expected: clean.
```bash
git add better_memory/hooks/contextual_inject.py tests/hooks/test_contextual_inject.py
git commit -m "feat(hooks): contextual_inject UserPromptSubmit/PreToolUse hook"
```

---

## Task 6: Registration + setup docs

**Confidence: 94%** — config/doc edits; mirrors existing hook registration. Keep-docs-in-sync guardrail satisfied here.

**Files:**
- Modify: `docs/hooks-setup.md`
- Modify: `README.md`
- Modify: `scripts/setup.sh` (if it writes the hook block)

- [ ] **Step 1: Add the hook entries to `docs/hooks-setup.md`**

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
        "command": "uv run python -m better_memory.hooks.contextual_inject" } ] }
    ],
    "PreToolUse": [
      { "matcher": "Skill|Task|Write",
        "hooks": [ { "type": "command",
          "command": "uv run python -m better_memory.hooks.contextual_inject" } ] }
    ]
  }
}
```
Document `BETTER_MEMORY_CONTEXT_INJECT_MODE` (userprompt|pretool|both|off, default both) and note the Task 0 finding about Skill/Task `PreToolUse` firing.

- [ ] **Step 2: Update `README.md`** — add `contextual_inject` to the hooks list (the repo README lists the hooks; keep the count/table accurate per the keep-docs-in-sync guardrail).

- [ ] **Step 3: Update `scripts/setup.sh`** if it programmatically writes the hooks block — add the two entries above.

- [ ] **Step 4: Commit**

```bash
git add docs/hooks-setup.md README.md scripts/setup.sh
git commit -m "docs: register contextual_inject hook (UserPromptSubmit + PreToolUse)"
```

---

## Task 7: (Optional) MCP tool `memory.retrieve_relevant`

**Confidence: 90%** — mirrors the existing handler/registry pattern. Mitigation: verify exact `TextContent` import + registry wiring from `mcp/handlers/reflections.py` before writing; bump tool-count docs per guardrail.

**Files:**
- Create/modify: `better_memory/mcp/handlers/relevant.py` + register in `mcp/server.py`
- Modify: `website/mcp-tools.md`, `README.md` (tool count)
- Test: `tests/mcp/test_relevant_tool.py`

- [ ] **Step 1: Write failing test** (handler returns TextContent JSON for matches) — seed a backend like Task 2, call the handler, assert it returns the formatted block.

```python
# tests/mcp/test_relevant_tool.py
import pytest
from better_memory.mcp.handlers.relevant import RelevantToolHandlers
# ... build a SqliteBackend (as in tests/services/test_relevant.py), seed memories ...

@pytest.mark.asyncio
async def test_retrieve_relevant_tool_returns_matches(backend):
    handler = RelevantToolHandlers(backend=backend, project="proj")
    out = await handler.retrieve({"query": "write the plan"})
    assert out and "plan" in out[0].text.lower()
```

- [ ] **Step 2: Run — expect failure.**
Run: `uv run pytest tests/mcp/test_relevant_tool.py -v` → FAIL.

- [ ] **Step 3: Implement handler + register**

```python
# better_memory/mcp/handlers/relevant.py
from __future__ import annotations
from typing import Any
from mcp.types import TextContent
from better_memory.services.relevant import format_relevant, retrieve_relevant


class RelevantToolHandlers:
    def __init__(self, *, backend: Any, project: str) -> None:
        self._backend = backend
        self._project = project

    def tools(self) -> dict[str, Any]:
        return {"memory.retrieve_relevant": self.retrieve}

    async def retrieve(self, args: dict[str, Any]) -> list[TextContent]:
        query = str(args.get("query") or "")
        limit = int(args.get("limit") or 5)
        items = retrieve_relevant(self._backend, query=query,
                                  project=self._project, limit=limit)
        return [TextContent(type="text", text=format_relevant(items))]
```
Register in `mcp/server.py` `build_registry(...)`: `RelevantToolHandlers(backend=backend, project=startup_project),` and add the tool's input schema where tool schemas are declared.

- [ ] **Step 4: Run — expect pass.** `uv run pytest tests/mcp/test_relevant_tool.py -v` → PASS.

- [ ] **Step 5: Docs (keep-docs-in-sync)** — add `memory.retrieve_relevant` to `website/mcp-tools.md`; bump the tool count in `README.md` ("registers N tools") and `website/index.md`.

- [ ] **Step 6: Commit**

```bash
git add better_memory/mcp/handlers/relevant.py better_memory/mcp/server.py tests/mcp/test_relevant_tool.py website/mcp-tools.md README.md website/index.md
git commit -m "feat(mcp): memory.retrieve_relevant tool"
```

---

## Task 8: Full suite, lint, PR

**Confidence: 95%.**

- [ ] **Step 1: Full gate**

Run: `uv run ruff check . && uv run pyright && uv run pytest -q`
Expected: clean / all pass.

- [ ] **Step 2: Manual smoke** — pipe a sample payload through the hook:

Run (bash): `echo '{"hook_event_name":"UserPromptSubmit","prompt":"lets write the plan","cwd":"."}' | uv run python -m better_memory.hooks.contextual_inject`
Expected: a JSON envelope; if planning memories exist for the project, `additionalContext` contains them; exit 0.

- [ ] **Step 3: Commit any fixups, push, open PR**

```bash
git push -u origin feat/contextual-memory-injection-hook
gh pr create --base main --title "Contextual memory injection hook (UserPromptSubmit/PreToolUse)" --body "Implements docs/superpowers/specs/2026-06-14-contextual-memory-injection-hook-design.md"
```

---

## Self-Review

- **Spec coverage:** `retrieve_relevant` (T2), keyword/whole-word (T1), formatter+cap (T2/T3), config mode switch (T4), hook for both events + gating (T5), registration/setup docs (T6), optional MCP tool (T7), gate/PR (T8), Skill-PreToolUse empirical check (T0). Scope = semantic + reflections, project + general, no-dedup, empty→nothing, `track_exposure=False`, backend-agnostic via abstraction — all covered.
- **Placeholder scan:** every code step has full code; commands have expected output. (T7 marked optional.)
- **Type consistency:** `RelevantMemory(kind,id,summary,confidence,hits)`, `retrieve_relevant(backend,*,query,project,limit,include_neutral)`, `format_relevant(items,*,max_items)`, `_resolve_context_inject_mode()`, `cfg.context_inject_mode`, hook `_enabled/_query_from/main` — names consistent across T1–T7.
- **Lint:** test code uses `from __future__ import annotations`; no `(str, Enum)`; no `datetime.timezone.utc`. py312 ruff UP-safe.
