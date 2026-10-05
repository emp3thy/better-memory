"""Tests for the contextual_inject hook."""
from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.hooks import contextual_inject as hook

_PROJECT = "ctx-inject-proj"


def _run(payload: dict, monkeypatch, capsys, mode="both"):
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_INJECT_MODE", mode)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as e:
        hook.main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    return json.loads(out) if out.strip() else {}


@pytest.fixture(autouse=True)
def bm_home(tmp_path: Path, monkeypatch) -> Path:
    """Isolated BETTER_MEMORY_HOME with migrations applied and a fixed project.

    autouse so every test in this module runs against a tmp BETTER_MEMORY_HOME,
    including the lower-level tests that don't reference the fixture by name
    (e.g. test_userprompt_emits_envelope, test_mode_off_is_noop) -- otherwise
    hook.main() falls back to the developer's real ~/.better-memory and writes
    memory.db, diagnostics, and state/ files there.

    Task 2 (remove-ollama-embeddings) deleted contextual_inject's SyncEmbedder
    / OllamaEmbedder construction entirely, so the hook never issues an HTTP
    call to localhost:11434 regardless of BETTER_MEMORY_EMBEDDINGS_BACKEND --
    no env pin is needed to guarantee that any more.
    """
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    monkeypatch.setenv("BETTER_MEMORY_PROJECT", _PROJECT)
    conn = connect(tmp_path / "memory.db")
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return tmp_path


def _seed_reflection(
    home: Path, rid: str, *, title: str, use_cases: str = "context",
    hints: list[str] | None = None, useful_count: int = 0,
    confidence: float = 0.8, polarity: str = "do",
    triggers: list[str] | None = None,
) -> None:
    conn = connect(home / "memory.db")
    try:
        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at, useful_count, triggers)
               VALUES (?, ?, ?, 'general', ?, ?, ?, ?, '2026-01-01',
                       '2026-01-01', ?, ?)""",
            (rid, title, _PROJECT, polarity, use_cases,
             json.dumps(hints or []), confidence, useful_count,
             json.dumps(triggers) if triggers else None),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_semantic(
    home: Path, sid: str, *, content: str, triggers: list[str] | None = None,
) -> None:
    conn = connect(home / "memory.db")
    try:
        conn.execute(
            """INSERT INTO semantic_memories
               (id, content, project, scope, created_at, updated_at, triggers)
               VALUES (?, ?, ?, 'project', '2026-01-01', '2026-01-01', ?)""",
            (sid, content, _PROJECT, json.dumps(triggers) if triggers else None),
        )
        conn.commit()
    finally:
        conn.close()


def _exposure_sources(home: Path, session_id: str) -> dict[str, str]:
    conn = connect(home / "memory.db")
    try:
        rows = conn.execute(
            "SELECT memory_id, source FROM session_memory_exposure WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    return {r["memory_id"]: r["source"] for r in rows}


def _diag_value(home: Path, metric: str) -> int | None:
    conn = connect(home / "memory.db")
    try:
        row = conn.execute(
            "SELECT value FROM rating_diagnostics WHERE metric = ?", (metric,)
        ).fetchone()
    finally:
        conn.close()
    return row["value"] if row else None


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


def test_pretool_event_echoed(monkeypatch, capsys):
    res = _run({"hook_event_name": "PreToolUse", "tool_name": "Skill",
                "tool_input": {"skill": "writing-plans"}, "cwd": "."},
               monkeypatch, capsys, mode="both")
    assert res["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_never_throws_on_garbage(monkeypatch, capsys):
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_INJECT_MODE", "both")
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    with pytest.raises(SystemExit) as e:
        hook.main()
    assert e.value.code == 0


def test_injection_renders_project_memory_block(bm_home, monkeypatch, capsys):
    _seed_reflection(bm_home, "refl-widget-deploy-1", title="widget deploy playbook")
    res = _run(
        {"hook_event_name": "UserPromptSubmit", "prompt": "deploy the widget service now",
         "cwd": ".", "session_id": "sess-1"},
        monkeypatch, capsys,
    )
    ctx = res["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("<project-memory")
    assert "refl-widget-deploy-1" in ctx


def test_exposure_row_written_with_contextual_source(bm_home, monkeypatch, capsys):
    _seed_reflection(bm_home, "refl-widget-deploy-2", title="widget deploy playbook")
    _run(
        {"hook_event_name": "UserPromptSubmit", "prompt": "deploy the widget service now",
         "cwd": ".", "session_id": "sess-2"},
        monkeypatch, capsys,
    )
    conn = connect(bm_home / "memory.db")
    try:
        row = conn.execute(
            "SELECT source FROM session_memory_exposure "
            "WHERE session_id = ? AND memory_id = ?",
            ("sess-2", "refl-widget-deploy-2"),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["source"] == "contextual"


def test_second_run_suppressed_by_seen_store(bm_home, monkeypatch, capsys):
    _seed_reflection(bm_home, "refl-widget-deploy-3", title="widget deploy playbook")
    payload = {
        "hook_event_name": "UserPromptSubmit", "prompt": "deploy the widget service now",
        "cwd": ".", "session_id": "sess-3",
    }
    first = _run(payload, monkeypatch, capsys)
    assert first["hookSpecificOutput"]["additionalContext"] != ""

    second = _run(payload, monkeypatch, capsys)
    assert second["hookSpecificOutput"]["additionalContext"] == ""
    assert _diag_value(bm_home, "contextual_suppressed_dedup") == 1


def test_below_floor_injects_nothing(bm_home, monkeypatch, capsys):
    # No BM25 overlap (zero shared tokens with the prompt), no vec leg
    # (the hook never builds a sync_embedder any more -- qvec is always
    # None), and conn is present so the keyword fallback never kicks in
    # either: no evidence on any leg -> no injection.
    _seed_reflection(bm_home, "refl-zebra-flamingo-4", title="zebra flamingo unrelated topic")
    res = _run(
        {"hook_event_name": "UserPromptSubmit", "prompt": "deploy the widget service now",
         "cwd": ".", "session_id": "sess-4"},
        monkeypatch, capsys,
    )
    assert res["hookSpecificOutput"]["additionalContext"] == ""
    assert _diag_value(bm_home, "contextual_suppressed_floor") == 1


def test_fired_counters(bm_home, monkeypatch, capsys):
    _run(
        {"hook_event_name": "UserPromptSubmit", "prompt": "hello world",
         "cwd": ".", "session_id": "sess-5a"},
        monkeypatch, capsys,
    )
    assert _diag_value(bm_home, "contextual_fired_userprompt") == 1

    _run(
        {"hook_event_name": "PreToolUse", "tool_name": "Skill",
         "tool_input": {"skill": "writing-plans"}, "cwd": ".", "session_id": "sess-5b"},
        monkeypatch, capsys,
    )
    assert _diag_value(bm_home, "contextual_fired_pretool") == 1


def test_agentcore_mode_opens_local_conn_for_exposure_ledger(bm_home, monkeypatch, capsys):
    """storage_backend=agentcore now opens a REAL local connection too — for
    the exposure ledger (session-operational state), never for memory
    CONTENT. connect() IS called; build_backend receives the real conn as
    memory_conn; retrieve_relevant still gets conn=None (agentcore has no
    FTS/vec substrate — see services/relevant.py's docstring).

    A true end-to-end agentcore hook test needs boto3/botocore stubs the
    hook-level suite doesn't set up, so build_backend itself stays stubbed;
    only the local connection's threading is verified here.
    """
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_INJECT_MODE", "both")
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")

    build_backend_calls = []

    class _FakeBackend:
        def retrieve(self, **kwargs):
            return {}

        def semantic_list(self, **kwargs):
            return []

        def record_exposures(self, **kwargs):
            pass

    def _fake_build_backend(**kwargs):
        build_backend_calls.append(kwargs)
        return _FakeBackend()

    monkeypatch.setattr(hook, "build_backend", _fake_build_backend)

    retrieve_relevant_calls = []
    real_retrieve_relevant = hook.retrieve_relevant

    def _tracking_retrieve_relevant(*args, **kwargs):
        retrieve_relevant_calls.append(kwargs)
        return real_retrieve_relevant(*args, **kwargs)

    monkeypatch.setattr(hook, "retrieve_relevant", _tracking_retrieve_relevant)

    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO(json.dumps({
            "hook_event_name": "UserPromptSubmit", "prompt": "hello world", "cwd": ".",
        })),
    )
    with pytest.raises(SystemExit) as e:
        hook.main()
    assert e.value.code == 0

    # A real local connection was opened and threaded through as memory_conn
    # — no longer None (that was the pre-Task-2 contract).
    assert len(build_backend_calls) == 1
    assert build_backend_calls[0]["memory_conn"] is not None
    # retrieve_relevant still gets conn=None for agentcore: that parameter
    # means "sqlite FTS/vec index available", not "any local connection at
    # all" — agentcore has no reflection_fts / *_embeddings substrate.
    assert len(retrieve_relevant_calls) == 1
    assert retrieve_relevant_calls[0]["conn"] is None


def test_agentcore_mode_exposure_lands_in_local_ledger(bm_home, monkeypatch, capsys):
    """Task 2: a `contextual` exposure survives a full hook run and lands as
    a session_memory_exposure row in the local memory.db, even though
    memory CONTENT (the reflection itself) stays in AgentCore. The fake
    backend's record_exposures forwards to the same shared exposure_log
    primitive AgentCoreBackend.record_exposures uses (unit-tested directly
    in tests/storage/test_agentcore_unit.py) — this test proves the HOOK
    wiring: the connection the hook opens is the one the ledger write lands
    in."""
    from better_memory.services import exposure_log

    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_INJECT_MODE", "both")
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")

    class _FakeAgentCoreBackend:
        def __init__(self, conn):
            self._conn = conn

        def retrieve(self, **kwargs):
            return {
                "do": [{
                    "id": "refl-agentcore-1",
                    "title": "widget deploy playbook",
                    "use_cases": "deploy widget service",
                    "hints": [],
                    "confidence": 0.8,
                    "tech": None,
                    "evidence_count": 0,
                    "useful_count": 0,
                    "times_overlooked": 0,
                    "times_ignored": 0,
                    "times_misled": 0,
                    "updated_at": None,
                }],
                "dont": [],
                "neutral": [],
            }

        def semantic_list(self, **kwargs):
            return []

        def record_exposures(self, *, session_id, items, source):
            exposure_log.record(
                self._conn,
                session_id=session_id,
                items=items,
                source=source,
                now=datetime.now(UTC).isoformat(),
            )
            self._conn.commit()

    build_backend_calls = []

    def _fake_build_backend(**kwargs):
        build_backend_calls.append(kwargs)
        return _FakeAgentCoreBackend(kwargs["memory_conn"])

    monkeypatch.setattr(hook, "build_backend", _fake_build_backend)

    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO(json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "prompt": "deploy the widget service now",
            "cwd": ".", "session_id": "ac-sess-ledger",
        })),
    )
    with pytest.raises(SystemExit) as e:
        hook.main()
    assert e.value.code == 0

    assert len(build_backend_calls) == 1
    assert build_backend_calls[0]["memory_conn"] is not None

    conn = connect(bm_home / "memory.db")
    try:
        row = conn.execute(
            "SELECT source FROM session_memory_exposure "
            "WHERE session_id = ? AND memory_id = ?",
            ("ac-sess-ledger", "refl-agentcore-1"),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["source"] == "contextual"


def test_exposure_write_failure_does_not_block_injection(bm_home, monkeypatch, capsys):
    from better_memory.storage.sqlite import SqliteBackend

    def _raise(*args, **kwargs):
        raise RuntimeError("exposure write boom")

    monkeypatch.setattr(SqliteBackend, "record_exposures", _raise)

    _seed_reflection(bm_home, "refl-widget-deploy-6", title="widget deploy playbook")
    res = _run(
        {"hook_event_name": "UserPromptSubmit", "prompt": "deploy the widget service now",
         "cwd": ".", "session_id": "sess-6"},
        monkeypatch, capsys,
    )
    ctx = res["hookSpecificOutput"]["additionalContext"]
    assert "refl-widget-deploy-6" in ctx


@pytest.mark.parametrize("text,human", [
    ("fix the bug", True),
    ("  <task-notification>x", False),
    ("Another Claude session sent a message: hi", False),
    ("   ", False),
    ("", False),
    ("<bash-input> ls", False),
    ("deploy <the> widget", True),
])
def test_nonhuman_prefix_detection(text, human):
    assert hook.is_human_prompt(text) is human


def test_peer_message_is_suppressed(bm_home, monkeypatch, capsys):
    _seed_reflection(bm_home, "refl-widget-deploy-peer", title="widget deploy playbook")
    res = _run(
        {"hook_event_name": "UserPromptSubmit",
         "prompt": "Another Claude session sent a message: deploy the widget now",
         "cwd": ".", "session_id": "sess-peer"},
        monkeypatch, capsys,
    )
    assert res["hookSpecificOutput"]["additionalContext"] == ""
    conn = connect(bm_home / "memory.db")
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM session_memory_exposure WHERE session_id = ?",
            ("sess-peer",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 0
    assert _diag_value(bm_home, "contextual_suppressed_nonhuman") == 1
    # A later human prompt in the same session is still served.
    res = _run(
        {"hook_event_name": "UserPromptSubmit", "prompt": "deploy the widget service now",
         "cwd": ".", "session_id": "sess-peer"},
        monkeypatch, capsys,
    )
    assert "refl-widget-deploy-peer" in res["hookSpecificOutput"]["additionalContext"]


_HEREDOC = {"command": "cat > notes.md <<'EOF'\nhello\nEOF"}


def test_pretool_trigger_hit_injects_with_reason(bm_home, monkeypatch, capsys):
    _seed_semantic(bm_home, "sem-heredoc", content="Use the Write tool for large files",
                   triggers=["bash:<<"])
    res = _run(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": _HEREDOC,
         "cwd": ".", "session_id": "sess-t1"},
        monkeypatch, capsys,
    )
    ctx = res["hookSpecificOutput"]["additionalContext"]
    assert "sem-heredoc" in ctx
    assert "Triggered by: bash:<<" in ctx
    assert _exposure_sources(bm_home, "sess-t1") == {"sem-heredoc": "trigger"}
    assert _diag_value(bm_home, "trigger_fired") == 1
    assert _diag_value(bm_home, "trigger_injected") == 1
    assert _diag_value(bm_home, "contextual_fired_pretool") == 1


def test_pretool_no_trigger_no_exposure(bm_home, monkeypatch, capsys):
    _seed_semantic(bm_home, "sem-heredoc", content="Use the Write tool for large files",
                   triggers=["bash:<<"])
    # Keyword overlap with the tool input must NOT serve a memory: the
    # PreToolUse channel is trigger-only.
    _seed_reflection(bm_home, "refl-read-files", title="read files playbook")
    res = _run(
        {"hook_event_name": "PreToolUse", "tool_name": "Read",
         "tool_input": {"file_path": "read files playbook.md"},
         "cwd": ".", "session_id": "sess-t2"},
        monkeypatch, capsys,
    )
    assert res["hookSpecificOutput"]["additionalContext"] == ""
    assert _exposure_sources(bm_home, "sess-t2") == {}
    assert _diag_value(bm_home, "trigger_fired") == 1
    assert _diag_value(bm_home, "trigger_injected") == 0


def test_pretool_fires_on_every_call(bm_home, monkeypatch, capsys):
    _seed_semantic(bm_home, "sem-heredoc", content="heredoc pitfall", triggers=["bash:<<"])
    _seed_reflection(bm_home, "refl-webfetch", title="WebFetch refusals",
                     triggers=["tool:WebFetch"])
    first = _run(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": _HEREDOC,
         "cwd": ".", "session_id": "sess-t3"},
        monkeypatch, capsys,
    )
    assert "sem-heredoc" in first["hookSpecificOutput"]["additionalContext"]
    second = _run(
        {"hook_event_name": "PreToolUse", "tool_name": "WebFetch",
         "tool_input": {"url": "https://example.com"}, "cwd": ".", "session_id": "sess-t3"},
        monkeypatch, capsys,
    )
    assert "refl-webfetch" in second["hookSpecificOutput"]["additionalContext"]
    assert _exposure_sources(bm_home, "sess-t3") == {
        "sem-heredoc": "trigger", "refl-webfetch": "trigger",
    }
    assert _diag_value(bm_home, "contextual_fired_pretool") == 2


def test_pretool_seen_store_dedups_trigger(bm_home, monkeypatch, capsys):
    _seed_semantic(bm_home, "sem-heredoc", content="heredoc pitfall", triggers=["bash:<<"])
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": _HEREDOC,
               "cwd": ".", "session_id": "sess-t4"}
    first = _run(payload, monkeypatch, capsys)["hookSpecificOutput"]["additionalContext"]
    assert "sem-heredoc" in first
    assert _run(payload, monkeypatch, capsys)["hookSpecificOutput"]["additionalContext"] == ""
    assert _diag_value(bm_home, "contextual_suppressed_dedup") == 1


def test_prompt_then_trigger_same_memory_once(bm_home, monkeypatch, capsys):
    _seed_semantic(bm_home, "sem-heredoc", content="Bash heredoc pitfall on windows",
                   triggers=["bash:<<"])
    prompt = {"hook_event_name": "UserPromptSubmit", "prompt": "the bash heredoc pitfall again",
              "cwd": ".", "session_id": "sess-t5"}
    first = _run(prompt, monkeypatch, capsys)["hookSpecificOutput"]["additionalContext"]
    assert "sem-heredoc" in first
    tool = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": _HEREDOC,
            "cwd": ".", "session_id": "sess-t5"}
    assert _run(tool, monkeypatch, capsys)["hookSpecificOutput"]["additionalContext"] == ""
    assert _exposure_sources(bm_home, "sess-t5") == {"sem-heredoc": "contextual"}


def test_pretool_mode_userprompt_only_disables_triggers(bm_home, monkeypatch, capsys):
    _seed_semantic(bm_home, "sem-heredoc", content="heredoc pitfall", triggers=["bash:<<"])
    res = _run(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": _HEREDOC,
         "cwd": ".", "session_id": "sess-t6"},
        monkeypatch, capsys, mode="userprompt",
    )
    assert res["hookSpecificOutput"]["additionalContext"] == ""
    assert _exposure_sources(bm_home, "sess-t6") == {}


def test_preclaimed_memory_is_not_served_again(bm_home, monkeypatch, capsys):
    """A parallel hook process that already claimed the memory wins; this
    process injects nothing and writes no exposure."""
    from better_memory.services.context_seen import SeenStore
    _seed_semantic(bm_home, "sem-heredoc", content="heredoc pitfall", triggers=["bash:<<"])
    SeenStore(bm_home / "state", "sess-claim").claim([("semantic", "sem-heredoc")])
    res = _run(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": _HEREDOC,
         "cwd": ".", "session_id": "sess-claim"},
        monkeypatch, capsys,
    )
    assert res["hookSpecificOutput"]["additionalContext"] == ""
    assert _exposure_sources(bm_home, "sess-claim") == {}


def test_pretool_on_agentcore_returns_before_backend_work(bm_home, monkeypatch, capsys):
    """The trigger channel cannot fire on agentcore (no trigger storage), so
    PreToolUse must not pay for a backend build on every tool call."""
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")

    def _boom(**kwargs):
        raise AssertionError("build_backend must not be called")

    monkeypatch.setattr(hook, "build_backend", _boom)
    res = _run(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": _HEREDOC,
         "cwd": ".", "session_id": "sess-ac"},
        monkeypatch, capsys,
    )
    assert res["hookSpecificOutput"]["additionalContext"] == ""
    conn = connect(bm_home / "memory.db")
    try:
        errors = conn.execute("SELECT COUNT(*) FROM hook_errors").fetchone()[0]
    finally:
        conn.close()
    assert errors == 0
    assert _diag_value(bm_home, "contextual_fired_pretool") in (0, None)


def test_pretool_no_match_does_not_bump_prompt_floor_counter(bm_home, monkeypatch, capsys):
    """contextual_suppressed_floor is the prompt gate's counter; tool calls
    that match no trigger must not swamp it (trigger_fired - trigger_injected
    is the trigger channel's own suppression figure)."""
    _run(
        {"hook_event_name": "PreToolUse", "tool_name": "Read",
         "tool_input": {"file_path": "x.md"}, "cwd": ".", "session_id": "sess-floor"},
        monkeypatch, capsys,
    )
    assert _diag_value(bm_home, "contextual_suppressed_floor") == 0
    assert _diag_value(bm_home, "trigger_fired") == 1
    assert _diag_value(bm_home, "trigger_injected") == 0
