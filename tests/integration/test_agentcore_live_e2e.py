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
real AWS; (b) live MCP ``memory.retrieve`` through the real server — the
one MCP data path actually wired to AgentCoreBackend; (c) direct
``AgentCoreBackend.observe -> list_observations`` read-after-write with
metadata survival. The author-round MCP observe->retrieve_observations
round-trip was REMOVED: in agentcore mode those tools dispatch to local
sqlite (the dispatch gap, design section 4 item 2), so it would greenwash
AWS coverage forever.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from better_memory.cli import agentcore as agentcore_cli
from better_memory.storage.agentcore_persistence import (
    AgentCoreConfig,
    load_agentcore_config,
    save_agentcore_config,
)
from tests.e2e._env import isolated_env
from tests.e2e.conftest import mcp_session, text_of

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

    memory.retrieve is the ONE MCP data tool actually wired to
    AgentCoreBackend (dispatch gap, design section 4 item 2), so this is the
    only end-to-end MCP-through-AWS path that exists. It fires the
    per-polarity ListMemoryRecords fan-out (reflections namespace +
    metadataFilters) against the real service — live filter/namespace
    validation the T2 fakes structurally cannot see. Fresh throwaway
    memories hold no reflections (extraction is minutes-async and the smoke
    leg's records live under the ``smoke`` actor's namespaces), so the
    buckets are exactly empty.
    """
    sem_record, epi_record = agentcore_throwaway_memories
    home = tmp_path / "home"
    home.mkdir()
    # isolated_env pins BETTER_MEMORY_HOME=<home>/.better-memory; the factory
    # loads agentcore.json from there.
    bm_home = home / ".better-memory"
    _write_throwaway_config(agentcore_throwaway_memories, agentcore_region, bm_home)

    env = _live_env(
        home,
        BETTER_MEMORY_STORAGE_BACKEND="agentcore",
        # EXPLICIT region: the runtime factory signs with env
        # BETTER_MEMORY_AGENTCORE_REGION (default eu-west-2), NOT
        # agentcore.json's region — the region split-brain (design section 4
        # item 4, pinned by e2e-ac-region-split-brain-pin). Without this pin
        # a non-default test region silently cross-regions the data plane.
        BETTER_MEMORY_AGENTCORE_REGION=agentcore_region,
        # FIXME(idvar-gate): config.py:293-301 requires both ID vars in
        # agentcore mode but nothing consumes their values (IDs come from
        # agentcore.json). Set to the REAL throwaway ids — never dummies in a
        # live test, so if the product ever starts consuming them they still
        # point at the right memories. Delete when the gate is fixed
        # (together with tests/e2e/_agentcore_env.py's dummy vars and
        # e2e-ac-neg-prehandshake-config-errors[idvar]).
        BETTER_MEMORY_AGENTCORE_SEMANTIC_MEMORY_ID=sem_record.memory_id,
        BETTER_MEMORY_AGENTCORE_EPISODIC_MEMORY_ID=epi_record.memory_id,
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
