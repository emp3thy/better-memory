"""E2E agentcore onboarding journey (J1-J8).

Design: ``docs/superpowers/specs/2026-07-12-agentcore-journey-tests-design.md``.

Every scenario runs under the EXACT state ``better-memory agentcore init``
leaves behind (fix plan section 4 item 1): ``agentcore.json`` +
``settings.json {"storage_backend": "agentcore"}`` in the fake home and
**no** backend env var (``BETTER_MEMORY_STORAGE_BACKEND=None`` pin) — the
backend resolves from settings.json, the region and memory ids come from
agentcore.json. This is the shipped onboarding config, unlike the D-suite
(``test_agentcore_t2.py``) which force-activates via the env var.

* J1 ``e2e-ac-journey-boot-wired-surface``      (complements D1)
* J2 ``e2e-ac-journey-bootstrap-hook-wire``     (NEW — zero prior coverage)
* J3 ``e2e-ac-journey-observe-retrieve-chain``  (inverse half of old D3 pin)
* J4 ``e2e-ac-journey-retrieve-fanout``         (complements D2)
* J5 ``e2e-ac-journey-semantic-roundtrip``      (NEW; complements D4)
* J6 ``e2e-ac-journey-rating-loop``             (complements D4)
* J7 ``e2e-ac-journey-contextual-inject``       (complements D6-A)
* J8 ``e2e-ac-journey-session-close-terminus``  (complements D5)

No-duplication boundary (design section 6): exhaustive CreateEvent /
BatchCreate / BatchUpdate payload+metadata shape is owned by D4
(``TestBackendWireFidelity``); polarity-filter internals by D2; the
env-gate matrix + json-region signing by D5; missing-agentcore.json
degradation by D6 B/C; region convergence by D7. The journey asserts only
(a) returned ids are the AWS ids (not local uuids), (b) exact operation +
target memory + request count, (c) zero local sqlite rows, and (d)
cross-step id chaining.

Every subprocess env is built through :func:`tests.e2e._agentcore_env.
agentcore_env` at the spawn site (the choke-point AST contract requires
the helper call to be textually visible where the env name is assigned,
so the onboarding helper below writes FILES only and never builds envs).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from tests.e2e._agentcore_env import (
    agentcore_env,
    write_backend_settings,
    write_fake_agentcore_json,
)
from tests.e2e._fake_agentcore import FakeAgentCore
from tests.e2e.conftest import mcp_session, run_hook
from tests.e2e.test_agentcore_t2 import (
    _DOCKER_REFLECTION_SUMMARY,
    EPISODE_AND_RETENTION_TOOLS,
    EXPECTED_TOOL_SUBSET,
    SYNTHESIZE_TOOLS,
)

BOOTSTRAP_HOOK = "better_memory.hooks.session_bootstrap"
INJECT_HOOK = "better_memory.hooks.contextual_inject"
CLOSE_HOOK = "better_memory.hooks.session_close"

#: The rating/exposure + semantic-CRUD tools the dispatch wiring exposes in
#: agentcore mode (design J1) — all wired to AgentCoreBackend by fix Task 2.
RATING_AND_SEMANTIC_TOOLS: frozenset[str] = frozenset(
    {
        "memory.retrieve_observations",
        "memory.apply_session_ratings",
        "memory.list_session_exposures",
        "memory.credit",
        "memory.semantic_retrieve",
        "memory.semantic_update",
        "memory.semantic_delete",
    }
)

#: >= 40 chars each: real botocore request validation enforces a 40-char
#: minimum on memoryRecordId, mirrored by the product-side guard in
#: memory.record_use (agentcore mode).
_REFL_ID = "refl-journey-" + "0" * 30 + "1"
_SEM_ID = "sem-journey-" + "1" * 30


# ---------------------------------------------------------------------------
# Onboarding anchor + local helpers
# ---------------------------------------------------------------------------


def _bm_home(home: Path) -> Path:
    return home / ".better-memory"


def _onboard(clean_slate_home: Path) -> Path:
    """Fabricate the onboarding FILE state ``agentcore init`` leaves behind:
    ``agentcore.json`` (provisioning) + ``settings.json`` (activation).

    Returns ``bm_home``. Deliberately does NOT build the child env — every
    test builds it at the spawn site via ``agentcore_env(clean_slate_home,
    fake.port, BETTER_MEMORY_STORAGE_BACKEND=None)`` so the env-choke-point
    AST contract (tests/e2e_meta/test_env_helper_contract.py) can bless the
    assignment textually.
    """
    bm_home = _bm_home(clean_slate_home)
    write_fake_agentcore_json(bm_home)
    write_backend_settings(bm_home)
    return bm_home


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


def _table_names(db_path: Path) -> set[str]:
    assert db_path.exists(), f"database file missing: {db_path}"
    with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    return {row[0] for row in rows}


def _row_count(db_path: Path, table: str) -> int:
    """Row count of ``table``; 0 when the file or table does not exist —
    the no-sqlite-leakage oracle must pass whether or not a migration ran."""
    if not db_path.exists():
        return 0
    if table not in _table_names(db_path):
        return 0
    with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)) as conn:
        (count,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608 — table from test constants
    return int(count)


def _session_end_markers(spool_dir: Path) -> list[Path]:
    """Top-level (NON-recursive) session_end markers (drain quarantines
    malformed files into a subdirectory; recursion would count those)."""
    if not spool_dir.is_dir():
        return []
    return sorted(spool_dir.glob("*_session_end_*.json"))


# ---------------------------------------------------------------------------
# J1 — onboarding boot advertises the wired tool surface
# ---------------------------------------------------------------------------


async def test_j1_onboarding_boot_advertises_wired_tool_surface(
    clean_slate_home: Path, tmp_path: Path
) -> None:
    """Server boot resolving the backend FROM settings.json (no env var):
    the wired data tools plus the rating/semantic tools are advertised;
    synthesize AND episode+retention tools are hidden (UD-1); boot makes
    ZERO AWS calls; both local sqlite DBs are still created + migrated
    (UD-4 doc-side resolution).

    regression_caught: reverting the supports_episodes gate re-advertises
    episode tools; reverting create_server's settings.json resolution boots
    sqlite (synthesize tools present, fake untouched but sqlite surface).
    """
    with FakeAgentCore() as fake:
        bm_home = _onboard(clean_slate_home)
        env = agentcore_env(
            clean_slate_home, fake.port, BETTER_MEMORY_STORAGE_BACKEND=None
        )

        with (tmp_path / "j1.stderr").open("w", encoding="utf-8") as errlog:
            async with mcp_session(env, errlog=errlog) as session:
                listed = await session.list_tools()

        names = {tool.name for tool in listed.tools}
        assert EXPECTED_TOOL_SUBSET <= names, (
            f"missing wired tools: {sorted(EXPECTED_TOOL_SUBSET - names)}"
        )
        assert RATING_AND_SEMANTIC_TOOLS <= names, (
            f"missing rating/semantic tools: "
            f"{sorted(RATING_AND_SEMANTIC_TOOLS - names)}"
        )
        assert not (SYNTHESIZE_TOOLS & names)
        assert not (EPISODE_AND_RETENTION_TOOLS & names)
        # Boot is client construction only — never a wire call.
        assert fake.requests == []

    # Local DBs still migrated in agentcore mode (subset, never exact set).
    assert {"observations", "episodes", "hook_errors"} <= _table_names(
        bm_home / "memory.db"
    )
    assert (bm_home / "knowledge.db").exists()


# ---------------------------------------------------------------------------
# J2 — session_bootstrap hook reaches AgentCore, not sqlite
# ---------------------------------------------------------------------------


def test_j2_session_bootstrap_hook_reaches_agentcore(
    clean_slate_home: Path, tmp_path: Path
) -> None:
    """SessionStart hook under the onboarding config routes through
    ``build_backend`` to ``AgentCoreBackend.session_bootstrap``: exactly 3
    ListMemoryRecords (2 EPI reflection namespaces — project + the
    general/promoted merge, polarity bucketed client-side — + 1 SEM), the
    envelope carries the reflection/semantic summary, and NO memory
    content lands in local sqlite.

    regression_caught: reverting the hook's build_backend routing (fix
    Task 3) falls back to SessionBootstrapService on sqlite — zero wire
    requests and sqlite rows appear.
    """
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    with FakeAgentCore() as fake:
        bm_home = _onboard(clean_slate_home)
        fake.set_response("ListMemoryRecords", {"memoryRecordSummaries": []})
        env = agentcore_env(
            clean_slate_home, fake.port, BETTER_MEMORY_STORAGE_BACKEND=None
        )

        rc, out, err = run_hook(
            BOOTSTRAP_HOOK,
            {"source": "startup", "session_id": "e2e-session-1", "cwd": str(proj_dir)},
            env,
        )
        assert rc == 0, err
        assert "Traceback" not in err
        hso = _hook_envelope(out)["hookSpecificOutput"]
        assert hso["hookEventName"] == "SessionStart"
        ctx = hso["additionalContext"]
        # The fallback directive would mean the backend path died.
        assert "session bootstrap failed" not in ctx, ctx
        assert "Reflections" in ctx
        assert "Semantic memories:" in ctx

        lists = fake.requests_for("ListMemoryRecords")
        assert len(lists) == 3
        assert len(fake.requests) == 3
        assert sum("EPI-FAKE-0001" in r.path for r in lists) == 2
        assert sum("SEM-FAKE-0001" in r.path for r in lists) == 1
        # No metadataFilters anywhere — polarity is not a legal filter key
        # on real AWS (the fake 400s it) and status is client-side.
        assert all("metadataFilters" not in r.body for r in lists)

    # No memory-content sqlite write (a hook_errors-only migration would be
    # tolerated; memory content must be zero either way).
    assert _row_count(bm_home / "memory.db", "observations") == 0
    assert _row_count(bm_home / "memory.db", "semantic_memories") == 0


# ---------------------------------------------------------------------------
# J3 — observe → retrieve_observations round-trip (dispatch anchor)
# ---------------------------------------------------------------------------


async def test_j3_observe_then_retrieve_observations_chains_aws_id(
    clean_slate_home: Path, tmp_path: Path
) -> None:
    """One MCP session: ``memory.observe`` returns the AWS eventId (a local
    uuid means it hit sqlite), then ``memory.retrieve_observations``
    surfaces that same id via ListEvents with the metadata flattened —
    the read-after-write chain plus the inverse half of the deleted D3
    pin (zero rows in local ``observations``).

    Hermetic note: the fake does not persist writes, so the ListEvents
    response is canned to echo the observed id; TRUE persistence is E3's
    job (live tier). Exhaustive CreateEvent payload shape is owned by D4.

    regression_caught: reverting the ``remote=`` wiring for either tool
    returns a local uuid / writes a sqlite row / drops the wire counts to 0.
    """
    with FakeAgentCore() as fake:
        bm_home = _onboard(clean_slate_home)
        fake.set_response("CreateEvent", {"event": {"eventId": "evt-journey-1"}})
        fake.set_response(
            "ListEvents",
            {
                "events": [
                    {
                        "eventId": "evt-journey-1",
                        "sessionId": "e2e-session-1",
                        "actorId": "e2e-project",
                        "payload": [
                            {
                                "conversational": {
                                    "content": {"text": "journey-obs-marker"}
                                }
                            }
                        ],
                        "metadata": {
                            "outcome": {"stringValue": "failure"},
                            "theme": {"stringValue": "bug"},
                        },
                    }
                ]
            },
        )
        env = agentcore_env(
            clean_slate_home, fake.port, BETTER_MEMORY_STORAGE_BACKEND=None
        )

        with (tmp_path / "j3.stderr").open("w", encoding="utf-8") as errlog:
            async with mcp_session(env, errlog=errlog) as session:
                observed = _single_json_dict(
                    await session.call_tool(
                        "memory.observe",
                        {
                            "content": "journey-obs-marker",
                            "outcome": "failure",
                            "theme": "bug",
                        },
                    )
                )
                # The AWS eventId, not a local uuid — dispatch switched.
                assert observed["id"] == "evt-journey-1"
                creates = fake.requests_for("CreateEvent")
                assert len(creates) == 1
                assert len(fake.requests) == 1
                assert "EPI-FAKE-0001" in creates[0].path

                fake.clear()
                rows = _single_json_list(
                    await session.call_tool(
                        "memory.retrieve_observations",
                        {"query": "journey-obs-marker"},
                    )
                )
                lists = fake.requests_for("ListEvents")
                assert len(lists) == 1
                assert len(fake.requests) == 1
                assert "EPI-FAKE-0001" in lists[0].path
                # sessionId/actorId are URI members on ListEvents.
                assert "/sessions/e2e-session-1" in lists[0].path
                assert lists[0].body["includePayloads"] is True

                assert len(rows) == 1
                row = rows[0]
                assert row["id"] == "evt-journey-1"  # cross-step id chain
                assert row["content"] == "journey-obs-marker"
                # Metadata flattened out of the stringValue wrappers.
                assert row["outcome"] == "failure"
                assert row["theme"] == "bug"
                # Key parity with the sqlite retrieve_observations rows —
                # no agentcore-only keys (session_id/actor_id/
                # event_timestamp) leak to the MCP payload.
                assert set(row) == {
                    "id", "content", "component", "theme", "outcome",
                    "reinforcement_score", "created_at",
                }

    # No sqlite leakage: the observation never landed locally.
    assert _row_count(bm_home / "memory.db", "observations") == 0


# ---------------------------------------------------------------------------
# J4 — memory.retrieve bucket fan-out under onboarding config
# ---------------------------------------------------------------------------


async def test_j4_retrieve_fans_out_three_epi_list_calls(
    clean_slate_home: Path, tmp_path: Path
) -> None:
    """``memory.retrieve`` under settings.json activation: exactly 2
    ListMemoryRecords to the EPI memory — the project reflections
    namespace plus the general/promoted merge — with no metadataFilters;
    buckets come back as exactly ``{"do": [], "dont": [], "neutral": []}``.
    Client-side bucketing internals are owned by D2.

    regression_caught: collapsing to a single namespace (promoted records
    invisible) or reverting dispatch makes the count != 2; resurrecting
    the polarity server filter now 400s against the dialect-enforcing fake.
    """
    with FakeAgentCore() as fake:
        _onboard(clean_slate_home)
        fake.set_response("ListMemoryRecords", {"memoryRecordSummaries": []})
        env = agentcore_env(
            clean_slate_home, fake.port, BETTER_MEMORY_STORAGE_BACKEND=None
        )

        with (tmp_path / "j4.stderr").open("w", encoding="utf-8") as errlog:
            async with mcp_session(env, errlog=errlog) as session:
                buckets = _single_json_dict(
                    await session.call_tool("memory.retrieve", {})
                )

        assert buckets == {"do": [], "dont": [], "neutral": []}
        lists = fake.requests_for("ListMemoryRecords")
        assert len(lists) == 2
        assert len(fake.requests) == 2
        for request in lists:
            assert "EPI-FAKE-0001" in request.path
            assert "metadataFilters" not in request.body
        assert {r.body.get("namespace") for r in lists} == {
            "projects/e2e-project/reflections/",
            "general/reflections/",
        }


# ---------------------------------------------------------------------------
# J5 — semantic_observe → semantic_retrieve round-trip (UD-2 merge)
# ---------------------------------------------------------------------------


async def test_j5_semantic_observe_then_retrieve_merges_two_namespaces(
    clean_slate_home: Path, tmp_path: Path
) -> None:
    """One MCP session: ``memory.semantic_observe`` fires exactly one
    BatchCreateMemoryRecords to the SEMANTIC memory (never episodic) and
    returns the AWS record id; ``memory.semantic_retrieve`` fires exactly
    TWO ListMemoryRecords to SEM (project + general namespaces, the UD-2
    merge), dedupes by id, and each merged item carries the stable payload
    keys with ``project``/``created_at``/``updated_at`` as None. Zero rows
    in local ``semantic_memories``. sha256 requestIdentifier + strategy-id
    routing are owned by D4.

    regression_caught: dropping the general-scope second call makes the
    list count 1, not 2; reverting semantic dispatch writes/reads sqlite
    with zero SEM wire traffic.
    """
    with FakeAgentCore() as fake:
        bm_home = _onboard(clean_slate_home)
        fake.set_response(
            "BatchCreateMemoryRecords",
            {
                "successfulRecords": [{"memoryRecordId": "sem-journey-1"}],
                "failedRecords": [],
            },
        )
        # The same canned summary answers BOTH list calls — the merged
        # payload having exactly one item proves the id-dedupe. The stored
        # namespace carries the leading slash real AWS adds on read-back
        # (live dialect) — the scope classifier must normalize it.
        fake.set_response(
            "ListMemoryRecords",
            {
                "memoryRecordSummaries": [
                    {
                        "memoryRecordId": "sem-journey-1",
                        "content": {"text": "journey-sem-marker"},
                        "namespaces": ["/projects/e2e-project/semantic/"],
                    }
                ]
            },
        )
        env = agentcore_env(
            clean_slate_home, fake.port, BETTER_MEMORY_STORAGE_BACKEND=None
        )

        with (tmp_path / "j5.stderr").open("w", encoding="utf-8") as errlog:
            async with mcp_session(env, errlog=errlog) as session:
                created = _single_json_dict(
                    await session.call_tool(
                        "memory.semantic_observe",
                        {"content": "journey-sem-marker"},
                    )
                )
                assert created["id"] == "sem-journey-1"
                batches = fake.requests_for("BatchCreateMemoryRecords")
                assert len(batches) == 1
                assert len(fake.requests) == 1
                assert "SEM-FAKE-0001" in batches[0].path
                assert "EPI-FAKE-0001" not in batches[0].text()  # never EPI

                fake.clear()
                merged = _single_json_list(
                    await session.call_tool("memory.semantic_retrieve", {})
                )
                lists = fake.requests_for("ListMemoryRecords")
                assert len(lists) == 2  # UD-2: project + general merge
                assert len(fake.requests) == 2
                for request in lists:
                    assert "SEM-FAKE-0001" in request.path
                assert {r.body["namespace"] for r in lists} == {
                    "projects/e2e-project/semantic/",
                    "general/semantic/",
                }

                # Id-dedupe across the two calls: exactly one merged item.
                assert len(merged) == 1
                item = merged[0]
                assert item["id"] == "sem-journey-1"  # cross-step id chain
                assert item["content"] == "journey-sem-marker"
                # "/projects/..." (stored leading slash) classifies as
                # project, not general — the read-back normalization.
                assert item["scope"] == "project"
                # Stable keys, present as None (agentcore records carry no
                # project/created_at/updated_at — UD-2 payload contract).
                assert item["project"] is None
                assert item["created_at"] is None
                assert item["updated_at"] is None

    assert _row_count(bm_home / "memory.db", "semantic_memories") == 0


# ---------------------------------------------------------------------------
# J6 — rating loop: record_use, short-id guard, semantic rating, exposures
# ---------------------------------------------------------------------------


async def test_j6_rating_loop_guard_and_empty_exposures(
    clean_slate_home: Path, tmp_path: Path
) -> None:
    """The rating loop over MCP dispatch in one session:

    * ``memory.record_use`` on a >= 40-char reflection id → GetMemoryRecord
      + BatchUpdateMemoryRecords, both to EPI, system metadata stripped,
      counter incremented from the GET response.
    * a SHORT id → clear isError naming the 40-char floor with ZERO wire
      requests (the Task-2 guard that avoids the ~20s transient-404 retry
      stall inside the serialized dispatch loop).
    * ``memory.apply_session_ratings`` kind=semantic → lookup + update both
      routed to SEM.
    * ``memory.list_session_exposures`` → the empty envelope with zero wire
      (agentcore has no exposure log — the rating-model difference from
      sqlite C5).

    Full-snapshot strip contract owned by D4.
    """
    with FakeAgentCore() as fake:
        bm_home = _onboard(clean_slate_home)
        env = agentcore_env(
            clean_slate_home, fake.port, BETTER_MEMORY_STORAGE_BACKEND=None
        )

        with (tmp_path / "j6.stderr").open("w", encoding="utf-8") as errlog:
            async with mcp_session(env, errlog=errlog) as session:
                # --- record_use on the reflection id (EPI) ---------------
                fake.set_response(
                    "GetMemoryRecord",
                    {
                        "memoryRecord": {
                            "memoryRecordId": _REFL_ID,
                            "metadata": {
                                "useful_count": {"numberValue": 2},
                                "status": {"stringValue": "active"},
                                "x-amz-agentcore-memory-recordType": {
                                    "stringValue": "EXTRACTED"
                                },
                            },
                        }
                    },
                )
                fake.set_response(
                    "BatchUpdateMemoryRecords",
                    {
                        "successfulRecords": [{"memoryRecordId": _REFL_ID}],
                        "failedRecords": [],
                    },
                )
                ok = _single_json_dict(
                    await session.call_tool(
                        "memory.record_use",
                        {"id": _REFL_ID, "outcome": "success"},
                    )
                )
                assert ok == {"ok": True}
                gets = fake.requests_for("GetMemoryRecord")
                updates = fake.requests_for("BatchUpdateMemoryRecords")
                assert len(gets) == 1
                assert len(updates) == 1
                assert "EPI-FAKE-0001" in gets[0].path
                assert "EPI-FAKE-0001" in updates[0].path
                metadata = updates[0].body["records"][0]["metadata"]
                assert not any(
                    key.startswith("x-amz-agentcore-memory-") for key in metadata
                ), metadata
                # Counter incremented from the GET response: 2 → 3.
                assert metadata["useful_count"]["numberValue"] == pytest.approx(3)

                # --- short-id guard: clear error, ZERO wire --------------
                fake.clear()
                short = await session.call_tool(
                    "memory.record_use", {"id": "refl-1", "outcome": "success"}
                )
                message = _error_text(short)
                assert "40" in message, message
                assert "refl-1" in message, message
                assert fake.requests == [], (
                    "short-id guard must reject BEFORE any wire call"
                )

                # --- semantic-kind rating routes to SEM ------------------
                fake.set_response(
                    "GetMemoryRecord",
                    {
                        "memoryRecord": {
                            "memoryRecordId": _SEM_ID,
                            "metadata": {
                                "useful_count": {"numberValue": 0},
                                "status": {"stringValue": "active"},
                            },
                        }
                    },
                )
                fake.set_response(
                    "BatchUpdateMemoryRecords",
                    {
                        "successfulRecords": [{"memoryRecordId": _SEM_ID}],
                        "failedRecords": [],
                    },
                )
                applied = _single_json_dict(
                    await session.call_tool(
                        "memory.apply_session_ratings",
                        {
                            "ratings": [
                                {"kind": "semantic", "id": _SEM_ID, "class": "cited"}
                            ]
                        },
                    )
                )
                # Sqlite-parity envelope: per-class applied counts + the
                # per-reason skipped counts, keyed by session_id.
                assert applied["session_id"] == "e2e-session-1"
                assert applied["applied"]["cited"] == 1
                assert sum(applied["applied"].values()) == 1
                assert sum(applied["skipped"].values()) == 0
                gets = fake.requests_for("GetMemoryRecord")
                updates = fake.requests_for("BatchUpdateMemoryRecords")
                assert len(gets) == 1
                assert len(updates) == 1
                assert "SEM-FAKE-0001" in gets[0].path
                assert "SEM-FAKE-0001" in updates[0].path

                # --- exposures: empty envelope, zero wire ----------------
                fake.clear()
                exposures = _single_json_dict(
                    await session.call_tool("memory.list_session_exposures", {})
                )
                assert exposures == {"session_id": "e2e-session-1", "exposures": []}
                assert fake.requests == []

    assert _row_count(bm_home / "memory.db", "observations") == 0


# ---------------------------------------------------------------------------
# J7 — contextual_inject per-prompt honours settings.json activation
# ---------------------------------------------------------------------------


def test_j7_contextual_inject_honours_settings_activation(
    clean_slate_home: Path, tmp_path: Path
) -> None:
    """The per-prompt hook reaches AWS with NO backend env var — proving
    ``contextual_inject``'s resolver honours settings.json (the load-
    bearing difference from D6-A, which force-sets the env var): 2 EPI
    list + 2 EPI relevance-search + 1 SEM list + 2 SEM relevance-search
    calls, and a ``<project-memory>`` block in the envelope.

    The docker reflection qualifies via relevance_ranks membership (a
    server-side RetrieveMemoryRecords "finding" it), not the keyword-hit
    fallback -- a legitimate empty search result must NOT re-qualify a
    memory via keyword overlap (design spec
    2026-07-24-agentcore-parity-design.md §3 / relevance_ranks' None vs
    {} contract) -- so RetrieveMemoryRecords is explicitly canned to
    return the docker record for the episodic memory (EPI-FAKE-0001) and
    {} for the semantic memory (SEM-FAKE-0001), which has no record to
    find. See test_agentcore_t2.py's D6-A case-A test for the identical
    fan-out this mirrors.

    regression_caught: reverting contextual_inject to env-only backend
    resolution ignores settings.json → empty envelope, zero wire.
    """
    with FakeAgentCore() as fake:
        _onboard(clean_slate_home)
        fake.set_response(
            "ListMemoryRecords",
            {"memoryRecordSummaries": [_DOCKER_REFLECTION_SUMMARY]},
        )

        def _retrieve_response(request: Any) -> dict[str, Any]:
            if "EPI-FAKE-0001" in request.path:
                return {"memoryRecordSummaries": [
                    {"memoryRecordId": "refl-fake-docker"},
                ]}
            return {"memoryRecordSummaries": []}

        fake.set_response("RetrieveMemoryRecords", _retrieve_response)
        env = agentcore_env(
            clean_slate_home,
            fake.port,
            BETTER_MEMORY_STORAGE_BACKEND=None,
            BETTER_MEMORY_CONTEXT_INJECT_MODE="userprompt",
        )

        rc, out, err = run_hook(
            INJECT_HOOK,
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "how do I deploy with docker compose",
                "session_id": "e2e-inject-session",
                "cwd": str(tmp_path / "proj"),
            },
            env,
        )
        assert rc == 0
        assert "Traceback" not in err
        hso = _hook_envelope(out)["hookSpecificOutput"]
        assert hso["hookEventName"] == "UserPromptSubmit"
        rendered = hso["additionalContext"]
        assert "<project-memory" in rendered
        assert "refl-fake-docker" in rendered

        paths = [r.path for r in fake.requests]
        # EPI-FAKE-0001: 2 ListMemoryRecords (backend.retrieve's Wilson
        # fetch, project + general/promoted, no query so no relevance
        # search of its own) + 2 RetrieveMemoryRecords (relevance_ranks'
        # "reflection" kind, project + general) = 4.
        # SEM-FAKE-0001: 1 ListMemoryRecords (backend.semantic_list,
        # project namespace only) + 2 RetrieveMemoryRecords
        # (relevance_ranks' "semantic" kind, project + general) = 3.
        assert sum("EPI-FAKE-0001" in p for p in paths) == 4
        assert sum("SEM-FAKE-0001" in p for p in paths) == 3


# ---------------------------------------------------------------------------
# J8 — session_close closure under activation (sequence terminus)
# ---------------------------------------------------------------------------


def test_j8_session_close_fires_closure_and_marker(
    clean_slate_home: Path, tmp_path: Path
) -> None:
    """The terminal narrative step under the onboarding config: the Stop
    hook fires exactly one closure CreateEvent (role=OTHER, EPI) resolved
    from settings.json, writes the session_end spool marker, and never
    creates memory.db. The full env-gate matrix + json-region signing are
    owned by D5.

    regression_caught: reverting session_close's settings-aware gate makes
    the env-absent hook return before the closure — zero wire (the exact
    pre-fix defect-4 behavior).
    """
    with FakeAgentCore() as fake:
        bm_home = _onboard(clean_slate_home)
        env = agentcore_env(
            clean_slate_home, fake.port, BETTER_MEMORY_STORAGE_BACKEND=None
        )

        rc, out, err = run_hook(
            CLOSE_HOOK,
            {
                "session_id": "e2e-session-1",
                "cwd": str(tmp_path / "proj"),
                "hook_event_name": "Stop",
            },
            env,
        )
        assert rc == 0
        assert out == ""  # no rating block, no noise — the marker path
        assert "Traceback" not in err

        closures = fake.requests_for("CreateEvent")
        assert len(closures) == 1
        assert len(fake.requests) == 1
        assert "EPI-FAKE-0001" in closures[0].path
        conversational = closures[0].body["payload"][0]["conversational"]
        assert conversational["role"] == "OTHER"
        assert closures[0].body["sessionId"] == "e2e-session-1"

    markers = _session_end_markers(bm_home / "spool")
    assert len(markers) == 1, f"expected exactly one session_end marker: {markers}"
    body = json.loads(markers[0].read_text(encoding="utf-8"))
    assert body["event_type"] == "session_end"
    # Closure succeeded → no hook_errors write → no memory.db at all.
    assert not (bm_home / "memory.db").exists()
