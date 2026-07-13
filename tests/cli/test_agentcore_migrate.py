"""Tests for `better-memory agentcore migrate` (T5).

Hermetic: the AWS control + data planes are mocked. Proves the dry-run makes
zero AWS calls, the config/provisioning gates return the right exit codes, a
full run emits the expected batched payloads and writes the ledger, re-runs are
idempotent, and partial failures are recorded for resume (rc=2).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from better_memory.cli import agentcore as cli
from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.storage.agentcore_persistence import (
    AgentCoreConfig,
    MemoryRecord,
    save_agentcore_config,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _args(home: Path, **over) -> argparse.Namespace:
    d = dict(
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


def _write_config(home: Path) -> None:
    cfg = AgentCoreConfig(
        schema_version=1,
        region="eu-west-2",
        episodic=MemoryRecord(
            memory_id="epi-X",
            memory_arn="arn:epi",
            memory_name="better_memory_episodic",
            strategy_id="epi-strat",
            strategy_name="episodicReflections",
            event_expiry_duration_days=90,
        ),
        semantic=MemoryRecord(
            memory_id="sem-X",
            memory_arn="arn:sem",
            memory_name="better_memory_semantic",
            strategy_id="sem-strat",
            strategy_name="userPreference",
            event_expiry_duration_days=365,
        ),
    )
    save_agentcore_config(cfg, home)


def _seed_db(home: Path, *, reflections=1, semantic=1) -> Path:
    db_path = home / "memory.db"
    conn = connect(db_path)
    try:
        apply_migrations(conn)
        for i in range(reflections):
            conn.execute(
                """
                INSERT INTO reflections
                  (id, title, project, tech, phase, polarity, use_cases,
                   hints, confidence, status, superseded_by, evidence_count,
                   created_at, updated_at, scope, useful_count, last_useful_at,
                   times_misled, last_misled_at, times_overlooked,
                   last_overlooked_at)
                VALUES (?, ?, ?, ?, 'implementation', 'do', 'when x', ?, 0.8,
                        'pending_review', NULL, 3,
                        '2026-07-01T00:00:00+00:00', '2026-07-10T00:00:00+00:00',
                        'project', 2, '2026-07-09T00:00:00+00:00',
                        1, '2026-07-08T00:00:00+00:00', 0, NULL)
                """,
                (f"refl-{i}", f"Title {i}", "better-memory", "python",
                 json.dumps([f"hint {i}"])),
            )
        for i in range(semantic):
            conn.execute(
                """
                INSERT INTO semantic_memories
                  (id, content, project, scope, created_at, updated_at,
                   useful_count, last_useful_at, times_misled, last_misled_at,
                   times_overlooked, last_overlooked_at)
                VALUES (?, ?, 'better-memory', 'project',
                        '2026-07-01T00:00:00+00:00', '2026-07-10T00:00:00+00:00',
                        3, '2026-07-05T00:00:00+00:00', 0, NULL, 1,
                        '2026-07-06T00:00:00+00:00')
                """,
                (f"sem-{i}", f"Preference {i}"),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _active_memory(memory_id: str, name: str, *, schema_keys=None) -> dict:
    strat: dict = {"strategyId": "strat", "status": "ACTIVE", "name": name}
    if schema_keys is not None:
        strat["configuration"] = {
            "userPreferenceOverride": {
                "memoryRecordSchema": {
                    "metadataSchema": [
                        {"key": k, "type": "STRING"} for k in schema_keys
                    ]
                }
            }
        }
    return {
        "id": memory_id,
        "name": name,
        "status": "ACTIVE",
        "strategies": [strat],
        "eventExpiryDuration": 90,
        "arn": f"arn:{memory_id}",
    }


class _Control:
    """Control-plane mock: both memories ACTIVE; semantic declares source_row_id."""

    def __init__(self, *, episodic_status="ACTIVE", semantic_schema=True,
                 widen_effective=True):
        self._widen_effective = widen_effective
        sem_keys = (
            ["useful_count", "source_row_id", "status"]
            if semantic_schema
            else ["useful_count", "status"]
        )
        self._mems = {
            "epi-X": {
                **_active_memory("epi-X", "better_memory_episodic"),
                "status": episodic_status,
                "strategies": [
                    {"strategyId": "epi-strat", "status": episodic_status,
                     "name": "episodicReflections"}
                ],
            },
            "sem-X": _active_memory(
                "sem-X", "better_memory_semantic", schema_keys=sem_keys
            ),
        }
        self.update_strategy_calls: list = []

    def get_memory(self, *, memoryId):
        return {"memory": self._mems[memoryId]}

    def update_memory_strategy(self, *, memoryId, memoryStrategyId, configuration, **kw):
        self.update_strategy_calls.append(
            {"memoryId": memoryId, "memoryStrategyId": memoryStrategyId,
             "configuration": configuration, **kw}
        )
        # Model a successful in-place widen: adopt the metadataSchema keys the
        # caller declared so the post-update re-read confirms source_row_id.
        # widen_effective=False models AWS accepting the call as a no-op (the
        # key is NOT actually added) — the confirm re-read must then fail.
        override = configuration.get("userPreferenceOverride", {})
        schema = (override.get("memoryRecordSchema") or {}).get("metadataSchema")
        if schema is not None and self._widen_effective:
            self._mems[memoryId]["strategies"][0]["configuration"] = {
                "userPreferenceOverride": {
                    "memoryRecordSchema": {"metadataSchema": schema}
                }
            }
        return {}


class _FakeData:
    def __init__(self, fail_reqs=None, remote_records=None):
        self.create_calls: list = []
        self.update_calls: list = []
        self.list_calls: list = []
        self._fail = set(fail_reqs or [])
        # remote_records: {namespace: [record_summary, ...]} for reconcile scans.
        self._remote = remote_records or {}
        self._n = 0

    def list_memory_records(self, *, memoryId, namespace, maxResults=100,
                            nextToken=None):
        self.list_calls.append((memoryId, namespace))
        return {
            "memoryRecordSummaries": list(self._remote.get(namespace, [])),
            "nextToken": None,
        }

    def batch_create_memory_records(self, *, memoryId, records):
        self.create_calls.append((memoryId, records))
        successful, failed = [], []
        for r in records:
            req = r["requestIdentifier"]
            if req in self._fail:
                failed.append({"requestIdentifier": req, "errorMessage": "boom"})
            else:
                self._n += 1
                successful.append(
                    {"requestIdentifier": req, "memoryRecordId": f"mem-{self._n}"}
                )
        return {"successfulRecords": successful, "failedRecords": failed}

    def batch_update_memory_records(self, *, memoryId, records):
        self.update_calls.append((memoryId, records))
        return {
            "successfulRecords": [
                {"memoryRecordId": r["memoryRecordId"]} for r in records
            ],
            "failedRecords": [],
        }

    def get_memory_record(self, *, memoryId, memoryRecordId):
        return {
            "memoryRecord": {
                "memoryRecordId": memoryRecordId,
                "content": {"text": "{}"},
            }
        }


def _ledger_rows(db_path: Path):
    conn = connect(db_path)
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
def test_dry_run_makes_zero_aws_calls_with_tallies(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path)
    _seed_db(tmp_path, reflections=2, semantic=1)

    def _boom(region):
        raise AssertionError("dry-run must not build an AWS client")

    monkeypatch.setattr(cli, "_build_control_client", _boom)
    monkeypatch.setattr(cli, "_build_data_client", _boom)

    rc = cli._handle_migrate(_args(tmp_path, dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out.lower()
    # 2 reflection creates + 1 semantic create, all fresh.
    assert "create=2" in out  # reflections namespace line
    assert "create=1" in out  # semantic namespace line
    # No ledger writes for a dry-run either (planning is read-only).
    assert _ledger_rows(tmp_path / "memory.db") == []


def test_missing_config_without_provision_rc1(tmp_path, capsys):
    # No agentcore.json and no --provision -> rc 1.
    rc = cli._handle_migrate(_args(tmp_path))
    assert rc == 1
    assert "agentcore.json" in capsys.readouterr().err


def test_missing_memory_without_provision_rc1(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path)
    _seed_db(tmp_path)
    monkeypatch.setattr(
        cli, "_build_control_client",
        lambda region: _Control(episodic_status="CREATING"),
    )
    monkeypatch.setattr(cli, "_build_data_client", lambda region: _FakeData())

    rc = cli._handle_migrate(_args(tmp_path))
    assert rc == 1
    assert "not ACTIVE" in capsys.readouterr().err


def test_full_run_creates_batched_payloads_and_writes_ledger(
    tmp_path, monkeypatch
):
    _write_config(tmp_path)
    db_path = _seed_db(tmp_path, reflections=2, semantic=1)
    control = _Control()
    data = _FakeData()
    monkeypatch.setattr(cli, "_build_control_client", lambda region: control)
    monkeypatch.setattr(cli, "_build_data_client", lambda region: data)

    rc = cli._handle_migrate(_args(tmp_path))
    assert rc == 0

    # Two batch_create calls: one against the episodic memory (reflections),
    # one against the semantic memory.
    called_mems = {mem for (mem, _recs) in data.create_calls}
    assert called_mems == {"epi-X", "sem-X"}

    # Reflection payload: body carries the migration marker + namespace.
    epi_records = next(recs for (mem, recs) in data.create_calls if mem == "epi-X")
    assert len(epi_records) == 2
    body = json.loads(epi_records[0]["content"]["text"])
    assert body["source_backend"] == "sqlite"
    assert epi_records[0]["namespaces"] == ["projects/better-memory/reflections/"]
    assert epi_records[0]["requestIdentifier"].startswith("bm-reflection-")

    # Semantic payload: declared metadata carries source_row_id.
    sem_records = next(recs for (mem, recs) in data.create_calls if mem == "sem-X")
    assert sem_records[0]["metadata"]["source_row_id"] == {"stringValue": "sem-0"}

    # No in-place schema widening was needed (schema already declares the key).
    assert control.update_strategy_calls == []

    # Ledger: every eligible row migrated with a minted target id.
    rows = _ledger_rows(db_path)
    assert len(rows) == 3
    assert all(r["status"] == "migrated" for r in rows)
    assert all(r["target_record_id"] for r in rows)


def test_rerun_is_idempotent_zero_creates(tmp_path, monkeypatch):
    _write_config(tmp_path)
    _seed_db(tmp_path, reflections=2, semantic=1)
    control = _Control()
    monkeypatch.setattr(cli, "_build_control_client", lambda region: control)

    first = _FakeData()
    monkeypatch.setattr(cli, "_build_data_client", lambda region: first)
    assert cli._handle_migrate(_args(tmp_path)) == 0
    assert len(first.create_calls) == 2  # populated the ledger

    # Second run: ledger short-circuits every row -> zero creates.
    second = _FakeData()
    monkeypatch.setattr(cli, "_build_data_client", lambda region: second)
    rc = cli._handle_migrate(_args(tmp_path))
    assert rc == 0
    assert second.create_calls == []
    assert second.update_calls == []


def test_partial_failure_records_failed_and_rc2(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path)
    db_path = _seed_db(tmp_path, reflections=2, semantic=0)
    control = _Control()
    monkeypatch.setattr(cli, "_build_control_client", lambda region: control)

    # Fail exactly one reflection create.
    data = _FakeData(fail_reqs={"bm-reflection-refl-1"})
    monkeypatch.setattr(cli, "_build_data_client", lambda region: data)

    rc = cli._handle_migrate(_args(tmp_path, include="reflections"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "failed record" in err

    rows = {r["source_id"]: r["status"] for r in _ledger_rows(db_path)}
    assert rows["refl-0"] == "migrated"
    assert rows["refl-1"] == "failed"

    # Re-run retries ONLY the failed row.
    retry = _FakeData()
    monkeypatch.setattr(cli, "_build_data_client", lambda region: retry)
    rc = cli._handle_migrate(_args(tmp_path, include="reflections"))
    assert rc == 0
    retried_reqs = [
        r["requestIdentifier"]
        for (_mem, recs) in retry.create_calls
        for r in recs
    ]
    assert retried_reqs == ["bm-reflection-refl-1"]


# --------------------------------------------------------------------------- #
# Issue 1 — _ensure_semantic_schema widens the schema for real and confirms it
# --------------------------------------------------------------------------- #
def test_narrow_semantic_schema_widened_in_place_then_migrates(
    tmp_path, monkeypatch
):
    _write_config(tmp_path)
    db_path = _seed_db(tmp_path, reflections=0, semantic=1)
    # Semantic strategy does NOT declare source_row_id -> must be widened.
    control = _Control(semantic_schema=False)
    data = _FakeData()
    monkeypatch.setattr(cli, "_build_control_client", lambda region: control)
    monkeypatch.setattr(cli, "_build_data_client", lambda region: data)

    rc = cli._handle_migrate(_args(tmp_path, include="semantic"))
    assert rc == 0

    # update_memory_strategy was called with the FULL schema declaring
    # source_row_id (not an empty customExtractionConfiguration).
    assert len(control.update_strategy_calls) == 1
    sent = control.update_strategy_calls[0]["configuration"]
    declared = {
        e["key"]
        for e in sent["userPreferenceOverride"]["memoryRecordSchema"]["metadataSchema"]
    }
    assert "source_row_id" in declared

    # The semantic row migrated (schema now declares the key).
    rows = _ledger_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "migrated"


def test_narrow_semantic_schema_widen_noop_aborts_rc1(tmp_path, monkeypatch):
    _write_config(tmp_path)
    _seed_db(tmp_path, reflections=0, semantic=1)
    # AWS accepts the widen call but does NOT actually add the key (no-op).
    control = _Control(semantic_schema=False, widen_effective=False)
    data = _FakeData()
    monkeypatch.setattr(cli, "_build_control_client", lambda region: control)
    monkeypatch.setattr(cli, "_build_data_client", lambda region: data)

    rc = cli._handle_migrate(_args(tmp_path, include="semantic"))
    # Confirm re-read still lacks source_row_id -> hard fail, no writes.
    assert rc == 1
    assert data.create_calls == []


def test_non_introspectable_semantic_schema_aborts_rc1(tmp_path, monkeypatch):
    _write_config(tmp_path)
    _seed_db(tmp_path, reflections=0, semantic=1)

    # A control whose semantic memory exposes NO metadataSchema at all
    # (introspection returns None) AND whose widen is a no-op -> cannot verify.
    class _NoSchemaControl(_Control):
        def __init__(self):
            super().__init__(widen_effective=False)
            # Strip the schema so _collect_declared_metadata_keys returns None.
            self._mems["sem-X"]["strategies"][0].pop("configuration", None)

    control = _NoSchemaControl()
    data = _FakeData()
    monkeypatch.setattr(cli, "_build_control_client", lambda region: control)
    monkeypatch.setattr(cli, "_build_data_client", lambda region: data)

    rc = cli._handle_migrate(_args(tmp_path, include="semantic"))
    assert rc == 1
    assert data.create_calls == []


# --------------------------------------------------------------------------- #
# Issue 2 — client-side reconcile-by-source_row_id (ledger-loss safety net)
# --------------------------------------------------------------------------- #
def test_lost_ledger_reconciles_and_updates_not_duplicates(tmp_path, monkeypatch):
    _write_config(tmp_path)
    db_path = _seed_db(tmp_path, reflections=1, semantic=0)
    control = _Control()

    # The ledger is empty (lost) but the record ALREADY exists remotely with a
    # matching source_row_id. Reconcile must reattach it so the run UPDATES the
    # existing record rather than CREATING a duplicate.
    remote_body = json.dumps(
        {"source_backend": "sqlite", "source_row_id": "refl-0",
         "title": "stale", "status": "active"}
    )
    data = _FakeData(remote_records={
        "projects/better-memory/reflections/": [
            {"memoryRecordId": "mem-remote-1", "content": {"text": remote_body}}
        ]
    })
    monkeypatch.setattr(cli, "_build_control_client", lambda region: control)
    monkeypatch.setattr(cli, "_build_data_client", lambda region: data)

    rc = cli._handle_migrate(_args(tmp_path, include="reflections"))
    assert rc == 0

    # No duplicate create; the reattached remote record was updated instead.
    assert data.create_calls == []
    assert len(data.update_calls) == 1
    mem_id, recs = data.update_calls[0]
    assert mem_id == "epi-X"
    assert recs[0]["memoryRecordId"] == "mem-remote-1"

    # Ledger points at the reconciled remote id.
    rows = _ledger_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["target_record_id"] == "mem-remote-1"
    assert rows[0]["status"] == "migrated"


# --------------------------------------------------------------------------- #
# Issue 3 — --provision mints a replacement; cfg + writes target the NEW memory
# --------------------------------------------------------------------------- #
class _ReplaceControl:
    """cfg's episodic memory is gone (get_memory raises); --provision recreates
    it. Semantic is healthy and declares source_row_id."""

    def __init__(self):
        self._mems = {
            "sem-X": _active_memory(
                "sem-X", "better_memory_semantic",
                schema_keys=["useful_count", "source_row_id", "status"],
            ),
        }
        self.created: list = []

    def get_memory(self, *, memoryId):
        if memoryId not in self._mems:
            raise RuntimeError(f"ResourceNotFoundException: {memoryId}")
        return {"memory": self._mems[memoryId]}

    def create_memory(self, *, name, **kw):
        new_id = "epi-NEW"
        self.created.append(new_id)
        mem = _active_memory(new_id, name)
        mem["strategies"][0]["strategyId"] = "epi-new-strat"
        self._mems[new_id] = mem
        return {
            "memory": {
                "id": new_id, "arn": f"arn:{new_id}", "status": "CREATING",
                "strategies": [
                    {"strategyId": "epi-new-strat", "status": "CREATING",
                     "name": name}
                ],
            }
        }


def test_provision_replacement_retargets_writes_and_persists_cfg(
    tmp_path, monkeypatch
):
    _write_config(tmp_path)  # episodic memory_id = "epi-X"
    db_path = _seed_db(tmp_path, reflections=2, semantic=0)
    control = _ReplaceControl()
    data = _FakeData()
    monkeypatch.setattr(cli, "_build_control_client", lambda region: control)
    monkeypatch.setattr(cli, "_build_data_client", lambda region: data)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    rc = cli._handle_migrate(
        _args(tmp_path, include="reflections", provision=True)
    )
    assert rc == 0
    assert control.created == ["epi-NEW"]

    # Every batch targeted the NEW memory, never the dead "epi-X".
    called_mems = {mem for (mem, _recs) in data.create_calls}
    assert called_mems == {"epi-NEW"}

    # cfg on disk was re-saved with the replacement memory + strategy id.
    saved = json.loads((tmp_path / "agentcore.json").read_text())
    assert saved["episodic"]["memory_id"] == "epi-NEW"
    assert saved["episodic"]["strategy_id"] == "epi-new-strat"

    rows = _ledger_rows(db_path)
    assert len(rows) == 2
    assert all(r["status"] == "migrated" for r in rows)


# --------------------------------------------------------------------------- #
# Issue 4 — provision-from-no-config: strategy re-key must not force re-updates
# --------------------------------------------------------------------------- #
class _FreshProvisionControl:
    """No prior config: --provision mints BOTH memories fresh."""

    def __init__(self):
        self._mems: dict = {}
        self._n = 0

    def get_memory(self, *, memoryId):
        return {"memory": self._mems[memoryId]}

    def create_memory(self, *, name, **kw):
        self._n += 1
        new_id = f"{name}-{self._n}"
        schema_keys = (
            ["useful_count", "source_row_id", "status"]
            if name == "better_memory_semantic"
            else None
        )
        self._mems[new_id] = _active_memory(new_id, name, schema_keys=schema_keys)
        return {
            "memory": {
                "id": new_id, "arn": f"arn:{new_id}", "status": "CREATING",
                "strategies": [
                    {"strategyId": f"{new_id}-strat", "status": "CREATING",
                     "name": name}
                ],
            }
        }


def test_provision_no_config_is_idempotent_second_run_no_updates(
    tmp_path, monkeypatch
):
    # No agentcore.json: first run provisions + migrates; second run (cfg now
    # present with the REAL strategy ids) must re-key to a hash that still
    # matches the ledger -> zero updates, zero creates.
    _seed_db(tmp_path, reflections=2, semantic=1)
    control = _FreshProvisionControl()
    monkeypatch.setattr(cli, "_build_control_client", lambda region: control)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    first = _FakeData()
    monkeypatch.setattr(cli, "_build_data_client", lambda region: first)
    rc = cli._handle_migrate(_args(tmp_path, provision=True))
    assert rc == 0
    assert len(first.create_calls) == 2  # reflections + semantic batches
    assert (tmp_path / "agentcore.json").exists()

    # Second run: cfg loaded from disk with the real strategy ids.
    second = _FakeData()
    monkeypatch.setattr(cli, "_build_data_client", lambda region: second)
    rc = cli._handle_migrate(_args(tmp_path, provision=True))
    assert rc == 0
    # The hash excludes memoryStrategyId, so the re-keyed records converge:
    # no spurious update on the second run.
    assert second.create_calls == []
    assert second.update_calls == []
