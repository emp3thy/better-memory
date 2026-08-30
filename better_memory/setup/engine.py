"""Render / inspect / diff / apply engine for the self-managing setup.

Pure functions compute the desired managed state (``render``), merge it
into existing user config (``merge_settings``, ``patch_mcp_entry``,
``splice_managed_block``), and report drift between desired and live state
(``diff``). ``apply()`` is the I/O half: it writes the managed subset of
every target file to match ``render(params)``, backing up before every
write and retrying once if ``~/.claude.json`` is rewritten concurrently by
Claude Code itself (spec concern 3).

Port of ``better_memory/cli/install_hooks.py``'s ``merge_settings_json``
(lines 141-215), ``merge_claude_json`` (lines 90-116), ``_backup`` /
``_atomic_write`` (lines 301-332), and ``install_skill_symlinks``
(lines 228-279). The pure merges are generalized to iterate the full
``MANAGED_HOOKS`` table (8 specs, including ``if_filter`` groups) and to
merge ``MANAGED_ENV`` alongside hooks; ``install_skill_symlinks`` is
parameterized by target dir + repo root and extended to loop
``MANAGED_SKILLS``.

``_backup``/``_atomic_write`` are duplicated here rather than imported from
``cli/install_hooks`` — that module becomes a thin shim over this engine in
a later task and must not become a dependency of it (Ruling B).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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

_LOCK_STALE_SECONDS = 60


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


# ------------------------------------------------------------------ apply


@dataclass
class ApplyReport:
    repaired: list[str]
    warnings: list[str]


def _atomic_write(path: Path, content: str) -> None:
    """Write to ``{path}.tmp`` then ``os.replace``. Caller handles backups.

    On failure during ``os.replace``, the tmp file persists for forensics
    and the original (if any) is untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _backup(
    src: Path,
    dst_dir: Path,
    *,
    clock: Callable[[], datetime] | None = None,
) -> Path | None:
    """Copy ``src`` into ``dst_dir`` with a timestamped name. Idempotent
    in the sense that missing source → no-op (returns None).

    Timestamp format: ``YYYYMMDD-HHMMSS`` (UTC if no clock injected; local
    time of injected clock if injected).
    """
    if not src.exists():
        return None
    dst_dir.mkdir(parents=True, exist_ok=True)
    now = (clock or datetime.now)()
    timestamp = now.strftime("%Y%m%d-%H%M%S")
    dst = dst_dir / f"{src.name}.{timestamp}.bak"
    shutil.copy2(src, dst)
    return dst


def _read_json_and_mtime(path: Path) -> tuple[dict | None, float]:
    """None for malformed JSON; ({}, 0.0) for missing file."""
    if not path.exists():
        return {}, 0.0
    try:
        return json.loads(path.read_text(encoding="utf-8")), path.stat().st_mtime
    except json.JSONDecodeError:
        return None, path.stat().st_mtime


def _acquire_lock(lock_path: Path) -> int | None:
    """Acquire the apply lock via O_CREAT|O_EXCL.

    A lock file older than ``_LOCK_STALE_SECONDS`` is treated as abandoned
    (a prior apply crashed without releasing it): it is removed and
    acquisition is retried once. Returns the open fd, or ``None`` if the
    lock is still held by a live apply after that one retry.
    """
    try:
        return os.open(lock_path, os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        pass

    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        age = 0.0
    if age <= _LOCK_STALE_SECONDS:
        return None

    try:
        lock_path.unlink()
    except OSError:
        pass
    try:
        return os.open(lock_path, os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        return None


def _apply_settings_json(
    paths: TargetPaths,
    params: MachineParams,
    backup_dir: Path,
    repaired: list[str],
    warnings: list[str],
) -> None:
    existing = _load_json_or_drift(paths.settings_json, warnings)
    if existing is None:
        return  # Malformed — warning already recorded; never write it.
    merged = merge_settings(existing, params)
    if merged == existing:
        return
    _backup(paths.settings_json, backup_dir)
    _atomic_write(paths.settings_json, json.dumps(merged, indent=2) + "\n")
    repaired.append(f"{paths.settings_json}: hooks + env")


def _apply_claude_json(
    paths: TargetPaths,
    params: MachineParams,
    backup_dir: Path,
    repaired: list[str],
    warnings: list[str],
) -> None:
    """Concern-3 sequence: read, compute patch, backup, RE-READ immediately
    before write; if the file moved under us (Claude Code itself rewrote it
    between our read and our write), recompute the patch from the fresh
    content — one retry — then write atomically.
    """
    data, mtime1 = _read_json_and_mtime(paths.claude_json)
    if data is None or not isinstance(data, dict):
        warnings.append(f"{paths.claude_json}: unparseable JSON (manual fix needed)")
        return  # Malformed — never write it.

    patched = patch_mcp_entry(data, params)
    if patched == data:
        return

    _backup(paths.claude_json, backup_dir)

    fresh_data, mtime2 = _read_json_and_mtime(paths.claude_json)
    if fresh_data is None or not isinstance(fresh_data, dict):
        warnings.append(f"{paths.claude_json}: unparseable JSON (manual fix needed)")
        return  # Became malformed under us — never write it.
    if mtime2 != mtime1:
        patched = patch_mcp_entry(fresh_data, params)  # One retry from fresh content.

    _atomic_write(paths.claude_json, json.dumps(patched, indent=2) + "\n")
    repaired.append(f"{paths.claude_json}: mcp server entry")


def _apply_claude_md(paths: TargetPaths, backup_dir: Path, repaired: list[str]) -> None:
    text = paths.claude_md.read_text(encoding="utf-8") if paths.claude_md.exists() else ""
    new_text = splice_managed_block(text, CLAUDE_MD_BLOCK)
    if new_text == text:
        return
    _backup(paths.claude_md, backup_dir)
    _atomic_write(paths.claude_md, new_text)
    repaired.append(f"{paths.claude_md}: managed block")


def install_skills(paths: TargetPaths, params: MachineParams) -> list[str]:
    """Symlink each ``MANAGED_SKILLS`` entry from the repo into
    ``paths.skills_dir``.

    Port of ``cli/install_hooks.py``'s ``install_skill_symlinks``,
    parameterized by target dir (``paths.skills_dir``) and repo root
    (``params.repo_root``) instead of hardcoded user-scope paths, and
    looping the full ``MANAGED_SKILLS`` tuple.

    Idempotent: if a link already points to the right target, nothing
    happens. If a different symlink, file, or directory exists at the
    target path, it is removed without backup before recreating the
    symlink — symlinks have no content to preserve, and a stale or
    user-replaced skill directory at the same name is treated as obsolete.

    Returns warning strings instead of printing to stderr — for a missing
    source directory, or for an ``OSError`` on symlink creation (e.g.
    Windows without symlink privilege: Developer Mode or elevation needed).
    """
    warnings: list[str] = []
    paths.skills_dir.mkdir(parents=True, exist_ok=True)

    for skill_name in MANAGED_SKILLS:
        source = _repo_skill_source(params, skill_name)
        if not source.is_dir():
            warnings.append(f"skill {skill_name!r}: source not found at {source}")
            continue

        link = paths.skills_dir / skill_name
        # Junctions (mklink /J) report is_symlink() == False but ARE links:
        # resolve() follows them and shutil.rmtree refuses them outright.
        # Treat them exactly like symlinks (see install_hooks.py precedent).
        if link.is_symlink() or os.path.isjunction(link):
            if link.resolve() == source.resolve():
                continue  # Already correct — nothing to do.
            try:
                link.unlink()
            except (OSError, PermissionError):
                os.rmdir(link)
        elif link.exists():
            if link.is_file():
                link.unlink()
            elif link.is_dir():
                shutil.rmtree(link)

        try:
            link.symlink_to(source, target_is_directory=True)
        except OSError as exc:
            warnings.append(
                f"skill symlink skipped ({skill_name}): {exc}; enable Windows "
                "Developer Mode or run as administrator to install skill symlinks."
            )

    return warnings


def apply(params: MachineParams, paths: TargetPaths, *, home: Path) -> ApplyReport:
    """Write the managed subset of every target file to match
    ``render(params)``.

    Only better-memory's managed entries are touched; foreign entries in
    each target file are preserved byte-for-byte (see the pure merge
    functions this delegates to). Every existing file is backed up to
    ``home / "install-backups"`` before it is overwritten. Malformed JSON
    in a target file produces a warning and that file is left untouched.
    ``~/.claude.json`` gets one retry if it is rewritten concurrently by
    Claude Code itself between the pre-write read and the atomic write
    (spec concern 3) — see ``_apply_claude_json``.

    Concurrent ``apply()`` calls are serialized via a lock file
    (``home / "state" / "setup-apply.lock"``) with a 60s stale timeout; if
    another apply holds the lock, this call is a no-op that returns a
    single warning.
    """
    home = Path(home)
    backup_dir = home / "install-backups"
    lock_path = home / "state" / "setup-apply.lock"
    backup_dir.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    fd = _acquire_lock(lock_path)
    if fd is None:
        return ApplyReport([], ["another apply in progress; skipped"])

    repaired: list[str] = []
    warnings: list[str] = []
    try:
        _apply_settings_json(paths, params, backup_dir, repaired, warnings)
        _apply_claude_json(paths, params, backup_dir, repaired, warnings)
        _apply_claude_md(paths, backup_dir, repaired)
        warnings.extend(install_skills(paths, params))
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except OSError:
            pass

    return ApplyReport(repaired=repaired, warnings=warnings)
