"""Best-effort hook error recorder. Writes to hook_errors table.

Hooks MUST NOT fail. This helper is wrapped in a defensive
try/except BaseException so a DB write failure (locked file,
missing migration, etc.) cannot break the hook itself.
"""

from __future__ import annotations

import os
import traceback
import uuid
from datetime import UTC, datetime

from better_memory.config import resolve_home
from better_memory.db.connection import connect


def record_hook_error(*, hook_name: str, exc: BaseException) -> None:
    """Write one row to hook_errors. Swallows ALL exceptions.

    Caller is expected to be inside a hook's broad except block;
    this helper extends that defensive posture by ensuring its own
    DB write cannot escape.
    """
    try:
        home = resolve_home()
        db_path = home / "memory.db"
        conn = connect(db_path)
        try:
            conn.execute(
                "INSERT INTO hook_errors "
                "(id, created_at, hook_name, exception_type, "
                " exception_message, traceback, cwd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    datetime.now(UTC).isoformat(),
                    hook_name,
                    type(exc).__name__,
                    str(exc),
                    traceback.format_exc(),
                    os.getcwd(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except BaseException:  # noqa: BLE001
        pass
