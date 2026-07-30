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

The PreToolUse "one real firing per session" latch is a sibling sentinel
file ``context_seen_<session_id>.pretool`` claimed atomically via
``os.open(..., O_CREAT|O_EXCL)``. Only :meth:`SeenStore.try_claim_pretool_fired`
is race-free; the older read-then-write pair (:meth:`pretool_fired` /
:meth:`mark_pretool_fired`) is retained for compatibility but callers on
the concurrent path must use the atomic claim.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

_FILE_RE = re.compile(r"^context_seen_.+\.(json|pretool)$")
_SAFE_SESSION_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _key(kind: str, id_: str) -> str:
    return f"{kind}:{id_}"


class SeenStore:
    def __init__(self, state_dir: Path, session_id: str) -> None:
        self._dir = state_dir
        safe = _SAFE_SESSION_RE.sub("_", session_id or "unknown")
        self._path = state_dir / f"context_seen_{safe}.json"
        self._sentinel = state_dir / f"context_seen_{safe}.pretool"
        self._data = self._load()

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
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data), encoding="utf-8")
            os.replace(tmp, self._path)
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

    def try_claim_pretool_fired(self) -> bool:
        """Atomic check-and-set on a sentinel file.

        Returns True iff this call created the sentinel (i.e. this process
        is the first firing this session); False if the sentinel already
        exists or the claim could not be established. Safe to race across
        processes: ``O_CREAT|O_EXCL`` guarantees at most one caller sees
        the True return per session.
        """
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                str(self._sentinel),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
            os.close(fd)
            return True
        except FileExistsError:
            return False
        except BaseException:  # noqa: BLE001 - best-effort; behave as "already claimed"
            return False

    def pretool_fired(self) -> bool:
        return self._sentinel.exists()

    def mark_pretool_fired(self) -> None:
        # Retained for compatibility; the sentinel is idempotent so
        # racing callers converge on the same on-disk state, though only
        # try_claim_pretool_fired() distinguishes the winner.
        self.try_claim_pretool_fired()


def prune_stale(state_dir: Path, *, now: datetime, max_age_days: int = 7) -> None:
    """Delete context_seen state / pretool sentinels older than max_age_days.

    Never raises.
    """
    try:
        cutoff = now.timestamp() - max_age_days * 86400
        for f in state_dir.iterdir():
            if _FILE_RE.match(f.name) and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except BaseException:  # noqa: BLE001
        pass
