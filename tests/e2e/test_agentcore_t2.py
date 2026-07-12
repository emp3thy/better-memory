"""T2 hermetic agentcore scenarios D1-D7 (design section 1D).

Every test runs against the local :class:`FakeAgentCore` endpoint via the
``AWS_ENDPOINT_URL`` seam — no mocks on the wire path: real botocore
serialization, real SigV4 signing (with fake creds), real HTTP.

Covers:

* D1 ``e2e-ac-server-boot-tools-hidden``
* D2 ``e2e-ac-retrieve-polarity-fanout``
* D3 ``e2e-ac-mcp-dispatch-wired``         (inverted from the old
      dispatch-gap KNOWN-DEFECT PIN — observe/semantic/record_use now
      dispatch to AgentCoreBackend over the wire)
* D4 ``e2e-ac-backend-wire-fidelity``      (in-process backend, real boto3)
* D5 ``e2e-ac-session-close-closure-and-env-gate``
* D6 ``e2e-ac-contextual-inject-wire-and-degradation``
* D7 ``e2e-ac-region-single-source``       (inverted from the old
      region-split-brain KNOWN-DEFECT PIN — both planes sign
      agentcore.json's region)
"""

from __future__ import annotations

import hashlib
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

EXPECTED_TOOL_SUBSET = {
    "memory.observe",
    "memory.retrieve",
    "memory.semantic_observe",
    "memory.session_bootstrap",
    "memory.record_use",
}
SYNTHESIZE_TOOLS = {
    "memory.synthesize_next_get_context",
    "memory.synthesize_next_apply",
}
#: Episode + retention tools are sqlite-only concepts: hidden in agentcore
#: mode via ``supports_episodes=False`` (fix plan UD-1); still advertised on
#: the sqlite path (owned by the sqlite journey + tools unit tests).
EPISODE_AND_RETENTION_TOOLS = {
    "memory.start_episode",
    "memory.close_episode",
    "memory.reconcile_episodes",
    "memory.list_episodes",
    "memory.run_retention",
}


def _bm_home(home: Path) -> Path:
    return home / ".better-memory"


def _table_names(db_path: Path) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    return {row[0] for row in rows}


def _tool_text(result: Any) -> str:
    return result.content[0].text


def _polarity_filter_value(body: dict) -> str | None:
    for entry in body.get("metadataFilters", []):
        if entry.get("left", {}).get("metadataKey") == "polarity":
            return entry["right"]["metadataValue"]["stringValue"]
    return None


def _has_active_status_filter(body: dict) -> bool:
    return any(
        entry.get("left", {}).get("metadataKey") == "status"
        and entry.get("operator") == "EQUALS_TO"
        and entry.get("right", {}).get("metadataValue", {}).get("stringValue")
        == "active"
        for entry in body.get("metadataFilters", [])
    )


# ---------------------------------------------------------------------------
# D1 — server boot, tools hidden, sqlite still migrated
# ---------------------------------------------------------------------------


class TestServerBootToolsHidden:
    async def test_boot_hides_synthesize_tools_and_still_migrates_sqlite(
        self, clean_slate_home: Path
    ) -> None:
        """Real server boots from a fabricated agentcore.json against the
        fake endpoint. Boot makes ZERO AWS calls; synthesize tools are
        hidden (supports_synthesis=False); episode + retention tools are
        hidden too (supports_episodes=False, fix plan UD-1); and — docs-
        contradiction pin — both local sqlite DBs are still created +
        migrated in agentcore mode (design section 4 item 12 resolved
        doc-side per UD-4: memory.db carries hook-error logging only)."""
        bm_home = _bm_home(clean_slate_home)
        with FakeAgentCore() as fake:
            write_fake_agentcore_json(bm_home)
            env = agentcore_env(clean_slate_home, fake.port)

            async with mcp_session(env) as session:
                tools = await session.list_tools()

            names = {tool.name for tool in tools.tools}
            assert EXPECTED_TOOL_SUBSET <= names
            assert not (SYNTHESIZE_TOOLS & names)
            # UD-1: backend no-op tools are not advertised in agentcore
            # mode (their sqlite-path presence is owned by the sqlite
            # journey and the tools unit tests).
            assert not (EPISODE_AND_RETENTION_TOOLS & names)
            # Boot never touches AWS (boto3 client construction only).
            assert fake.requests == []

        tables = _table_names(bm_home / "memory.db")
        # Subset, never the exact column/table set (design rule).
        assert {"observations", "episodes", "hook_errors"} <= tables
        assert (bm_home / "knowledge.db").exists()


# ---------------------------------------------------------------------------
# D2 — retrieve polarity fan-out (the flagship wired path)
# ---------------------------------------------------------------------------


class TestRetrievePolarityFanout:
    async def test_retrieve_fans_out_three_filtered_list_records(
        self, clean_slate_home: Path
    ) -> None:
        """``memory.retrieve`` is the ONE data tool wired to
        ``AgentCoreBackend`` (ReflectionToolHandlers → backend.retrieve):
        exactly 3 ListMemoryRecords against the EPISODIC memory's
        reflections namespace, order-insensitive polarity set
        {do,dont,neutral}, each carrying a status=active filter; buckets
        come back as exact empty lists. A single-polarity call restricts
        the fan-out to exactly 1 request."""
        bm_home = _bm_home(clean_slate_home)
        with FakeAgentCore() as fake:
            write_fake_agentcore_json(bm_home)
            fake.set_response("ListMemoryRecords", {"memoryRecordSummaries": []})
            env = agentcore_env(clean_slate_home, fake.port)

            async with mcp_session(env) as session:
                result = await session.call_tool("memory.retrieve", {})
                assert not result.isError
                buckets = json.loads(_tool_text(result))
                assert {"do", "dont", "neutral"} <= set(buckets)
                # Exact empty lists — the clean-slate contract a brand-new
                # agentcore user sees.
                assert buckets["do"] == []
                assert buckets["dont"] == []
                assert buckets["neutral"] == []

                requests = list(fake.requests)
                # Exactly 3 wire requests, all ListMemoryRecords — a
                # collapsed/unfiltered rewrite flips this red.
                assert len(requests) == 3
                assert {r.operation for r in requests} == {"ListMemoryRecords"}
                for request in requests:
                    assert "EPI-FAKE-0001" in request.path
                    assert "reflections" in request.body.get("namespace", "")
                    assert _has_active_status_filter(request.body), request.body
                polarities = {
                    _polarity_filter_value(r.body) for r in requests
                }
                # Order-insensitive: ThreadPoolExecutor fan-out is unordered.
                assert polarities == {"do", "dont", "neutral"}

                # Single-polarity leg: exactly one request, filter 'do'.
                fake.clear()
                result2 = await session.call_tool(
                    "memory.retrieve", {"polarity": "do"}
                )
                assert not result2.isError
                single = fake.requests_for("ListMemoryRecords")
                assert len(single) == 1
                assert _polarity_filter_value(single[0].body) == "do"


# ---------------------------------------------------------------------------
# D3 — MCP dispatch wired to AgentCoreBackend (inverted from the old
#      dispatch-gap KNOWN-DEFECT PIN, fix plan Task 2)
# ---------------------------------------------------------------------------

#: ≥40 chars: real botocore request validation enforces a 40-char minimum
#: on memoryRecordId, so the MCP-level record_use leg must use a genuine-
#: shaped id (short observe-returned EVENT ids are guarded product-side).
_MCP_RECORD_USE_ID = "refl-mcp-dispatch-" + "0" * 30 + "7"


class TestMcpDispatchWired:
    async def test_observe_semantic_record_use_reach_the_wire(
        self, clean_slate_home: Path
    ) -> None:
        """Replaces the old TestMcpDispatchGapPin per its deletion
        contract (design section 4 item 2 FIXED by the dispatch wiring):
        in agentcore mode ``memory.observe``, ``memory.semantic_observe``
        and ``memory.record_use`` now dispatch to AgentCoreBackend over
        MCP — real requests on the wire, ZERO rows in the local sqlite
        file (the exact inversion of the old pin's two halves)."""
        bm_home = _bm_home(clean_slate_home)
        marker_obs = "dispatch-wired-marker-obs"
        marker_sem = "dispatch-wired-marker-semantic"
        expected_req_id = hashlib.sha256(
            marker_sem.encode("utf-8")
        ).hexdigest()[:80]

        with FakeAgentCore() as fake:
            write_fake_agentcore_json(bm_home)
            fake.set_response(
                "CreateEvent", {"event": {"eventId": "fake-evt-mcp-1"}}
            )
            fake.set_response(
                "BatchCreateMemoryRecords",
                {
                    "successfulRecords": [{"memoryRecordId": "fake-rec-mcp-1"}],
                    "failedRecords": [],
                },
            )
            fake.set_response(
                "GetMemoryRecord",
                {
                    "memoryRecord": {
                        "memoryRecordId": _MCP_RECORD_USE_ID,
                        "metadata": {
                            "useful_count": {"numberValue": 1},
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
                    "successfulRecords": [
                        {"memoryRecordId": _MCP_RECORD_USE_ID}
                    ],
                    "failedRecords": [],
                },
            )
            env = agentcore_env(clean_slate_home, fake.port)

            async with mcp_session(env) as session:
                # -- memory.observe → backend.observe → CreateEvent -------
                observe = await session.call_tool(
                    "memory.observe", {"content": marker_obs}
                )
                assert not observe.isError
                # The returned id IS the AgentCore eventId, not a local uuid.
                assert json.loads(_tool_text(observe))["id"] == "fake-evt-mcp-1"

                creates = fake.requests_for("CreateEvent")
                assert len(creates) == 1
                assert len(fake.requests) == 1  # exactly ONE wire request
                create = creates[0]
                assert "EPI-FAKE-0001" in create.path
                assert create.body["sessionId"] == "e2e-session-1"
                conversational = create.body["payload"][0]["conversational"]
                assert conversational["role"] == "USER"
                assert conversational["content"]["text"] == marker_obs

                # -- memory.semantic_observe → BatchCreateMemoryRecords ---
                fake.clear()
                semantic = await session.call_tool(
                    "memory.semantic_observe", {"content": marker_sem}
                )
                assert not semantic.isError

                batch_creates = fake.requests_for("BatchCreateMemoryRecords")
                assert len(batch_creates) == 1
                assert len(fake.requests) == 1
                batch = batch_creates[0]
                assert "SEM-FAKE-0001" in batch.path
                assert "EPI-FAKE-0001" not in batch.text()  # never EPI
                record = batch.body["records"][0]
                assert record["requestIdentifier"] == expected_req_id

                # -- memory.record_use → GetMemoryRecord + BatchUpdate ----
                fake.clear()
                record_use = await session.call_tool(
                    "memory.record_use",
                    {"id": _MCP_RECORD_USE_ID, "outcome": "success"},
                )
                assert not record_use.isError

                gets = fake.requests_for("GetMemoryRecord")
                updates = fake.requests_for("BatchUpdateMemoryRecords")
                assert len(gets) == 1
                assert len(updates) == 1
                assert "EPI-FAKE-0001" in gets[0].path
                assert "EPI-FAKE-0001" in updates[0].path
                metadata = updates[0].body["records"][0]["metadata"]
                # System keys stripped (echoing them back is a real AWS 400).
                assert not any(
                    key.startswith("x-amz-agentcore-memory-")
                    for key in metadata
                ), metadata

        # The inverted half of the old pin: NOTHING landed in local sqlite.
        with closing(
            sqlite3.connect(_bm_home(clean_slate_home) / "memory.db")
        ) as conn:
            (obs_count,) = conn.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()
            (sem_count,) = conn.execute(
                "SELECT COUNT(*) FROM semantic_memories"
            ).fetchone()
        assert obs_count == 0
        assert sem_count == 0


# ---------------------------------------------------------------------------
# D4 — backend wire fidelity (in-process AgentCoreBackend, real boto3)
# ---------------------------------------------------------------------------


@pytest.fixture
def scrubbed_aws_process_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """In-process boto3 clients read THIS process's env for the config /
    credential chains. Explicit kwargs (endpoint_url, creds, region) win
    for everything we assert, but scrub the hazardous vars anyway so a dev
    shell with AWS_PROFILE/SSO material can never influence these tests."""
    for var in (
        "AWS_PROFILE",
        "AWS_DEFAULT_REGION",
        "AWS_REGION",
        "AWS_SESSION_TOKEN",
        "AWS_ENDPOINT_URL",
        "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS",
        # botocore resolves env proxies for loopback targets too — a dev
        # shell's corporate proxy would swallow the fake-endpoint traffic.
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv(
        "AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-such-credentials")
    )
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-such-config"))


#: Real botocore request validation enforces a 40-char minimum on
#: memoryRecordId — short ids like 'refl-1' are rejected client-side
#: before any wire traffic (a wire-fidelity finding in itself).
_REFL_RECORD_ID = "refl-e2e-" + "0" * 30 + "1"
_SEM_RECORD_ID = "sem-rec-e2e-" + "1" * 30


@pytest.mark.usefixtures("scrubbed_aws_process_env")
class TestBackendWireFidelity:
    """Direct AgentCoreBackend with REAL boto3 clients against the fake —
    real botocore serialization, no MagicMocks. Complements the MCP-level
    dispatch tests (TestMcpDispatchWired): since the fix-wave dispatch
    wiring, these paths are reachable both over MCP and from the hooks
    (contextual_inject, session_close); this class owns the exhaustive
    wire-shape assertions at the backend boundary."""

    def _make_backend(
        self,
        fake: FakeAgentCore,
        tmp_path: Path,
        *,
        session_id: str | None = "e2e-wire-session",
        project: str = "e2e-project",
    ) -> Any:
        import boto3
        from botocore.config import Config as BotoConfig

        from better_memory.storage.agentcore import AgentCoreBackend

        cfg = write_fake_agentcore_json(tmp_path / ".better-memory")
        client_kwargs: dict[str, Any] = {
            "endpoint_url": fake.endpoint_url,
            "region_name": cfg.region,
            "aws_access_key_id": "bm-e2e-fake",
            "aws_secret_access_key": "bm-e2e-fake-secret",
            "config": BotoConfig(retries={"mode": "standard", "max_attempts": 5}),
        }
        return AgentCoreBackend(
            config=cfg,
            data_client=boto3.client("bedrock-agentcore", **client_kwargs),
            control_client=boto3.client(
                "bedrock-agentcore-control", **client_kwargs
            ),
            session_id=session_id,
            project=project,
        )

    async def test_observe_create_event_wire_shape(self, tmp_path: Path) -> None:
        """CreateEvent: EPI routing, sessionId/actorId from construction,
        USER role, stringValue-ONLY event metadata, None values dropped."""
        with FakeAgentCore() as fake:
            fake.set_response("CreateEvent", {"event": {"eventId": "fake-evt-1"}})
            backend = self._make_backend(fake, tmp_path)

            event_id = await backend.observe(
                content="wire-marker-observe-7f3a",
                outcome="failure",
                component="e2ecomp",
                theme="gotcha",
            )
            assert event_id == "fake-evt-1"

            requests = fake.requests_for("CreateEvent")
            assert len(requests) == 1
            request = requests[0]
            assert "EPI-FAKE-0001" in request.path
            body = request.body
            assert body["sessionId"] == "e2e-wire-session"
            assert body["actorId"] == "e2e-project"
            conversational = body["payload"][0]["conversational"]
            assert conversational["role"] == "USER"
            assert conversational["content"]["text"] == "wire-marker-observe-7f3a"
            metadata = body["metadata"]
            assert metadata["outcome"] == {"stringValue": "failure"}
            assert metadata["component"] == {"stringValue": "e2ecomp"}
            assert metadata["theme"] == {"stringValue": "gotcha"}
            # None-valued fields never serialize (real AWS rejects nulls).
            assert "scope_path" not in metadata
            assert "trigger_type" not in metadata
            # Event metadata is stringValue-only (numberValue on events is
            # rejected by real AWS).
            for value in metadata.values():
                assert set(value) == {"stringValue"}

    async def test_observe_without_session_id_raises_before_wire(
        self, tmp_path: Path
    ) -> None:
        """observe with no session id, no CLAUDE_SESSION_ID env (the unit
        conftest strips it) and no session marker in the isolated home →
        lazy re-resolution finds nothing → ValueError, ZERO wire requests
        (no uuid4 fallback fabricating identities — folded set-3 gap-2)."""
        with FakeAgentCore() as fake:
            backend = self._make_backend(fake, tmp_path, session_id=None)
            with pytest.raises(ValueError, match="session"):
                await backend.observe(content="never-sent")
            assert fake.requests == []

    def test_semantic_observe_sha256_request_identifier_and_routing(
        self, tmp_path: Path
    ) -> None:
        """BatchCreateMemoryRecords: requestIdentifier is EXACTLY
        sha256(content)[:80] (computed independently here — a genuine
        oracle, not an echo), SEM routing (never EPI), strategy id flows
        from agentcore.json, initial counter metadata present."""
        content = "prefers uv over pip for this repo — semantic-marker-91c2"
        expected_req_id = hashlib.sha256(content.encode("utf-8")).hexdigest()[:80]

        with FakeAgentCore() as fake:
            fake.set_response(
                "BatchCreateMemoryRecords",
                {
                    "successfulRecords": [{"memoryRecordId": "fake-rec-77"}],
                    "failedRecords": [],
                },
            )
            backend = self._make_backend(fake, tmp_path)

            record_id = backend.semantic_observe(content=content)
            assert record_id == "fake-rec-77"

            requests = fake.requests_for("BatchCreateMemoryRecords")
            assert len(requests) == 1
            request = requests[0]
            assert "SEM-FAKE-0001" in request.path
            assert "EPI-FAKE-0001" not in request.text()
            record = request.body["records"][0]
            assert record["requestIdentifier"] == expected_req_id
            namespaces = record["namespaces"]
            assert len(namespaces) == 1
            assert "e2e-project" in namespaces[0]
            assert "semantic" in namespaces[0]
            assert record["memoryStrategyId"] == "STRAT-SEM-1"
            metadata = record["metadata"]
            assert metadata["status"] == {"stringValue": "active"}
            assert metadata["useful_count"] == {"numberValue": 0}

    def test_record_use_strips_system_metadata_full_snapshot(
        self, tmp_path: Path
    ) -> None:
        """record_use sends a FULL metadata snapshot with every
        x-amz-agentcore-memory-* key STRIPPED (echoing them back is a real
        AWS 400 'reserved names or prefixes' — the live smoke already hit
        it once) and the counter incremented from the GET response."""
        with FakeAgentCore() as fake:
            fake.set_response(
                "GetMemoryRecord",
                {
                    "memoryRecord": {
                        "memoryRecordId": _REFL_RECORD_ID,
                        "metadata": {
                            "useful_count": {"numberValue": 2},
                            "status": {"stringValue": "active"},
                            "x-amz-agentcore-memory-createdAt": {
                                "stringValue": "2026-01-01T00:00:00Z"
                            },
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
                    "successfulRecords": [{"memoryRecordId": _REFL_RECORD_ID}],
                    "failedRecords": [],
                },
            )
            backend = self._make_backend(fake, tmp_path)

            backend.record_use(_REFL_RECORD_ID, outcome="success")

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
            assert metadata["useful_count"]["numberValue"] == pytest.approx(3)
            # Full snapshot, not a diff: non-counter keys preserved.
            assert metadata["status"] == {"stringValue": "active"}
            assert "last_credited_at" in metadata  # presence only

    def test_credit_one_semantic_kind_routes_to_sem_and_strips(
        self, tmp_path: Path
    ) -> None:
        """credit_one duplicates the snapshot path record_use uses and is
        what the rating flow actually hits — same strip contract, plus
        kind='semantic' must route lookup AND update to the SEMANTIC
        memory (an episodic lookup would 404 on real AWS)."""
        with FakeAgentCore() as fake:
            fake.set_response(
                "GetMemoryRecord",
                {
                    "memoryRecord": {
                        "memoryRecordId": _SEM_RECORD_ID,
                        "metadata": {
                            "useful_count": {"numberValue": 4},
                            "status": {"stringValue": "active"},
                            "x-amz-agentcore-memory-updatedAt": {
                                "stringValue": "2026-01-02T00:00:00Z"
                            },
                        },
                    }
                },
            )
            fake.set_response(
                "BatchUpdateMemoryRecords",
                {
                    "successfulRecords": [{"memoryRecordId": _SEM_RECORD_ID}],
                    "failedRecords": [],
                },
            )
            backend = self._make_backend(fake, tmp_path)

            result = backend.credit_one(
                session_id="e2e-wire-session",
                kind="semantic",
                id=_SEM_RECORD_ID,
                classification="cited",
            )
            assert result == {"applied": _SEM_RECORD_ID, "skipped": None}

            gets = fake.requests_for("GetMemoryRecord")
            updates = fake.requests_for("BatchUpdateMemoryRecords")
            assert len(gets) == 1
            assert len(updates) == 1
            assert "SEM-FAKE-0001" in gets[0].path
            assert "SEM-FAKE-0001" in updates[0].path

            metadata = updates[0].body["records"][0]["metadata"]
            assert not any(
                key.startswith("x-amz-agentcore-memory-") for key in metadata
            ), metadata
            # cited → useful_count += 1 (4 → 5).
            assert metadata["useful_count"]["numberValue"] == pytest.approx(5)
            assert "last_credited_at" in metadata


# ---------------------------------------------------------------------------
# D5 — session_close closure event + env gate
# ---------------------------------------------------------------------------


class TestSessionCloseClosureAndEnvGate:
    def test_stop_hook_fires_one_closure_event_signed_with_json_region(
        self, clean_slate_home: Path, tmp_path: Path
    ) -> None:
        """Case A: the Stop hook builds a REAL boto3 client (zero coverage
        elsewhere — the hook unit tests MagicMock it) and fires exactly one
        role=OTHER closure CreateEvent, SigV4-signed with agentcore.json's
        region (json says us-east-1, deliberately not the fabrication
        default eu-west-2 — proving cfg.region drives the hook client);
        the spool marker is still written. Env-precedence regression
        guard: BETTER_MEMORY_STORAGE_BACKEND=agentcore is set here."""
        bm_home = _bm_home(clean_slate_home)
        with FakeAgentCore() as fake:
            # Deliberately different from the fabrication default region.
            write_fake_agentcore_json(bm_home, region="us-east-1")
            env = agentcore_env(clean_slate_home, fake.port)

            rc, out, err = run_hook(
                "better_memory.hooks.session_close",
                {
                    "session_id": "payload-session-ignored",
                    "cwd": str(tmp_path / "proj"),
                    "hook_event_name": "Stop",
                },
                env,
            )
            assert rc == 0
            # No memory.db → the rating-directive branch short-circuits →
            # empty stdout is the strong no-block oracle.
            assert out == ""
            assert "Traceback" not in err

            requests = list(fake.requests)
            assert len(requests) == 1
            request = requests[0]
            assert request.operation == "CreateEvent"
            assert "EPI-FAKE-0001" in request.path
            conversational = request.body["payload"][0]["conversational"]
            assert conversational["role"] == "OTHER"
            # env CLAUDE_SESSION_ID wins over the stdin payload session_id.
            assert request.body["sessionId"] == "e2e-session-1"
            # The hook signs with agentcore.json's region (session_close.py
            # builds its client from cfg.region) — the hook half of the
            # convergence asserted in TestRegionSingleSource.
            assert request.sigv4_region == "us-east-1"

        markers = list((bm_home / "spool").glob("*_session_end_*.json"))
        assert len(markers) == 1
        marker_body = json.loads(markers[0].read_text(encoding="utf-8"))
        assert marker_body["event_type"] == "session_end"
        # Closure succeeded → no hook_errors write → no memory.db at all.
        assert not (bm_home / "memory.db").exists()

    def test_stop_hook_without_backend_env_resolves_settings_and_fires_closure(
        self, clean_slate_home: Path, tmp_path: Path
    ) -> None:
        """Case B — INVERTED from the defect-4 KNOWN-DEFECT PIN (design
        section 4 item 5, hook env-propagation gap): the installer still
        writes NO env into hook commands, but the Stop hook now resolves
        the backend from settings.json (written by ``agentcore init``,
        fabricated here via ``write_backend_settings``) when
        BETTER_MEMORY_STORAGE_BACKEND is absent — so a real onboarded
        user's closure event FIRES: exactly one CreateEvent (role=OTHER,
        EPI-FAKE-0001 in path) SigV4-signed with agentcore.json's region.
        The spool marker is still written and no memory.db is created."""
        bm_home = _bm_home(clean_slate_home)
        with FakeAgentCore() as fake:
            # Non-default json region: proves the closure client signs the
            # json's region on the settings-resolved path too.
            write_fake_agentcore_json(bm_home, region="us-east-1")
            write_backend_settings(bm_home)
            env = agentcore_env(
                clean_slate_home, fake.port, BETTER_MEMORY_STORAGE_BACKEND=None
            )

            rc, out, err = run_hook(
                "better_memory.hooks.session_close",
                {
                    "session_id": "e2e-session-1",
                    "cwd": str(tmp_path / "proj"),
                    "hook_event_name": "Stop",
                },
                env,
            )
            assert rc == 0
            assert out == ""
            assert "Traceback" not in err

            requests = list(fake.requests)
            assert len(requests) == 1
            request = requests[0]
            assert request.operation == "CreateEvent"
            assert "EPI-FAKE-0001" in request.path
            conversational = request.body["payload"][0]["conversational"]
            assert conversational["role"] == "OTHER"
            assert request.sigv4_region == "us-east-1"

        markers = list((bm_home / "spool").glob("*_session_end_*.json"))
        assert len(markers) == 1
        # Closure succeeded → no hook_errors write → no memory.db at all.
        assert not (bm_home / "memory.db").exists()

    def test_stop_hook_sqlite_default_ignores_agentcore_json_existence(
        self, clean_slate_home: Path, tmp_path: Path
    ) -> None:
        """Sqlite-safety oracle (fix plan Task 3 sibling): env absent AND
        no settings.json → the hook stays on the sqlite default and makes
        ZERO wire requests even though agentcore.json exists — existence
        is not consent (a once-provisioned-then-reverted sqlite user must
        never be silently switched back). Marker still written."""
        bm_home = _bm_home(clean_slate_home)
        with FakeAgentCore() as fake:
            write_fake_agentcore_json(bm_home)  # provisioned, NOT activated
            env = agentcore_env(
                clean_slate_home, fake.port, BETTER_MEMORY_STORAGE_BACKEND=None
            )

            rc, out, err = run_hook(
                "better_memory.hooks.session_close",
                {
                    "session_id": "e2e-session-1",
                    "cwd": str(tmp_path / "proj"),
                    "hook_event_name": "Stop",
                },
                env,
            )
            assert rc == 0
            assert out == ""
            assert "Traceback" not in err
            assert fake.requests == []

        markers = list((bm_home / "spool").glob("*_session_end_*.json"))
        assert len(markers) == 1


# ---------------------------------------------------------------------------
# D6 — contextual_inject wire + degradation (the only shipped per-prompt
#      backend path)
# ---------------------------------------------------------------------------

_INJECT_PROMPT = "how do I deploy with docker compose"

#: A reflection record whose title clears the min-hits floor (3 distinct
#: whole-word keyword hits from the prompt: deploy/docker/compose >= the
#: default BETTER_MEMORY_CONTEXT_MIN_HITS=2).
_DOCKER_REFLECTION_SUMMARY = {
    "memoryRecordId": "refl-fake-docker",
    "content": {
        "text": json.dumps(
            {
                "title": "docker compose deploy pitfalls",
                "phase": "implementation",
                "use_cases": "deploying docker compose stacks",
                "hints": "- always pass --build",
                "confidence": 0.9,
            }
        )
    },
    "metadata": {
        "useful_count": {"numberValue": 2},
        "status": {"stringValue": "active"},
    },
}


def _inject_payload(tmp_path: Path) -> dict[str, Any]:
    return {
        "hook_event_name": "UserPromptSubmit",
        "prompt": _INJECT_PROMPT,
        "session_id": "e2e-inject-session",
        "cwd": str(tmp_path / "proj"),
    }


class TestContextualInjectWireAndDegradation:
    def test_case_a_happy_path_hits_both_memories_and_injects(
        self, clean_slate_home: Path, tmp_path: Path
    ) -> None:
        """Case A: with valid agentcore config the per-prompt hook reaches
        BOTH fake memories on the wire (EPI reflections fan-out via
        backend.retrieve + SEM via backend.semantic_list) and injects a
        <project-memory> block into the envelope."""
        bm_home = _bm_home(clean_slate_home)
        with FakeAgentCore() as fake:
            write_fake_agentcore_json(bm_home)
            fake.set_response(
                "ListMemoryRecords",
                {"memoryRecordSummaries": [_DOCKER_REFLECTION_SUMMARY]},
            )
            env = agentcore_env(
                clean_slate_home,
                fake.port,
                BETTER_MEMORY_CONTEXT_INJECT_MODE="userprompt",
            )

            rc, out, err = run_hook(
                "better_memory.hooks.contextual_inject",
                _inject_payload(tmp_path),
                env,
            )
            assert rc == 0
            assert "Traceback" not in err
            envelope = json.loads(out)
            hook_output = envelope["hookSpecificOutput"]
            assert hook_output["hookEventName"] == "UserPromptSubmit"
            rendered = hook_output["additionalContext"]
            assert "<project-memory" in rendered
            assert "refl-fake-docker" in rendered

            paths = [r.path for r in fake.requests]
            # backend.retrieve → 3 polarity-filtered EPI list calls;
            # backend.semantic_list → 1 SEM list call.
            assert sum("EPI-FAKE-0001" in p for p in paths) == 3
            assert sum("SEM-FAKE-0001" in p for p in paths) == 1

    def test_case_b_misconfig_clean_slate_silent_noop_with_stray_db(
        self, clean_slate_home: Path, tmp_path: Path
    ) -> None:
        """Case B — misconfig re-expressed post-fix (was the idvar-gate
        defect pin, design section 4 item 6): settings.json activates
        agentcore (env var absent) but agentcore.json was never written —
        the realistic broken state after a hand-rolled activation or a
        half-copied home. Every prompt silently gets the empty envelope,
        zero wire traffic; build_backend's FileNotFoundError is swallowed
        by the hook, record_hook_error's connect() leaves a stray
        schema-less memory.db behind (INSERT no-ops — no table), and the
        SeenStore has already dropped its state/ dir (the failure now
        happens AFTER the seen-store block, unlike the old config-stage
        death)."""
        bm_home = _bm_home(clean_slate_home)
        with FakeAgentCore() as fake:
            write_backend_settings(bm_home)  # activation without provisioning
            env = agentcore_env(
                clean_slate_home,
                fake.port,
                BETTER_MEMORY_CONTEXT_INJECT_MODE="userprompt",
                BETTER_MEMORY_STORAGE_BACKEND=None,
            )

            rc, out, err = run_hook(
                "better_memory.hooks.contextual_inject",
                _inject_payload(tmp_path),
                env,
            )
            assert rc == 0
            assert err == ""
            assert json.loads(out) == {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "",
                }
            }
            assert fake.requests == []

        # Exact artifact set: the pre-written settings.json, the stray
        # schema-less DB, and the seen-store's state/ dir.
        assert {child.name for child in bm_home.iterdir()} == {
            "memory.db",
            "settings.json",
            "state",
        }
        assert _table_names(bm_home / "memory.db") == set()

    def test_case_c_misconfig_premigrated_db_records_one_hook_error_row(
        self, clean_slate_home: Path, tmp_path: Path
    ) -> None:
        """Case C: same misconfig (settings.json activation, no
        agentcore.json) against a pre-migrated memory.db → exactly one
        hook_errors row (hook_name='contextual_inject',
        exception_type='FileNotFoundError', exception_message naming
        agentcore.json and the init remediation — column names per
        migration 0005_phase_c.sql)."""
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations

        bm_home = _bm_home(clean_slate_home)
        conn = connect(bm_home / "memory.db")
        try:
            apply_migrations(conn)
        finally:
            conn.close()

        with FakeAgentCore() as fake:
            write_backend_settings(bm_home)
            env = agentcore_env(
                clean_slate_home,
                fake.port,
                BETTER_MEMORY_CONTEXT_INJECT_MODE="userprompt",
                BETTER_MEMORY_STORAGE_BACKEND=None,
            )
            rc, out, err = run_hook(
                "better_memory.hooks.contextual_inject",
                _inject_payload(tmp_path),
                env,
            )
            assert rc == 0
            assert err == ""
            assert json.loads(out) == {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "",
                }
            }
            assert fake.requests == []

        with closing(sqlite3.connect(bm_home / "memory.db")) as check:
            rows = check.execute(
                "SELECT hook_name, exception_type, exception_message "
                "FROM hook_errors"
            ).fetchall()
        assert len(rows) == 1
        hook_name, exception_type, exception_message = rows[0]
        assert hook_name == "contextual_inject"
        assert exception_type == "FileNotFoundError"
        # The error names the missing file and the accurate remediation.
        assert "agentcore.json" in exception_message
        assert "agentcore init" in exception_message


# ---------------------------------------------------------------------------
# D7 — region single-source (inverted from the old split-brain pin)
# ---------------------------------------------------------------------------


class TestRegionSingleSource:
    async def test_server_and_hook_both_sign_json_region(
        self, clean_slate_home: Path, tmp_path: Path
    ) -> None:
        """INVERTED from the old region-split-brain KNOWN-DEFECT PIN
        (design section 4 item 4): the region env var was deleted and the
        factory now signs with agentcore.json's region, so with json
        region=us-east-1 BOTH planes converge — (a) the MCP server's
        ``memory.retrieve`` signs SigV4 with us-east-1 while consuming the
        json's memory id (MEM-EPI-JSON on the wire — id provenance proof);
        (b) the session_close hook signs us-east-1 too (json-derived, as
        it always did — the convergence target)."""
        bm_home = _bm_home(clean_slate_home)
        with FakeAgentCore() as fake:
            write_fake_agentcore_json(
                bm_home,
                region="us-east-1",
                episodic_memory_id="MEM-EPI-JSON",
                semantic_memory_id="MEM-SEM-JSON",
            )
            fake.set_response("ListMemoryRecords", {"memoryRecordSummaries": []})
            env = agentcore_env(clean_slate_home, fake.port)

            # (a) server plane — memory.retrieve is the WIRED trigger.
            async with mcp_session(env) as session:
                result = await session.call_tool("memory.retrieve", {})
                assert not result.isError

            server_requests = fake.requests_for("ListMemoryRecords")
            assert len(server_requests) == 3
            for request in server_requests:
                # Single source of truth: the json's region, never a
                # product default.
                assert request.sigv4_region == "us-east-1"
                # Runtime IDs come from agentcore.json too — id provenance.
                assert "MEM-EPI-JSON" in request.path

            # (b) hook plane — session_close signs the same json region.
            fake.clear()
            rc, out, _err = run_hook(
                "better_memory.hooks.session_close",
                {
                    "session_id": "e2e-session-1",
                    "cwd": str(tmp_path / "proj"),
                    "hook_event_name": "Stop",
                },
                env,
            )
            assert rc == 0
            assert out == ""
            closures = fake.requests_for("CreateEvent")
            assert len(closures) == 1
            assert closures[0].sigv4_region == "us-east-1"
            assert "MEM-EPI-JSON" in closures[0].path
