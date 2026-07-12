"""E2E installer scenarios A1-A7 (design catalog section 1.A, T1).

Subprocess-level tests of ``python -m better_memory.cli.install_hooks``
against a truly clean-slate fake home built by ``isolated_env``. Unlike the
unit-level ``tests/cli/test_install_hooks.py`` (whose ``mock_home`` fixture
pre-creates ``.claude/`` and ``install-backups/``), these exercise the real
brand-new-user path: ``_atomic_write``'s parent mkdir,
``_resolve_user_skills_dir``'s mkdir, and the full argv -> argparse -> merge
-> atomic-write pipeline in a separate process.

Scenario map (ids from ``2026-07-12-e2e-clean-slate-smoke-design.md``):

* A1 ``e2e-install-fresh-clean-slate``        -> test_fresh_clean_slate_writes_exact_shapes
* A2 ``e2e-install-idempotent-rerun``          -> test_idempotent_rerun_byte_identical_no_dup
* A3 ``e2e-install-foreign-config-preserved``  -> test_foreign_config_preserved_and_legacy_scrubbed
* A4 ``e2e-install-malformed-claude-json-refused``
                                               -> test_malformed_claude_json_refuses_whole_install
* A5 ``e2e-install-backup-before-overwrite``   -> test_backup_before_overwrite
* A6 ``e2e-install-symlink-oserror-fallback``  -> test_symlink_oserror_fallback_warns_and_continues
* A7 ``e2e-install-symlink-replacement-ladder``-> test_symlink_replacement_ladder / _noop

Interpreter paths deliberately contain an embedded space: the quoting in
``_hook_entry`` (``"{interpreter}" -m {module}``) is load-bearing for every
venv path with a space, and no other test in the repo passes one.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from better_memory.cli import install_hooks as installer_module
from tests.e2e._env import isolated_env

# Embedded space is load-bearing: dropping the quotes in _hook_entry would
# pass any test using a space-free interpreter path. Values are never
# executed - the installer only serializes them into config.
VENV_PY = "C:/fake venv/python.exe"
VENV_PYW = "C:/fake venv/pythonw.exe"

#: The exact source dir the installer symlinks from (mirrors
#: install_skill_symlinks' parents[2] resolution so the assertion cannot
#: drift from the implementation's own notion of "the repo").
REPO_SKILLS_DIR = (
    Path(installer_module.__file__).resolve().parents[2] / ".claude" / "skills"
)

SKILL_NAMES = ("better-memory-synthesize", "rate-session-memories")

#: (event, module, expected async, expected group matcher) for all 5 managed
#: entries. Interpreter selection: needs_stdout hooks get VENV_PY, the rest
#: VENV_PYW (pythonw would silently null stdout and kill additionalContext).
EXPECTED_ENTRIES: dict[str, tuple[str, str, bool, str | None]] = {
    "SessionStart": ("better_memory.hooks.session_bootstrap", VENV_PY, False, None),
    "PostToolUse": ("better_memory.hooks.observer", VENV_PYW, True, "Write|Edit|Bash"),
    "Stop": ("better_memory.hooks.session_close", VENV_PYW, True, None),
    "UserPromptSubmit": ("better_memory.hooks.contextual_inject", VENV_PY, False, None),
    "PreToolUse": ("better_memory.hooks.contextual_inject", VENV_PY, False, "Skill|Task|Write"),
}


@dataclass
class InstallHarness:
    """One clean-slate fake home plus everything needed to run the installer."""

    home: Path              # the fake user home (empty at construction)
    tmp: Path               # pytest tmp_path - side artifacts live here
    env: dict[str, str]     # isolated_env-built child environment
    home_arg: str           # exact --home string passed (== BETTER_MEMORY_HOME)

    @property
    def claude_json(self) -> Path:
        return self.home / ".claude.json"

    @property
    def settings_json(self) -> Path:
        return self.home / ".claude" / "settings.json"

    @property
    def skills_dir(self) -> Path:
        return self.home / ".claude" / "skills"

    @property
    def backups_dir(self) -> Path:
        return self.home / ".better-memory" / "install-backups"

    def run(self) -> subprocess.CompletedProcess[str]:
        """One real installer run: python -m better_memory.cli.install_hooks."""
        return _spawn(
            self.env,
            [sys.executable, "-m", "better_memory.cli.install_hooks", *self.installer_args],
        )

    @property
    def installer_args(self) -> list[str]:
        return [
            "--venv-py", VENV_PY,
            "--venv-pyw", VENV_PYW,
            "--home", self.home_arg,
        ]


def _spawn(
    env: dict[str, str], argv: list[str], *, timeout: float = 60
) -> subprocess.CompletedProcess[str]:
    """The module's single spawn site. ``env`` MUST come from isolated_env
    (enforced by tests/e2e_meta/test_env_helper_contract.py contract C)."""
    return subprocess.run(  # noqa: S603 - test harness, fixed argv
        argv, env=env, capture_output=True, text=True, timeout=timeout
    )


@pytest.fixture
def harness(clean_slate_home: Path, tmp_path: Path) -> InstallHarness:
    home_arg = str(clean_slate_home / ".better-memory")
    return InstallHarness(
        home=clean_slate_home,
        tmp=tmp_path,
        env=isolated_env(clean_slate_home),
        home_arg=home_arg,
    )


# --------------------------------------------------------------------- helpers


def bm_hook_entries(settings: dict) -> list[tuple[str, dict, dict]]:
    """Every (event, group, entry) whose command references a bm hook module.

    Subset-tolerant by construction: foreign events/groups/entries are walked
    but only better-memory module commands are collected.
    """
    found: list[tuple[str, dict, dict]] = []
    for event, groups in settings.get("hooks", {}).items():
        for group in groups:
            for entry in group.get("hooks", []):
                if "better_memory.hooks." in entry.get("command", ""):
                    found.append((event, group, entry))
    return found


def assert_managed_shapes(settings: dict) -> None:
    """The full 5-entry contract: distribution, quoting, py/pyw split,
    async flags, and matchers."""
    entries = bm_hook_entries(settings)
    assert len(entries) == 5, (
        f"expected exactly 5 better-memory hook entries, got {len(entries)}: "
        f"{[(e, en['command']) for e, _, en in entries]}"
    )
    by_event = {event: (group, entry) for event, group, entry in entries}
    assert set(by_event) == set(EXPECTED_ENTRIES), (
        "expected exactly one better-memory entry per event in "
        f"{sorted(EXPECTED_ENTRIES)}, got {sorted(e for e, _, _ in entries)}"
    )
    for event, (module, interpreter, is_async, matcher) in EXPECTED_ENTRIES.items():
        group, entry = by_event[event]
        assert entry["type"] == "command"
        # Quoted interpreter with the embedded space intact, exact format.
        assert entry["command"] == f'"{interpreter}" -m {module}', (
            f"{event}: bad command {entry['command']!r}"
        )
        if is_async:
            assert entry["async"] is True, f"{event}: async flag missing"
        else:
            assert "async" not in entry, f"{event}: unexpected async flag"
        if matcher is None:
            assert "matcher" not in group, f"{event}: unexpected matcher"
        else:
            assert group["matcher"] == matcher, f"{event}: bad matcher"


def assert_mcp_server_shape(claude_config: dict, harness: InstallHarness) -> None:
    bm = claude_config["mcpServers"]["better-memory"]
    assert bm["type"] == "stdio"
    assert bm["command"] == VENV_PY
    assert bm["args"] == ["-m", "better_memory.mcp"]
    assert bm["env"]["BETTER_MEMORY_HOME"] == harness.home_arg


def can_symlink(base: Path) -> bool:
    """Runtime capability probe: True iff this process may create dir
    symlinks (POSIX; Windows with Developer Mode or elevation)."""
    probe_target = base / "symlink-probe-target"
    probe_target.mkdir(exist_ok=True)
    probe_link = base / "symlink-probe-link"
    try:
        probe_link.symlink_to(probe_target, target_is_directory=True)
    except OSError:
        return False
    probe_link.unlink()
    return True


# ------------------------------------------------------ A1: fresh clean slate


def test_fresh_clean_slate_writes_exact_shapes(harness: InstallHarness) -> None:
    """A1 ``e2e-install-fresh-clean-slate``.

    Truly clean slate (no .claude/, no .claude.json, no .better-memory):
    one subprocess run writes both targets with the exact managed shapes and
    zero side writes. Traps: needs_stdout interpreter swap (pythonw silently
    kills additionalContext), dropped interpreter quoting, missing
    UserPromptSubmit/PreToolUse merge branches, the installer growing a
    DB-creating side effect.
    """
    # Preconditions: nothing pre-created.
    assert list(harness.home.iterdir()) == []
    symlinks_expected = can_symlink(harness.tmp)

    proc = harness.run()

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("(created fresh)") == 2, proc.stdout
    assert "[install_hooks] Restart Claude Code" in proc.stdout

    # .claude.json subset (foreign keys tolerated - none here, but the
    # assertion never enumerates the full key set).
    claude_config = json.loads(harness.claude_json.read_text(encoding="utf-8"))
    assert_mcp_server_shape(claude_config, harness)

    # settings.json: the full 5-entry contract.
    settings = json.loads(harness.settings_json.read_text(encoding="utf-8"))
    assert_managed_shapes(settings)

    # Zero DB / runtime side writes: the installer must never touch storage.
    assert list(harness.home.rglob("memory.db")) == []
    assert list(harness.home.rglob("knowledge.db")) == []
    # Nothing to back up on a clean slate -> _backup no-ops, dir not created.
    assert not harness.backups_dir.exists()
    # No _atomic_write litter next to either target.
    assert list(harness.home.rglob("*.tmp")) == []

    # Skill symlinks, capability-probed on both branches.
    if symlinks_expected:
        for name in SKILL_NAMES:
            link = harness.skills_dir / name
            assert link.is_symlink(), f"{name} not symlinked"
            assert link.resolve() == (REPO_SKILLS_DIR / name).resolve()
        assert "WARN skill symlink skipped" not in proc.stderr
    else:
        # Locked-down host: the documented degraded path must have fired.
        assert "WARN skill symlink skipped" in proc.stderr


# --------------------------------------------------- A2: idempotent re-runs


def test_idempotent_rerun_byte_identical_no_dup(harness: InstallHarness) -> None:
    """A2 ``e2e-install-idempotent-rerun``.

    Runs 2 and 3 are byte-identical for BOTH targets; the managed entry
    count stays exactly 5. Traps: REMOVE-pass predicate drift (exact-match
    vs the module-path substring at install_hooks.py:167) appending duplicate
    hook groups per re-run; serialization drift (sort_keys/indent changes).
    """
    proc1 = harness.run()
    assert proc1.returncode == 0, proc1.stderr
    claude_1 = harness.claude_json.read_bytes()
    settings_1 = harness.settings_json.read_bytes()

    proc2 = harness.run()
    assert proc2.returncode == 0, proc2.stderr
    claude_2 = harness.claude_json.read_bytes()
    settings_2 = harness.settings_json.read_bytes()

    proc3 = harness.run()
    assert proc3.returncode == 0, proc3.stderr
    claude_3 = harness.claude_json.read_bytes()
    settings_3 = harness.settings_json.read_bytes()

    # Byte identity across runs 1-3 for both targets. Three runs catch
    # once-stable-then-drifting bugs a single re-run misses.
    assert settings_2 == settings_1
    assert settings_3 == settings_2
    assert claude_2 == claude_1
    assert claude_3 == claude_2

    # Still exactly 5 managed entries - no duplicate groups accumulated.
    settings = json.loads(settings_3.decode("utf-8"))
    assert len(bm_hook_entries(settings)) == 5

    # Re-runs back up the previous run's output instead of "(created fresh)".
    assert "(created fresh)" not in proc2.stdout
    assert proc2.stdout.count("(backup:") == 2, proc2.stdout

    # At least one settings backup snapshots a state that already contained
    # the 5 managed entries (run 1's output). "At least one", never exact
    # counts: the 1-second timestamp granularity makes same-second re-runs
    # overwrite the same .bak name.
    settings_baks = list(harness.backups_dir.glob("settings.json.*.bak"))
    assert settings_baks, "no settings.json backups written on re-run"
    assert any(
        len(bm_hook_entries(json.loads(bak.read_text(encoding="utf-8")))) == 5
        for bak in settings_baks
    )


# ------------------------------- A3: foreign config preserved + legacy scrub


def test_foreign_config_preserved_and_legacy_scrubbed(
    harness: InstallHarness,
) -> None:
    """A3 ``e2e-install-foreign-config-preserved``.

    Seeded foreign servers/hooks/top-level keys and the user's custom
    BETTER_MEMORY_HOME survive; the command path is refreshed; legacy
    ``session_start``/``session_retrieve`` entries are scrubbed while
    co-resident user hooks in the same group survive (legacy seeds folded in
    per the judge round - no standalone legacy scenario). Traps:
    ``env.setdefault`` -> unconditional assignment (repoints the user's data
    dir), dict rebuild dropping ``model``/foreign events,
    ``_LEGACY_HOOK_MODULES`` deletion, group-level (vs entry-level) removal.
    """
    seeded_other_server = {"type": "stdio", "command": "/x"}
    seeded_top_level = {"a": 1}
    harness.claude_json.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "other-server": seeded_other_server,
                    "better-memory": {
                        "type": "stdio",
                        "command": "/stale/old-python",
                        "args": ["-m", "better_memory.mcp"],
                        "env": {
                            "BETTER_MEMORY_HOME": "D:/custom/bm-home",
                            "EXTRA_VAR": "keep-me",
                        },
                    },
                },
                "someOtherTopLevel": seeded_top_level,
            }
        ),
        encoding="utf-8",
    )
    seeded_precompact_group = {
        "hooks": [{"type": "command", "command": "echo foreign-event"}],
    }
    harness.settings_json.parent.mkdir(parents=True)
    harness.settings_json.write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {
                    # Legacy bm entry co-resident with a user hook: the
                    # REMOVE pass must strip per-entry, not per-group.
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": '"/old/python" -m better_memory.hooks.session_start',
                                },
                                {"type": "command", "command": "echo user-owned-hook"},
                            ],
                        },
                    ],
                    # Legacy-only group: emptied by removal -> dropped.
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        '"/old/python" -m '
                                        "better_memory.hooks.session_retrieve"
                                    ),
                                },
                            ],
                        },
                    ],
                    # Stale current-module entry: refreshed via remove+add.
                    "PostToolUse": [
                        {
                            "matcher": "Write|Edit|Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": '"/older/pythonw" -m better_memory.hooks.observer',
                                },
                            ],
                        },
                    ],
                    # Foreign event the installer knows nothing about.
                    "PreCompact": [seeded_precompact_group],
                },
            }
        ),
        encoding="utf-8",
    )

    proc = harness.run()
    assert proc.returncode == 0, proc.stderr

    claude_config = json.loads(harness.claude_json.read_text(encoding="utf-8"))
    # Foreign server + foreign top-level key deep-equal their seeds.
    assert claude_config["mcpServers"]["other-server"] == seeded_other_server
    assert claude_config["someOtherTopLevel"] == seeded_top_level
    bm = claude_config["mcpServers"]["better-memory"]
    # User's custom home WINS over --home (setdefault semantics)...
    assert bm["env"]["BETTER_MEMORY_HOME"] == "D:/custom/bm-home"
    assert bm["env"]["EXTRA_VAR"] == "keep-me"
    # ...while the command path is refreshed from the stale seed.
    assert bm["command"] == VENV_PY

    settings = json.loads(harness.settings_json.read_text(encoding="utf-8"))
    # Top-level foreign key + foreign event survive dict(existing).
    assert settings["model"] == "opus"
    assert settings["hooks"]["PreCompact"] == [seeded_precompact_group]

    # Legacy scrub: no command anywhere references a legacy module. (Safe
    # against canonical entries: 'session_start' is not a substring of
    # 'session_bootstrap'.)
    all_commands = [
        entry.get("command", "")
        for groups in settings["hooks"].values()
        for group in groups
        for entry in group.get("hooks", [])
    ]
    assert not any("better_memory.hooks.session_start" in c for c in all_commands)
    assert not any("better_memory.hooks.session_retrieve" in c for c in all_commands)

    # Co-resident user hook survived the per-entry strip; its group no
    # longer carries the legacy entry; the canonical group was appended
    # alongside -> exactly 2 SessionStart groups.
    session_start_groups = settings["hooks"]["SessionStart"]
    assert len(session_start_groups) == 2, session_start_groups
    user_groups = [
        g
        for g in session_start_groups
        if any(e["command"] == "echo user-owned-hook" for e in g["hooks"])
    ]
    assert len(user_groups) == 1
    assert user_groups[0]["hooks"] == [
        {"type": "command", "command": "echo user-owned-hook"}
    ]

    # Legacy-only UserPromptSubmit group dropped; exactly the canonical one.
    upsubmit_groups = settings["hooks"]["UserPromptSubmit"]
    assert len(upsubmit_groups) == 1, upsubmit_groups

    # Stale observer refreshed: exactly one observer entry, current VENV_PYW.
    observer_commands = [c for c in all_commands if "better_memory.hooks.observer" in c]
    assert observer_commands == [f'"{VENV_PYW}" -m better_memory.hooks.observer']
    assert not any("/older/pythonw" in c for c in all_commands)

    # Full canonical shape holds on top of the preserved foreign config.
    assert_managed_shapes(settings)


# --------------------------------------- A4: malformed .claude.json refused


def test_malformed_claude_json_refuses_whole_install(
    harness: InstallHarness,
) -> None:
    """A4 ``e2e-install-malformed-claude-json-refused``.

    Malformed ``~/.claude.json`` (the FIRST target - the direction existing
    unit tests don't cover; they seed malformed settings.json): exit 1,
    path+lineno+remediation on stderr, BOTH files byte-unchanged, no
    backups, no ``*.tmp`` litter. Pins the swallow-and-treat-as-``{}``
    hazard: a catch-JSONDecodeError-continue-with-``{}`` change would
    atomically replace the user's entire MCP config.
    """
    malformed = b'{"mcpServers": {broken'
    harness.claude_json.write_bytes(malformed)
    harness.settings_json.parent.mkdir(parents=True)
    valid_settings = b'{"hooks": {}}'
    harness.settings_json.write_bytes(valid_settings)

    proc = harness.run()

    assert proc.returncode == 1
    # Actionable remediation: <path>:<lineno>: <msg> + fix-then-re-run.
    assert re.search(
        rf"{re.escape(str(harness.claude_json))}:\d+:", proc.stderr
    ), proc.stderr
    assert "Fix the file then re-run" in proc.stderr

    # The malformed file was NOT replaced with a minimal managed config.
    assert harness.claude_json.read_bytes() == malformed
    # The VALID second target was not merged either: validation of ALL
    # targets precedes ANY write (install_hooks.py main() two-pass loop).
    assert harness.settings_json.read_bytes() == valid_settings

    # No backup taken on a refused install; no atomic-write litter.
    assert not harness.backups_dir.exists()
    assert list(harness.home.rglob("*.tmp")) == []

    # Deliberate NON-assertion: <home>/.claude/skills MAY exist -
    # install_skill_symlinks() runs before validation. Pinning only the two
    # JSON targets keeps this honest on symlink-capable and locked-down
    # hosts alike.


# ------------------------------------------- A5: backup before overwrite


def test_backup_before_overwrite(harness: InstallHarness) -> None:
    """A5 ``e2e-install-backup-before-overwrite``.

    Backups land in ``$BETTER_MEMORY_HOME/install-backups/`` with
    timestamped names containing the PRE-run bytes; the dir is auto-created.
    Trap: moving the ``_backup`` call after ``_atomic_write`` (backup-of-
    result destroys the user's only rollback artifact), and drift in the
    backup location/name format that docs point users at.
    """
    claude_seed = b'{"mcpServers": {"sentinel": {"command": "/s"}}}'
    settings_seed = (
        b'{"hooks": {"Stop": [{"hooks": '
        b'[{"type": "command", "command": "echo sentinel-stop"}]}]}}'
    )
    harness.claude_json.write_bytes(claude_seed)
    harness.settings_json.parent.mkdir(parents=True)
    harness.settings_json.write_bytes(settings_seed)
    assert not harness.backups_dir.exists()  # precondition: auto-creation

    proc = harness.run()

    assert proc.returncode == 0, proc.stderr
    # Relative-path rendering; the prefix is separator-agnostic (the sep
    # comes after the matched substring on both OSes).
    assert proc.stdout.count("(backup: install-backups") == 2, proc.stdout

    # install-backups auto-created (mkdir parents in _backup); exactly 2
    # backups - one per pre-existing target, single run so the 1-second
    # timestamp cannot collide.
    assert harness.backups_dir.is_dir()
    backups = sorted(p.name for p in harness.backups_dir.iterdir())
    assert len(backups) == 2, backups
    claude_baks = [n for n in backups if re.fullmatch(r"\.claude\.json\.\d{8}-\d{6}\.bak", n)]
    settings_baks = [n for n in backups if re.fullmatch(r"settings\.json\.\d{8}-\d{6}\.bak", n)]
    assert len(claude_baks) == 1 and len(settings_baks) == 1, backups

    # Backups contain the PRE-run bytes exactly - captured before merge,
    # never the merged output.
    assert (harness.backups_dir / claude_baks[0]).read_bytes() == claude_seed
    assert (harness.backups_dir / settings_baks[0]).read_bytes() == settings_seed

    # And the live files now hold sentinel content AND managed entries -
    # proving backup+merge+write ordering, not backup-of-result.
    claude_config = json.loads(harness.claude_json.read_text(encoding="utf-8"))
    assert claude_config["mcpServers"]["sentinel"] == {"command": "/s"}
    assert_mcp_server_shape(claude_config, harness)
    settings = json.loads(harness.settings_json.read_text(encoding="utf-8"))
    stop_commands = [
        e["command"] for g in settings["hooks"]["Stop"] for e in g["hooks"]
    ]
    assert "echo sentinel-stop" in stop_commands
    assert_managed_shapes(settings)


# ------------------------------------- A6: symlink OSError(1314) fallback


_SYMLINK_DENIED_DRIVER = """\
import pathlib
import sys


def _deny(self, *args, **kwargs):
    raise OSError(1314, "A required privilege is not held by the client")


# Patch BEFORE importing the installer: link.symlink_to resolves through the
# class, so every skill's symlink attempt raises the Windows no-Developer-
# Mode error deterministically on every host (incl. POSIX and Dev-Mode
# Windows, where a real symlink would succeed and skip the branch).
pathlib.Path.symlink_to = _deny

from better_memory.cli.install_hooks import main

main(sys.argv[1:])
"""


def test_symlink_oserror_fallback_warns_and_continues(
    harness: InstallHarness,
) -> None:
    """A6 ``e2e-install-symlink-oserror-fallback``.

    Driver script patches ``Path.symlink_to`` to raise ``OSError(1314)``
    (Windows-without-Developer-Mode simulation): exit 0, one WARN per skill
    plus the Developer Mode remediation on stderr, both configs still fully
    written. Trap: narrowing/removing the ``except OSError`` - symlinks run
    BEFORE the JSON writes, so an unhandled raise is a total install failure
    for exactly the locked-down-Windows population the fallback serves.
    """
    driver = harness.tmp / "symlink_denied_driver.py"  # outside the fake home
    driver.write_text(_SYMLINK_DENIED_DRIVER, encoding="utf-8")

    proc = _spawn(
        harness.env,
        [sys.executable, str(driver), *harness.installer_args],
    )

    # Symlink failure is non-fatal: the core install completes.
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.count("WARN skill symlink skipped") >= 2, proc.stderr
    assert "Developer Mode" in proc.stderr
    assert "[install_hooks] Restart Claude Code" in proc.stdout

    # Degraded skills, intact core: both configs carry the full shapes.
    claude_config = json.loads(harness.claude_json.read_text(encoding="utf-8"))
    assert_mcp_server_shape(claude_config, harness)
    settings = json.loads(harness.settings_json.read_text(encoding="utf-8"))
    assert_managed_shapes(settings)

    # Dir creation succeeded (separate from link creation), links did not.
    assert harness.skills_dir.is_dir()
    for name in SKILL_NAMES:
        assert not (harness.skills_dir / name).exists()


# --------------------------------------- A7: symlink replacement ladder


LADDER_SKILL = "better-memory-synthesize"


def _seed_obstacle(kind: str, link: Path, tmp: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if kind == "wrong-symlink":
        wrong_target = tmp / "wrong-target"
        wrong_target.mkdir()
        link.symlink_to(wrong_target, target_is_directory=True)
    elif kind == "plain-file":
        link.write_text("user-materialized skill file", encoding="utf-8")
    elif kind == "real-dir-with-sentinel":
        link.mkdir(parents=True)
        (link / "SENTINEL.txt").write_text("user sentinel content", encoding="utf-8")
    else:  # pragma: no cover - parametrize guards this
        raise AssertionError(kind)


@pytest.mark.parametrize(
    "obstacle", ["wrong-symlink", "plain-file", "real-dir-with-sentinel"]
)
def test_symlink_replacement_ladder(
    harness: InstallHarness, obstacle: str
) -> None:
    """A7 ``e2e-install-symlink-replacement-ladder`` (gap, per judges).

    Capability-probed: a pre-existing wrong symlink / plain file / real dir
    with a sentinel at the skill path each get replaced by a correct symlink
    with NO warn; the sentinel is gone (pins the documented no-backup
    destructive contract, so any future softening is a deliberate change).
    Trap: removing the pre-clearing makes ``symlink_to`` raise
    ``FileExistsError`` - an OSError - silently converting every upgrade
    into perpetual "WARN skipped": skills never update again.
    """
    if not can_symlink(harness.tmp):
        pytest.skip("symlink capability unavailable (Windows without Developer Mode)")

    link = harness.skills_dir / LADDER_SKILL
    _seed_obstacle(obstacle, link, harness.tmp)

    proc = harness.run()

    assert proc.returncode == 0, proc.stderr
    assert "WARN skill symlink skipped" not in proc.stderr

    expected_source = (REPO_SKILLS_DIR / LADDER_SKILL).resolve()
    assert link.is_symlink(), f"{obstacle}: obstacle not replaced by symlink"
    assert link.resolve() == expected_source

    if obstacle == "real-dir-with-sentinel":
        # The seeded dir was rmtree'd without backup: the sentinel resolves
        # through the new symlink into the repo skill dir, where it must
        # not exist - and must never have been copied into the repo.
        assert not (link / "SENTINEL.txt").exists()
        assert not (expected_source / "SENTINEL.txt").exists()


def test_symlink_already_correct_is_noop(harness: InstallHarness) -> None:
    """A7 (continued): an already-correct link is a no-op (lstat compare).

    Trap: an ADD-style rewrite that unconditionally unlinks+recreates would
    churn the link on every install; the lstat identity assertion flips.
    """
    if not can_symlink(harness.tmp):
        pytest.skip("symlink capability unavailable (Windows without Developer Mode)")

    link = harness.skills_dir / LADDER_SKILL
    link.parent.mkdir(parents=True)
    link.symlink_to(REPO_SKILLS_DIR / LADDER_SKILL, target_is_directory=True)
    # ino + ctime identify the link object itself; a delete/recreate cannot
    # reproduce either (NTFS/ext ctime resolution is far below the >100ms
    # subprocess runtime).
    before = os.lstat(link)
    before_identity = (before.st_ino, before.st_ctime_ns)

    proc = harness.run()

    assert proc.returncode == 0, proc.stderr
    assert "WARN skill symlink skipped" not in proc.stderr
    assert link.is_symlink()
    assert link.resolve() == (REPO_SKILLS_DIR / LADDER_SKILL).resolve()
    after = os.lstat(link)
    assert (after.st_ino, after.st_ctime_ns) == before_identity, (
        "already-correct symlink was recreated instead of no-op'd"
    )
