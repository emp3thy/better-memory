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

from better_memory._common import (
    default_spool_dir,
    env_session_id,
    get_session_id,
    safe_timestamp,
)
from better_memory.config import get_config

# Mirror the observer cap: reject any stdin payload above 1 MiB without
# raising. Hooks must never fail.
_MAX_STDIN_BYTES = 1_048_576


def _synthesise_marker() -> dict[str, str]:
    """Build a minimal ``session_end`` payload from env + clock."""
    return {
        "event_type": "session_end",
        "timestamp": datetime.now(UTC).isoformat(),
        "cwd": os.environ.get("PWD") or os.getcwd(),
        "session_id": get_session_id(),
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
            # Standalone copy, intentionally not delegated to
            # better_memory.services.exposure_log.list_unrated (see that
            # module for the shared implementation) — kept inline here so
            # this hook has no service-layer import dependency.
            rows = conn.execute(
                """
                SELECT e.memory_kind, e.memory_id,
                       MIN(e.exposed_at) AS exposed_at,
                       MIN(e.source) AS source,
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
        source_counts: dict[str, int] = {}
        for r in rows:
            display = (r["display"] or "")[:TRUNC]
            source = r["source"] or "bootstrap"
            source_counts[source] = source_counts.get(source, 0) + 1
            line = f"- {r['memory_id']} [{source}]: {display}"
            if r["memory_kind"] == "reflection":
                refl_lines.append(line)
            else:
                sem_lines.append(line)
        counts_line = "sources: " + ", ".join(
            f"{k} {v}" for k, v in sorted(source_counts.items())
        )

        directive = (
            "RATE_MEMORIES — before this session ends, classify the "
            "memories that were exposed during this session and that you "
            "did NOT already credit via memory.credit.\n"
            f"({counts_line})\n\n"
            f"Reflections ({len(refl_lines)}):\n"
            + ("\n".join(refl_lines) if refl_lines else "  (none)")
            + f"\n\nSemantic memories ({len(sem_lines)}):\n"
            + ("\n".join(sem_lines) if sem_lines else "  (none)")
            + "\n\n"
            "For each id: FIRST write one line of evidence (what the memory "
            "changed, or a quote) - if you cannot, the class is `ignored`.\n"
            "Classes: cited / shaped / ignored / misled / overlooked.\n"
            "Non-ignored ratings without an evidence line are rejected. "
            "Invoke the skill `rate-session-memories`."
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
            data["session_id"] = get_session_id()
        if "cwd" not in data or not data["cwd"]:
            data["cwd"] = os.environ.get("PWD") or os.getcwd()

        session_id_str = (
            env_session_id() or data.get("session_id") or ""
        )
        # Stop hooks fire again after a previous Stop-hook block returns
        # from the LLM's continuation turn; Claude Code sets
        # ``stop_hook_active=True`` in the payload on that re-fire (see
        # Claude Code hook docs). Without this guard the sweep can loop:
        # the continuation turn's own tool calls (memory.retrieve inserts
        # source='retrieve' exposures; a first PreToolUse inserts
        # source='contextual' rows) create fresh unrated rows, the next
        # Stop finds them and blocks again, ad infinitum. On re-entry we
        # skip the block check and fall through to write the marker so
        # the spooler sees session_end and downstream synthesis runs
        # exactly once. (Bug: #100.)
        if (
            not bool(parsed and isinstance(parsed, dict)
                     and parsed.get("stop_hook_active"))
            and session_id_str
            and _emit_rating_directive_if_unrated(str(session_id_str))
        ):
            # Block was emitted — Claude Code re-fires Stop after the
            # rating turn. Skip the spool-marker write so the consumer
            # sees session_end exactly once (on the final fire) and
            # downstream synthesis runs AFTER ratings land.
            sys.exit(0)

        # Agentcore mode: session-lifecycle emissions are a no-op (user
        # directive) — this hook no longer fires a closure/completion
        # CreateEvent. That marker was often the ONLY event in a thin/
        # empty/system-only session, so AWS extracted a low-value
        # "no actionable content" reflection from it. Real sessions still
        # get extracted on AWS's own idle timer; empty sessions now produce
        # zero events, so there is nothing for AWS to extract.
        spool_dir = default_spool_dir()
        spool_dir.mkdir(parents=True, exist_ok=True)

        ts_component = safe_timestamp(str(data.get("timestamp")))
        # Salt the hash with monotonic-nanosecond clock + PID so two
        # byte-identical payloads in the same second can't collide on
        # filename. The salt does NOT appear in the written body.
        serialised = json.dumps(data, sort_keys=True).encode("utf-8")
        salt = f"{time.time_ns()}:{os.getpid()}".encode()
        hash_hex = hashlib.sha256(serialised + salt).hexdigest()[:12]

        file_name = f"{ts_component}_session_end_{hash_hex}.json"
        # Publish atomically: write to a sibling ``*.json.tmp`` then
        # ``os.replace`` onto the final name. ``SpoolService.drain`` globs
        # ``*.json`` (spool.py:111), so an in-flight tmp file is never
        # picked up mid-write.
        final_path = spool_dir / file_name
        tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp_path, final_path)
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
