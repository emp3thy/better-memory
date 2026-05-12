"""Stop / session-close hook.

Writes a ``session_end`` marker JSON file to the spool directory so the
consolidation service can use session boundaries when clustering observations.

Accepts an optional stdin payload; if stdin is empty or unparseable, a marker
is synthesised from environment variables and the current time. Never raises;
always exits 0.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from better_memory.config import get_config

# Mirror the observer cap: reject any stdin payload above 1 MiB without
# raising. Hooks must never fail.
_MAX_STDIN_BYTES = 1_048_576


def _default_spool_dir() -> Path:
    """Return ``$BETTER_MEMORY_HOME/spool``, defaulting to ``~/.better-memory``.

    Mirrors the observer hook. Kept duplicated to avoid a cross-module import
    that would slow hook startup.
    """
    home = os.environ.get("BETTER_MEMORY_HOME")
    if home:
        return Path(home).expanduser() / "spool"
    return Path.home() / ".better-memory" / "spool"


def _safe_timestamp(raw: str | None) -> str:
    if not raw:
        raw = datetime.now(UTC).isoformat()
    return raw.replace(":", "-")


def _synthesise_marker() -> dict[str, str]:
    """Build a minimal ``session_end`` payload from env + clock."""
    return {
        "event_type": "session_end",
        "timestamp": datetime.now(UTC).isoformat(),
        "cwd": os.environ.get("PWD") or os.getcwd(),
        "session_id": os.environ.get("CLAUDE_SESSION_ID") or uuid4().hex,
    }


def _emit_rating_directive_if_unrated(session_id: str) -> bool:
    """Best-effort: if the current session has any unrated exposures,
    emit a decision:block directive on stdout asking the LLM to rate
    them via the rate-session-memories skill.

    Returns True if a block directive was emitted (caller must SKIP the
    spool-marker write — Claude Code will fire Stop again after the
    rating turn, and the marker should land only on that final fire so
    downstream spool consumers don't see two session_end events or run
    synthesis before ratings land). Returns False otherwise.

    Never raises. On any failure, swallows the exception and returns False.
    """
    try:
        from better_memory.db.connection import connect
        cfg = get_config()
        if not cfg.memory_db.exists():
            return False
        conn = connect(cfg.memory_db)
        try:
            # Dedupe by (memory_kind, memory_id) — a memory can have two
            # exposure rows (bootstrap + retrieve) in one session. The
            # MIN(exposed_at) keeps deterministic ordering; the rating
            # apply path stamps ALL unrated rows per (kind, id) in one
            # UPDATE, so one rating per unique memory is the correct
            # contract to surface to the LLM.
            rows = conn.execute(
                """
                SELECT e.memory_kind, e.memory_id,
                       MIN(e.exposed_at) AS exposed_at,
                       COALESCE(r.title, s.content) AS display
                  FROM session_memory_exposure e
                  LEFT JOIN reflections        r ON e.memory_kind='reflection'
                                                AND e.memory_id = r.id
                  LEFT JOIN semantic_memories  s ON e.memory_kind='semantic'
                                                AND e.memory_id = s.id
                 WHERE e.session_id = ? AND e.rated_at IS NULL
                 GROUP BY e.memory_kind, e.memory_id
                 ORDER BY exposed_at ASC
                """,
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return False

        TRUNC = 80
        CAP_BYTES = 8 * 1024
        refl_lines = []
        sem_lines = []
        for r in rows:
            display = (r["display"] or "")[:TRUNC]
            if r["memory_kind"] == "reflection":
                refl_lines.append(f"- {r['memory_id']}: {display}")
            else:
                sem_lines.append(f"- {r['memory_id']}: {display}")

        directive = (
            "RATE_MEMORIES — before this session ends, classify the "
            "memories that were exposed during this session and that you "
            "did NOT already credit via memory.credit.\n\n"
            f"Reflections ({len(refl_lines)}):\n"
            + ("\n".join(refl_lines) if refl_lines else "  (none)")
            + f"\n\nSemantic memories ({len(sem_lines)}):\n"
            + ("\n".join(sem_lines) if sem_lines else "  (none)")
            + "\n\nFor each id, classify as one of:\n"
            "  cited / shaped / ignored / misled (default: ignored)\n\n"
            "Most exposures default to `ignored` — only flag the few "
            "that actually shaped the session or misled you. Invoke "
            "the skill `rate-session-memories`."
        )
        encoded = directive.encode("utf-8")
        if len(encoded) > CAP_BYTES:
            directive = encoded[: CAP_BYTES - 200].decode("utf-8", errors="ignore") + (
                "\n\n(list truncated; call memory.list_session_exposures "
                "for the full set)"
            )

        payload = {
            "decision": "block",
            "reason": (
                f"RATE_MEMORIES — {len(rows)} pending rating(s) for "
                f"this session"
            ),
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": directive,
            },
        }
        sys.stdout.write(json.dumps(payload))
        sys.stdout.flush()
        return True
    except BaseException as _exc:
        try:
            from better_memory.hooks._error_log import record_hook_error
            record_hook_error(hook_name="session_close_rating", exc=_exc)
        except BaseException:
            pass
        return False


def main() -> None:
    try:
        data: dict[str, object]

        raw_stdin = ""
        try:
            # Read one byte past the cap so we can detect oversize without
            # holding more than MAX+1 bytes in memory.
            raw_stdin = sys.stdin.read(_MAX_STDIN_BYTES + 1)
        except Exception:
            raw_stdin = ""

        if len(raw_stdin) > _MAX_STDIN_BYTES:
            # Oversized — silently drop and exit 0; hooks never fail.
            sys.exit(0)

        parsed: object = None
        if raw_stdin.strip():
            try:
                parsed = json.loads(raw_stdin)
            except Exception:
                parsed = None

        if isinstance(parsed, dict):
            data = dict(parsed)
        else:
            data = dict(_synthesise_marker())

        # Always coerce event_type — this hook only ever emits session_end.
        data["event_type"] = "session_end"
        if "timestamp" not in data or not data["timestamp"]:
            data["timestamp"] = datetime.now(UTC).isoformat()
        if "session_id" not in data or not data["session_id"]:
            data["session_id"] = (
                os.environ.get("CLAUDE_SESSION_ID") or uuid4().hex
            )
        if "cwd" not in data or not data["cwd"]:
            data["cwd"] = os.environ.get("PWD") or os.getcwd()

        session_id_str = (
            os.environ.get("CLAUDE_SESSION_ID")
            or data.get("session_id")
            or ""
        )
        if session_id_str and _emit_rating_directive_if_unrated(
            str(session_id_str)
        ):
            # Block was emitted — Claude Code re-fires Stop after the
            # rating turn. Skip the spool-marker write so the consumer
            # sees session_end exactly once (on the final fire) and
            # downstream synthesis runs AFTER ratings land.
            sys.exit(0)

        spool_dir = _default_spool_dir()
        spool_dir.mkdir(parents=True, exist_ok=True)

        ts_component = _safe_timestamp(str(data.get("timestamp")))
        # Salt the hash with monotonic-nanosecond clock + PID so two
        # byte-identical payloads in the same second can't collide on
        # filename. The salt does NOT appear in the written body.
        serialised = json.dumps(data, sort_keys=True).encode("utf-8")
        salt = f"{time.time_ns()}:{os.getpid()}".encode()
        hash_hex = hashlib.sha256(serialised + salt).hexdigest()[:12]

        file_name = f"{ts_component}_session_end_{hash_hex}.json"
        (spool_dir / file_name).write_text(
            json.dumps(data), encoding="utf-8"
        )
    except Exception as _exc:
        # Hooks must never fail. Best-effort: record to hook_errors
        # for /diagnostics visibility.
        try:
            from better_memory.hooks._error_log import record_hook_error
            record_hook_error(hook_name="session_close", exc=_exc)
        except BaseException:  # noqa: BLE001
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()
