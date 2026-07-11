"""Per-session seen-store for contextual memory injection dedup.

Backend-independent (works in agentcore mode where there is no exposure
table) and cheap: one small JSON file per session under
``<better-memory home>/state``. Never raises: corrupt or unwritable state
degrades to "nothing seen".

File format: ``context_seen_<session_id>.json`` ->
``{"turn": int, "seen": {"<kind>:<id>": last_injected_turn}}``.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

_FILE_RE = re.compile(r"^context_seen_.+\.json$")
_SAFE_SESSION_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _key(kind: str, id_: str) -> str:
    return f"{kind}:{id_}"


class SeenStore:
    def __init__(self, state_dir: Path, session_id: str) -> None:
        self._dir = state_dir
        safe = _SAFE_SESSION_RE.sub("_", session_id or "unknown")
        self._path = state_dir / f"context_seen_{safe}.json"
        self._data = self._load()

    def _load(self) -> dict:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("seen"), dict):
                return {"turn": int(raw.get("turn") or 0), "seen": raw["seen"]}
        except BaseException:  # noqa: BLE001 - corrupt/missing -> empty
            pass
        return {"turn": 0, "seen": {}}

    def _save(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data), encoding="utf-8")
        except BaseException:  # noqa: BLE001 - best-effort
            pass

    def bump_turn(self) -> int:
        self._data["turn"] = int(self._data.get("turn") or 0) + 1
        self._save()
        return self._data["turn"]

    def filter_unseen(
        self, ids: list[tuple[str, str]], *, reinject_turns: int,
    ) -> list[tuple[str, str]]:
        turn = int(self._data.get("turn") or 0)
        out: list[tuple[str, str]] = []
        for kind, id_ in ids:
            last = self._data["seen"].get(_key(kind, id_))
            if last is None:
                out.append((kind, id_))
            elif reinject_turns > 0 and (turn - int(last)) > reinject_turns:
                out.append((kind, id_))
        return out

    def mark_seen(self, ids: list[tuple[str, str]]) -> None:
        turn = int(self._data.get("turn") or 0)
        for kind, id_ in ids:
            self._data["seen"][_key(kind, id_)] = turn
        self._save()


def prune_stale(state_dir: Path, *, now: datetime, max_age_days: int = 7) -> None:
    """Delete context_seen files older than max_age_days. Never raises."""
    try:
        cutoff = now.timestamp() - max_age_days * 86400
        for f in state_dir.iterdir():
            if _FILE_RE.match(f.name) and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except BaseException:  # noqa: BLE001
        pass
