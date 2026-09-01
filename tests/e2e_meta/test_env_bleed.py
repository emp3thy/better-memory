"""Poisoned-shell re-run of a T1 slice (design F: ``meta-env-bleed-poisoned-shell``).

Dev-shell ``BETTER_MEMORY_*`` / ``AWS_*`` / ``OLLAMA_*`` / ``CLAUDE_*``
pollution must not change e2e outcomes. The failure mechanism is real and
verified in source: ``config.py:293-301`` raises ValueError pre-handshake
when ``BETTER_MEMORY_STORAGE_BACKEND=agentcore`` leaks without the ID vars,
so any fixture that reintroduces the ``{**os.environ, ...}`` spawn pattern
(the historical tests/mcp precedent) dies loudly under this poison; a
fixture forgetting to pin ``BETTER_MEMORY_PROJECT`` surfaces as
``poison-project`` scoping failures.

Mechanism: build a maximally hostile outer env ON TOP of the real
``os.environ`` (poison is added, nothing is stripped — that is the point),
then re-run the sqlite-mode T1 slice whose assertions the poisoned
``backend=agentcore`` value would break if any fixture inherited it. A
decoy ``BETTER_MEMORY_HOME`` dir with a hashed sentinel pins location bleed
independently of pass/fail outcomes.

Deviation from the author scenario (documented): HOME/USERPROFILE are
redirected to a neutral tmp home in the hostile env. The scenario's
assertions never involve the real home, and leaving the real HOME in place
would make the inner run's session-scoped ``real_home_canary`` fixture
plant/delete the SAME-named dot-canary files in the real home as an outer
``pytest tests`` session's fixture (both resolve worker id 'main'),
producing a false HARNESS ISOLATION BREACH at outer teardown. The neutral
home removes that collision without weakening any poison assertion.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The T1 sqlite happy-path + hook-contract modules: they assert sqlite-mode
#: behavior that an inherited backend=agentcore poison would break, making
#: them the highest-signal bleed detectors.
INNER_SLICE: tuple[str, ...] = (
    "tests/e2e/test_sqlite_journey.py",
    "tests/e2e/test_hooks_contracts.py",
)

#: Canonical-cased poison values (fixed strings; ``.invalid`` is
#: RFC-2606-reserved so DNS can never resolve; us-gov-west-1 and
#: AWS_PROFILE=poison point at nothing real).
POISON_VARS: dict[str, str] = {
    "BETTER_MEMORY_STORAGE_BACKEND": "agentcore",
    "BETTER_MEMORY_AGENTCORE_SEMANTIC_MEMORY_ID": "POISON-SEM",
    "BETTER_MEMORY_AGENTCORE_EPISODIC_MEMORY_ID": "POISON-EPI",
    "BETTER_MEMORY_AGENTCORE_REGION": "us-gov-west-1",
    "BETTER_MEMORY_PROJECT": "poison-project",
    "BETTER_MEMORY_CONTEXT_INJECT_MODE": "aggressive",
    "OLLAMA_HOST": "http://does-not-exist.invalid:1",
    "CLAUDE_SESSION_ID": "poison-session",
    "AWS_PROFILE": "poison",
    "AWS_DEFAULT_REGION": "us-east-1",
}

#: Poison markers that must never appear in inner output — catches partial
#: bleed where a var leaks into one subprocess's error message even if the
#: tests still pass.
POISON_MARKERS: tuple[str, ...] = ("poison-project", "POISON-SEM")

INNER_TIMEOUT = 420


def _set_ci(env: dict[str, str], key: str, value: str) -> None:
    """Set ``key``, removing case-insensitive duplicates first (Windows env
    keys carry arbitrary case — a naive assignment can leave 'Ollama_Host'
    alive next to 'OLLAMA_HOST')."""
    for existing in [k for k in env if k.upper() == key.upper()]:
        del env[existing]
    env[key] = value


def _poisoned_env(decoy_home: Path, neutral_home: Path) -> dict[str, str]:
    env = dict(os.environ)  # deliberately inherit-everything: worst case
    for key, value in POISON_VARS.items():
        _set_ci(env, key, value)
    _set_ci(env, "BETTER_MEMORY_HOME", str(decoy_home))
    _set_ci(env, "HOME", str(neutral_home))
    _set_ci(env, "USERPROFILE", str(neutral_home))
    if sys.platform == "win32":
        drive, tail = os.path.splitdrive(str(neutral_home))
        if drive:
            _set_ci(env, "HOMEDRIVE", drive)
            _set_ci(env, "HOMEPATH", tail or "\\")
    # PYTEST_ADDOPTS could inject -m filters into the inner run and turn the
    # bleed check vacuous.
    for key in [k for k in env if k.upper() == "PYTEST_ADDOPTS"]:
        del env[key]
    return env


@dataclass(frozen=True)
class BleedRun:
    rc: int
    stdout: str
    stderr: str
    decoy: Path
    sentinel: Path
    sentinel_sha: str


@pytest.fixture(scope="module")
def bleed_run(tmp_path_factory: pytest.TempPathFactory) -> BleedRun:
    decoy = tmp_path_factory.mktemp("decoy-bm-home")
    sentinel = decoy / "sentinel.txt"
    sentinel.write_text(f"decoy sentinel {uuid.uuid4()}\n", encoding="utf-8")
    sentinel_sha = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    neutral_home = tmp_path_factory.mktemp("neutral-home")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            *INNER_SLICE,
        ],
        cwd=str(REPO_ROOT),
        env=_poisoned_env(decoy, neutral_home),
        capture_output=True,
        text=True,
        timeout=INNER_TIMEOUT,
    )
    return BleedRun(
        rc=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        decoy=decoy,
        sentinel=sentinel,
        sentinel_sha=sentinel_sha,
    )


def _tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.splitlines()[-lines:])


def test_inner_slice_not_vacuous(bleed_run: BleedRun) -> None:
    assert bleed_run.rc != 5, (
        f"{list(INNER_SLICE)} collected nothing — bleed check is vacuous\n"
        f"{_tail(bleed_run.stdout)}"
    )


def test_inner_slice_green_under_poisoned_shell(bleed_run: BleedRun) -> None:
    """sqlite-mode tests still pass with backend=agentcore + POISON ids +
    aggressive inject mode in the outer shell — proving every fixture
    constructs its env from the explicit allowlist rather than inheriting."""
    assert bleed_run.rc == 0, (
        f"inner pytest rc={bleed_run.rc}\n"
        f"--- stdout tail ---\n{_tail(bleed_run.stdout)}\n"
        f"--- stderr tail ---\n{_tail(bleed_run.stderr)}"
    )
    assert "passed" in bleed_run.stdout, _tail(bleed_run.stdout)


def test_decoy_home_untouched(bleed_run: BleedRun) -> None:
    """No test used the outer BETTER_MEMORY_HOME's location: the decoy still
    contains exactly its one sentinel file with an unchanged hash."""
    files = [p for p in bleed_run.decoy.rglob("*")]
    assert files == [bleed_run.sentinel], (
        f"decoy BETTER_MEMORY_HOME was touched: {[str(p) for p in files]}"
    )
    assert hashlib.sha256(bleed_run.sentinel.read_bytes()).hexdigest() == bleed_run.sentinel_sha


def test_no_poison_strings_in_inner_output(bleed_run: BleedRun) -> None:
    combined = bleed_run.stdout + bleed_run.stderr
    leaked = [marker for marker in POISON_MARKERS if marker in combined]
    assert leaked == [], (
        f"poison values bled into inner-run output: {leaked}\n{_tail(combined, 60)}"
    )
