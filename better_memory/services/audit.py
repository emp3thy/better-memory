"""Audit trail helper.

Single entry point for every service mutation: write one immutable row per
state transition. The function is intentionally connection-scoped and does
NOT commit — the caller owns the enclosing transaction so the audit row
lands atomically with the state change it describes.

Deliberate non-audit surfaces
-----------------------------
* Knowledge-base changes (:mod:`better_memory.services.knowledge`) are NOT
  audited here. Knowledge documents originate on the filesystem and are
  edited by humans; the filesystem is the source of truth. Reindex writes
  mirror those human edits rather than representing AI state transitions,
  so they do not earn audit rows.
* Spool drains (:mod:`better_memory.services.spool`) are NOT audited here
  either. Each inserted ``hook_events`` row is itself the audit surface
  for the event it describes — writing an additional ``audit_log`` row
  would duplicate the record without adding information.
* The ``memory.start_ui`` MCP tool is a stub today and performs no state
  transition; nothing to audit until it actually does something.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from uuid import uuid4


def log(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    actor: str = "ai",
    triggered_by: str | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    session_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Insert a row into ``audit_log``.

    Does NOT commit — the caller owns the transaction boundary so the audit
    row lands atomically with the state change it describes. ``detail`` is
    JSON-encoded if provided; ``None`` is stored as SQL ``NULL``. The row
    id is a fresh ``uuid4().hex``. ``action`` is free-form and set by the
    calling service — there is no central registry of valid actions.
    """
    conn.execute(
        """
        INSERT INTO audit_log (
            id, entity_type, entity_id, action,
            from_status, to_status, triggered_by, actor, detail, session_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid4().hex,
            entity_type,
            entity_id,
            action,
            from_status,
            to_status,
            triggered_by,
            actor,
            json.dumps(detail) if detail is not None else None,
            session_id,
        ),
    )
