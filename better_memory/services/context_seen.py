"""Per-session seen-store for contextual memory injection dedup.

Backend-independent and cheap: one small JSON file per session under
``<better-memory home>/state``, deliberately separate from the
``session_memory_exposure`` ledger (which now backs both backends — see
``services/exposure_log.py``) since this dedup state is per-hook-firing
scratch, not a rating record. Never raises: corrupt or unwritable state
degrades to "nothing seen".

File format: ``context_seen_<session_id>.json`` ->
``{"turn": int, "seen": {"<kind>:<id>": last_injected_turn}}``. Writes go
through a temp file + :func:`os.replace` so a concurrent reader never sees
a partial JSON, and mutators re-read the on-disk snapshot before merging
so a second process's writes are not silently clobbered.

Older versions kept a PreToolUse "one firing per session" latch as a
sibling ``context_seen_<session_id>.pretool`` sentinel. The latch is gone
(PreToolUse is trigger-only and fires on every call); :func:`prune_stale`
still sweeps leftover sentinels from upgraded installs.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

# Matches the state JSON, per-memory claim sentinels, legacy pretool
# sentinels, and stray `.tmp` siblings left behind by a process hard-killed
# between mkstemp and os.replace.
_FILE_RE = re.compile(r"^context_seen_.+\.(json|claim|pretool|json\..+\.tmp)$")
_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9_.-]")
_SAFE_SESSION_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _key(kind: str, id_: str) -> str:
    return f"{kind}:{id_}"


class SeenStore:
    def __init__(self, state_dir: Path, session_id: str) -> None:
        self._dir = state_dir
        safe = _SAFE_SESSION_RE.sub("_", session_id or "unknown")
        self._safe = safe
        self._path = state_dir / f"context_seen_{safe}.json"
        self._data = self._load()

    def claim(self, ids: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Atomically claim each (kind, id) for this session; return the ones
        THIS caller won, in input order.

        Hook processes run in parallel (Claude issues parallel tool calls),
        and :meth:`filter_unseen` reads a snapshot taken at process start, so
        two processes can both see a memory as unseen. The claim is an
        ``O_CREAT|O_EXCL`` sentinel per (session, kind, id, epoch):
        ``context_seen_<session>.<kind>-<id>.<epoch>.claim`` where the epoch
        is the turn the memory was last marked seen at in this process's
        snapshot (0 if never). Parallel callers in the same re-inject window
        share a snapshot and therefore a filename, so exactly one wins; once
        ``mark_seen`` records a later turn and the
        ``BETTER_MEMORY_CONTEXT_REINJECT_TURNS`` window re-admits the memory,
        the epoch changes and a fresh claim is possible. Never raises: an
        unwritable state dir claims nothing, so a failure degrades to "do
        not serve" rather than "serve twice".
        """
        won: list[tuple[str, str]] = []
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except BaseException:  # noqa: BLE001 - cannot claim -> serve nothing
            return won
        seen = self._data.get("seen") or {}
        for kind, id_ in ids:
            name = _SAFE_KEY_RE.sub("_", f"{kind}-{id_}")
            epoch = int(seen.get(_key(kind, id_)) or 0)
            sentinel = self._dir / f"context_seen_{self._safe}.{name}.{epoch}.claim"
            try:
                fd = os.open(str(sentinel), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.close(fd)
                won.append((kind, id_))
            except FileExistsError:
                continue
            except BaseException:  # noqa: BLE001 - best-effort; behave as "lost"
                continue
        return won

    def _load(self) -> dict:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("seen"), dict):
                return {
                    "turn": int(raw.get("turn") or 0),
                    "seen": raw["seen"],
                }
        except BaseException:  # noqa: BLE001 - corrupt/missing -> empty
            pass
        return {"turn": 0, "seen": {}}

    def _save(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            # Per-call-unique tmp so concurrent writers to the same
            # session file don't truncate each other's in-flight temp
            # and cause one writer's os.replace to silently publish the
            # other's content (or fail after the other's replace already
            # moved the shared tmp). Mirrors runtime/session_marker.py.
            fd, tmp_name = tempfile.mkstemp(
                prefix=f"{self._path.name}.",
                suffix=".tmp",
                dir=self._dir,
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(self._data))
                os.replace(tmp_path, self._path)
            except BaseException:  # noqa: BLE001
                try:
                    tmp_path.unlink(missing_ok=True)
                except BaseException:  # noqa: BLE001
                    pass
        except BaseException:  # noqa: BLE001 - best-effort
            pass

    def bump_turn(self) -> int:
        # Re-read latest on-disk snapshot so a concurrent process's turn
        # bump isn't silently overwritten by our stale copy.
        self._data = self._load()
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
        # Re-read + merge so a concurrent writer's mark_seen entries are
        # preserved when we write our own back. Stamp new entries with
        # the freshly-read turn (or our own if it's ahead), never our
        # possibly-stale local snapshot — otherwise a concurrent bump_turn
        # between our load and this call would leave the entry stamped
        # with an older turn and filter_unseen would trip its
        # (turn - last) > reinject_turns gate one turn early.
        latest = self._load()
        turn = max(
            int(latest.get("turn") or 0),
            int(self._data.get("turn") or 0),
        )
        merged_seen = dict(latest.get("seen") or {})
        for kind, id_ in ids:
            merged_seen[_key(kind, id_)] = turn
        self._data = {"turn": turn, "seen": merged_seen}
        self._save()


def prune_stale(state_dir: Path, *, now: datetime, max_age_days: int = 7) -> None:
    """Delete context_seen state, claim sentinels and legacy pretool
    sentinels older than max_age_days.

    Never raises.
    """
    try:
        cutoff = now.timestamp() - max_age_days * 86400
        for f in state_dir.iterdir():
            if _FILE_RE.match(f.name) and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except BaseException:  # noqa: BLE001
        pass
