"""T2 fake-endpoint integration tests for ``agentcore migrate`` (design §9 T2).

Every test drives the REAL migrate CLI (`cli._handle_migrate`) with REAL boto3
clients pointed at the local :class:`FakeAgentCore` — real botocore
serialization, real SigV4 signing (fake creds), real HTTP — and the fake runs
its opt-in STATEFUL record store, so writes actually persist and are read back
through the REAL reader (`AgentCoreBackend.retrieve` / `.semantic_list`). That
closes the end-to-end parity loop the pure-mock T1 CLI tests
(`tests/cli/test_agentcore_migrate.py`) cannot: a migrated row is only "correct"
if the shipped reader reconstructs it.

The fake models the live-proven AgentCore metadata dialect (design §1b, three
real-AWS probes on 2026-07-12, acct 708306701628 eu-west-2; memory ids
c588ca1c…, 9bd09ba6…): a client BASE write on the **episodic reflections**
namespace has its entire custom metadata map silently dropped (only the JSON
content BODY round-trips, and content-BODY updates persist durably), while the
**userPreference semantic** namespace retains keys DECLARED in its
memoryRecordSchema (undeclared keys dropped). See
``tests/e2e/_fake_agentcore.py::gate_client_metadata`` for the enforcement and
the citation. These tests are meaningful precisely because of that gate: the
reflection reader is body-first (metadata would be invisible), and the semantic
idempotency key + counters ride declared metadata.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from better_memory.cli import agentcore as cli
from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from tests.e2e._agentcore_env import write_fake_agentcore_json
from tests.e2e._fake_agentcore import FakeAgentCore, RecordedRequest

_REGION = "eu-west-2"
_EPI_ID = "EPI-FAKE-0001"
_SEM_ID = "SEM-FAKE-0001"
_PROJECT = "better-memory"


# --------------------------------------------------------------------------- #
# Process-env hygiene (in-process boto3 reads THIS process's config chains).
# --------------------------------------------------------------------------- #
@pytest.fixture
def scrubbed_aws_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Explicit endpoint/creds/region kwargs win for everything asserted here,
    but scrub the hazardous vars so a dev shell with AWS_PROFILE/SSO material or
    a corporate proxy can never influence (or swallow) the fake traffic."""
    for var in (
        "AWS_PROFILE",
        "AWS_DEFAULT_REGION",
        "AWS_REGION",
        "AWS_SESSION_TOKEN",
        "AWS_ENDPOINT_URL",
        "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS",
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


# --------------------------------------------------------------------------- #
# Wiring helpers
# --------------------------------------------------------------------------- #
def _real_client(service: str, fake: FakeAgentCore, region: str) -> Any:
    import boto3
    from botocore.config import Config as BotoConfig

    return boto3.client(
        service,
        endpoint_url=fake.endpoint_url,
        region_name=region,
        aws_access_key_id="bm-e2e-fake",
        aws_secret_access_key="bm-e2e-fake-secret",
        config=BotoConfig(retries={"mode": "standard", "max_attempts": 5}),
    )


def _point_clients_at_fake(
    monkeypatch: pytest.MonkeyPatch, fake: FakeAgentCore
) -> None:
    """Route the migrate CLI's data + control clients to the fake — the exact
    seam AWS_ENDPOINT_URL provides for the real subprocess/hook paths."""
    monkeypatch.setattr(
        cli,
        "_build_data_client",
        lambda region: _real_client("bedrock-agentcore", fake, region),
    )
    monkeypatch.setattr(
        cli,
        "_build_control_client",
        lambda region: _real_client("bedrock-agentcore-control", fake, region),
    )


def _install_active_get_memory(fake: FakeAgentCore) -> None:
    """Can a GetMemory control response: both memories ACTIVE; the semantic
    strategy DECLARES source_row_id so `_ensure_semantic_schema` is satisfied
    without an in-place widen (the already-provisioned-correctly path)."""

    def _get_memory(record: RecordedRequest) -> dict[str, Any]:
        is_semantic = "SEM" in record.path
        strategy: dict[str, Any] = {
            "strategyId": "STRAT-SEM-1" if is_semantic else "STRAT-EPI-1",
            "status": "ACTIVE",
            "name": "userPreference" if is_semantic else "episodicReflections",
        }
        if is_semantic:
            strategy["memoryRecordSchema"] = {
                "metadataSchema": [
                    {"key": "useful_count", "type": "NUMBER"},
                    {"key": "times_misled", "type": "NUMBER"},
                    {"key": "overlooked_count", "type": "NUMBER"},
                    {"key": "last_credited_at", "type": "STRING"},
                    {"key": "status", "type": "STRING"},
                    {"key": "source_row_id", "type": "STRING"},
                ]
            }
        memory_id = _SEM_ID if is_semantic else _EPI_ID
        return {
            "memory": {
                "id": memory_id,
                "name": memory_id,
                "status": "ACTIVE",
                "strategies": [strategy],
            }
        }

    fake.set_response("GetMemory", _get_memory)


def _args(home: Path, **over: Any) -> argparse.Namespace:
    d: dict[str, Any] = dict(
        subcommand="migrate",
        home=str(home),
        dry_run=False,
        include="reflections,semantic",
        restart=False,
        provision=False,
        db=None,
        project=None,
        region=None,
        batch_size=25,
        verify=False,
    )
    d.update(over)
    return argparse.Namespace(**d)


def _make_backend(fake: FakeAgentCore, cfg: Any) -> Any:
    from better_memory.storage.agentcore import AgentCoreBackend

    return AgentCoreBackend(
        config=cfg,
        data_client=_real_client("bedrock-agentcore", fake, _REGION),
        control_client=_real_client("bedrock-agentcore-control", fake, _REGION),
        session_id="t2-migrate-readback",
        project=_PROJECT,
    )


# --------------------------------------------------------------------------- #
# DB seeding
# --------------------------------------------------------------------------- #
_REFL_COLS = (
    "id, title, project, tech, phase, polarity, use_cases, hints, confidence, "
    "status, superseded_by, evidence_count, created_at, updated_at, scope, "
    "useful_count, last_useful_at, times_misled, last_misled_at, "
    "times_overlooked, last_overlooked_at"
)


def _insert_reflection(conn: Any, **f: Any) -> None:
    row = {
        "id": f["id"],
        "title": f.get("title", "T"),
        "project": _PROJECT,
        "tech": f.get("tech", "python"),
        "phase": f.get("phase", "implementation"),
        "polarity": f.get("polarity", "neutral"),
        "use_cases": f.get("use_cases", "when x"),
        "hints": json.dumps(f.get("hints", ["h1"])),
        "confidence": f.get("confidence", 0.8),
        "status": f.get("status", "pending_review"),
        "superseded_by": None,
        "evidence_count": f.get("evidence_count", 0),
        "created_at": "2026-07-01T00:00:00+00:00",
        "updated_at": f.get("updated_at", "2026-07-10T00:00:00+00:00"),
        "scope": f.get("scope", "project"),
        "useful_count": f.get("useful_count", 0),
        "last_useful_at": f.get("last_useful_at"),
        "times_misled": f.get("times_misled", 0),
        "last_misled_at": f.get("last_misled_at"),
        "times_overlooked": f.get("times_overlooked", 0),
        "last_overlooked_at": f.get("last_overlooked_at"),
    }
    placeholders = ", ".join("?" for _ in _REFL_COLS.split(", "))
    conn.execute(
        f"INSERT INTO reflections ({_REFL_COLS}) VALUES ({placeholders})",
        tuple(row[c.strip()] for c in _REFL_COLS.split(",")),
    )


def _insert_semantic(conn: Any, **f: Any) -> None:
    conn.execute(
        """
        INSERT INTO semantic_memories
          (id, content, project, scope, created_at, updated_at, useful_count,
           last_useful_at, times_misled, last_misled_at, times_overlooked,
           last_overlooked_at)
        VALUES (?, ?, ?, ?, '2026-07-01T00:00:00+00:00',
                '2026-07-10T00:00:00+00:00', ?, ?, ?, ?, ?, ?)
        """,
        (
            f["id"],
            f["content"],
            _PROJECT,
            f.get("scope", "project"),
            f.get("useful_count", 0),
            f.get("last_useful_at"),
            f.get("times_misled", 0),
            f.get("last_misled_at"),
            f.get("times_overlooked", 0),
            f.get("last_overlooked_at"),
        ),
    )


def _open_db(home: Path) -> Any:
    conn = connect(home / "memory.db")
    apply_migrations(conn)
    return conn


def _ledger_rows(home: Path) -> list[Any]:
    conn = connect(home / "memory.db")
    try:
        return conn.execute(
            "SELECT source_kind, source_id, status, target_record_id "
            "FROM agentcore_migration ORDER BY source_kind, source_id"
        ).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("scrubbed_aws_env")
class TestFullMigrateRoundTrip:
    def test_reflections_and_semantic_read_back_through_the_real_reader(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A full migrate lands reflections with ALL state in the body and
        semantic with DECLARED metadata; both read back through the shipped
        `AgentCoreBackend.retrieve` / `.semantic_list` with correct buckets and
        counters — end-to-end parity through the real reader, not a shape echo.
        Also proves the metadata-drop: the stored reflection carries an EMPTY
        metadata map (episodic drop) yet retrieve() reconstructs every field."""
        home = tmp_path / "home"
        cfg = write_fake_agentcore_json(home)
        conn = _open_db(home)
        # 'do' project reflection (pending_review -> active, admitted in the
        # project namespace) and a 'dont' general reflection (confirmed ->
        # promoted, admitted only in general/reflections/).
        _insert_reflection(
            conn, id="refl-do", title="Do A", polarity="do",
            status="pending_review", scope="project", useful_count=5,
            times_misled=1, times_overlooked=0, evidence_count=4,
            confidence=0.9, hints=["always pass --build"],
            updated_at="2026-07-11T00:00:00+00:00",
        )
        _insert_reflection(
            conn, id="refl-dont", title="Dont B", polarity="dont",
            status="confirmed", scope="general", useful_count=2,
            times_misled=0, evidence_count=3, confidence=0.7,
            updated_at="2026-07-09T00:00:00+00:00",
        )
        _insert_semantic(
            conn, id="sem-0", content="prefers uv over pip", useful_count=3,
            times_misled=0, times_overlooked=1,
            last_useful_at="2026-07-05T00:00:00+00:00",
        )
        conn.commit()
        conn.close()

        with FakeAgentCore(record_store=True) as fake:
            _install_active_get_memory(fake)
            _point_clients_at_fake(monkeypatch, fake)

            assert cli._handle_migrate(_args(home)) == 0

            # -- ledger: every eligible row migrated with a minted target. ----
            rows = _ledger_rows(home)
            assert {(r["source_kind"], r["source_id"]) for r in rows} == {
                ("reflection", "refl-do"),
                ("reflection", "refl-dont"),
                ("semantic", "sem-0"),
            }
            assert all(r["status"] == "migrated" for r in rows)
            assert all(r["target_record_id"] for r in rows)

            # -- episodic drop: reflection records carry an EMPTY metadata map;
            #    ALL state is in the JSON body (design §1b). --------------------
            epi_stored = fake.stored_records(_EPI_ID)
            assert len(epi_stored) == 2
            for rec in epi_stored:
                assert rec["metadata"] == {}
                body = json.loads(rec["content"]["text"])
                assert body["source_backend"] == "sqlite"

            # -- reflection parity through the REAL reader. -------------------
            backend = _make_backend(fake, cfg)
            buckets = backend.retrieve(project=_PROJECT)
            assert set(buckets) == {"do", "dont", "neutral"}
            assert buckets["neutral"] == []

            assert len(buckets["do"]) == 1
            do = buckets["do"][0]
            assert do["title"] == "Do A"
            assert do["useful_count"] == 5
            assert do["times_misled"] == 1
            assert do["evidence_count"] == 4
            assert do["confidence"] == pytest.approx(0.9)
            assert do["tech"] == "python"
            assert do["phase"] == "implementation"
            assert do["hints"] == ["always pass --build"]
            assert do["updated_at"] == "2026-07-11T00:00:00+00:00"
            # Public shape is key-identical to the sqlite reflection dict — no
            # internal ranking/bucketing helpers leak.
            assert not any(k.startswith("_") for k in do)

            assert len(buckets["dont"]) == 1
            assert buckets["dont"][0]["title"] == "Dont B"
            assert buckets["dont"][0]["evidence_count"] == 3

            # -- semantic parity: declared metadata retained + counters read. -
            sem_stored = fake.stored_records(_SEM_ID)
            assert len(sem_stored) == 1
            sem_meta = sem_stored[0]["metadata"]
            assert sem_meta["source_row_id"] == {"stringValue": "sem-0"}
            assert sem_meta["useful_count"] == {"numberValue": 3}
            # userPreference content stays the user's raw text, not JSON.
            assert sem_stored[0]["content"]["text"] == "prefers uv over pip"

            semantics = backend.semantic_list(project=_PROJECT)
            assert len(semantics) == 1
            sm = semantics[0]
            assert sm.content == "prefers uv over pip"
            assert sm.useful_count == 3
            assert sm.times_overlooked == 1
            assert sm.scope == "project"


@pytest.mark.usefixtures("scrubbed_aws_env")
class TestIdempotentRerun:
    def test_second_run_short_circuits_via_ledger_zero_creates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-running an already-converged migrate against the SAME fake +
        ledger issues ZERO BatchCreate/BatchUpdate calls (the ledger
        short-circuit) and the remote record count is stable — no duplicates."""
        home = tmp_path / "home"
        write_fake_agentcore_json(home)
        conn = _open_db(home)
        _insert_reflection(conn, id="refl-1", polarity="do")
        _insert_reflection(conn, id="refl-2", polarity="dont", scope="general",
                           status="confirmed")
        _insert_semantic(conn, id="sem-1", content="fact one")
        conn.commit()
        conn.close()

        with FakeAgentCore(record_store=True) as fake:
            _install_active_get_memory(fake)
            _point_clients_at_fake(monkeypatch, fake)

            assert cli._handle_migrate(_args(home)) == 0
            first_creates = len(fake.requests_for("BatchCreateMemoryRecords"))
            assert first_creates == 2  # one reflection batch, one semantic batch
            epi_count = len(fake.stored_records(_EPI_ID))
            sem_count = len(fake.stored_records(_SEM_ID))
            assert (epi_count, sem_count) == (2, 1)

            # -- second run: nothing to do. ----------------------------------
            fake.clear()
            assert cli._handle_migrate(_args(home)) == 0
            assert fake.requests_for("BatchCreateMemoryRecords") == []
            assert fake.requests_for("BatchUpdateMemoryRecords") == []
            # Remote store unchanged — the idempotency invariant.
            assert len(fake.stored_records(_EPI_ID)) == 2
            assert len(fake.stored_records(_SEM_ID)) == 1

            # Ledger stable: still exactly the 3 migrated rows.
            rows = _ledger_rows(home)
            assert len(rows) == 3
            assert all(r["status"] == "migrated" for r in rows)


@pytest.mark.usefixtures("scrubbed_aws_env")
class TestDryRun:
    def test_dry_run_makes_zero_wire_calls_and_prints_tallies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """--dry-run computes the plan from SQLite + the ledger and makes ZERO
        AWS calls (not even GetMemory / list); tallies are printed; the ledger
        is untouched."""
        home = tmp_path / "home"
        write_fake_agentcore_json(home)
        conn = _open_db(home)
        _insert_reflection(conn, id="refl-1", polarity="do")
        _insert_reflection(conn, id="refl-2", polarity="do")
        _insert_semantic(conn, id="sem-1", content="fact")
        conn.commit()
        conn.close()

        with FakeAgentCore(record_store=True) as fake:
            _install_active_get_memory(fake)
            _point_clients_at_fake(monkeypatch, fake)

            assert cli._handle_migrate(_args(home, dry_run=True)) == 0

            # Not one byte on the wire (dry-run returns before client build).
            assert fake.requests == []

        out = capsys.readouterr().out.lower()
        assert "dry-run" in out
        assert "create=2" in out  # reflections namespace line
        assert "create=1" in out  # semantic namespace line
        # Planning is read-only — no ledger rows written.
        assert _ledger_rows(home) == []


@pytest.mark.usefixtures("scrubbed_aws_env")
class TestPartialFailureResume:
    def test_injected_failed_record_is_marked_and_only_it_retries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An injected failedRecords entry -> ledger marks that row 'failed'
        (rc 2); the converged row stays 'migrated'; a re-run retries ONLY the
        failed row (exactly one create carrying its requestIdentifier)."""
        home = tmp_path / "home"
        write_fake_agentcore_json(home)
        conn = _open_db(home)
        _insert_reflection(conn, id="refl-ok", polarity="do")
        _insert_reflection(conn, id="refl-bad", polarity="dont")
        conn.commit()
        conn.close()

        with FakeAgentCore(record_store=True) as fake:
            _install_active_get_memory(fake)
            _point_clients_at_fake(monkeypatch, fake)

            fake.fail_on_create = {"bm-reflection-refl-bad"}
            rc = cli._handle_migrate(_args(home, include="reflections"))
            assert rc == 2
            assert "failed record" in capsys.readouterr().err

            statuses = {r["source_id"]: r["status"] for r in _ledger_rows(home)}
            assert statuses["refl-ok"] == "migrated"
            assert statuses["refl-bad"] == "failed"
            # Only the converged record actually persisted.
            assert len(fake.stored_records(_EPI_ID)) == 1

            # -- clear the injection and re-run: only refl-bad retries. -------
            fake.fail_on_create = set()
            fake.clear()
            rc = cli._handle_migrate(_args(home, include="reflections"))
            assert rc == 0

            retried_reqs = [
                rec["requestIdentifier"]
                for req in fake.requests_for("BatchCreateMemoryRecords")
                for rec in req.body["records"]
            ]
            assert retried_reqs == ["bm-reflection-refl-bad"]
            assert {r["status"] for r in _ledger_rows(home)} == {"migrated"}
            assert len(fake.stored_records(_EPI_ID)) == 2


@pytest.mark.usefixtures("scrubbed_aws_env")
class TestVerifyReadback:
    def test_verify_reads_back_one_record_per_kind_through_get_memory_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """`--verify` (design §9 T2, sixth bullet) exercises `_verify_sample`:
        after the writes it reads ONE migrated record per kind back through the
        real `get_memory_record` (the fake's read-your-write store,
        `_fake_agentcore.py`), diffs against SQLite, and prints 'readback ok'
        for each kind. Guards the shipped verify path (agentcore.py:1230/1530,
        incl. its ResourceNotFoundException retry loop) against regressions in
        the error-code check, the ledger query, or the get_memory_record
        kwargs — every other T2/T1 case runs verify=False, so this is the sole
        coverage."""
        home = tmp_path / "home"
        write_fake_agentcore_json(home)
        conn = _open_db(home)
        _insert_reflection(conn, id="refl-v", polarity="do")
        _insert_semantic(conn, id="sem-v", content="verify me")
        conn.commit()
        conn.close()

        with FakeAgentCore(record_store=True) as fake:
            _install_active_get_memory(fake)
            _point_clients_at_fake(monkeypatch, fake)

            assert cli._handle_migrate(_args(home, verify=True)) == 0

            # The verify readback hit get_memory_record with the exact target
            # id the ledger recorded for each kind — one GET per kind.
            ledger = {
                r["source_kind"]: r["target_record_id"]
                for r in _ledger_rows(home)
            }
            get_paths = [
                req.path
                for req in fake.requests_for("GetMemoryRecord")
            ]
            assert any(ledger["reflection"] in p for p in get_paths)
            assert any(ledger["semantic"] in p for p in get_paths)

        out = capsys.readouterr().out
        assert f"verify: reflection {ledger['reflection']} readback ok" in out
        assert f"verify: semantic {ledger['semantic']} readback ok" in out


@pytest.mark.usefixtures("scrubbed_aws_env")
class TestInvariantsSurviveRoundTrip:
    def test_last_credited_at_stringValue_and_status_remap_survive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two landmine invariants survive the wire round trip:

        * ``last_credited_at`` is emitted (and stored) as a **stringValue**, not
          a dateTimeValue — the declared STRING indexed key whose dateTimeValue
          form fails the whole record update on real AWS (agentcore.py:616-621).
        * status is **remapped** on write (pending_review->active,
          confirmed->promoted) and drives client-side retrieval admission: the
          promoted general reflection is retrievable, the pending project one is
          retrievable, and a promoted record in the PROJECT namespace would be
          excluded (only active is admitted there)."""
        home = tmp_path / "home"
        cfg = write_fake_agentcore_json(home)
        conn = _open_db(home)
        # confirmed -> promoted, general scope: retrievable via general ns.
        _insert_reflection(
            conn, id="refl-promoted", title="Promoted", polarity="do",
            status="confirmed", scope="general", useful_count=1,
        )
        # pending_review -> active, project scope.
        _insert_reflection(
            conn, id="refl-active", title="Active", polarity="dont",
            status="pending_review", scope="project",
        )
        _insert_semantic(
            conn, id="sem-cred", content="uses ruff",
            last_useful_at="2026-07-05T00:00:00+00:00",
            last_misled_at="2026-07-06T00:00:00+00:00",
        )
        conn.commit()
        conn.close()

        with FakeAgentCore(record_store=True) as fake:
            _install_active_get_memory(fake)
            _point_clients_at_fake(monkeypatch, fake)
            assert cli._handle_migrate(_args(home)) == 0

            # -- status remap persisted in the reflection bodies. -------------
            statuses = {
                json.loads(r["content"]["text"])["source_row_id"]:
                    json.loads(r["content"]["text"])["status"]
                for r in fake.stored_records(_EPI_ID)
            }
            assert statuses == {"refl-promoted": "promoted", "refl-active": "active"}

            # -- last_credited_at is a stringValue (max of the per-class stamps),
            #    NOT dateTimeValue — and survived the declared-metadata gate. --
            sem_meta = fake.stored_records(_SEM_ID)[0]["metadata"]
            credited = sem_meta["last_credited_at"]
            assert set(credited) == {"stringValue"}
            assert credited["stringValue"] == "2026-07-06T00:00:00+00:00"

            # -- remap drives retrieval admission through the real reader. -----
            backend = _make_backend(fake, cfg)
            buckets = backend.retrieve(project=_PROJECT)
            titles = {
                b["title"] for bucket in buckets.values() for b in bucket
            }
            assert titles == {"Promoted", "Active"}
            assert buckets["do"][0]["title"] == "Promoted"   # general/promoted
            assert buckets["dont"][0]["title"] == "Active"   # project/active


@pytest.mark.usefixtures("scrubbed_aws_env")
class TestFakeModelsProvenDialect:
    def test_episodic_metadata_drop_and_body_persist_on_update(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guards the fake extension itself against the live-proven facts
        (design §1b probes 1 & 2), so the parity tests above are meaningful.
        Raw boto3, no product code: a client BASE write to the episodic
        reflections namespace has its custom metadata dropped while the content
        BODY round-trips, and a subsequent content-BODY update persists durably
        (read-your-write GET) — while a userPreference semantic write keeps its
        DECLARED metadata and drops UNDECLARED keys (probe 3)."""
        from datetime import UTC, datetime

        with FakeAgentCore(record_store=True) as fake:
            _point_clients_at_fake(monkeypatch, fake)
            client = _real_client("bedrock-agentcore", fake, _REGION)

            # -- episodic: custom metadata dropped, body round-trips. ---------
            created = client.batch_create_memory_records(
                memoryId=_EPI_ID,
                records=[{
                    "requestIdentifier": "probe-refl-1",
                    "namespaces": ["projects/p/reflections/"],
                    "content": {"text": json.dumps({"title": "orig", "v": 1})},
                    "timestamp": datetime.now(UTC),
                    "metadata": {
                        "polarity": {"stringValue": "do"},
                        "useful_count": {"numberValue": 9},
                    },
                }],
            )
            rid = created["successfulRecords"][0]["memoryRecordId"]

            fetched = client.get_memory_record(
                memoryId=_EPI_ID, memoryRecordId=rid
            )["memoryRecord"]
            assert fetched.get("metadata", {}) == {}          # dropped (probe 1)
            assert json.loads(fetched["content"]["text"]) == {"title": "orig", "v": 1}

            # -- content-BODY update persists durably (probe 2). --------------
            client.batch_update_memory_records(
                memoryId=_EPI_ID,
                records=[{
                    "memoryRecordId": rid,
                    "timestamp": datetime.now(UTC),
                    "content": {"text": json.dumps({"title": "updated", "v": 2})},
                }],
            )
            refetched = client.get_memory_record(
                memoryId=_EPI_ID, memoryRecordId=rid
            )["memoryRecord"]
            assert json.loads(refetched["content"]["text"]) == {
                "title": "updated", "v": 2,
            }

            # -- semantic: DECLARED kept, UNDECLARED dropped (probe 3). --------
            client.batch_create_memory_records(
                memoryId=_SEM_ID,
                records=[{
                    "requestIdentifier": "probe-sem-1",
                    "namespaces": ["projects/p/semantic/"],
                    "content": {"text": "a preference"},
                    "timestamp": datetime.now(UTC),
                    "metadata": {
                        "source_row_id": {"stringValue": "sem-9"},
                        "useful_count": {"numberValue": 4},
                        "not_declared": {"stringValue": "gone"},
                    },
                }],
            )
            sem = client.list_memory_records(
                memoryId=_SEM_ID, namespace="projects/p/semantic/",
                maxResults=100,
            )["memoryRecordSummaries"][0]
            assert set(sem["metadata"]) == {"source_row_id", "useful_count"}
            assert "not_declared" not in sem["metadata"]
