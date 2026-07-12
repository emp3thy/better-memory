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

from better_memory._common import (
    default_spool_dir,
    env_session_id,
    get_session_id,
    resolve_home,
    safe_timestamp,
)
from better_memory.config import get_config, project_name

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


def _build_agentcore_data_client(region: str):
    """Construct the bedrock-agentcore (data plane) boto3 client.

    Defined as a module-level function so tests can patch it without needing
    boto3 installed. boto3 is imported lazily so sqlite-mode hooks never pay
    for the import."""
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError as exc:
        # Same install hint as better_memory/storage/factory.py — keep the
        # two lazy-import surfaces consistent.
        raise ModuleNotFoundError(
            "boto3 is required for the agentcore storage backend. "
            "Install it with: pip install 'better-memory[agentcore]'"
        ) from exc
    return boto3.client(
        "bedrock-agentcore",
        config=BotoConfig(
            region_name=region, retries={"mode": "standard", "max_attempts": 5}
        ),
    )


def _fire_agentcore_closure(*, session_id: str, cwd: str) -> bool:
    """In agentcore mode, fire one CreateEvent(role=OTHER) against the
    current session. Returns True if a closure event was fired, False if
    we short-circuited (sqlite mode, missing config, or any failure).

    NEVER raises. AgentCore-side failure is logged via _error_log and
    the spool-marker write proceeds anyway (idle-detection fallback).

    Reuses Plan 2's `closure_event_payload()` + `resolve_actor_id()` from
    `better_memory/storage/session.py` so there's a single source of truth
    for the payload shape and actor-id resolution — AgentCoreBackend.observe
    uses the same helpers.

    ``cwd`` is the payload's working directory; the project is resolved from
    it via ``config.project_name`` INSIDE this function (after the backend
    gate) so it matches the server's ``resolve_actor_id(project_name())``
    exactly — a plain ``basename(cwd)`` diverges on git worktrees,
    subdirectories, and ``BETTER_MEMORY_PROJECT`` / ``.better-memory``
    overrides, sending the closure event to an actor stream the session's
    events never used. Empty ``cwd`` resolves to the ``"general"`` bucket,
    and sqlite mode never pays the git-walk cost."""
    # Env-var check BEFORE any lazy import or file I/O: an explicit env value
    # keeps today's semantics — sqlite-mode (or any non-agentcore value) pays
    # nothing and skips even the settings.json resolver.
    env_backend = os.environ.get("BETTER_MEMORY_STORAGE_BACKEND")
    if env_backend is not None and env_backend != "agentcore":
        return False

    try:
        if env_backend is None:
            # No env override: resolve via the shared helper (env →
            # $BETTER_MEMORY_HOME/settings.json → "sqlite"). Installed hooks
            # get no env from Claude Code, so the settings file written by
            # `agentcore init` is what activates the closure (defect 4).
            # Import stays lazy so the explicit-env fast path above never
            # touches the resolver. A resolver error (e.g. corrupt
            # settings.json) falls into the except below: record_hook_error
            # + return False — hooks never fail, marker still written.
            from better_memory.config import resolve_storage_backend

            if resolve_storage_backend() != "agentcore":
                return False

        # Lazy imports — sqlite mode short-circuited above and never reaches
        # this block.
        from datetime import UTC, datetime

        from better_memory.storage.agentcore_persistence import (
            load_agentcore_config,
        )
        from better_memory.storage.session import (
            closure_event_payload,
            resolve_actor_id,
        )

        home = resolve_home()
        cfg = load_agentcore_config(home)
        if cfg is None:
            return False

        # Actor-id parity with the server (see docstring): resolve the
        # project through the same helper the server uses. None (empty cwd)
        # falls back to the "general" bucket inside resolve_actor_id.
        project = project_name(Path(cwd)) if cwd.strip() else None

        client = _build_agentcore_data_client(cfg.region)
        client.create_event(
            memoryId=cfg.episodic.memory_id,
            actorId=resolve_actor_id(project),
            sessionId=session_id,
            eventTimestamp=datetime.now(UTC),
            payload=closure_event_payload(),
        )
        return True
    except BaseException as _exc:
        try:
            from better_memory.hooks._error_log import record_hook_error
            record_hook_error(hook_name="session_close_agentcore", exc=_exc)
        except BaseException:
            pass
        return False


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
            + "\n\nFor each id, classify as one of:\n"
            "  cited / shaped / ignored / misled / overlooked "
            "(default: ignored)\n\n"
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
            data["session_id"] = get_session_id()
        if "cwd" not in data or not data["cwd"]:
            data["cwd"] = os.environ.get("PWD") or os.getcwd()

        session_id_str = (
            env_session_id() or data.get("session_id") or ""
        )
        if session_id_str and _emit_rating_directive_if_unrated(
            str(session_id_str)
        ):
            # Block was emitted — Claude Code re-fires Stop after the
            # rating turn. Skip the spool-marker write so the consumer
            # sees session_end exactly once (on the final fire) and
            # downstream synthesis runs AFTER ratings land.
            sys.exit(0)

        # Agentcore mode: fire a closure-marker event so the episodic
        # strategy triggers extraction within minutes rather than waiting
        # ~15-20m for idle detection (spec § "Spike findings" Finding 2).
        # Non-fatal: failure is logged but does not block the spool marker.
        # Project resolution happens INSIDE _fire_agentcore_closure (after
        # the backend gate) via config.project_name, matching the server's
        # actor-id resolution — see the function docstring.
        cwd_for_closure = data.get("cwd")
        _fire_agentcore_closure(
            session_id=str(session_id_str or ""),
            cwd=cwd_for_closure if isinstance(cwd_for_closure, str) else "",
        )

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
