"""Mutation smoke driver for the e2e clean-slate suite (design section 5, task 13).

Proves ``tests/e2e`` (and its safety harness) still has teeth: each mutation
deliberately breaks one pinned contract inside a throwaway git worktree and
the suite must go RED with the expected sentinel node id in the failure
output while at least one other test still PASSES; after reverting, a final
unpatched control run must be GREEN.

Everything runs in ``git worktree add --detach <scratch>/wt HEAD`` — never in
the live tree (which carries scores of untracked scratch files and other
agents' uncommitted edits). Consequence: results reflect **HEAD**, not the
working tree; uncommitted changes are invisible to this gate.

Mutations (artifacts live in ``tests/e2e_meta/mutations/``):

* M1 ``M1.patch``  — ``better_memory/mcp/server.py``: skip ``apply_migrations``
  for memory.db at boot. Sentinel: ``test_first_boot_migrates_tools_knowledge``.
* M2 ``M2.patch``  — ``better_memory/hooks/session_bootstrap.py``: delete the
  ``write_session_id`` call entirely (the session-id bridge marker is never
  written). Sentinel: ``test_hook_before_server_degraded`` (marker-exists
  assertion — the hoisted-write contract).
* M3 ``M3.patch``  — ``better_memory/hooks/contextual_inject.py``: except path
  exits without printing the envelope. Sentinel:
  ``test_a_config_error_swallowed_envelope_still_printed`` (exactly-one-JSON-line).
* M4 ``M4_seeded_breach.py`` — NOT a patch: a hostile test file dropped in as
  ``tests/e2e/test_zz_seeded_breach.py`` that writes into ``Path.home()``
  (armed via ``BM_E2E_SEEDED_BREACH=1``; refuses to run outside the seeded
  canary home). The canary meta-run must FAIL its sentinel-hash /
  file-set-diff assertions — who watches the watchers.

Usage::

    python scripts/e2e_mutation_smoke.py                 # full gate: M1-M4 + control
    python scripts/e2e_mutation_smoke.py --mutation 2    # one mutation + control
    python scripts/e2e_mutation_smoke.py --mutation 2 --no-control
    python scripts/e2e_mutation_smoke.py --check-only    # CI: patches apply cleanly

Exit code 0 = every executed check passed; 1 = any failure (including a
mutation the suite did NOT catch — that is the whole point of this gate).
"""

from __future__ import annotations

import argparse
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MUTATIONS_DIR = REPO_ROOT / "tests" / "e2e_meta" / "mutations"

#: Per-pytest-run wall clock ceiling. The canary meta-run (M4) spawns an
#: inner pytest with its own 540s timeout; the full tests/e2e control run is
#: the longest leg.
PYTEST_TIMEOUT_SEC = 2400
GIT_TIMEOUT_SEC = 120

#: Driver-owned control variables: never inherited from the outer shell,
#: only set per-mutation (M4 arms the breach through them).
_CONTROL_VARS = ("BM_E2E_SEEDED_BREACH", "BM_E2E_CANARY_INNER_SCOPE", "PYTEST_ADDOPTS")

_FAILED_LINE_RE = re.compile(r"^(?:FAILED|ERROR)\s+\S+", re.MULTILINE)
_PASSED_RE = re.compile(r"(\d+) passed")


@dataclass(frozen=True)
class Mutation:
    num: int
    title: str
    kind: str  # "patch" | "dropin"
    artifact: str  # file name under tests/e2e_meta/mutations/
    #: repo-relative tracked files to revert with ``git checkout --`` (patch
    #: kind) — the dropin kind reverts by deleting ``dropin_dest`` instead.
    targets: tuple[str, ...]
    pytest_args: tuple[str, ...]
    #: The run is judged CAUGHT when ANY of these node-id substrings appears
    #: in a FAILED/ERROR line of the pytest output.
    expected_failed_nodes: tuple[str, ...]
    dropin_dest: str = ""
    extra_env: dict[str, str] = field(default_factory=dict)


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        num=1,
        title="server.py: skip apply_migrations for memory.db at boot",
        kind="patch",
        artifact="M1.patch",
        targets=("better_memory/mcp/server.py",),
        pytest_args=("tests/e2e",),
        expected_failed_nodes=("test_first_boot_migrates_tools_knowledge",),
    ),
    Mutation(
        num=2,
        title="session_bootstrap.py: write_session_id call deleted",
        kind="patch",
        artifact="M2.patch",
        targets=("better_memory/hooks/session_bootstrap.py",),
        pytest_args=("tests/e2e",),
        expected_failed_nodes=("test_hook_before_server_degraded",),
    ),
    Mutation(
        num=3,
        title="contextual_inject.py: except path exits without envelope",
        kind="patch",
        artifact="M3.patch",
        targets=("better_memory/hooks/contextual_inject.py",),
        pytest_args=("tests/e2e",),
        expected_failed_nodes=(
            "test_a_config_error_swallowed_envelope_still_printed",
        ),
    ),
    Mutation(
        num=4,
        title="seeded breach test in tests/e2e — canary harness must catch it",
        kind="dropin",
        artifact="M4_seeded_breach.py",
        targets=(),
        # The sentinel here is the WATCHER, not tests/e2e: the canary
        # meta-run reruns an inner slice (scoped below) under the seeded
        # canary home and must flip red on its integrity assertions.
        pytest_args=("tests/e2e_meta/test_canary_home.py",),
        expected_failed_nodes=(
            "test_sentinel_files_byte_identical",
            "test_file_set_diff_is_empty",
        ),
        dropin_dest="tests/e2e/test_zz_seeded_breach.py",
        extra_env={
            "BM_E2E_SEEDED_BREACH": "1",
            # Reduced inner scope: one honest module (so >=1 inner test
            # passes and the run is not vacuous) + the breach file.
            "BM_E2E_CANARY_INNER_SCOPE": (
                "tests/e2e/test_hooks_contracts.py "
                "tests/e2e/test_zz_seeded_breach.py"
            ),
        },
    ),
)


# --------------------------------------------------------------------------- infra


def _log(msg: str) -> None:
    print(f"[mutation-smoke] {msg}", flush=True)


def _run(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = GIT_TIMEOUT_SEC,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — dev tooling, fixed argv
        cmd,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _git(worktree_or_repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(worktree_or_repo), *args], cwd=REPO_ROOT)


def _pytest_env(mutation: Mutation | None) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.upper() in _CONTROL_VARS:
            del env[key]
    # The worktree must resolve its OWN editable venv — an inherited
    # UV_PROJECT_ENVIRONMENT / VIRTUAL_ENV would silently point `uv run`
    # back at the live repo's install and test unpatched code.
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    env.pop("VIRTUAL_ENV", None)
    if mutation is not None:
        env.update(mutation.extra_env)
    return env


def _scratch_dir() -> Path:
    """A short-lived scratch root for the throwaway worktree.

    On Windows this must NOT live under %TEMP%: Git Bash mounts %TEMP% as
    /tmp, so setup.sh sees the worktree at /tmp/... — a non-drive-letter
    MSYS path that win_path() cannot rewrite, which breaks the two
    test_setup_sh interpreter-path assertions in the control run for
    environment reasons unrelated to HEAD. LOCALAPPDATA itself (the parent
    of %TEMP%) is not mounted at /tmp and keeps paths short.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base and Path(base).is_dir():
            return Path(tempfile.mkdtemp(prefix="bm-mutsmoke-", dir=base))
    return Path(tempfile.mkdtemp(prefix="bm-mutsmoke-"))


def _tail(text: str, lines: int = 30) -> str:
    return "\n".join(text.splitlines()[-lines:])


class MutationSmokeError(Exception):
    """A gate-level failure (setup problem or a mutation the suite missed)."""


# --------------------------------------------------------------------------- worktree


def _add_worktree(scratch: Path) -> Path:
    worktree = scratch / "wt"
    _log(f"git worktree add --detach {worktree} HEAD")
    proc = _git(REPO_ROOT, "worktree", "add", "--detach", str(worktree), "HEAD")
    if proc.returncode != 0:
        raise MutationSmokeError(f"git worktree add failed:\n{proc.stderr}")
    return worktree


def _remove_worktree(worktree: Path) -> None:
    proc = _git(REPO_ROOT, "worktree", "remove", "--force", str(worktree))
    if proc.returncode != 0 and worktree.exists():
        # .venv locks etc. — brute-force the directory, then let git forget it.
        shutil.rmtree(worktree, ignore_errors=True)
        _git(REPO_ROOT, "worktree", "prune")
    if worktree.exists():
        _log(f"WARNING: could not fully remove worktree at {worktree}")


def _assert_worktree_clean(worktree: Path, context: str) -> None:
    """Tracked-file cleanliness gate (judge fix: --untracked-files=no)."""
    proc = _git(worktree, "status", "--porcelain", "--untracked-files=no")
    if proc.returncode != 0:
        raise MutationSmokeError(f"git status failed in worktree:\n{proc.stderr}")
    if proc.stdout.strip():
        raise MutationSmokeError(
            f"worktree dirty {context}:\n{proc.stdout}"
        )


def _require_sentinel_suite(worktree: Path) -> None:
    missing = [
        rel
        for rel in ("tests/e2e", "tests/e2e_meta/test_canary_home.py")
        if not (worktree / rel).exists()
    ]
    if missing:
        raise MutationSmokeError(
            f"missing at HEAD: {missing} — the mutation gate runs against a "
            "worktree of HEAD, so the e2e suite must be committed first"
        )


# --------------------------------------------------------------------------- apply / revert


def _apply(worktree: Path, mutation: Mutation) -> None:
    artifact = MUTATIONS_DIR / mutation.artifact
    if not artifact.is_file():
        raise MutationSmokeError(f"mutation artifact missing: {artifact}")
    if mutation.kind == "patch":
        proc = _git(worktree, "apply", "--whitespace=nowarn", str(artifact))
        if proc.returncode != 0:
            raise MutationSmokeError(
                f"M{mutation.num} failed to apply:\n{proc.stderr}"
            )
    else:  # dropin
        dest = worktree / mutation.dropin_dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(artifact, dest)


def _revert(worktree: Path, mutation: Mutation) -> None:
    if mutation.kind == "patch":
        proc = _git(worktree, "checkout", "--", *mutation.targets)
        if proc.returncode != 0:
            raise MutationSmokeError(
                f"M{mutation.num} revert failed:\n{proc.stderr}"
            )
    else:
        dest = worktree / mutation.dropin_dest
        if dest.exists():
            dest.unlink()
    _assert_worktree_clean(worktree, f"after reverting M{mutation.num}")


# --------------------------------------------------------------------------- pytest


def _pytest(
    worktree: Path, args: tuple[str, ...], mutation: Mutation | None
) -> subprocess.CompletedProcess[str]:
    # ``uv run`` inside the worktree provisions the worktree's own .venv
    # (editable install of the PATCHED sources) — never the live repo's venv,
    # whose editable install points at the unpatched tree.
    cmd = ["uv", "run", "pytest", *args, "-q", "--tb=line", "-p", "no:cacheprovider"]
    _log("  " + " ".join(cmd))
    return _run(
        cmd,
        cwd=worktree,
        env=_pytest_env(mutation),
        timeout=PYTEST_TIMEOUT_SEC,
    )


def _judge_red_run(
    mutation: Mutation, proc: subprocess.CompletedProcess[str]
) -> list[str]:
    """Return a list of problems (empty = the suite caught the mutation)."""
    out = proc.stdout + "\n" + proc.stderr
    problems: list[str] = []
    if proc.returncode == 0:
        problems.append("suite stayed GREEN — mutation not caught")
    if proc.returncode == 5:
        problems.append("exit 5: nothing collected — sentinel run is vacuous")
    failed_lines = _FAILED_LINE_RE.findall(out)
    if not any(
        node in line for line in failed_lines for node in mutation.expected_failed_nodes
    ):
        problems.append(
            "expected sentinel node id not in failure output "
            f"(any of {list(mutation.expected_failed_nodes)}); "
            f"FAILED lines seen: {failed_lines or 'none'}"
        )
    passed = sum(int(n) for n in _PASSED_RE.findall(out))
    if passed < 1:
        problems.append(
            f"no other test passed ({passed} passed) — cannot rule out a "
            "harness-wide breakage masquerading as a caught mutation"
        )
    return problems


# --------------------------------------------------------------------------- modes


def run_check_only(worktree: Path) -> int:
    """CI cheap check: every patch applies cleanly; the dropin compiles."""
    failures = 0
    for mutation in MUTATIONS:
        artifact = MUTATIONS_DIR / mutation.artifact
        if not artifact.is_file():
            _log(f"M{mutation.num}: FAIL — artifact missing: {artifact}")
            failures += 1
            continue
        if mutation.kind == "patch":
            proc = _git(worktree, "apply", "--check", "--whitespace=nowarn", str(artifact))
            if proc.returncode != 0:
                _log(f"M{mutation.num}: FAIL — patch does not apply:\n{proc.stderr}")
                failures += 1
            else:
                _log(f"M{mutation.num}: OK (applies cleanly)")
        else:
            # cfile under the throwaway worktree: never litter the live
            # repo's tests/e2e_meta/mutations/__pycache__.
            cfile = worktree / f".m{mutation.num}-dropin.pyc"
            try:
                py_compile.compile(str(artifact), cfile=str(cfile), doraise=True)
            except py_compile.PyCompileError as exc:
                _log(f"M{mutation.num}: FAIL — dropin does not compile: {exc}")
                failures += 1
            else:
                _log(f"M{mutation.num}: OK (dropin compiles)")
    return failures


def run_mutation(worktree: Path, mutation: Mutation) -> list[str]:
    _log(f"M{mutation.num}: {mutation.title}")
    _apply(worktree, mutation)
    try:
        proc = _pytest(worktree, mutation.pytest_args, mutation)
        problems = _judge_red_run(mutation, proc)
        if problems:
            _log(f"M{mutation.num}: NOT CAUGHT")
            for problem in problems:
                _log(f"  - {problem}")
            _log("  --- pytest tail ---")
            for line in _tail(proc.stdout).splitlines():
                _log(f"  | {line}")
        else:
            _log(f"M{mutation.num}: caught (rc={proc.returncode})")
        return problems
    finally:
        _revert(worktree, mutation)


def run_control(worktree: Path, include_canary: bool) -> list[str]:
    args: tuple[str, ...] = ("tests/e2e",)
    if include_canary:
        # M4's "then green again after removal" leg (design section 5).
        args = ("tests/e2e", "tests/e2e_meta/test_canary_home.py")
    _log("control run (unpatched)")
    proc = _pytest(worktree, args, mutation=None)
    if proc.returncode != 0:
        return [
            f"control run rc={proc.returncode} — unpatched suite is not green:\n"
            f"{_tail(proc.stdout)}\n{_tail(proc.stderr, 10)}"
        ]
    _log("control run: green")
    return []


# --------------------------------------------------------------------------- main


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--mutation",
        type=int,
        choices=sorted(m.num for m in MUTATIONS),
        help="run a single mutation instead of all four",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="only verify the patches apply cleanly at HEAD (no test execution)",
    )
    parser.add_argument(
        "--no-control",
        action="store_true",
        help="skip the final unpatched control run (iteration aid)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    dirty = _git(REPO_ROOT, "status", "--porcelain", "--untracked-files=no")
    if dirty.stdout.strip():
        _log("NOTE: tracked files are modified in the live tree; this gate")
        _log("      runs against HEAD — those modifications are NOT exercised:")
        for line in dirty.stdout.strip().splitlines():
            _log(f"      {line}")

    scratch = _scratch_dir()
    worktree: Path | None = None
    failures: list[str] = []
    try:
        worktree = _add_worktree(scratch)

        if args.check_only:
            return 1 if run_check_only(worktree) else 0

        _require_sentinel_suite(worktree)
        selected = [m for m in MUTATIONS if args.mutation in (None, m.num)]
        for mutation in selected:
            failures.extend(
                f"M{mutation.num}: {problem}"
                for problem in run_mutation(worktree, mutation)
            )
        if not args.no_control:
            failures.extend(
                run_control(worktree, include_canary=any(m.num == 4 for m in selected))
            )
    except MutationSmokeError as exc:
        failures.append(str(exc))
    finally:
        if worktree is not None:
            _remove_worktree(worktree)
        shutil.rmtree(scratch, ignore_errors=True)

    if failures:
        _log(f"RESULT: FAIL ({len(failures)} problem(s))")
        for failure in failures:
            _log(f"  * {failure}")
        return 1
    _log("RESULT: OK — every executed mutation was caught; control green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
