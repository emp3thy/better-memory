"""Pure builders + local ledger for the SQLite -> AgentCore migration.

This module is the *first-ever writer of reflection records* (design §1/§1b):
better-memory otherwise only mutates AWS-extracted reflection metadata. It
constructs ``batch_create_memory_records`` payloads directly from SQLite rows.

Design principle (§1b, validated live against real AWS): the custom metadata
map is silently dropped on client-authored BASE records in the *episodic*
(reflections) namespace, so **all reflection state lives in the JSON content
body**. The *semantic* (userPreference) strategy DOES honour a declared
``memoryRecordSchema``, so semantic state lives in a declared metadata map with
the content staying the user's raw text.

Everything here is a pure function (no boto3, no AWS) except ``push_batch``,
which is the thin batching wrapper, and the ledger helpers, which touch the
SOURCE SQLite db. Builders take the row + the target strategy id so they are
unit-testable without a live config.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from better_memory.storage.session import resolve_actor_id, resolve_namespace

# AWS caps requestIdentifier at 80 chars (mirrors agentcore.py content-hash
# dedup truncation).
_REQUEST_ID_MAX = 80

# System-managed metadata keys are stripped before any client write (mirrors
# agentcore.py::_full_metadata_snapshot); no migrated record emits them.
_AGENTCORE_SYSTEM_METADATA_PREFIX = "x-amz-agentcore-memory-"

# SQLite reflection.status -> AgentCore status. ``superseded`` has no target
# (skipped on create). ``retired`` maps for the status-transition path but is
# also skipped on *create* (§2: retired/superseded excluded from retrieval).
_STATUS_REMAP: dict[str, str] = {
    "pending_review": "active",
    "confirmed": "promoted",
    "retired": "retired",
}

# Statuses that are never *created* as a fresh migrated record (design §2).
_SKIP_ON_CREATE: frozenset[str] = frozenset({"retired", "superseded"})

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS agentcore_migration (
    source_kind      TEXT NOT NULL,
    source_id        TEXT NOT NULL,
    namespace        TEXT NOT NULL,
    target_record_id TEXT,
    content_hash     TEXT NOT NULL,
    status           TEXT NOT NULL,
    last_error       TEXT,
    migrated_at      TEXT,
    PRIMARY KEY (source_kind, source_id)
)
"""


# --------------------------------------------------------------------------- #
# Row access helpers (tolerate sqlite3.Row, dict, or any Mapping)
# --------------------------------------------------------------------------- #
def _get(row: Any, key: str, default: Any = None) -> Any:
    """Column access that works for sqlite3.Row, dict, and Mapping."""
    try:
        val = row[key]
    except (KeyError, IndexError):
        return default
    return default if val is None else val


def _scope(row: Any) -> str:
    scope = _get(row, "scope", "project")
    return "general" if scope == "general" else "project"


def _namespace_for(row: Any, kind: Literal["reflections", "semantic"]) -> str:
    """Resolve the target namespace for a reflection/semantic row.

    ``kind`` is the session-namespace kind: ``'reflections'`` or ``'semantic'``.
    """
    if _scope(row) == "general":
        return resolve_namespace("general", kind)
    actor = resolve_actor_id(_get(row, "project"))
    return resolve_namespace(actor, kind)


def _decode_hints(raw: Any) -> list[Any]:
    """Return hints as a list (SQLite stores a JSON-encoded list, §retrieve)."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        if not raw.strip():
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return [raw]
        return parsed if isinstance(parsed, list) else [parsed]
    return []


def remap_status(sqlite_status: str) -> str | None:
    """Map a SQLite reflection status to its AgentCore equivalent.

    ``superseded`` -> ``None`` (no target; skipped). See §3 status remap.
    """
    return _STATUS_REMAP.get(sqlite_status)


def _max_last_credited_at(row: Any) -> str | None:
    """Collapse the three per-class rating timestamps into one (§3, §3.2).

    Falls back to ``updated_at`` so the declared stringValue is always present.
    """
    candidates = [
        _get(row, "last_useful_at"),
        _get(row, "last_misled_at"),
        _get(row, "last_overlooked_at"),
    ]
    stamps = [str(c) for c in candidates if c]
    if stamps:
        return max(stamps)
    updated = _get(row, "updated_at")
    return str(updated) if updated else None


# --------------------------------------------------------------------------- #
# Record builders (pure)
# --------------------------------------------------------------------------- #
def build_reflection_record(
    row: Any,
    *,
    strategy_id: str,
    timestamp: datetime | None = None,
) -> dict[str, Any] | None:
    """Build the ``batch_create_memory_records`` payload for one reflection row.

    Returns ``None`` for ``retired`` / ``superseded`` rows (skipped on create,
    §2). ALL reflection state lives in the JSON content body (design §1b); NO
    custom metadata map is emitted (AWS would silently drop it on the episodic
    namespace).
    """
    status = _get(row, "status")
    if status in _SKIP_ON_CREATE:
        return None

    sqlite_id = _get(row, "id")
    body: dict[str, Any] = {
        "title": _get(row, "title", ""),
        "use_cases": _get(row, "use_cases", ""),
        "hints": _decode_hints(_get(row, "hints")),
        "confidence": _get(row, "confidence"),
        "tech": _get(row, "tech"),
        "phase": _get(row, "phase"),
        "evidence_count": _get(row, "evidence_count", 0),
        "updated_at": _get(row, "updated_at"),
        "polarity": _get(row, "polarity", "neutral"),
        "status": remap_status(status),
        "useful_count": _get(row, "useful_count", 0),
        "times_misled": _get(row, "times_misled", 0),
        "times_overlooked": _get(row, "times_overlooked", 0),
        "source_row_id": str(sqlite_id),
        "source_backend": "sqlite",
    }

    return {
        "requestIdentifier": f"bm-reflection-{sqlite_id}"[:_REQUEST_ID_MAX],
        "namespaces": [_namespace_for(row, "reflections")],
        # Canonical (sorted) body so the payload — and its content_hash — is
        # deterministic across runs.
        "content": {"text": json.dumps(body, sort_keys=True)},
        "timestamp": timestamp or datetime.now(UTC),
        "memoryStrategyId": strategy_id,
        # NO metadata map — everything is in the body (design §1b).
    }


def build_semantic_record(
    row: Any,
    *,
    strategy_id: str,
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Build the ``batch_create_memory_records`` payload for one semantic row.

    ``content.text`` is the user's raw preference text (NOT JSON). Idempotency
    + counters live in a DECLARED metadata map (userPreference strategy honours
    its ``memoryRecordSchema``). ``source_row_id`` MUST be a declared key or AWS
    drops it (design §1b probe 3).
    """
    sqlite_id = _get(row, "id")

    metadata: dict[str, dict[str, Any]] = {
        "useful_count": {"numberValue": int(_get(row, "useful_count", 0))},
        "times_misled": {"numberValue": int(_get(row, "times_misled", 0))},
        # SQLite ``times_overlooked`` -> declared key ``overlooked_count`` (§3.2).
        "overlooked_count": {"numberValue": int(_get(row, "times_overlooked", 0))},
        "source_row_id": {"stringValue": str(sqlite_id)},
        "status": {"stringValue": "active"},
    }
    # last_credited_at MUST be a stringValue, not dateTimeValue — it is a
    # declared STRING indexed key; a dateTimeValue rejects the whole record
    # (§3, agentcore.py:616-621 landmine).
    credited = _max_last_credited_at(row)
    if credited is not None:
        metadata["last_credited_at"] = {"stringValue": credited}

    return {
        "requestIdentifier": f"bm-semantic-{sqlite_id}"[:_REQUEST_ID_MAX],
        "namespaces": [_namespace_for(row, "semantic")],
        "content": {"text": _get(row, "content", "")},
        "timestamp": timestamp or datetime.now(UTC),
        "memoryStrategyId": strategy_id,
        "metadata": metadata,
    }


# --------------------------------------------------------------------------- #
# Batching (§6.4 — the missing wrapper)
# --------------------------------------------------------------------------- #
def chunk(
    records: Sequence[dict[str, Any]], batch_size: int
) -> Iterator[list[dict[str, Any]]]:
    """Yield ``records`` in lists of at most ``batch_size`` (>=1)."""
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    for start in range(0, len(records), batch_size):
        yield list(records[start : start + batch_size])


def push_batch(
    data_client: Any,
    memory_id: str,
    records: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Write one batch via ``batch_create_memory_records``; return the response.

    Does not raise on ``failedRecords`` — partial-failure handling belongs to
    the caller (design §8: no rollback, per-row ledger).
    """
    return data_client.batch_create_memory_records(
        memoryId=memory_id,
        records=list(records),
    )


# --------------------------------------------------------------------------- #
# Deterministic content hash (§5.2 — sorted keys, timestamp excluded)
# --------------------------------------------------------------------------- #
def canonical_content_hash(record: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical (body+metadata) payload, sorted keys.

    The volatile ``timestamp`` is excluded so re-running the same source row
    yields the *same* hash (the ledger's skip/update decision depends on this).
    ``memoryStrategyId`` is ALSO excluded: it is a routing/target attribute, not
    migrated content, and it is only known after provisioning. Excluding it keeps
    the hash stable across the ``--provision`` path — records are planned with a
    placeholder strategy id, then re-keyed to the real id before the write (see
    ``_rekey_strategy``); if the strategy id fed the hash, the ledger would store
    a hash-of-placeholder that never matches a later run's hash-of-real-id,
    forcing a spurious ``update`` on EVERY subsequent run (§5.2 idempotency).
    ``requestIdentifier``, ``content``, ``metadata`` and ``namespaces`` are all
    deterministic, so the hash changes iff the migrated content changes.
    """
    material = {
        k: v
        for k, v in record.items()
        if k not in ("timestamp", "memoryStrategyId")
    }
    canonical = json.dumps(
        material, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Migration ledger (in the SOURCE sqlite db) — §5.2
# --------------------------------------------------------------------------- #
def ensure_ledger(conn: sqlite3.Connection) -> None:
    """Lazily create the ``agentcore_migration`` ledger table (idempotent)."""
    conn.execute(_LEDGER_DDL)
    conn.commit()


def plan_row(
    conn: sqlite3.Connection,
    kind: str,
    source_id: Any,
    content_hash: str | None,
) -> str:
    """Decide what to do with one source row this run.

    Pass ``content_hash=None`` to signal the source row is no longer eligible
    (retired/superseded in SQLite) — that drives the retire transition.

    Returns one of ``'create' | 'update' | 'skip' | 'retire'``.
    """
    existing = conn.execute(
        "SELECT content_hash, status FROM agentcore_migration "
        "WHERE source_kind = ? AND source_id = ?",
        (kind, str(source_id)),
    ).fetchone()

    if content_hash is None:
        # Source row no longer eligible.
        if existing is None:
            return "skip"  # never migrated -> nothing to converge
        if existing[1] == "retired":
            return "skip"  # already converged
        return "retire"

    if existing is None:
        return "create"
    existing_hash, status = existing[0], existing[1]
    if status in ("pending", "failed"):
        # Never successfully written; (re)create.
        return "create"
    if status == "migrated" and existing_hash == content_hash:
        return "skip"
    return "update"


def record_success(
    conn: sqlite3.Connection,
    *,
    kind: str,
    source_id: Any,
    namespace: str | None = None,
    content_hash: str | None = None,
    target_record_id: str | None = None,
    status: str = "migrated",
) -> None:
    """Upsert a successful (or retired) ledger row.

    Missing ``namespace`` / ``content_hash`` / ``target_record_id`` on an
    existing entry are preserved (used by the retire transition, which only
    flips status).
    """
    now = datetime.now(UTC).isoformat()
    # Update-then-insert (not ON CONFLICT): SQLite validates the proposed
    # INSERT row's NOT NULL constraints *before* routing to the conflict
    # clause, so a retire transition (namespace/content_hash omitted) cannot
    # ride the upsert. Explicit UPDATE lets absent fields COALESCE to the
    # existing value; the INSERT branch runs only for genuinely new rows,
    # where create/update callers always supply both required columns.
    cur = conn.execute(
        """
        UPDATE agentcore_migration SET
            namespace        = COALESCE(?, namespace),
            target_record_id = COALESCE(?, target_record_id),
            content_hash     = COALESCE(?, content_hash),
            status           = ?,
            last_error       = NULL,
            migrated_at      = ?
        WHERE source_kind = ? AND source_id = ?
        """,
        (namespace, target_record_id, content_hash, status, now,
         kind, str(source_id)),
    )
    if cur.rowcount == 0:
        conn.execute(
            """
            INSERT INTO agentcore_migration
                (source_kind, source_id, namespace, target_record_id,
                 content_hash, status, last_error, migrated_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (kind, str(source_id), namespace or "", target_record_id,
             content_hash or "", status, now),
        )
    conn.commit()


def reconcile_ledger(
    conn: sqlite3.Connection,
    *,
    kind: str,
    source_id: Any,
    namespace: str,
    target_record_id: str,
) -> bool:
    """Reattach a remote record's server id to the ledger (§5.3 safety net).

    Client-side reconcile-by-``source_row_id``: when a run scans a target
    namespace and finds a record whose ``source_row_id`` has no
    ``target_record_id`` in the local ledger (lost or absent ledger), this
    binds the found ``target_record_id`` so the subsequent plan yields
    ``update`` instead of ``create`` — preventing the duplicate records that a
    ledger-loss ``create`` would produce (design §4 ``--restart`` contract,
    §5.3). Returns ``True`` if it reattached, ``False`` if the ledger already
    had a target (nothing to do).

    ``content_hash`` is written empty so the next ``plan_row`` re-verifies the
    reattached record via an idempotent ``update`` (the migrated payload's real
    hash cannot match ``''``); we never reconstruct the remote hash. An existing
    target is never overwritten.
    """
    existing = conn.execute(
        "SELECT target_record_id FROM agentcore_migration "
        "WHERE source_kind = ? AND source_id = ?",
        (kind, str(source_id)),
    ).fetchone()
    if existing is not None and existing[0]:
        return False

    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        """
        UPDATE agentcore_migration SET
            namespace        = COALESCE(?, namespace),
            target_record_id = ?,
            status           = 'migrated',
            last_error       = NULL,
            migrated_at      = ?
        WHERE source_kind = ? AND source_id = ?
        """,
        (namespace, target_record_id, now, kind, str(source_id)),
    )
    if cur.rowcount == 0:
        conn.execute(
            """
            INSERT INTO agentcore_migration
                (source_kind, source_id, namespace, target_record_id,
                 content_hash, status, last_error, migrated_at)
            VALUES (?, ?, ?, ?, '', 'migrated', NULL, ?)
            """,
            (kind, str(source_id), namespace or "", target_record_id, now),
        )
    conn.commit()
    return True


def record_failure(
    conn: sqlite3.Connection,
    *,
    kind: str,
    source_id: Any,
    last_error: str,
    namespace: str | None = None,
    content_hash: str | None = None,
) -> None:
    """Upsert a ``failed`` ledger row with ``last_error`` for a later resume."""
    cur = conn.execute(
        """
        UPDATE agentcore_migration SET
            namespace    = COALESCE(?, namespace),
            content_hash = COALESCE(?, content_hash),
            status       = 'failed',
            last_error   = ?
        WHERE source_kind = ? AND source_id = ?
        """,
        (namespace, content_hash, last_error, kind, str(source_id)),
    )
    if cur.rowcount == 0:
        conn.execute(
            """
            INSERT INTO agentcore_migration
                (source_kind, source_id, namespace, target_record_id,
                 content_hash, status, last_error, migrated_at)
            VALUES (?, ?, ?, NULL, ?, 'failed', ?, NULL)
            """,
            (kind, str(source_id), namespace or "",
             content_hash or "", last_error),
        )
    conn.commit()
