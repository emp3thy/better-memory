# The Stop hook must be synchronous with stdout attached

## Status

Accepted — 2026-07-21.

## Context

better-memory learns which memories are worth surfacing from a per-session
rating signal: at session end the LLM classifies each exposed memory as
`cited / shaped / ignored / misled / overlooked`. Those ratings drive the
retrieval ranking and the retention/pruning decisions. If ratings never land,
the whole feedback loop is open — the system keeps surfacing whatever it
surfaced on day one, forever.

The rating turn is triggered by the `Stop` hook (`hooks/session_close.py`).
When a session has unrated exposures, the hook replies with:

```json
{"decision": "block",
 "reason": "RATE_MEMORIES — N pending rating(s)",
 "hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "<directive>"}}
```

`decision: "block"` is a **control-flow** response: it tells Claude Code not to
stop yet and to run one more turn with the directive as context. Claude Code
only honours control-flow output from a **blocking** hook — one it waits on and
reads stdout from. A hook registered `"async": true` is fire-and-forget: its
stdout is not consumed for control flow, so the block is silently dropped and
the rating turn never happens.

The hook was registered `is_async=True, needs_stdout=False`
(`cli/install_hooks.py`), which also selected `pythonw.exe` on Windows.

## Evidence

Two live headless sessions, identical task and identical copy of the memory
DB, differing only in the Stop hook's registration:

| Stop hook            | exposed | rated | useful | useful% |
|----------------------|---------|-------|--------|---------|
| async (as shipped)   | 44      | 0     | 0      | 0.00%   |
| sync + stdout        | 44      | 44    | 9      | 20.45%  |

Across a 12-task control run every session rated **zero** memories. On the live
production DB, 30 of 39 sessions in the measured month had zero rated
exposures. The loop was effectively never closing.

`pythonw.exe` nulling stdout was considered as the cause and **ruled out**: a
direct probe showed `pythonw.exe` stdout survives redirection to a pipe. The
defect is the async registration, not the interpreter — but the fix moves to
`python.exe` anyway because a blocking hook that must emit stdout has no reason
to use the windowless interpreter.

## Decision

Register `Stop` as `is_async=False, needs_stdout=True`:

```python
HookSpec("better_memory.hooks.session_close", "Stop", None, False, True)
```

`needs_stdout=True` selects `python.exe` (the interpreter whose stdout Claude
Code reads); `is_async=False` makes Claude Code wait for and honour the block.

## Consequences

- The rating turn fires reliably; the ranking/retention loop closes.
- A one-turn cost at session end when unrated exposures exist. The hook already
  short-circuits (no block) when everything is rated or nothing was exposed.
- The tests that pinned the old shape (`async`, `pythonw.exe`) are updated to
  pin the new one; they are golden-shape assertions, not behaviour checks.
