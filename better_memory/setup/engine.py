"""Render / inspect / diff half of the self-managing setup engine.

Pure functions that compute the desired managed state (``render``), merge
it into existing user config (``merge_settings``, ``patch_mcp_entry``,
``splice_managed_block``), and report drift between desired and live state
(``diff``). No writes happen here — see a later task's ``apply()`` for the
I/O half.

Port of ``better_memory/cli/install_hooks.py``'s ``merge_settings_json``
(lines 141-215) and ``merge_claude_json`` (lines 90-116), generalized to
iterate the full ``MANAGED_HOOKS`` table (8 specs, including ``if_filter``
groups) and to merge ``MANAGED_ENV`` alongside hooks.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from better_memory.setup.manifest import (
    BEGIN_MARKER,
    CLAUDE_MD_BLOCK,
    END_MARKER,
    LEGACY_HOOK_MODULES,
    MANAGED_ENV,
    MANAGED_HOOKS,
    MANAGED_SKILLS,
    MachineParams,
    hook_entry,
    mcp_server_entry,
)


@dataclass(frozen=True)
class TargetPaths:
    claude_json: Path
    settings_json: Path
    claude_md: Path
    skills_dir: Path


def default_target_paths() -> TargetPaths:
    home = Path.home()
    return TargetPaths(
        claude_json=home / ".claude.json",
        settings_json=home / ".claude" / "settings.json",
        claude_md=home / ".claude" / "CLAUDE.md",
        skills_dir=home / ".claude" / "skills",
    )


def _repo_skill_source(params: MachineParams, skill_name: str) -> Path:
    return Path(params.repo_root) / ".claude" / "skills" / skill_name


def _canonical_hook_groups(params: MachineParams) -> dict[str, list[dict]]:
    """Build the canonical per-event matcher-group lists for every managed
    hook spec. Shared by ``render()`` and ``merge_settings()``'s ADD pass so
    the two can never drift apart from each other.

    SessionStart specs share a single group; every other spec gets its own
    group, with ``matcher`` copied on when the spec has one. ``if`` is
    already embedded in the hook entry itself (see ``hook_entry``) and is
    never duplicated onto the group.
    """
    hooks: dict[str, list[dict]] = {}

    session_start_specs = [s for s in MANAGED_HOOKS if s.event == "SessionStart"]
    if session_start_specs:
        hooks.setdefault("SessionStart", []).append({
            "hooks": [hook_entry(s, params) for s in session_start_specs],
        })

    for spec in MANAGED_HOOKS:
        if spec.event == "SessionStart":
            continue
        group: dict = {"hooks": [hook_entry(spec, params)]}
        if spec.matcher is not None:
            group["matcher"] = spec.matcher
        hooks.setdefault(spec.event, []).append(group)

    return hooks


def render(params: MachineParams) -> dict:
    """Build the desired-state dict for every managed surface."""
    skills = tuple(
        (name, str(_repo_skill_source(params, name))) for name in MANAGED_SKILLS
    )

    return {
        "settings_hooks": _canonical_hook_groups(params),
        "settings_env": dict(MANAGED_ENV),
        "mcp_entry": mcp_server_entry(params),
        "claude_md_block": CLAUDE_MD_BLOCK,
        "skills": skills,
    }


def merge_settings(existing: dict, params: MachineParams) -> dict:
    """Smart-merge managed hook entries + env into settings.json content.

    Two-pass strategy:
    1. REMOVE — walk every event's every matcher-group's every hook. If the
       hook's ``command`` contains any current or legacy better-memory
       module path, strip it. Drop matcher-groups whose ``hooks`` array is
       empty after removal.
    2. ADD — append canonical matcher-groups at the end of each event's
       array, one group per ``MANAGED_HOOKS`` spec (SessionStart specs
       share a single group). ``matcher`` is copied onto the group when the
       spec has one; ``if`` is already embedded in the hook entry itself
       (see ``hook_entry``) and is never duplicated onto the group.
    3. Merge ``MANAGED_ENV`` into ``env``, preserving the user's other keys.

    User's other (non-better-memory) hooks, matcher-groups, and top-level
    keys are untouched.
    """
    config = dict(existing)
    hooks = dict(config.get("hooks", {}))
    strip_modules = {s.module for s in MANAGED_HOOKS} | LEGACY_HOOK_MODULES

    # Pass 1: REMOVE
    for event_name in list(hooks.keys()):
        groups: list[dict] = []
        for group in hooks[event_name]:
            kept_hooks = [
                h for h in group.get("hooks", [])
                if not any(mp in h.get("command", "") for mp in strip_modules)
            ]
            if kept_hooks:
                new_group = dict(group)
                new_group["hooks"] = kept_hooks
                groups.append(new_group)
            # else: empty after removal — drop it.
        hooks[event_name] = groups

    # Pass 2: ADD canonical groups.
    for event_name, canonical_groups in _canonical_hook_groups(params).items():
        hooks.setdefault(event_name, []).extend(canonical_groups)

    config["hooks"] = hooks

    # Pass 3: merge env.
    env = dict(config.get("env", {}))
    env.update(MANAGED_ENV)
    config["env"] = env

    return config


def patch_mcp_entry(existing: dict, params: MachineParams) -> dict:
    """Smart-merge the better-memory MCP server into .claude.json content.

    Preserves the user's custom ``env`` values; ensures
    ``BETTER_MEMORY_HOME`` is set if absent. Refreshes ``command`` and
    ``args`` to current paths on every run. Other keys in ``mcpServers``
    and other top-level config are untouched.
    """
    config = dict(existing)
    mcp_servers = dict(config.get("mcpServers", {}))
    existing_bm = mcp_servers.get("better-memory", {})

    merged = mcp_server_entry(params)
    if "env" in existing_bm:
        env = dict(existing_bm["env"])
        env.setdefault("BETTER_MEMORY_HOME", params.home)
        merged["env"] = env
    else:
        merged["env"] = {"BETTER_MEMORY_HOME": params.home}

    mcp_servers["better-memory"] = merged
    config["mcpServers"] = mcp_servers
    return config


def splice_managed_block(text: str, block: str) -> str:
    """Insert/replace the managed block in ``text``.

    If both markers are present, everything between them (inclusive) is
    replaced. Otherwise the block is appended at the end.
    """
    if BEGIN_MARKER in text and END_MARKER in text:
        start = text.index(BEGIN_MARKER)
        end = text.index(END_MARKER) + len(END_MARKER)
        return (
            text[:start] + BEGIN_MARKER + "\n" + block + "\n" + END_MARKER + text[end:]
        )
    return text + "\n\n" + BEGIN_MARKER + "\n" + block + "\n" + END_MARKER + "\n"


def extract_managed_block(text: str) -> str | None:
    """Return the text between the markers, stripped of one leading/trailing
    newline, or ``None`` if either marker is absent."""
    if BEGIN_MARKER not in text or END_MARKER not in text:
        return None
    start = text.index(BEGIN_MARKER) + len(BEGIN_MARKER)
    end = text.index(END_MARKER)
    if end < start:
        return None
    inner = text[start:end]
    if inner.startswith("\n"):
        inner = inner[1:]
    if inner.endswith("\n"):
        inner = inner[:-1]
    return inner


def fingerprint(params: MachineParams) -> str:
    canon = json.dumps(render(params), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _load_json_or_drift(path: Path, drifts: list[str]) -> dict | None:
    """Load JSON from ``path``. Missing -> {}. Malformed, or valid JSON that
    doesn't parse to an object (e.g. ``5``, ``null``, ``[1,2,3]``) -> append
    a drift line and return None (caller skips further checks for this
    target). ``merge_settings``/``patch_mcp_entry`` both do ``dict(existing)``
    and would raise TypeError on a non-dict, so non-dict shapes must be
    filtered out here rather than passed through.
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        drifts.append(f"{path}: unparseable JSON (manual fix needed)")
        return None
    if not isinstance(parsed, dict):
        drifts.append(f"{path}: unparseable JSON (manual fix needed)")
        return None
    return parsed


def diff(params: MachineParams, paths: TargetPaths) -> list[str]:
    """Compare live state on disk against the desired managed state.

    Returns human-readable drift lines; ``[]`` when everything matches.
    Never raises — malformed JSON becomes a drift line instead of an
    exception.
    """
    drifts: list[str] = []

    live_settings = _load_json_or_drift(paths.settings_json, drifts)
    if live_settings is not None:
        merged_settings = merge_settings(live_settings, params)
        if merged_settings != live_settings:
            live_hooks = live_settings.get("hooks", {})
            merged_hooks = merged_settings.get("hooks", {})
            for event in sorted(set(live_hooks) | set(merged_hooks)):
                if live_hooks.get(event) != merged_hooks.get(event):
                    drifts.append(f"settings.json: hook event {event!r} drifted")
            if live_settings.get("env", {}) != merged_settings.get("env", {}):
                drifts.append("settings.json: env drifted")

    live_claude = _load_json_or_drift(paths.claude_json, drifts)
    if live_claude is not None:
        patched_claude = patch_mcp_entry(live_claude, params)
        if patched_claude != live_claude:
            drifts.append(f"{paths.claude_json}: mcp server entry drifted")

    live_md = paths.claude_md.read_text(encoding="utf-8") if paths.claude_md.exists() else ""
    if extract_managed_block(live_md) != CLAUDE_MD_BLOCK:
        drifts.append(f"{paths.claude_md}: managed block missing or stale")

    for skill_name in MANAGED_SKILLS:
        link = paths.skills_dir / skill_name
        source = _repo_skill_source(params, skill_name)
        if not (link.is_symlink() or os.path.isjunction(link)):
            drifts.append(f"skill {skill_name!r}: not linked")
            continue
        try:
            resolved_ok = link.resolve() == source.resolve()
        except OSError:
            resolved_ok = False
        if not resolved_ok:
            drifts.append(f"skill {skill_name!r}: link does not resolve to repo skill dir")

    return drifts
