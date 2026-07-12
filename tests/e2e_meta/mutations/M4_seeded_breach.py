"""Mutation M4: hostile drop-in test — who watches the watchers.

This is NOT a patch. ``scripts/e2e_mutation_smoke.py`` copies this file to
``tests/e2e/test_zz_seeded_breach.py`` inside a throwaway git worktree, arms
it with ``BM_E2E_SEEDED_BREACH=1``, and points the canary meta-run
(``tests/e2e_meta/test_canary_home.py``) at it via
``BM_E2E_CANARY_INNER_SCOPE``. The canary harness must then FAIL its
sentinel-hash / file-set-diff assertions (the isolation-breach messages
naming ``.claude.json`` and ``.better-memory/leak.txt``). If the harness
stays green with this file in the suite, the safety net is decorative —
that is the design §5 M4 verdict.

Safety guards (belt and braces — this test must NEVER touch a real home):

1. Skips unless ``BM_E2E_SEEDED_BREACH=1``, which only the mutation driver
   sets. Someone running ``pytest tests/e2e`` in a worktree where the file
   was accidentally left behind gets a skip, not a breach.
2. Skips unless ``Path.home()/.claude.json`` parses as JSON and carries the
   ``__canary`` marker key seeded by
   ``tests/e2e_meta/test_canary_home.py::_seed_canary_home`` — so even an
   armed run refuses to write into anything but the seeded canary home.

Note: this file deliberately does NOT use ``tests.e2e._env.isolated_env`` —
it runs *inside* the canary's inner pytest (whose HOME already points at the
canary) and its entire purpose is to escape per-test isolation the way a
buggy fixture would.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def test_zz_seeded_breach_writes_into_home() -> None:
    """Deliberately corrupt the (canary) home so the harness must catch it."""
    if os.environ.get("BM_E2E_SEEDED_BREACH") != "1":
        pytest.skip("seeded breach not armed (BM_E2E_SEEDED_BREACH != 1)")

    home = Path.home()
    claude_json = home / ".claude.json"
    if not claude_json.is_file():
        pytest.skip("no ~/.claude.json — refusing to breach a non-canary home")
    try:
        data = json.loads(claude_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pytest.skip("~/.claude.json unreadable — refusing to breach")
    if not isinstance(data, dict) or "__canary" not in data:
        pytest.skip("home is not the seeded canary — refusing to breach a real home")

    # Breach 1: mutate a sha256-pinned sentinel file
    # (-> test_sentinel_files_byte_identical must fail naming .claude.json).
    data["__breach"] = "M4 seeded breach"
    claude_json.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Breach 2: drop a leak artifact under the canary's .better-memory tree
    # (-> test_file_set_diff_is_empty must fail on the created path).
    leak_dir = home / ".better-memory"
    leak_dir.mkdir(exist_ok=True)
    (leak_dir / "leak.txt").write_text("M4 seeded breach\n", encoding="utf-8")

    # The breach test itself PASSES — the canary harness, not this test,
    # must be the thing that goes red.
    assert (leak_dir / "leak.txt").is_file()
