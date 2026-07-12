"""Marker / tier wiring (design F: meta-marker-tier-wiring).

Pins the collection contract that keeps the default run hermetic:

* plain ``pytest`` collects T1+T2 (tests/e2e) and ZERO nodes from the live
  T3 module — the default run can never touch real AWS;
* the ``e2e`` marker is declared in pyproject (no unknown-mark warning);
* ``-m integration`` without ``BETTER_MEMORY_TEST_AGENTCORE=1`` goes
  green-**skipped** — never red, never exit-5 no-tests-collected — matching
  tests/integration/conftest.py's ``pytest.skip`` gate.

Each check spawns a real pytest subprocess with an ``isolated_env``-built
environment, so a dev box with ``BETTER_MEMORY_TEST_AGENTCORE=1`` (or real
AWS creds) exported cannot turn the gate check into a live AWS run.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.e2e._env import isolated_env

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_DIR = REPO_ROOT / "tests" / "e2e"
#: The T3 live-AWS module (design task 12; may not exist yet mid-phase).
LIVE_T3 = REPO_ROOT / "tests" / "integration" / "test_agentcore_live_e2e.py"
#: Existing fully integration-marked module using the same skip gate —
#: fallback target until the T3 module lands.
ROUNDTRIP = REPO_ROOT / "tests" / "integration" / "test_agentcore_roundtrip.py"


def _run_pytest(
    args: list[str], tmp_path: Path, *, timeout: float = 240
) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "pytest-home"
    home.mkdir(exist_ok=True)
    # isolated_env strips BETTER_MEMORY_TEST_AGENTCORE(+_KEEP), all AWS_*
    # creds and PYTEST_ADDOPTS — the spawned pytest sees only pyproject
    # defaults and can never reach live AWS.
    env = isolated_env(home)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _node_ids(stdout: str) -> list[str]:
    return [line.strip() for line in stdout.splitlines() if "::" in line]


def _e2e_has_test_modules() -> bool:
    return any(E2E_DIR.glob("test_*.py"))


def test_default_collection_excludes_live_t3(tmp_path: Path) -> None:
    """Plain pytest (default addopts) on tests/e2e + tests/integration must
    never pick up a node from the live-AWS T3 module."""
    proc = _run_pytest(["--collect-only", "-q", "tests/e2e", "tests/integration"], tmp_path)
    assert proc.returncode in (0, 5), proc.stdout + proc.stderr
    nodes = _node_ids(proc.stdout)
    live_nodes = [n for n in nodes if "test_agentcore_live_e2e" in n]
    assert live_nodes == [], f"T3 live nodes leaked into the default run: {live_nodes}"
    # The fully integration-marked roundtrip module must be deselected too.
    marked_nodes = [n for n in nodes if "test_agentcore_roundtrip" in n]
    assert marked_nodes == [], f"integration-marked nodes in default run: {marked_nodes}"
    if _e2e_has_test_modules():
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert any(n.startswith("tests/e2e/") for n in nodes), (
            f"no tests/e2e nodes in default collection:\n{proc.stdout}"
        )


def test_e2e_marker_is_declared(tmp_path: Path) -> None:
    """`pytest --markers` is pytest's own view of declared markers — red the
    moment someone deletes the pyproject declaration."""
    proc = _run_pytest(["--markers"], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "e2e: end-to-end clean-slate smoke tests (hermetic)" in proc.stdout
    # The pre-existing marker must survive the addition.
    assert "integration:" in proc.stdout


def test_e2e_marked_collection_emits_no_unknown_mark_warning(tmp_path: Path) -> None:
    proc = _run_pytest(["--collect-only", "-q", "-m", "e2e", "tests/e2e"], tmp_path)
    combined = proc.stdout + proc.stderr
    assert "PytestUnknownMarkWarning" not in combined, combined
    assert "Unknown pytest.mark.e2e" not in combined, combined
    assert proc.returncode in (0, 5), combined
    if _e2e_has_test_modules():
        # conftest auto-marking guarantees every tests/e2e test carries e2e.
        assert proc.returncode == 0, combined
        assert len(_node_ids(proc.stdout)) > 0, combined


def test_t3_nodes_reachable_under_integration_marker(tmp_path: Path) -> None:
    """T3 exists and is collectable when explicitly requested (step 3)."""
    if not LIVE_T3.exists():
        pytest.skip("tests/integration/test_agentcore_live_e2e.py not implemented yet (task 12)")
    proc = _run_pytest(
        [
            "--collect-only",
            "-q",
            "-m",
            "integration",
            "--override-ini",
            "addopts=",
            LIVE_T3.relative_to(REPO_ROOT).as_posix(),
        ],
        tmp_path,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(_node_ids(proc.stdout)) > 0, proc.stdout


def test_integration_without_env_gate_is_green_skipped(tmp_path: Path) -> None:
    """`-m integration` on a credential-less box must SKIP, never fail and
    never exit-5: the gate must stay a pytest.skip (an assert-based refactor
    flips this red on every machine without AWS)."""
    target = LIVE_T3 if LIVE_T3.exists() else ROUNDTRIP
    proc = _run_pytest(
        [
            "-q",
            "-m",
            "integration",
            "--override-ini",
            "addopts=",
            target.relative_to(REPO_ROOT).as_posix(),
        ],
        tmp_path,
        timeout=300,
    )
    out = proc.stdout + proc.stderr
    # rc 0: not red (gate failed), not exit-5 (vacuously collected nothing).
    assert proc.returncode == 0, out
    summary = next(
        (
            line
            for line in reversed(proc.stdout.splitlines())
            if re.search(r"\d+ (skipped|passed|failed|error)", line)
        ),
        "",
    )
    assert re.search(r"\d+ skipped", summary), f"expected skips in summary: {summary!r}\n{out}"
    assert "failed" not in summary, summary
    assert "error" not in summary, summary
