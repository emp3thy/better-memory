"""Per-session wiring drift check with mtime+fingerprint short-circuit.

Called from hooks.session_bootstrap. Near-zero cost on the happy path:
state/wiring_fingerprint.json stores the manifest fingerprint plus the
mtimes of the target files; when nothing moved, no diff runs. Kill switch:
BETTER_MEMORY_WIRING_AUTOCHECK=off (add to website/configuration.md).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from better_memory.setup import engine, manifest, repo_hook

_STATE_NAME = "wiring_fingerprint.json"


def _mtimes(paths: engine.TargetPaths) -> dict[str, float]:
    result = {}
    # skills_dir: a directory's mtime changes when entries are added or
    # removed, so a manually deleted skill link busts the cache and gets
    # relinked next session.
    for p in (paths.claude_json, paths.settings_json, paths.claude_md,
              paths.skills_dir):
        result[str(p)] = p.stat().st_mtime if p.exists() else 0.0
    return result


def maybe_repair(home: Path, cwd: Path) -> str | None:
    if os.environ.get("BETTER_MEMORY_WIRING_AUTOCHECK", "").lower() == "off":
        return None
    params = manifest.detect_machine_params(home=str(home))
    paths = engine.default_target_paths()
    fp = engine.fingerprint(params)
    state_path = home / "state" / _STATE_NAME
    current = {"fingerprint": fp, "mtimes": _mtimes(paths)}
    try:
        cached = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cached = None
    repo_msg = repo_hook.ensure_post_commit(cwd, params)
    if cached == current and repo_msg is None:
        return None
    messages: list[str] = [repo_msg] if repo_msg else []
    warnings: list[str] = []
    drift = engine.diff(params, paths)
    if drift:
        report = engine.apply(params, paths, home=home)
        if report.repaired:
            messages.append(
                f"better-memory doctor: repaired {len(report.repaired)} "
                f"item(s): {'; '.join(report.repaired)} (effective next session)"
            )
        warnings = report.warnings
        messages.extend(f"better-memory doctor: WARN {w}" for w in warnings)
    if not warnings:
        # Cache ONLY a clean outcome. A cached fingerprint after warnings
        # would silence a persistent problem after one report (lift-pass fix).
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"fingerprint": fp, "mtimes": _mtimes(paths)}),
            encoding="utf-8",
        )
    return " | ".join(messages) if messages else None
