"""E2E sqlite journey (design section C, tasks C1-C6).

Hermetic clean-slate smoke tests for the sqlite-mode new-user journey,
per ``docs/superpowers/specs/2026-07-12-e2e-clean-slate-smoke-design.md``:

* C1 ``e2e-sqlite-first-boot-migrates-tools-knowledge``
* C2 ``e2e-sqlite-hook-before-server-degraded`` (KNOWN-DEFECT PIN)
* C3 ``e2e-sqlite-hook-first-then-server-heals``
* C4 ``e2e-sqlite-observe-retrieve-record-use-offline``
* C5 ``e2e-sqlite-session-close-rate-then-marker``
* C6 ``e2e-sqlite-spool-drain-synthesize-loop``

Deviation from the design's file split: the degraded/heal hook scenarios
(C2, C3) live HERE rather than in ``test_hooks_contracts.py`` — this module
is single-owner for the whole sqlite journey while another task owns the
contextual-inject contracts.

Every subprocess env is built through :func:`tests.e2e._env.isolated_env`
(the single choke point); server spawns go through
:func:`tests.e2e.conftest.mcp_session`; hook spawns through ``run_hook``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from better_memory.runtime.session_marker import encode_project_dir
from tests.e2e._env import isolated_env
from tests.e2e.conftest import mcp_session, run_hook

BOOTSTRAP_HOOK = "better_memory.hooks.session_bootstrap"
CLOSE_HOOK = "better_memory.hooks.session_close"

#: Wire-visible sqlite-mode tool surface (subset — never exact equality).
#: Includes BOTH synthesize tools: they are gated on
#: ``backend.supports_synthesis`` and must be advertised in sqlite mode.
EXPECTED_TOOLS: frozenset[str] = frozenset(
    {
        "memory.observe",
        "memory.retrieve",
        "memory.retrieve_observations",
        "memory.record_use",
        "memory.session_bootstrap",
        "memory.start_episode",
        "memory.close_episode",
        "memory.list_session_exposures",
        "memory.apply_session_ratings",
        "memory.synthesize_next_get_context",
        "memory.synthesize_next_apply",
        "knowledge.search",
        "knowledge.list",
    }
)

#: memory.db schema superset after migrations (subset assertion — a new
#: migration adding tables must not break this suite).
EXPECTED_MEMORY_TABLES: frozenset[str] = frozenset(
    {
        "observations",
        "episodes",
        "hook_errors",
        "session_memory_exposure",
        "reflections",
        "semantic_memories",
    }
)


# ---------------------------------------------------------------------------
# Local wire/db helpers
# ---------------------------------------------------------------------------


def _single_json_dict(result: Any) -> dict[str, Any]:
    """Extract + parse the single TextContent payload of a successful call."""
    content = result.content
    assert not getattr(result, "isError", False), f"tool errored: {content!r}"
    assert len(content) == 1, f"expected one content block: {content!r}"
    block = content[0]
    assert getattr(block, "type", None) == "text", f"not a text block: {block!r}"
    parsed = json.loads(block.text)
    assert isinstance(parsed, dict), f"expected JSON object, got {block.text!r}"
    return parsed


def _single_json_list(result: Any) -> list[Any]:
    content = result.content
    assert not getattr(result, "isError", False), f"tool errored: {content!r}"
    assert len(content) == 1, f"expected one content block: {content!r}"
    block = content[0]
    assert getattr(block, "type", None) == "text", f"not a text block: {block!r}"
    parsed = json.loads(block.text)
    assert isinstance(parsed, list), f"expected JSON array, got {block.text!r}"
    return parsed


def _error_text(result: Any) -> str:
    """Assert the call failed at the MCP layer and return the error text."""
    content = result.content
    assert getattr(result, "isError", False), f"expected isError, got: {content!r}"
    assert len(content) >= 1, "error result carried no content blocks"
    block = content[0]
    assert getattr(block, "type", None) == "text", f"not a text block: {block!r}"
    return block.text


def _hook_envelope(stdout: str) -> dict[str, Any]:
    """Parse a hook's stdout as exactly one JSON envelope line."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one JSON line on stdout: {stdout!r}"
    parsed = json.loads(lines[0])
    assert isinstance(parsed, dict)
    return parsed


def _ro_connect(db_path: Path) -> sqlite3.Connection:
    """Read-only sqlite connection (post-exit inspection only)."""
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(db_path: Path) -> set[str]:
    assert db_path.exists(), f"database file missing: {db_path}"
    with closing(_ro_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    return {r["name"] for r in rows}


def _query_one(db_path: Path, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with closing(_ro_connect(db_path)) as conn:
        return conn.execute(sql, params).fetchone()


def _session_end_markers(spool_dir: Path) -> list[Path]:
    """Top-level (NON-recursive) session_end markers.

    Deliberately non-recursive: SpoolService.drain quarantines malformed
    files into a subdirectory; a recursive glob would wrongly count those.
    """
    if not spool_dir.is_dir():
        return []
    return sorted(spool_dir.glob("*_session_end_*.json"))


def _bootstrap_payload(proj_dir: Path, session_id: str = "e2e-session-1") -> dict:
    return {"source": "startup", "session_id": session_id, "cwd": str(proj_dir)}


# ---------------------------------------------------------------------------
# C1 — e2e-sqlite-first-boot-migrates-tools-knowledge
# ---------------------------------------------------------------------------


async def test_first_boot_migrates_tools_knowledge(
    clean_slate_home: Path, tmp_path: Path
) -> None:
    """First ``python -m better_memory.mcp`` on a NONEXISTENT home:
    initialize succeeds fully offline, the sqlite tool surface (incl. both
    synthesize tools) is advertised, both DBs are created + migrated, the
    knowledge tools return well-formed empty results on the unindexed
    corpus, and ``knowledge-base/`` is NOT auto-created.

    KNOWN-DEFECT PIN (design §4 item 9): the server never mkdirs
    ``knowledge-base/`` — pip installs get silently-empty knowledge search.
    Flip the final assertion when the product decides to auto-create it.
    """
    env = isolated_env(clean_slate_home)
    bm_home = clean_slate_home / ".better-memory"
    assert not bm_home.exists(), "precondition: virgin home, no .better-memory"

    with (tmp_path / "c1.stderr").open("w", encoding="utf-8") as errlog:
        async with mcp_session(env, errlog=errlog) as session:
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            assert EXPECTED_TOOLS <= names, (
                f"missing tools: {sorted(EXPECTED_TOOLS - names)}"
            )

            # Folded gap-3: knowledge tools on a fresh, unindexed corpus
            # return empty arrays — never errors. This is the mandated
            # startup knowledge_search path of every fresh install.
            search_hits = _single_json_list(
                await session.call_tool(
                    "knowledge.search", {"query": "anything at all"}
                )
            )
            assert search_hits == []
            docs = _single_json_list(await session.call_tool("knowledge.list", {}))
            assert docs == []

    # Post-exit inspection (no WAL-concurrency ambiguity).
    memory_tables = _table_names(bm_home / "memory.db")
    assert EXPECTED_MEMORY_TABLES <= memory_tables, (
        f"missing tables: {sorted(EXPECTED_MEMORY_TABLES - memory_tables)}"
    )
    assert any("trigram" in name for name in memory_tables), (
        f"no trigram FTS table (migration 0011) in: {sorted(memory_tables)}"
    )
    knowledge_tables = _table_names(bm_home / "knowledge.db")
    assert len(knowledge_tables) >= 1

    # DEFECT PIN: server does not auto-create the knowledge-base tree.
    assert not (bm_home / "knowledge-base").exists(), (
        "knowledge-base/ was auto-created — the documented gap (design §4 "
        "item 9) has been fixed; update this pin deliberately"
    )


# ---------------------------------------------------------------------------
# C2 — e2e-sqlite-hook-before-server-degraded (KNOWN-DEFECT PIN)
# ---------------------------------------------------------------------------


def test_hook_before_server_degraded(
    clean_slate_home: Path, tmp_path: Path
) -> None:
    """SessionStart hook fired before any server boot on a virgin home.
    Contract: exit 0, empty stderr, a single fallback-directive envelope, a
    schema-less ``memory.db`` (the file exists, 0 tables), the
    ``runtime/sessions`` marker IS written with the payload's session id
    (``write_session_id`` is hoisted above the bootstrap call — the id
    comes from stdin, so even the degraded first session leaves a valid
    bridge for the server to resolve), no spool, and the ``hook_errors``
    write silently no-ops (no table to insert into).

    The remaining KNOWN-DEFECT PIN (design §4 item 3) is the no-migrations
    half: flipping the 0-tables assertion (hook-side migrations) must update
    this contract and the heal-sequence test together.
    """
    env = isolated_env(clean_slate_home)
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    bm_home = clean_slate_home / ".better-memory"

    rc, out, err = run_hook(BOOTSTRAP_HOOK, _bootstrap_payload(proj_dir), env)

    assert rc == 0
    assert err == ""
    envelope = _hook_envelope(out)
    assert set(envelope) == {"hookSpecificOutput"}
    hso = envelope["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart"
    ctx = hso["additionalContext"]
    # Prefix match — the table name is coupled to internal query order.
    assert ctx.startswith(
        "better-memory: session bootstrap failed "
        "(OperationalError: no such table:"
    ), f"unexpected fallback directive: {ctx!r}"
    assert "Call mcp__better-memory__memory_session_bootstrap manually" in ctx

    # connect() created the file but never migrated it.
    db_path = bm_home / "memory.db"
    assert db_path.exists()
    assert _table_names(db_path) == set(), "hook must not run migrations"

    # Marker WRITTEN before bootstrap (hoisted write_session_id): the id
    # comes from the stdin payload, so the degraded path still leaves a
    # valid session bridge keyed by the payload cwd. Exactly one file —
    # no .sid-* tempfile droppings.
    sessions_dir = bm_home / "runtime" / "sessions"
    marker = sessions_dir / encode_project_dir(str(proj_dir))
    assert marker.is_file(), f"session marker missing: {marker}"
    assert marker.read_text(encoding="utf-8").strip() == "e2e-session-1"
    assert [p for p in sessions_dir.iterdir()] == [marker]
    assert not (bm_home / "spool").exists()
    # Only the marker tree beside the db file (+ possible WAL side files).
    assert [p.name for p in bm_home.iterdir() if p.is_dir()] == ["runtime"]
    assert {p.name for p in bm_home.iterdir()} <= {
        "memory.db",
        "memory.db-wal",
        "memory.db-shm",
        "runtime",
    }


# ---------------------------------------------------------------------------
# C3 — e2e-sqlite-hook-first-then-server-heals
# ---------------------------------------------------------------------------


async def test_hook_first_then_server_heals(
    clean_slate_home: Path, tmp_path: Path
) -> None:
    """The real new-user SEQUENCE: degraded SessionStart hook leaves a
    schema-less WAL ``memory.db`` plus the session marker (hoisted
    ``write_session_id`` fires before bootstrap) → the first server boot
    heals the db (migrations apply onto the pre-existing 0-table file) →
    the fallback directive's promised ``memory.session_bootstrap`` MCP tool
    works (``episode.action == 'opened'``) → a re-run of the hook reports
    ``Episode: reused`` and the marker still holds (rewritten to the same
    key — exactly one file). Exactly one open episode row exists throughout.
    """
    env = isolated_env(clean_slate_home)
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    bm_home = clean_slate_home / ".better-memory"
    payload = _bootstrap_payload(proj_dir)

    # Leg 1 — degraded hook on the virgin home (C2's core assertions).
    rc, out, err = run_hook(BOOTSTRAP_HOOK, payload, env)
    assert rc == 0, err
    ctx = _hook_envelope(out)["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("better-memory: session bootstrap failed (")
    db_path = bm_home / "memory.db"
    assert _table_names(db_path) == set()
    # Hoisted write_session_id: the marker lands even on the degraded leg,
    # so the server booted in leg 2 can already resolve the session id.
    degraded_marker = (
        bm_home / "runtime" / "sessions" / encode_project_dir(str(proj_dir))
    )
    assert degraded_marker.is_file()
    assert (
        degraded_marker.read_text(encoding="utf-8").strip() == "e2e-session-1"
    )

    # Leg 2 — server boots onto the pre-existing 0-table WAL file and
    # heals it; the directive's remediation tool actually works.
    with (tmp_path / "c3.stderr").open("w", encoding="utf-8") as errlog:
        async with mcp_session(env, errlog=errlog) as session:
            result = _single_json_dict(
                await session.call_tool(
                    "memory.session_bootstrap",
                    {"session_id": "e2e-session-1", "cwd": str(proj_dir)},
                )
            )
            assert result["episode"]["action"] == "opened", result
            assert result["episode"]["id"]

    # Migrations applied onto the previously schema-less file.
    memory_tables = _table_names(db_path)
    assert EXPECTED_MEMORY_TABLES <= memory_tables
    assert any("trigram" in name for name in memory_tables)

    # Leg 3 — hook re-run now succeeds: episode reused, marker written.
    rc, out, err = run_hook(BOOTSTRAP_HOOK, payload, env)
    assert rc == 0, err
    ctx = _hook_envelope(out)["hookSpecificOutput"]["additionalContext"]
    assert "Episode: reused" in ctx, f"expected reuse, got: {ctx!r}"

    sessions_dir = bm_home / "runtime" / "sessions"
    marker_files = [p for p in sessions_dir.iterdir() if p.is_file()]
    assert len(marker_files) == 1, (
        f"expected exactly one marker (no .sid-* droppings): {marker_files}"
    )
    assert marker_files[0].read_text(encoding="utf-8").strip() == "e2e-session-1"

    # Episodes bind to sessions via the episode_sessions join table
    # (an episode may span multiple sessions — migration 0002).
    row = _query_one(
        db_path,
        "SELECT COUNT(DISTINCT e.id) AS n FROM episodes e "
        "JOIN episode_sessions es ON es.episode_id = e.id "
        "WHERE es.session_id = ? AND e.ended_at IS NULL",
        ("e2e-session-1",),
    )
    assert row is not None and row["n"] == 1, (
        "exactly one open background episode expected across tool call + re-run"
    )


# ---------------------------------------------------------------------------
# C4 — e2e-sqlite-observe-retrieve-record-use-offline
# ---------------------------------------------------------------------------


async def test_observe_retrieve_record_use_offline(
    clean_slate_home: Path, tmp_path: Path
) -> None:
    """Full offline data loop in ONE server session: observe → trigram
    retrieve (the obs id surfaces) → bucket shape ``{do,dont,neutral}`` →
    ``record_use(success)`` on A and ``record_use(failure)`` on B →
    not-found id is an MCP isError. Durability is proven by POST-EXIT DB
    reads (a dropped commit passes any same-connection unit test but loses
    the ratings at process exit).

    The <30s wall-clock budget around the observe call is the tripwire for
    a regression reintroducing the Ollama embed path (3 retries against the
    poisoned host blow past it or raise EmbeddingError).
    """
    env = isolated_env(clean_slate_home)
    bm_home = clean_slate_home / ".better-memory"

    with (tmp_path / "c4.stderr").open("w", encoding="utf-8") as errlog:
        async with mcp_session(env, errlog=errlog) as session:
            t0 = time.monotonic()
            obs_a = _single_json_dict(
                await session.call_tool(
                    "memory.observe",
                    {
                        "content": (
                            "Zebra flux capacitor timed out during spool "
                            "drain on Windows"
                        ),
                        "outcome": "failure",
                        "component": "spool",
                        "theme": "bug",
                    },
                )
            )["id"]
            observe_elapsed = time.monotonic() - t0
            assert observe_elapsed < 30, (
                f"observe took {observe_elapsed:.1f}s — sqlite embeddings "
                "bypass lost? (Ollama retry path reintroduced)"
            )
            assert isinstance(obs_a, str) and obs_a

            obs_b = _single_json_dict(
                await session.call_tool(
                    "memory.observe",
                    {
                        "content": "quiet baseline observation for failure leg",
                        "outcome": "neutral",
                    },
                )
            )["id"]

            # Trigram FTS populated synchronously by the insert trigger.
            rows = _single_json_list(
                await session.call_tool(
                    "memory.retrieve_observations",
                    {"query": "flux capacitor timed out"},
                )
            )
            by_id = {r["id"]: r for r in rows}
            assert obs_a in by_id, f"trigram retrieval missed {obs_a}: {rows}"
            assert "flux capacitor" in by_id[obs_a]["content"]
            assert by_id[obs_a]["outcome"] == "failure"

            # Bucket shape contract every fresh install's CLAUDE.md relies on.
            buckets = _single_json_dict(
                await session.call_tool("memory.retrieve", {})
            )
            assert {"do", "dont", "neutral"} <= set(buckets)

            # Both reinforcement branches + the not-found contract.
            ok_a = _single_json_dict(
                await session.call_tool(
                    "memory.record_use", {"id": obs_a, "outcome": "success"}
                )
            )
            assert ok_a == {"ok": True}
            ok_b = _single_json_dict(
                await session.call_tool(
                    "memory.record_use", {"id": obs_b, "outcome": "failure"}
                )
            )
            assert ok_b == {"ok": True}

            not_found = await session.call_tool(
                "memory.record_use",
                {"id": "does-not-exist", "outcome": "success"},
            )
            assert "not found" in _error_text(not_found).lower()

    # Post-exit durability: re-open the DB after the server process died.
    db_path = bm_home / "memory.db"
    row_a = _query_one(
        db_path,
        "SELECT used_count, validated_true, validated_false, "
        "reinforcement_score, last_used, last_validated "
        "FROM observations WHERE id = ?",
        (obs_a,),
    )
    assert row_a is not None
    assert row_a["used_count"] == 1
    assert row_a["validated_true"] == 1
    assert row_a["validated_false"] == 0
    assert row_a["reinforcement_score"] == pytest.approx(1.0)
    assert row_a["last_used"], "last_used must be stamped"
    assert row_a["last_validated"], "last_validated must be stamped"

    row_b = _query_one(
        db_path,
        "SELECT used_count, validated_true, validated_false, "
        "reinforcement_score FROM observations WHERE id = ?",
        (obs_b,),
    )
    assert row_b is not None
    assert row_b["used_count"] == 1
    assert row_b["validated_true"] == 0
    assert row_b["validated_false"] == 1
    assert row_b["reinforcement_score"] == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# C5 — e2e-sqlite-session-close-rate-then-marker
# ---------------------------------------------------------------------------


async def test_session_close_rate_then_marker(
    clean_slate_home: Path, tmp_path: Path
) -> None:
    """Two-fire Stop sequence with a REAL rating turn between the fires.

    Fire 1 blocks with RATE_MEMORIES (memory id + skill name in the
    directive) and writes ZERO session_end markers. The rating turn runs
    against a server spawned with ``cwd=proj_dir`` and ``CLAUDE_SESSION_ID``
    DELETED — forcing the hook→marker-file→server session bridge
    (``resolve_session_id`` falls back to the marker keyed by the server's
    cwd, which must equal the payload cwd the bootstrap hook keyed on).
    Fire 2 emits EMPTY stdout and exactly one session_end marker.
    """
    env = isolated_env(clean_slate_home)
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    bm_home = clean_slate_home / ".better-memory"
    spool_dir = bm_home / "spool"

    # Seed one semantic memory through the real server (also migrates).
    with (tmp_path / "c5-seed.stderr").open("w", encoding="utf-8") as errlog:
        async with mcp_session(env, errlog=errlog) as session:
            sem_id = _single_json_dict(
                await session.call_tool(
                    "memory.semantic_observe",
                    {"content": "Always poison OLLAMA_HOST in hermetic tests"},
                )
            )["id"]
    assert isinstance(sem_id, str) and sem_id

    # Bootstrap hook: creates the source='bootstrap' exposure row and
    # writes the session marker keyed by the payload cwd (proj_dir).
    rc, out, err = run_hook(BOOTSTRAP_HOOK, _bootstrap_payload(proj_dir), env)
    assert rc == 0, err
    boot_ctx = _hook_envelope(out)["hookSpecificOutput"]["additionalContext"]
    assert "session bootstrap failed" not in boot_ctx, boot_ctx
    assert "Episode:" in boot_ctx

    # Fire 1 — unrated exposure exists → decision:block, marker SKIPPED.
    close_payload = {"session_id": "e2e-session-1", "cwd": str(proj_dir)}
    rc, out, err = run_hook(CLOSE_HOOK, close_payload, env)
    assert rc == 0, err
    fire1 = _hook_envelope(out)
    assert fire1["decision"] == "block"
    assert "RATE_MEMORIES" in fire1["reason"]
    assert fire1["hookSpecificOutput"]["hookEventName"] == "Stop"
    directive = fire1["hookSpecificOutput"]["additionalContext"]
    assert sem_id in directive
    assert "rate-session-memories" in directive
    assert _session_end_markers(spool_dir) == [], (
        "marker written while blocking — double session_end downstream"
    )

    # Rating turn — marker-bridge leg: CLAUDE_SESSION_ID deleted from the
    # server env, server cwd pinned to proj_dir so the getcwd() fallback
    # resolves the same marker key the hook wrote.
    bridge_env = isolated_env(clean_slate_home, CLAUDE_SESSION_ID=None)
    with (tmp_path / "c5-rate.stderr").open("w", encoding="utf-8") as errlog:
        async with mcp_session(bridge_env, errlog=errlog, cwd=proj_dir) as session:
            exposures_payload = _single_json_dict(
                await session.call_tool("memory.list_session_exposures", {})
            )
            # Session id resolved FROM THE MARKER FILE — the bridge works.
            assert exposures_payload["session_id"] == "e2e-session-1"
            exposures = exposures_payload["exposures"]
            assert exposures, "expected at least the seeded semantic exposure"
            assert any(
                e["kind"] == "semantic" and e["id"] == sem_id for e in exposures
            ), exposures
            for entry in exposures:
                # Real wire shape: kind/id/title-or-content/exposed_at/source.
                # There is NO rated_at on the wire (unrated filter is in SQL).
                assert {"kind", "id", "exposed_at", "source"} <= set(entry)
                assert "rated_at" not in entry

            # Exact payload shape the tool schema accepts:
            # items require exactly {kind, id, class}.
            ratings = [
                {"kind": e["kind"], "id": e["id"], "class": "ignored"}
                for e in exposures
            ]
            applied_payload = _single_json_dict(
                await session.call_tool(
                    "memory.apply_session_ratings", {"ratings": ratings}
                )
            )
            assert sum(applied_payload["applied"].values()) >= 1, applied_payload

    # Fire 2 — everything rated: EMPTY stdout (stronger than substring
    # absence: the marker path prints nothing at all), exactly one marker.
    rc, out, err = run_hook(CLOSE_HOOK, close_payload, env)
    assert rc == 0, err
    assert out == "", f"fire 2 must print nothing, got: {out!r}"
    markers = _session_end_markers(spool_dir)
    assert len(markers) == 1, f"expected exactly one session_end marker: {markers}"
    body = json.loads(markers[0].read_text(encoding="utf-8"))
    assert body["event_type"] == "session_end"
    assert body["session_id"] == "e2e-session-1"


# ---------------------------------------------------------------------------
# C6 — e2e-sqlite-spool-drain-synthesize-loop
# ---------------------------------------------------------------------------


async def test_spool_drain_synthesize_loop(
    clean_slate_home: Path, tmp_path: Path
) -> None:
    """session_end marker + next-session ``memory.retrieve`` drains the
    spool and closes the background episode as ``no_outcome``; the
    get_context/apply loop distills it into a reflection that the final
    retrieve surfaces in the ``dont`` bucket.

    Session A: bootstrap hook opens background episode E → observe links
    obs to E → Stop hook writes the marker immediately (nothing seeded, so
    no RATE block). Session B (``CLAUDE_SESSION_ID=e2e-session-2``
    explicitly): retrieve drains + closes E; ``start_episode`` reports
    ``pending_synthesis.pending >= 1``; get_context carries obs_id; apply
    returns ``ok/counts.created==1/queue``; a second get_context excludes E.
    """
    env = isolated_env(clean_slate_home)  # CLAUDE_SESSION_ID=e2e-session-1
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    bm_home = clean_slate_home / ".better-memory"
    spool_dir = bm_home / "spool"

    # Migrate the home (initialize-only boot).
    with (tmp_path / "c6-migrate.stderr").open("w", encoding="utf-8") as errlog:
        async with mcp_session(env, errlog=errlog):
            pass

    # Session A: hook opens background episode E for e2e-session-1.
    rc, out, err = run_hook(BOOTSTRAP_HOOK, _bootstrap_payload(proj_dir), env)
    assert rc == 0, err
    boot_ctx = _hook_envelope(out)["hookSpecificOutput"]["additionalContext"]
    assert "session bootstrap failed" not in boot_ctx, boot_ctx

    # Observe inside session A — links to E via the active-episode lookup.
    with (tmp_path / "c6-a.stderr").open("w", encoding="utf-8") as errlog:
        async with mcp_session(env, errlog=errlog) as session:
            obs_id = _single_json_dict(
                await session.call_tool(
                    "memory.observe",
                    {
                        "content": "spool drain e2e observation about flux",
                        "outcome": "failure",
                    },
                )
            )["id"]

    # Stop hook: no unrated exposures → marker written immediately.
    rc, out, err = run_hook(
        CLOSE_HOOK, {"session_id": "e2e-session-1", "cwd": str(proj_dir)}, env
    )
    assert rc == 0, err
    assert out == "", f"no RATE block expected (nothing seeded): {out!r}"
    assert len(_session_end_markers(spool_dir)) == 1

    # Session B: a NEW session drains, synthesizes, and retrieves.
    env_b = isolated_env(clean_slate_home, CLAUDE_SESSION_ID="e2e-session-2")
    with (tmp_path / "c6-b.stderr").open("w", encoding="utf-8") as errlog:
        async with mcp_session(env_b, errlog=errlog) as session:
            # retrieve triggers SpoolService.drain: consumes the marker and
            # closes background episode E as no_outcome.
            buckets = _single_json_dict(
                await session.call_tool("memory.retrieve", {})
            )
            assert {"do", "dont", "neutral"} <= set(buckets)
            assert _session_end_markers(spool_dir) == [], (
                "session_end marker not consumed by the drain"
            )

            started = _single_json_dict(
                await session.call_tool(
                    "memory.start_episode", {"goal": "next task"}
                )
            )
            assert started["pending_synthesis"]["pending"] >= 1, started

            ctx = _single_json_dict(
                await session.call_tool("memory.synthesize_next_get_context", {})
            )
            episode_e = ctx["episode_id"]
            assert episode_e, f"queue empty — drain did not close E: {ctx}"
            assert ctx["episode"]["goal"] is None, (
                "expected the BACKGROUND episode (goal NULL), not a foreground one"
            )
            assert obs_id in {o["id"] for o in ctx["observations"]}, ctx

            decision = {
                "new": [
                    {
                        "title": "Pin flux drain timeouts",
                        "phase": "implementation",
                        "polarity": "dont",
                        "use_cases": "when draining spool in e2e",
                        "hints": ["always set a timeout"],
                        "confidence": 0.8,
                        "source_observation_ids": [obs_id],
                    }
                ],
                "augment": [],
                "merge": [],
                "ignore": [],
            }
            applied = _single_json_dict(
                await session.call_tool(
                    "memory.synthesize_next_apply",
                    {"episode_id": episode_e, "decision": decision},
                )
            )
            assert applied["ok"] is True, applied
            assert applied["counts"]["created"] == 1, applied
            assert {"pending", "in_cooldown", "done"} <= set(applied["queue"])

            # Mark-synthesized idempotency: E is never offered again.
            ctx2 = _single_json_dict(
                await session.call_tool("memory.synthesize_next_get_context", {})
            )
            assert ctx2["episode_id"] != episode_e

            # The loop closes: the new reflection surfaces for future sessions.
            final = _single_json_dict(
                await session.call_tool("memory.retrieve", {})
            )
            assert any(
                r["title"] == "Pin flux drain timeouts" for r in final["dont"]
            ), final

    # Post-exit DB: E closed by the drain, stamped by the apply.
    db_path = bm_home / "memory.db"
    episode_row = _query_one(
        db_path,
        "SELECT outcome, ended_at, synthesized_at FROM episodes WHERE id = ?",
        (episode_e,),
    )
    assert episode_row is not None
    assert episode_row["outcome"] == "no_outcome"
    assert episode_row["ended_at"], "drain must stamp ended_at"
    assert episode_row["synthesized_at"], "apply must stamp synthesized_at"

    refl_count = _query_one(
        db_path,
        "SELECT COUNT(*) AS n FROM reflections WHERE title = ?",
        ("Pin flux drain timeouts",),
    )
    assert refl_count is not None and refl_count["n"] == 1
