"""JSONL audit log for the synthesize drain loop.

The synthesize drain loop is driven by the IDE LLM across many
round-trips (one per pending episode). When that loop appears to
freeze, server-side timing is the only evidence we have — the LLM
side is opaque. Each call writes two JSONL rows to
``{config.home}/logs/synthesize.jsonl``:

  {"phase": "start",    "call_id": "...", "tool": "...", ...}
  {"phase": "complete", "call_id": "...", "tool": "...",
   "latency_ms": N, "result_kind": "...", ...}

Paired by call_id. A start row with no matching complete row points
to the call that hung. Best-effort: any IO error is swallowed so the
audit log can never block or fail the synthesize tool itself.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def append_synth_audit(home: Path, payload: dict[str, Any]) -> None:
    """Append one JSONL row to ``{home}/logs/synthesize.jsonl``."""
    try:
        log_dir = home / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "synthesize.jsonl"
        line = json.dumps(payload, separators=(",", ":"))
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 — best-effort audit
        logger.exception("synth audit write failed")


@contextlib.contextmanager
def audit_synth_call(
    home: Path,
    *,
    tool: str,
    project: str,
    episode_id: str | None,
) -> Iterator[dict[str, Any]]:
    """Bracket a synthesize tool call with start + complete audit rows.

    Yields a mutable ``state`` dict the caller fills in (``result_kind``,
    ``error``, ``counts``, ``obs_count``, ``refl_count``, and may
    overwrite ``episode_id`` once known). The complete row is written
    on both normal exit and exception. Exceptions still propagate.
    """
    call_id = uuid.uuid4().hex[:12]
    t0 = time.perf_counter()
    append_synth_audit(home, {
        "phase": "start",
        "call_id": call_id,
        "tool": tool,
        "ts": datetime.now(UTC).isoformat(),
        "project": project,
        "episode_id": episode_id,
    })
    state: dict[str, Any] = {
        "phase": "complete",
        "call_id": call_id,
        "tool": tool,
        "project": project,
        "episode_id": episode_id,
        "result_kind": None,
    }
    try:
        yield state
    except BaseException as exc:
        if state.get("result_kind") is None:
            state["result_kind"] = "exception"
        state.setdefault("error", f"{type(exc).__name__}: {exc}")
        state["ts"] = datetime.now(UTC).isoformat()
        state["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        append_synth_audit(home, state)
        raise
    state["ts"] = datetime.now(UTC).isoformat()
    state["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    append_synth_audit(home, state)
