"""Declarative manifest of every Claude Code artifact better-memory manages.

Managed surface (spec table, rows 1-12): eight hook entries and one env key
in ~/.claude/settings.json, the MCP server entry in ~/.claude.json, the
CLAUDE.md managed block, and the user-scope skills enumerated from the repo
checkout's .claude/skills/. Row 13 (per-repo post-commit hook) lives in
setup/repo_hook.py.

Pure data + rendering helpers; no I/O except detect_machine_params() and
managed_skills() (a read-only directory listing).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MachineParams:
    venv_py: str
    venv_pyw: str
    home: str
    repo_root: str


def detect_machine_params(home: str | None = None) -> MachineParams:
    repo_root = Path(__file__).resolve().parents[2]
    if sys.platform == "win32":
        venv_py = repo_root / ".venv" / "Scripts" / "python.exe"
        venv_pyw = repo_root / ".venv" / "Scripts" / "pythonw.exe"
    else:
        venv_py = repo_root / ".venv" / "bin" / "python"
        venv_pyw = venv_py
    resolved_home = home or str(Path.home() / ".better-memory")
    return MachineParams(
        venv_py=str(venv_py), venv_pyw=str(venv_pyw),
        home=resolved_home, repo_root=str(repo_root),
    )


@dataclass(frozen=True)
class HookSpec:
    module: str           # e.g. "better_memory.hooks.session_bootstrap"
    event: str            # a Claude Code hook event name
    matcher: str | None   # None for unscoped events; tool-name alternation otherwise
    is_async: bool        # True for PostToolUse + Stop
    needs_stdout: bool    # True for SessionStart bootstrap — Claude Code reads
                          # the hook's stdout for additionalContext, so the
                          # interpreter MUST keep stdout attached. On Windows
                          # pythonw.exe silently nulls sys.stdout; the bootstrap
                          # would print nothing and Claude would get no context.
    if_filter: str | None = None  # Optional if-filter for conditional execution


MANAGED_HOOKS: tuple[HookSpec, ...] = (
    # SessionStart bootstrap: curated-memory injection + session state setup.
    HookSpec(
        "better_memory.hooks.session_bootstrap", "SessionStart", None, False, True
    ),
    # PostToolUse observer: primary write hook for recording observations.
    HookSpec(
        "better_memory.hooks.observer",
        "PostToolUse",
        "Write|Edit|Bash",
        True,
        False,
    ),
    # Stop MUST be synchronous with stdout attached. The rating sweep replies with
    # a `decision: "block"` payload (hooks/session_close.py) — a control-flow
    # response that Claude Code only honours from a blocking hook. Registered
    # async, the block is dropped and RATE_MEMORIES never forces a rating turn.
    # Measured A/B on identical tasks: async => 0% of exposures rated;
    # sync => 100% rated. See docs/decisions/stop-hook-must-be-sync.md.
    HookSpec("better_memory.hooks.session_close", "Stop", None, False, True),
    # Contextual injection: surface curated memories relevant to the current
    # prompt / tool-input. Sync + needs_stdout (reads additionalContext from
    # stdout). Gated at runtime by BETTER_MEMORY_CONTEXT_INJECT_MODE; both
    # events install and the mode no-ops whichever isn't selected.
    HookSpec(
        "better_memory.hooks.contextual_inject", "UserPromptSubmit", None, False, True
    ),
    # Matcher is None (unscoped = all tools) rather than a tool-name
    # alternation: the hook's per-session PreToolUse latch (SeenStore
    # .pretool_fired/.mark_pretool_fired) makes an unscoped matcher cheap —
    # only the first PreToolUse event per session does real work; every
    # later one short-circuits on the state file before touching the DB.
    HookSpec(
        "better_memory.hooks.contextual_inject", "PreToolUse", None, False, True
    ),
    # Commit checkpoint: records pre-commit observations for commit messages.
    HookSpec(
        "better_memory.hooks.commit_checkpoint",
        "PreToolUse",
        "Bash",
        False,
        True,
        if_filter="Bash(git commit*)",
    ),
    # Stop sweep: final observation recording at session end.
    HookSpec("better_memory.hooks.stop_sweep", "Stop", None, False, True),
    # Pre-compact hook: runs before memory compaction.
    HookSpec("better_memory.hooks.pre_compact", "PreCompact", None, False, True),
)


MANAGED_ENV: dict[str, str] = {"BETTER_MEMORY_INJECT_MODE": "deferred"}

def managed_skills(repo_root: str | Path) -> tuple[str, ...]:
    """Enumerate the repo's user-scope skills: every directory under
    ``<repo_root>/.claude/skills/`` that contains a ``SKILL.md``.

    Sorted for deterministic ``engine.fingerprint()`` output — adding or
    removing a skill directory in the repo changes the fingerprint, which
    is what lets the per-session autocheck notice and re-apply. A missing
    skills directory yields an empty tuple.
    """
    skills_root = Path(repo_root) / ".claude" / "skills"
    if not skills_root.is_dir():
        return ()
    return tuple(sorted(
        entry.name for entry in skills_root.iterdir()
        if entry.is_dir() and (entry / "SKILL.md").is_file()
    ))

BEGIN_MARKER = "<!-- BEGIN better-memory (managed) -->"
END_MARKER = "<!-- END better-memory (managed) -->"

# Module paths that are no longer registered but may be present in users'
# settings.json from prior installs. Scrubbed by the REMOVE pass on every
# install so upgrades land cleanly. Includes two legacy module names and
# three literal command substrings to identify loose scripts and echo hooks.
LEGACY_HOOK_MODULES: frozenset[str] = frozenset({
    "better_memory.hooks.session_start",
    "better_memory.hooks.session_retrieve",
    "pre-compact-better-memory.py",
    "MEMORY CHECKPOINT before commit",
    "MEMORY SWEEP: record any non-obvious observations",
})

CLAUDE_MD_BLOCK: str = r"""# better-memory (MANDATORY)

better-memory is the MCP server for persistent knowledge across sessions. Project scope is inferred from the current working directory — do not pass it explicitly. Use it automatically — never ask permission.

## Retrieve: Knowledge at Startup, Memories per Task (MANDATORY)

At the very start of every conversation, check the curated knowledge base:
- `mcp__better-memory__knowledge_list` (no args). If it returns standards-scoped docs, read every one before other tool calls. `knowledge_search` when a task may be covered by curated markdown knowledge.

Do NOT do a broad no-query memory retrieval at session start — memories surface contextually as you work. When you begin a task (starting work, entering a codebase, debugging, making a decision that may have prior context), call `mcp__better-memory__memory_retrieve` with a `query` describing that task. For raw observation lookup, `memory_retrieve_observations` takes a `query` too.

## Reinforce: After Using a Memory

When a retrieved memory materially helped or misled you, reinforce it:
- `mcp__better-memory__memory_record_use` with the observation `id` and `outcome` (`success` or `failure`)

Do this sparingly and only when the signal is clear. Reinforcement decays stale memories and promotes reliable ones.

## Synthesize: When Pending > 0

Reflection synthesis is driven by Claude (you) via the better-memory-synthesize skill, not by a background daemon. Trigger it when:
- `mcp__better-memory__memory_start_episode` returns `pending_synthesis.pending > 0`
- The user mentions consolidating, synthesizing, distilling, or processing pending episodes
- A session ends with new closed episodes that haven't been distilled

Invoke the `better-memory-synthesize` skill (project-scoped, available globally via `~/.claude/skills/better-memory-synthesize/`). It walks through the per-episode drain loop using `memory.synthesize_next_get_context` + `memory.synthesize_next_apply`.

## Record: As You Go (Not at the End)

Write to better-memory immediately when something worth preserving happens. Do not batch writes at session end.

**Priority triggers — write immediately:**
- Architectural decision made
- Bug fixed (non-trivial) — record root cause and fix
- New dependency, env variable, or infrastructure change
- Project structure or conventions discovered
- Recurring pattern or gotcha identified
- User preference or workflow requirement stated

**Mandatory record triggers — not judgment calls, always do these:**
- **After every code-review fix commit.** If the task reviewer (spec or quality verdict) flagged an issue and you committed a fix, record the bug/gap as a `failure` observation. The fix's existence proves it was non-obvious. Do this BEFORE moving on to the next task.
- **Before marking a subagent task complete.** Sweep that task's fix commits and reviewer findings. If any revealed a non-obvious fact, record it.
- **At the end of each phase / PR cycle.** Before invoking `superpowers:finishing-a-development-branch`, pause and do a memory sweep: walk the phase's commits and reviewer comments for anything worth preserving across sessions.

These triggers override the "worth preserving" judgment call — if the trigger fires, you record. Skipping a trigger is a CLAUDE.md violation.

**How to write:**
- `mcp__better-memory__memory_observe` with a concise, factual `content` summary
- Set `outcome` deliberately: `success` (do this again), `failure` (don't do this), `neutral` (reference only). The outcome determines which bucket the memory lands in on retrieval — choose it based on what future-you should take away.
- Always fill the typed fields where applicable (see schema below)
- Retrieve first to avoid duplicates

**Do not record:**
- Trivial tasks or one-off commands
- Speculative conclusions from incomplete information
- Anything already in CLAUDE.md

## Observation Schema (MANDATORY)

`memory_observe` takes typed fields instead of a free-form metadata dict. Fill them deliberately so future retrievals hit.

| Field | Required | Description |
|-------|----------|-------------|
| `content` | Yes | Concise factual summary. Include enough specifics (names, paths, values) that the memory stands alone. |
| `outcome` | Yes in practice | `success`, `failure`, or `neutral`. Defaults to `neutral` if omitted, but omitting loses signal — set it explicitly. |
| `component` | When applicable | Subsystem / module / package name (e.g. `orchestrator`, `dashboard`, `growatt_client`). Enables component-scoped retrieval. |
| `theme` | When applicable | Cross-cutting topic tag (e.g. `bug`, `decision`, `architecture`, `convention`, `gotcha`, `dependency`, `infrastructure`, `preference`). Roughly equivalent to a category. |
| `trigger_type` | When applicable | What prompted the observation (e.g. `user-feedback`, `test-failure`, `review`, `deploy`). Optional but useful for filtering. |

Project scope is attached automatically from the session/cwd. Do not try to embed a project name in `content` unless it is genuinely cross-project.

**Examples:**

Bug fix:
```
memory_observe(
  content="Growatt API Timespan.day returned inflated consumption; switched to Timespan.hour (288 snapshots / 12 = kWh). Removed get_daily_data.",
  component="growatt_client",
  theme="bug",
  outcome="failure",
  trigger_type="debugging"
)
```

Architectural decision:
```
memory_observe(
  content="Calculator uses _estimate_generation_hourly + _morning_floor_kwh; charge = max(gap_pct, morning_floor_pct).",
  component="calculator",
  theme="decision",
  outcome="success"
)
```

General-purpose gotcha (no component):
```
memory_observe(
  content="Python ZoneInfo is unavailable on Windows without tzdata package; install tzdata or fall back to zoneinfo backport.",
  theme="gotcha",
  outcome="failure"
)
```"""  # noqa: E501


def hook_entry(spec: HookSpec, params: MachineParams) -> dict:
    """Build the JSON object for a single hook entry.

    Interpreter selection: hooks marked ``needs_stdout`` use ``venv_py``
    (python.exe on Windows) so Claude Code can read the hook's stdout
    for ``additionalContext``. Other hooks use ``venv_pyw`` (pythonw.exe
    on Windows) to avoid the brief console flash on each tool call.
    On non-Windows systems setup.sh passes the same path for both.
    """
    interpreter = params.venv_py if spec.needs_stdout else params.venv_pyw
    entry: dict = {
        "type": "command",
        "command": f'"{interpreter}" -m {spec.module}',
    }
    if spec.is_async:
        entry["async"] = True
    if spec.if_filter is not None:
        entry["if"] = spec.if_filter
    return entry


def mcp_server_entry(params: MachineParams) -> dict:
    """Build the JSON object for the MCP server entry."""
    return {
        "type": "stdio",
        "command": params.venv_py,
        "args": ["-m", "better_memory.mcp"],
        "env": {"BETTER_MEMORY_HOME": params.home},
    }
