"""UserPromptSubmit / PreToolUse hook: inject curated memories relevant to the
current prompt (typed-prompt channel) or tool call (trigger channel). Gated
by BETTER_MEMORY_CONTEXT_INJECT_MODE (userprompt | pretool | both | off).
Never raises; always exits 0.

UserPromptSubmit: non-human prompts (command output, system tags, peer
agent messages) are skipped outright; human prompts go through
retrieve_relevant's distinct-hit evidence gate (services/relevant.py) and
are capped at cfg.context_max_items.

PreToolUse fires on EVERY tool call (no per-session latch any more) and is
trigger-only: memories carrying triggers (services/triggers.py grammar)
are matched against the tool name + input by triggered_memories; nothing
is keyword-matched against tool input. Measured cost of the full path is
within noise of the old latched short-circuit (184 ms vs 175 ms, of which
168 ms is interpreter start). Hits are logged with exposure source
'trigger' so the diagnostics panel can report the channel on its own.

A per-session SeenStore dedups injected memories across both channels and
across turns (cfg.context_reinject_turns controls re-injection after N
turns). Exposure writes are best-effort -- a failure never blocks
injection. Counters in rating_diagnostics: contextual_fired_userprompt /
pretool, contextual_injected, contextual_suppressed_floor / dedup /
nonhuman, trigger_fired, trigger_injected.
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from better_memory.config import get_config, project_name
from better_memory.db.connection import connect
from better_memory.hooks._error_log import record_hook_error
from better_memory.services.context_seen import SeenStore, prune_stale
from better_memory.services.relevant import (
    format_relevant,
    retrieve_relevant,
    triggered_memories,
)
from better_memory.storage import build_backend

_MAX_STDIN_BYTES = 1_000_000


class _SkipInjection(Exception):
    """Module-local sentinel: this firing injects nothing by design (a
    non-human prompt). Caught explicitly (never via the outer BaseException
    guard) to leave ``rendered = ""`` without treating the skip as an error.
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


#: Prompt prefixes that mark a UserPromptSubmit payload as NOT typed by a
#: human: command output and system tags (``<...>``) and peer-agent
#: messages. The hook payload carries no origin field, so the text is the
#: only signal. Measured: the hook fired 571 times across sessions holding
#: 261 typed prompts and 296 peer messages (spec Assumption A5).
NON_HUMAN_PREFIXES: tuple[str, ...] = ("<", "Another Claude session sent a message")


def is_human_prompt(text: str) -> bool:
    """True for a non-empty prompt that does not start with a non-human prefix."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    return not stripped.startswith(NON_HUMAN_PREFIXES)


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
            if event == "UserPromptSubmit" and not is_human_prompt(query):
                # Peer-agent messages, command output and system tags are
                # not prompts: no injection, no exposure, no turn bump.
                with closing(connect(cfg.memory_db)) as conn:
                    _bump_diagnostic(conn, cfg, "contextual_suppressed_nonhuman")
                raise _SkipInjection()
            if event == "PreToolUse" and cfg.storage_backend != "sqlite":
                # The trigger channel needs trigger storage, which only the
                # sqlite backend has. Return before any connection or
                # backend build: on agentcore that would mean a boto3 import
                # and two client builds on EVERY tool call for a channel
                # that cannot fire.
                raise _SkipInjection()
            seen.bump_turn()
            # A real local connection is opened in BOTH backend modes: the
            # exposure ledger is session-operational state in the local
            # memory.db regardless of where memory CONTENT lives. The
            # ``conn`` passed to retrieve_relevant means "sqlite FTS
            # substrate available", so agentcore gets None there and
            # retrieve_relevant swaps in backend.relevance_ranks.
            with closing(connect(cfg.memory_db)) as conn:
                _bump_diagnostic(
                    conn, cfg,
                    "contextual_fired_userprompt" if event == "UserPromptSubmit"
                    else "contextual_fired_pretool",
                )
                backend = build_backend(
                    config=cfg,
                    memory_conn=conn,
                    session_id=session_id or None,
                    project=project,
                )
                if event == "PreToolUse":
                    _bump_diagnostic(conn, cfg, "trigger_fired")
                    tool_input = payload.get("tool_input")
                    items = triggered_memories(
                        backend, project=project,
                        tool_name=str(payload.get("tool_name") or ""),
                        tool_input=tool_input if isinstance(tool_input, dict) else {},
                        large_write_chars=cfg.trigger_large_write_chars,
                    )
                    source, injected_metric = "trigger", "trigger_injected"
                else:
                    items = retrieve_relevant(
                        backend, query=query, project=project,
                        conn=conn if cfg.storage_backend == "sqlite" else None,
                        max_items=cfg.context_max_items,
                        min_hits=cfg.context_min_hits,
                    )
                    source, injected_metric = "contextual", "contextual_injected"
                had_candidates = bool(items)
                pairs = [(m.kind, m.id) for m in items]
                unseen = set(seen.filter_unseen(
                    pairs, reinject_turns=cfg.context_reinject_turns,
                ))
                items = [m for m in items if (m.kind, m.id) in unseen]
                items = items[: cfg.context_max_items]
                # Atomic per-memory claim: parallel hook processes (parallel
                # tool calls) each read their own SeenStore snapshot, so
                # without this both would serve the same memory.
                won = set(seen.claim([(m.kind, m.id) for m in items]))
                items = [m for m in items if (m.kind, m.id) in won]
                if items:
                    rendered = format_relevant(items)
                    survivors = [(m.kind, m.id) for m in items]
                    exposure_items = [(m.kind, m.id, m.text) for m in items]
                    try:
                        backend.record_exposures(
                            session_id=session_id,
                            items=exposure_items,
                            source=source,
                        )
                    except BaseException as exc:  # noqa: BLE001 - never block injection
                        try:
                            record_hook_error(hook_name="contextual_inject_exposure", exc=exc)
                        except BaseException:  # noqa: BLE001
                            pass
                    seen.mark_seen(survivors)
                    _bump_diagnostic(conn, cfg, injected_metric)
                elif had_candidates:
                    _bump_diagnostic(conn, cfg, "contextual_suppressed_dedup")
                elif event == "UserPromptSubmit":
                    # The floor counter is the PROMPT gate's figure (it
                    # drives retuning of the distinct-hit floor); tool calls
                    # that match no trigger are measured by
                    # trigger_fired - trigger_injected instead.
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
