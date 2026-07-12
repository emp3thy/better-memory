"""T3 live-AWS e2e journey (design scenarios E1-E2).

Design: docs/superpowers/specs/2026-07-12-e2e-clean-slate-smoke-design.md
section 1E. Gated exactly like the rest of ``tests/integration``:

* ``@pytest.mark.integration`` (deselected by default via pyproject addopts)
* skip unless ``BETTER_MEMORY_TEST_AGENTCORE=1`` (E2 legs inherit the gate
  from the session-scoped ``agentcore_throwaway_memories`` fixture in
  ``tests/integration/conftest.py``; E1 gates explicitly because it
  provisions its own memories through the real ``agentcore init`` CLI).

Real AWS credentials come from boto3's default discovery chain in the
outer environment. Region from ``BETTER_MEMORY_TEST_AGENTCORE_REGION``
(default eu-west-2, via the ``agentcore_region`` conftest fixture).

E1 — ``e2e-live-init-status-journey``: the only live coverage of the
``init -> agentcore.json -> status`` operator journey (the unit tests are
fully mocked, and the conftest fixture deliberately bypasses the init CLI).
Catches real-AWS drift in the strategy/indexedKeys shape, the ACTIVE poll
loop, and the refuse-to-clobber money-safety contract.

E2 — ``e2e-live-smoke-retrieve-backend-roundtrip`` (rebuilt per both
judges): (a) the shipped ``agentcore smoke`` 6-step data-plane loop against
real AWS; (b) live MCP ``memory.retrieve`` through the real server under
the onboarding configuration (agentcore.json + settings.json, no backend/
region/id env vars — exactly what ``agentcore init`` leaves behind, per
the onboarding-fix wave); (c) direct ``AgentCoreBackend.observe ->
list_observations`` read-after-write with metadata survival. The MCP
observe->retrieve_observations round-trip (non-tautological now that the
dispatch layer is wired to AgentCoreBackend) is owned by the E3+ journey
additions (2026-07-12-agentcore-journey-tests-design.md).

E3-E7 — live onboarding-config journey additions (journey design section
3, T3 tier). Every child home carries agentcore.json + settings.json and
NO backend/region/id env vars — the shipped onboarding state:

* E3 — MCP ``observe -> retrieve_observations`` round-trip (raw events are
  promptly consistent) with zero local sqlite rows.
* E4 — MCP semantic CRUD round-trip (observe -> retrieve -> update ->
  delete; record-level operations are promptly consistent).
* E5 — rating/credit against a REAL >= 40-char semantic record id (the
  only live credit against a genuine AWS record id).
* E6 — session_bootstrap + contextual_inject hooks reach AWS under the
  onboarding config (well-formed envelopes; no hook_errors rows).
* E7 — session_close Stop hook fires exactly one closure
  CreateEvent(role=OTHER) with settings.json only, verified by a direct
  ``list_events`` readback on the throwaway episodic memory.

What the live tier deliberately SKIPS (journey design section 5):

* **Async reflection extraction is not promptly assertable** — AWS's
  built-in episodicMemoryStrategy extracts reflections MINUTES after
  ``observe``, so no live scenario asserts an observed fact surfaces in
  ``memory.retrieve`` buckets or bootstrap counts; E3 asserts
  ``observe -> retrieve_observations`` (raw events) instead, and E2(b)'s
  "buckets are exactly empty for fresh memories" contract stands.
* **``record_use`` on an EXTRACTED reflection is not promptly testable**
  (no reflections exist yet) — E5 credits a real SEMANTIC record instead,
  created synchronously by ``semantic_observe``.
* **Cross-session ``list_observations`` is out of scope** — ListEvents
  requires a sessionId; each live test pins a unique per-run session id
  so readback sees exactly its own events.

Region single-source (fix plan item 5): run E3-E7 with
``BETTER_MEMORY_TEST_AGENTCORE_REGION`` set to a NON-default region — the
region env var is deleted, so a factory regression that signs the default
region cross-regions the data plane and every call 404s. Live SigV4 region
is not directly observable client-side; the assertion is indirect via
request success.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from better_memory.cli import agentcore as agentcore_cli
from better_memory.storage.agentcore_persistence import (
    AgentCoreConfig,
    load_agentcore_config,
    save_agentcore_config,
)
from tests.e2e._agentcore_env import write_backend_settings
from tests.e2e._env import isolated_env
from tests.e2e.conftest import mcp_session, run_hook, text_of

pytestmark = [pytest.mark.integration]


def _require_live() -> None:
    """Same gate wording/mechanism as tests/integration/conftest.py — MUST
    stay a pytest.skip (never assert): ``-m integration`` on a
    credential-less box has to end green-skipped (meta-marker-tier-wiring)."""
    if os.environ.get("BETTER_MEMORY_TEST_AGENTCORE") != "1":
        pytest.skip("Set BETTER_MEMORY_TEST_AGENTCORE=1 to run real-AWS tests.")


def _live_env(tmp_home: Path, **pins: str | None) -> dict[str, str]:
    """Hermetic-home env with real AWS credentials passed back through.

    Built on the shared ``isolated_env`` choke point (fake HOME/USERPROFILE,
    outer CLAUDE_*/BETTER_MEMORY_*/OLLAMA_* stripped) — but live tests need
    boto3's default credential chain, which ``isolated_env`` strips along
    with everything else AWS_*. Re-pin every outer ``AWS_*`` var verbatim,
    and if credentials live only in the real ``~/.aws`` files (no env vars),
    point the file-location vars at the real files, because HOME/USERPROFILE
    are redirected to ``tmp_home``.
    """
    aws_pins: dict[str, str | None] = {
        k: v for k, v in os.environ.items() if k.upper().startswith("AWS_")
    }
    real_aws_dir = Path.home() / ".aws"
    upper = {k.upper() for k in aws_pins}
    if "AWS_SHARED_CREDENTIALS_FILE" not in upper:
        credentials = real_aws_dir / "credentials"
        if credentials.exists():
            aws_pins["AWS_SHARED_CREDENTIALS_FILE"] = str(credentials)
    if "AWS_CONFIG_FILE" not in upper:
        config_file = real_aws_dir / "config"
        if config_file.exists():
            aws_pins["AWS_CONFIG_FILE"] = str(config_file)
    return isolated_env(tmp_home, **{**aws_pins, **pins})


def _cli_argv(*args: str) -> list[str]:
    """Invoke the real CLI dispatcher as a subprocess (module has a
    ``__main__`` guard; the ``better-memory`` console script resolves to
    the same ``main``)."""
    return [sys.executable, "-m", "better_memory.cli.main", *args]


# ---------------------------------------------------------------------------
# E1 — e2e-live-init-status-journey
# ---------------------------------------------------------------------------


def test_live_init_status_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    request: pytest.FixtureRequest,
    agentcore_region: str,
) -> None:
    """``agentcore init`` provisions two ACTIVE memories under throwaway
    ``bm_int_*`` names, writes schema-1 agentcore.json, refuses re-init
    (money-safety pin), and ``status`` reports both ids ACTIVE.

    init runs IN-PROCESS (``handle(Namespace(...))``) so the monkeypatched
    default names apply; status runs as a real subprocess so the whole
    json -> get_memory -> ACTIVE aggregation journey is exercised end to
    end. Slow (~3-4 min of memory provisioning) but not flaky: unique
    suffixes, pre-registered cleanup, and the conftest >1h ``bm_int_``
    stale sweep bound any residue.
    """
    _require_live()
    import atexit

    import boto3
    from botocore.config import Config as BotoConfig

    # init hardcodes DEFAULT_EPISODIC_NAME / DEFAULT_SEMANTIC_NAME; both are
    # imported into cli.agentcore's own namespace and read as module globals
    # in _handle_init, so patching cli.agentcore.DEFAULT_* redirects them.
    # bm_int_ prefix rides the existing >1h stale-memory sweep; names satisfy
    # the AWS regex [a-zA-Z][a-zA-Z0-9_]{0,47}.
    suffix = uuid.uuid4().hex[:8]
    monkeypatch.setattr(agentcore_cli, "DEFAULT_EPISODIC_NAME", f"bm_int_initepi_{suffix}")
    monkeypatch.setattr(agentcore_cli, "DEFAULT_SEMANTIC_NAME", f"bm_int_initsem_{suffix}")

    bm_home = tmp_path / "bm-home"
    config_path = bm_home / "agentcore.json"

    control = boto3.client(
        "bedrock-agentcore-control",
        config=BotoConfig(
            region_name=agentcore_region,
            retries={"mode": "standard", "max_attempts": 5},
        ),
    )

    # Cleanup registered BEFORE init runs (atexit + finalizer, mirroring the
    # conftest fixture pattern) so Ctrl-C / hard kill after a successful
    # create still deletes both billable memories. If init itself fails
    # mid-flight, its own created_ids orphan cleanup plus the bm_int_ sweep
    # cover the gap (agentcore.json won't exist yet in that case).
    cleaned: list[bool] = []

    def _cleanup() -> None:
        if cleaned or os.environ.get("BETTER_MEMORY_TEST_AGENTCORE_KEEP") == "1":
            return
        cleaned.append(True)
        if not config_path.exists():
            return
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — best-effort teardown
            print(f"WARN: could not read {config_path} for cleanup: {exc!r}")
            return
        for kind in ("episodic", "semantic"):
            memory_id = (raw.get(kind) or {}).get("memory_id")
            if not memory_id:
                continue
            try:
                control.delete_memory(memoryId=memory_id)
            except Exception as exc:  # noqa: BLE001 — sweep catches leftovers
                print(
                    f"WARN: failed to delete {memory_id}: {exc!r} "
                    f"(the bm_int_ stale sweep will catch it)"
                )

    atexit.register(_cleanup)
    request.addfinalizer(_cleanup)

    # --- init: provisions both memories, writes agentcore.json -------------
    rc = agentcore_cli.handle(
        argparse.Namespace(
            subcommand="init", home=str(bm_home), region=agentcore_region, force=False
        )
    )
    assert rc == 0
    assert config_path.exists()

    cfg = load_agentcore_config(bm_home)
    assert cfg is not None
    assert cfg.schema_version == 1
    assert cfg.region == agentcore_region
    assert cfg.episodic.memory_id
    assert cfg.semantic.memory_id
    assert cfg.episodic.memory_id != cfg.semantic.memory_id
    # strategyId captured from the ACTIVE memory, not a placeholder —
    # downstream semantic writes depend on it.
    assert cfg.episodic.strategy_id
    assert cfg.semantic.strategy_id

    # --- re-init refusal: money-safety pin ---------------------------------
    # A silent overwrite would orphan two billable memories. The refusal
    # happens BEFORE any AWS client is built, so the file must be
    # byte-identical afterwards.
    before_bytes = config_path.read_bytes()
    capsys.readouterr()  # drain init's progress output
    rc2 = agentcore_cli.handle(
        argparse.Namespace(
            subcommand="init", home=str(bm_home), region=agentcore_region, force=False
        )
    )
    captured = capsys.readouterr()
    assert rc2 == 1
    assert "already exists" in captured.err
    assert "force" in captured.err
    assert config_path.read_bytes() == before_bytes

    # --- status subprocess: json -> get_memory -> ACTIVE aggregation -------
    # Region comes from cfg.region inside status (no --region passed);
    # credentials come from the AWS_* passthrough in _live_env.
    proc_home = tmp_path / "status-home"
    proc_home.mkdir()
    status = subprocess.run(  # noqa: S603 — test harness, fixed argv
        _cli_argv("agentcore", "status", "--home", str(bm_home)),
        env=_live_env(proc_home),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert status.returncode == 0, status.stdout + status.stderr
    assert cfg.episodic.memory_id in status.stdout
    assert cfg.semantic.memory_id in status.stdout
    assert "episodic:" in status.stdout
    assert "semantic:" in status.stdout
    assert "ACTIVE" in status.stdout


# ---------------------------------------------------------------------------
# E2 — e2e-live-smoke-retrieve-backend-roundtrip
# ---------------------------------------------------------------------------


def _write_throwaway_config(
    throwaway_memories: tuple, region: str, bm_home: Path
) -> AgentCoreConfig:
    """The throwaway MemoryRecords ARE the config — no second init needed."""
    sem_record, epi_record = throwaway_memories
    cfg = AgentCoreConfig(
        schema_version=1,
        region=region,
        semantic=sem_record,
        episodic=epi_record,
    )
    save_agentcore_config(cfg, bm_home)
    return cfg


def test_live_smoke_cli_passes(
    tmp_path: Path, agentcore_throwaway_memories, agentcore_region: str
) -> None:
    """E2(a): the shipped ``agentcore smoke`` CLI passes against real AWS.

    Pins the full 6-step data-plane loop (create_event x2 incl. the
    role=OTHER closure, list_events >= 2, batch_create with metadata,
    list readback, batch_delete) against real AWS wire validation —
    tests/cli/test_agentcore_smoke.py is fully mocked, so this is the only
    execution of that loop against the real service.
    """
    bm_home = tmp_path / "bm-home"
    _write_throwaway_config(agentcore_throwaway_memories, agentcore_region, bm_home)

    proc_home = tmp_path / "home"
    proc_home.mkdir()
    proc = subprocess.run(  # noqa: S603 — test harness, fixed argv
        _cli_argv("agentcore", "smoke", "--home", str(bm_home)),
        env=_live_env(proc_home),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "AgentCore smoke PASSED" in proc.stdout


async def test_live_mcp_retrieve_wired_path(
    tmp_path: Path, agentcore_throwaway_memories, agentcore_region: str
) -> None:
    """E2(b): real MCP server boot with real creds; ``memory.retrieve`` live.

    Runs under the exact ONBOARDING configuration `agentcore init` leaves
    behind (fix plan section 4 item 1): agentcore.json + settings.json in
    the child home, NO backend/region/id env vars — the backend resolves
    from settings.json and the region is single-sourced from
    agentcore.json (a non-default test region makes a factory regression
    404 loudly). It fires the per-polarity ListMemoryRecords fan-out
    (reflections namespace + metadataFilters) against the real service —
    live filter/namespace validation the T2 fakes structurally cannot see.
    Fresh throwaway memories hold no reflections (extraction is
    minutes-async and the smoke leg's records live under the ``smoke``
    actor's namespaces), so the buckets are exactly empty.
    """
    home = tmp_path / "home"
    home.mkdir()
    # isolated_env pins BETTER_MEMORY_HOME=<home>/.better-memory; the factory
    # loads agentcore.json from there and settings.json activates the
    # backend without any env var.
    bm_home = home / ".better-memory"
    _write_throwaway_config(agentcore_throwaway_memories, agentcore_region, bm_home)
    write_backend_settings(bm_home)

    env = _live_env(
        home,
        # No dashes: actor ids derive from the project name.
        BETTER_MEMORY_PROJECT="bmintproj",
        CLAUDE_SESSION_ID=f"bm-int-{uuid.uuid4().hex[:8]}",
    )

    errlog_path = tmp_path / "server.stderr"  # outside the fake home
    with errlog_path.open("w", encoding="utf-8") as errlog:
        async with mcp_session(
            env, errlog=errlog, read_timeout=timedelta(seconds=90)
        ) as session:
            result = await session.call_tool("memory.retrieve", {})
            content = result.content
            assert not getattr(result, "isError", False), (
                f"memory.retrieve errored: {content!r}\n"
                f"server stderr:\n{errlog_path.read_text(encoding='utf-8', errors='replace')}"
            )
            assert len(content) == 1, f"expected one content block: {content!r}"
            assert getattr(content[0], "type", None) == "text"
            buckets = json.loads(text_of(content[0]))

    # Exact empty buckets: real AWS accepted the namespace + status
    # metadataFilters and returned zero reflections for this actor.
    assert buckets == {"do": [], "dont": [], "neutral": []}


def test_live_backend_observe_metadata_survives_roundtrip(agentcore_backend) -> None:
    """E2(c): direct AgentCoreBackend observe -> list_observations with a
    unique marker: read-after-write plus METADATA SURVIVAL through the
    stringValue flattening (storage/agentcore.py list_observations mapping)
    — the assertion tests/integration/test_agentcore_roundtrip.py lacks
    (it only checks event-id presence).

    Event read-after-write is promptly consistent (the shipped smoke CLI
    relies on it); nothing here waits on async reflection extraction. The
    fixture's unique per-test session id means list_observations (current
    session only) sees exactly this event.
    """
    marker = f"bm-int-e2e-{uuid.uuid4().hex[:8]}"
    event_id = asyncio.run(
        agentcore_backend.observe(
            content=marker,
            outcome="success",
            theme="integration",
        )
    )
    assert isinstance(event_id, str) and event_id

    events = asyncio.run(agentcore_backend.list_observations(limit=50))
    matches = [e for e in events if e.get("content") == marker]
    assert len(matches) == 1, (
        f"expected exactly the marker event back, got {len(matches)} matches "
        f"among {len(events)} events"
    )
    item = matches[0]
    assert item["id"] == event_id
    # outcome/theme were sent as metadata stringValues on CreateEvent and
    # flattened back out of ListEvents — the AWS round-trip must preserve them.
    assert item.get("outcome") == "success"
    assert item.get("theme") == "integration"


# ---------------------------------------------------------------------------
# E3-E7 — live onboarding-config journey
# (2026-07-12-agentcore-journey-tests-design.md, section 3 T3 tier)
# ---------------------------------------------------------------------------

#: Actor ids derive from BETTER_MEMORY_PROJECT — no dashes (see E2(b)).
_JOURNEY_PROJECT = "bmintproj"


def _onboarding_home(
    tmp_path: Path, throwaway_memories: tuple, region: str
) -> tuple[Path, Path]:
    """A child home in the exact state ``agentcore init`` leaves behind:
    agentcore.json (the throwaway memories) + settings.json activation.
    Returns ``(home, bm_home)``."""
    home = tmp_path / "home"
    home.mkdir()
    bm_home = home / ".better-memory"
    _write_throwaway_config(throwaway_memories, region, bm_home)
    write_backend_settings(bm_home)
    return home, bm_home


def _journey_session_id() -> str:
    """Unique per-run session id: ListEvents readback (current session
    only) sees exactly this run's events."""
    return f"bm-int-{uuid.uuid4().hex[:8]}"


def _tool_json(result: Any) -> Any:
    """Parse the single text content block of a successful tool call."""
    content = result.content
    assert not getattr(result, "isError", False), f"tool errored: {content!r}"
    assert len(content) == 1, f"expected one content block: {content!r}"
    return json.loads(text_of(content[0]))


def _local_row_count(bm_home: Path, table: str) -> int:
    """Rows in a local sqlite table; 0 when the file/table is absent."""
    db = bm_home / "memory.db"
    if not db.exists():
        return 0
    with closing(
        sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    ) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if table not in names:
            return 0
        (count,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608 — table from test constants
    return int(count)


async def test_live_e3_mcp_observe_retrieve_observations_roundtrip(
    tmp_path: Path, agentcore_throwaway_memories, agentcore_region: str
) -> None:
    """E3: the non-tautological MCP write path the dispatch wiring enables
    (fix plan section 4 item 2): ``memory.observe`` over MCP returns a real
    AWS eventId and ``memory.retrieve_observations`` (raw events — promptly
    consistent) reads exactly that event back with content/outcome/theme
    surviving the round-trip. Zero rows land in the child home's local
    ``observations`` table.

    regression_caught: dispatch not wired → observe returns a local uuid
    and writes a sqlite row; region single-sourcing broken → cross-region
    404 (when run in a non-default region).
    """
    home, bm_home = _onboarding_home(
        tmp_path, agentcore_throwaway_memories, agentcore_region
    )
    env = _live_env(
        home,
        BETTER_MEMORY_PROJECT=_JOURNEY_PROJECT,
        CLAUDE_SESSION_ID=_journey_session_id(),
    )
    marker = f"bm-int-journey-{uuid.uuid4().hex[:8]}"

    errlog_path = tmp_path / "e3-server.stderr"  # outside the fake home
    with errlog_path.open("w", encoding="utf-8") as errlog:
        async with mcp_session(
            env, errlog=errlog, read_timeout=timedelta(seconds=90)
        ) as session:
            observed = _tool_json(
                await session.call_tool(
                    "memory.observe",
                    {"content": marker, "outcome": "failure", "theme": "bug"},
                )
            )
            event_id = observed["id"]
            assert isinstance(event_id, str) and event_id

            rows = _tool_json(
                await session.call_tool(
                    "memory.retrieve_observations", {"query": marker}
                )
            )
            matches = [r for r in rows if r.get("content") == marker]
            assert len(matches) == 1, (
                f"expected exactly the marker event, got {len(matches)} "
                f"matches among {len(rows)} rows"
            )
            assert matches[0]["id"] == event_id
            assert matches[0].get("outcome") == "failure"
            assert matches[0].get("theme") == "bug"

    # Dispatch switched: nothing landed in local sqlite.
    assert _local_row_count(bm_home, "observations") == 0


async def test_live_e4_mcp_semantic_crud_roundtrip(
    tmp_path: Path, agentcore_throwaway_memories, agentcore_region: str
) -> None:
    """E4: full MCP semantic round-trip against real AWS (fix plan section
    4 item 3): observe → retrieve surfaces the record (project + general
    UD-2 merge) → update changes the text → delete removes it. All
    record-level operations are promptly consistent. Zero local
    ``semantic_memories`` rows.
    """
    home, bm_home = _onboarding_home(
        tmp_path, agentcore_throwaway_memories, agentcore_region
    )
    env = _live_env(
        home,
        BETTER_MEMORY_PROJECT=_JOURNEY_PROJECT,
        CLAUDE_SESSION_ID=_journey_session_id(),
    )
    marker = f"bm-int-sem-{uuid.uuid4().hex[:8]}"

    def _mine(items: list, record_id: str) -> list:
        return [i for i in items if i.get("id") == record_id]

    errlog_path = tmp_path / "e4-server.stderr"
    with errlog_path.open("w", encoding="utf-8") as errlog:
        async with mcp_session(
            env, errlog=errlog, read_timeout=timedelta(seconds=90)
        ) as session:
            created = _tool_json(
                await session.call_tool(
                    "memory.semantic_observe", {"content": marker}
                )
            )
            record_id = created["id"]
            # A genuine AWS memoryRecordId (>= 40 chars) — not a local uuid.
            assert isinstance(record_id, str) and len(record_id) >= 40

            listed = _tool_json(
                await session.call_tool("memory.semantic_retrieve", {})
            )
            mine = _mine(listed, record_id)
            assert len(mine) == 1, f"record not surfaced: {listed!r}"
            assert mine[0]["content"] == marker
            # Stable payload keys under the UD-2 merge contract.
            assert {"project", "scope", "created_at", "updated_at"} <= set(mine[0])

            updated_marker = f"{marker}-v2"
            assert _tool_json(
                await session.call_tool(
                    "memory.semantic_update",
                    {"id": record_id, "content": updated_marker},
                )
            ) == {"ok": True}
            re_listed = _tool_json(
                await session.call_tool("memory.semantic_retrieve", {})
            )
            mine = _mine(re_listed, record_id)
            assert len(mine) == 1
            assert mine[0]["content"] == updated_marker

            assert _tool_json(
                await session.call_tool(
                    "memory.semantic_delete", {"id": record_id}
                )
            ) == {"ok": True}
            final = _tool_json(
                await session.call_tool("memory.semantic_retrieve", {})
            )
            assert _mine(final, record_id) == []

    assert _local_row_count(bm_home, "semantic_memories") == 0


async def test_live_e5_rating_credits_real_semantic_record_id(
    tmp_path: Path, agentcore_throwaway_memories, agentcore_region: str
) -> None:
    """E5: the only live credit against a GENUINE >= 40-char AWS record id
    (fix plan section 4 item 4): ``memory.apply_session_ratings`` with
    class=cited performs the full-snapshot update (system
    x-amz-agentcore-memory-* keys stripped — echoing them is a real AWS
    400). The record is created synchronously by ``semantic_observe`` —
    reflections are extraction-async and not promptly creditable.

    Creates its own record (rather than reusing E4's) so the test is
    order-independent; cleaned up via semantic_delete + the throwaway
    memory teardown.
    """
    home, _bm_home = _onboarding_home(
        tmp_path, agentcore_throwaway_memories, agentcore_region
    )
    env = _live_env(
        home,
        BETTER_MEMORY_PROJECT=_JOURNEY_PROJECT,
        CLAUDE_SESSION_ID=_journey_session_id(),
    )
    marker = f"bm-int-rate-{uuid.uuid4().hex[:8]}"

    errlog_path = tmp_path / "e5-server.stderr"
    with errlog_path.open("w", encoding="utf-8") as errlog:
        async with mcp_session(
            env, errlog=errlog, read_timeout=timedelta(seconds=90)
        ) as session:
            record_id = _tool_json(
                await session.call_tool(
                    "memory.semantic_observe", {"content": marker}
                )
            )["id"]
            assert isinstance(record_id, str) and len(record_id) >= 40

            payload = _tool_json(
                await session.call_tool(
                    "memory.apply_session_ratings",
                    {
                        "ratings": [
                            {"kind": "semantic", "id": record_id, "class": "cited"}
                        ]
                    },
                )
            )
            assert payload["applied"] == 1, payload
            assert payload["failed"] == 0, payload

            # Best-effort cleanup; teardown deletes the whole memory anyway.
            await session.call_tool("memory.semantic_delete", {"id": record_id})


def test_live_e6_bootstrap_and_inject_hooks_reach_aws(
    tmp_path: Path, agentcore_throwaway_memories, agentcore_region: str
) -> None:
    """E6: the SessionStart bootstrap hook and the contextual_inject
    UserPromptSubmit hook both reach AWS under the onboarding config (fix
    plan section 4 item 7): well-formed envelopes, the bootstrap summary
    rendered from real ListMemoryRecords responses (counts are empty for
    fresh memories — extraction is minutes-async), and NO hook_errors rows
    (the hooks' failure paths record-and-degrade; a clean home proves the
    AWS path ran).
    """
    home, bm_home = _onboarding_home(
        tmp_path, agentcore_throwaway_memories, agentcore_region
    )
    session_id = _journey_session_id()
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    env = _live_env(
        home,
        BETTER_MEMORY_PROJECT=_JOURNEY_PROJECT,
        CLAUDE_SESSION_ID=session_id,
        BETTER_MEMORY_CONTEXT_INJECT_MODE="userprompt",
    )

    rc, out, err = run_hook(
        "better_memory.hooks.session_bootstrap",
        {"source": "startup", "session_id": session_id, "cwd": str(proj_dir)},
        env,
    )
    assert rc == 0, err
    assert "Traceback" not in err
    hso = json.loads(out)["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart"
    ctx = hso["additionalContext"]
    # The AWS-rendered summary — the fallback directive would mean the
    # backend path died and was swallowed.
    assert "session bootstrap failed" not in ctx, ctx
    assert "Reflections" in ctx
    assert "Semantic memories:" in ctx

    rc, out, err = run_hook(
        "better_memory.hooks.contextual_inject",
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "how do I deploy with docker compose",
            "session_id": session_id,
            "cwd": str(proj_dir),
        },
        env,
    )
    assert rc == 0, err
    assert "Traceback" not in err
    envelope = json.loads(out)
    assert envelope["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"

    # Neither hook recorded a swallowed failure.
    assert _local_row_count(bm_home, "hook_errors") == 0
    assert _local_row_count(bm_home, "observations") == 0
    assert _local_row_count(bm_home, "semantic_memories") == 0


def test_live_e7_session_close_closure_with_settings_only(
    tmp_path: Path, agentcore_throwaway_memories, agentcore_region: str
) -> None:
    """E7: the live version of hermetic J8 (fix plan section 4 item 6):
    with settings.json only (no backend env var) the Stop hook fires
    exactly one closure CreateEvent(role=OTHER) against the REAL episodic
    memory — verified by a direct ``list_events`` readback on the unique
    per-run session id — and writes the spool marker. The closure event
    rides the throwaway memory's teardown.
    """
    import boto3
    from botocore.config import Config as BotoConfig

    home, bm_home = _onboarding_home(
        tmp_path, agentcore_throwaway_memories, agentcore_region
    )
    session_id = _journey_session_id()
    # The hook derives the closure actorId from the payload cwd's basename.
    proj_dir = tmp_path / "bmintclose"
    proj_dir.mkdir()
    env = _live_env(
        home,
        BETTER_MEMORY_PROJECT=_JOURNEY_PROJECT,
        CLAUDE_SESSION_ID=session_id,
    )

    rc, out, err = run_hook(
        "better_memory.hooks.session_close",
        {
            "session_id": session_id,
            "cwd": str(proj_dir),
            "hook_event_name": "Stop",
        },
        env,
    )
    assert rc == 0, err
    assert out == ""
    assert "Traceback" not in err

    markers = sorted((bm_home / "spool").glob("*_session_end_*.json"))
    assert len(markers) == 1, f"expected exactly one session_end marker: {markers}"
    marker_body = json.loads(markers[0].read_text(encoding="utf-8"))
    assert marker_body["event_type"] == "session_end"
    # Closure succeeded → no hook_errors write → no memory.db at all.
    assert not (bm_home / "memory.db").exists()

    # Ground truth: exactly one role=OTHER closure event landed on the real
    # episodic memory under this run's unique session id.
    _sem_record, epi_record = agentcore_throwaway_memories
    data = boto3.client(
        "bedrock-agentcore",
        config=BotoConfig(
            region_name=agentcore_region,
            retries={"mode": "standard", "max_attempts": 5},
        ),
    )
    events = data.list_events(
        memoryId=epi_record.memory_id,
        actorId="bmintclose",
        sessionId=session_id,
        includePayloads=True,
        maxResults=10,
    )["events"]
    assert len(events) == 1, events
    assert events[0]["payload"][0]["conversational"]["role"] == "OTHER"
