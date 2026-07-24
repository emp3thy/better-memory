"""UserPromptSubmit / PreToolUse hook: inject curated memories relevant to the
current prompt or tool-input. Gated by BETTER_MEMORY_CONTEXT_INJECT_MODE
(userprompt | pretool | both | off). Never raises; always exits 0.

Candidates are scored via retrieve_relevant's three-leg evidence gate (BM25 /
vector cosine / keyword-hit fallback — see services/relevant.py) and capped
at cfg.context_max_items. A per-session SeenStore dedups injected memories
across turns within a run (cfg.context_reinject_turns controls re-injection
after N turns). Survivors are recorded as 'contextual' exposures (best-effort;
a write failure never blocks injection) and counted in rating_diagnostics for
observability (contextual_fired_userprompt/pretool, contextual_injected,
contextual_suppressed_floor, contextual_suppressed_dedup).

PreToolUse is latched to one real firing per session (SeenStore.pretool_fired
/ mark_pretool_fired): the installed matcher is unscoped (all tools), so
without the latch every tool call would re-run the full retrieval path.
Later PreToolUse events in the same session short-circuit on the state file
before any DB/embedder work. UserPromptSubmit is unaffected by the latch.
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import closing, nullcontext
from datetime import UTC, datetime
from pathlib import Path

from better_memory.config import get_config, project_name
from better_memory.db.connection import connect
from better_memory.embeddings.ollama import OllamaEmbedder
from better_memory.embeddings.sync_embed import SyncEmbedder
from better_memory.hooks._error_log import record_hook_error
from better_memory.services.context_seen import SeenStore, prune_stale
from better_memory.services.relevant import format_relevant, retrieve_relevant
from better_memory.storage import build_backend

_MAX_STDIN_BYTES = 1_000_000


class _SkipInjection(Exception):
    """Module-local sentinel: PreToolUse latch already fired this session.

    Caught explicitly (never via the outer BaseException guard) to leave
    ``rendered = ""`` without treating the skip as an error.
    """


def _bump_diagnostic(conn, cfg, metric: str) -> None:
    """Best-effort observability counter. Sqlite mode only; never raises."""
    if cfg.storage_backend != "sqlite" or conn is None:
        return
    try:
        conn.execute(
            "UPDATE rating_diagnostics SET value = value + 1, updated_at = ? "
            "WHERE metric = ?",
            (datetime.now(UTC).isoformat(), metric),
        )
        conn.commit()
    except BaseException:  # noqa: BLE001
        pass


def _enabled(event: str, mode: str) -> bool:
    if mode == "off":
        return False
    if event == "UserPromptSubmit":
        return mode in ("userprompt", "both")
    if event == "PreToolUse":
        return mode in ("pretool", "both")
    return False


def _query_from(payload: dict, event: str) -> str:
    if event == "UserPromptSubmit":
        return str(payload.get("prompt") or "")
    if event == "PreToolUse":
        tool = payload.get("tool_name") or ""
        return f"{tool} {json.dumps(payload.get('tool_input') or {})}"
    return ""


def main() -> None:
    raw = ""
    try:
        raw = sys.stdin.read(_MAX_STDIN_BYTES + 1)
    except BaseException:  # noqa: BLE001 — hooks never fail
        pass
    payload: dict = {}
    if raw.strip() and len(raw) <= _MAX_STDIN_BYTES:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except BaseException:  # noqa: BLE001
            pass

    event = str(payload.get("hook_event_name") or "UserPromptSubmit")
    rendered = ""
    try:
        cfg = get_config()
        if _enabled(event, cfg.context_inject_mode):
            query = _query_from(payload, event)
            session_id = str(payload.get("session_id") or "")
            cwd = str(payload.get("cwd") or os.getcwd())
            project = project_name(Path(cwd))
            state_dir = cfg.home / "state"
            prune_stale(state_dir, now=datetime.now(UTC))
            seen = SeenStore(state_dir, session_id)
            if event == "PreToolUse":
                if seen.pretool_fired():
                    raise _SkipInjection()  # module-local sentinel; caught below
                seen.mark_pretool_fired()
            seen.bump_turn()
            conn_ctx = (
                closing(connect(cfg.memory_db))
                if cfg.storage_backend == "sqlite"
                else nullcontext(None)
            )
            with conn_ctx as conn:
                _bump_diagnostic(
                    conn, cfg,
                    "contextual_fired_userprompt" if event == "UserPromptSubmit"
                    else "contextual_fired_pretool",
                )
                backend = build_backend(
                    config=cfg,
                    memory_conn=conn,
                    embedder=None,
                    session_id=session_id or None,
                    project=project,
                )
                sync_embedder = None
                if cfg.embeddings_backend == "ollama":
                    sync_embedder = SyncEmbedder(
                        lambda: OllamaEmbedder(timeout=5.0, max_retries=1),
                        down_state_file=cfg.home / "state" / "embed_down_until",
                    )
                items = retrieve_relevant(
                    backend, query=query, project=project,
                    conn=conn,
                    sync_embedder=sync_embedder,
                    vec_floor=cfg.context_vec_floor,
                    max_items=cfg.context_max_items,
                )
                had_candidates = bool(items)
                pairs = [(m.kind, m.id) for m in items]
                unseen = set(seen.filter_unseen(
                    pairs, reinject_turns=cfg.context_reinject_turns,
                ))
                items = [m for m in items if (m.kind, m.id) in unseen]
                if items:
                    rendered = format_relevant(items)
                    survivors = [(m.kind, m.id) for m in items]
                    try:
                        backend.record_exposures(
                            session_id=session_id,
                            items=survivors,
                            source="contextual",
                        )
                    except BaseException as exc:  # noqa: BLE001 - never block injection
                        try:
                            record_hook_error(hook_name="contextual_inject_exposure", exc=exc)
                        except BaseException:  # noqa: BLE001
                            pass
                    seen.mark_seen(survivors)
                    _bump_diagnostic(conn, cfg, "contextual_injected")
                elif had_candidates:
                    _bump_diagnostic(conn, cfg, "contextual_suppressed_dedup")
                else:
                    _bump_diagnostic(conn, cfg, "contextual_suppressed_floor")
    except _SkipInjection:
        rendered = ""
    except BaseException as exc:  # noqa: BLE001
        try:
            record_hook_error(hook_name="contextual_inject", exc=exc)
        except BaseException:  # noqa: BLE001
            pass
        rendered = ""

    try:
        print(
            json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": rendered,
                }
            }),
            flush=True,
        )
    except BaseException:  # noqa: BLE001
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
