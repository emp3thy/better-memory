"""Contract tests for the e2e harness env choke point (design F: meta-env-helper-contract).

Four lenses from the harness-safety scenario
``harness-windows-posix-env-helper-contract``:

* **A — dict-level contract**: ``isolated_env`` sets HOME+USERPROFILE on every
  OS, preserves only the system allowlist, strips all
  CLAUDE_*/BETTER_MEMORY_*/AWS_*/OLLAMA_* case-insensitively, and yields
  case-insensitively-unique keys.
* **B — child-process ground truth**: a real spawned python reports
  ``Path.home()``/``expanduser('~')`` == tmp and can import ssl/sqlite3
  (SYSTEMROOT preserved) — cannot be fooled by dict-level mocking.
* **C — single-choke-point enforcement**: every spawn site in ``tests/e2e``
  derives its env from the helper; hand-rolled env dicts and
  ``os.environ``-copy patterns are structurally banned.
* **D — skip-decoration checks**: setup.sh tests skip (not error) without
  bash; symlink content assertions are capability-probed.

Plus the ``mcp_session`` boot smoke: proves the shared helper can drive the
real MCP server hermetically before any journey test builds on it.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.e2e._env import (
    ALLOWLIST,
    POISONED_OLLAMA_HOST,
    STRIPPED_PREFIXES,
    isolated_env,
)
from tests.e2e.conftest import mcp_session, run_hook

E2E_DIR = Path(__file__).resolve().parents[1] / "e2e"

#: The only STRIPPED_PREFIXES-matching keys isolated_env may emit by default —
#: each one a deliberate pin, not an inherited value.
DELIBERATE_PINS = {
    "BETTER_MEMORY_HOME",
    "BETTER_MEMORY_PROJECT",
    "BETTER_MEMORY_EMBEDDINGS_BACKEND",
    "CLAUDE_SESSION_ID",
    "OLLAMA_HOST",
}


# ---------------------------------------------------------------------------
# Contract test A — dict-level
# ---------------------------------------------------------------------------


class TestContractADictLevel:
    def test_home_and_userprofile_both_set_on_every_os(self, tmp_path: Path) -> None:
        """Both vars, unconditionally — the POSIX-contributor-deletes-
        USERPROFILE decay path is the exact bug that torches a Windows dev's
        real ~/.claude.json."""
        env = isolated_env(tmp_path)
        assert env["HOME"] == str(tmp_path)
        assert env["USERPROFILE"] == str(tmp_path)
        if sys.platform == "win32":
            drive, tail = os.path.splitdrive(str(tmp_path))
            assert env["HOMEDRIVE"] == drive
            assert env["HOMEPATH"] == tail
            assert env["HOMEDRIVE"] + env["HOMEPATH"] == str(tmp_path)

    def test_hostile_outer_vars_stripped_case_insensitively(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")
        monkeypatch.setenv("AWS_PROFILE", "prod")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "C:/real")
        # Mixed/lower case — Windows env keys carry arbitrary case and a
        # case-sensitive strip would let these through on POSIX CI.
        monkeypatch.setenv("aws_secret_access_key", "AKIA-POISON")
        monkeypatch.setenv("Ollama_Host", "http://real-ollama:11434")

        env = isolated_env(tmp_path)
        uppers = {k.upper() for k in env}
        assert "BETTER_MEMORY_STORAGE_BACKEND" not in uppers
        assert "AWS_PROFILE" not in uppers
        assert "CLAUDE_PROJECT_DIR" not in uppers
        assert "AWS_SECRET_ACCESS_KEY" not in uppers
        assert "AKIA-POISON" not in env.values()
        # The poison pin won over the outer daemon address.
        assert env["OLLAMA_HOST"] == POISONED_OLLAMA_HOST
        assert env["OLLAMA_HOST"].endswith(".invalid:1")

    def test_only_deliberate_pins_carry_stripped_prefixes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exact-set assertion: any new prefixed key appearing by default is
        either an inherited leak or an undocumented pin — both must be
        deliberate, so this test must be updated consciously."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "leak")
        monkeypatch.setenv("BETTER_MEMORY_CONTEXT_INJECT_MODE", "aggressive")
        monkeypatch.setenv("AWS_ENDPOINT_URL", "https://bedrock.example")

        env = isolated_env(tmp_path)
        prefixed = {k.upper() for k in env if k.upper().startswith(STRIPPED_PREFIXES)}
        assert prefixed == DELIBERATE_PINS

    def test_system_critical_allowlist_preserved(self, tmp_path: Path) -> None:
        env = isolated_env(tmp_path)
        uppers = {k.upper() for k in env}
        assert "PATH" in uppers
        if sys.platform == "win32":
            # Child pythons fail to import ssl/sqlite3 without SystemRoot;
            # COMSPEC is required by anything that shells out.
            assert "SYSTEMROOT" in uppers
            assert "COMSPEC" in uppers

    def test_non_allowlisted_outer_var_not_inherited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Allowlist-built, not strip-built: an arbitrary outer var (no
        stripped prefix) still never reaches the child."""
        monkeypatch.setenv("E2E_RANDOM_SENTINEL", "leak-me")
        env = isolated_env(tmp_path)
        assert "E2E_RANDOM_SENTINEL" not in {k.upper() for k in env}
        assert "leak-me" not in env.values()

    def test_case_insensitive_key_uniqueness(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Duplicate 'Path'/'PATH' style keys corrupt Windows CreateProcess
        env blocks; the helper must dedupe by upper-cased name."""
        if sys.platform != "win32":
            # POSIX os.environ CAN hold case-colliding keys — seed a pair.
            monkeypatch.setenv("Temp", "/case/one")
            monkeypatch.setenv("TEMP", "/case/two")
        env = isolated_env(tmp_path)
        uppers = [k.upper() for k in env]
        assert len(set(uppers)) == len(uppers), sorted(env)

    def test_pins_override_none_removes_and_extras_added(self, tmp_path: Path) -> None:
        env = isolated_env(
            tmp_path,
            BETTER_MEMORY_PROJECT="custom-proj",
            CLAUDE_SESSION_ID=None,
            EXTRA_PIN="extra-value",
        )
        assert env["BETTER_MEMORY_PROJECT"] == "custom-proj"
        assert "CLAUDE_SESSION_ID" not in {k.upper() for k in env}
        assert env["EXTRA_PIN"] == "extra-value"

    def test_default_pin_values(self, tmp_path: Path) -> None:
        env = isolated_env(tmp_path)
        assert env["BETTER_MEMORY_HOME"] == str(tmp_path / ".better-memory")
        assert env["BETTER_MEMORY_EMBEDDINGS_BACKEND"] == "sqlite"
        assert env["OLLAMA_HOST"] == POISONED_OLLAMA_HOST
        assert env["CLAUDE_SESSION_ID"] == "e2e-session-1"
        assert env["BETTER_MEMORY_PROJECT"] == "e2e-project"

    def test_allowlist_and_stripped_prefixes_are_disjoint(self) -> None:
        """A prefixed name sneaking into the allowlist would reopen the leak."""
        assert not {n for n in ALLOWLIST if n.startswith(STRIPPED_PREFIXES)}


# ---------------------------------------------------------------------------
# Contract test B — child-process ground truth
# ---------------------------------------------------------------------------

_CHILD_PROBE = (
    "from pathlib import Path\n"
    "import os, json, tempfile, sqlite3, ssl\n"  # ssl/sqlite3: SYSTEMROOT proof on Windows
    "print(json.dumps({\n"
    "    'home': str(Path.home()),\n"
    "    'expanduser': os.path.expanduser('~'),\n"
    "    'gettempdir': tempfile.gettempdir(),\n"
    "    'prefixed': sorted(\n"
    "        k.upper() for k in os.environ\n"
    "        if k.upper().startswith(('CLAUDE_', 'BETTER_MEMORY_', 'AWS_', 'OLLAMA_'))\n"
    "    ),\n"
    "}))\n"
)


class TestContractBChildGroundTruth:
    def _spawn_probe(self, env: dict[str, str]) -> dict[str, Any]:
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD_PROBE],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, f"child probe died: {proc.stderr}"
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_child_home_and_expanduser_land_in_tmp(self, tmp_path: Path) -> None:
        report = self._spawn_probe(isolated_env(tmp_path))
        assert report["home"] == str(tmp_path)
        assert report["expanduser"] == str(tmp_path)
        # TEMP/TMP preserved: gettempdir resolved without crashing or
        # falling back to the cwd.
        assert report["gettempdir"]

    def test_child_sees_only_deliberate_prefixed_pins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hostile outer shell → the child's actual environment (not just the
        dict we built) contains exactly the deliberate pins."""
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA-REAL")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(Path.home()))
        monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")
        report = self._spawn_probe(isolated_env(tmp_path))
        assert set(report["prefixed"]) == DELIBERATE_PINS


# ---------------------------------------------------------------------------
# Contract test C — single-choke-point enforcement (lint-as-test)
# ---------------------------------------------------------------------------

_HELPER_NAME = re.compile(r"\b(isolated_env|agentcore_env)\b")
_SUBPROCESS_FUNC = re.compile(r"^(subprocess\.)?(run|Popen|check_output|check_call|call)$")
#: Parameter names allowed to carry a caller-supplied env (the conftest
#: wrapper helpers). Anything else must trace back to a helper call.
_ENV_PARAM = re.compile(r"^(env|.*_env)$")


def _func_source(src: str, node: ast.Call) -> str:
    return (ast.get_source_segment(src, node.func) or "").strip()


def _blessed_names(src: str, tree: ast.AST) -> set[str]:
    """Names whose value provably derives from isolated_env/agentcore_env.

    Seeds: function parameters named ``env``/``*_env`` (their values flow
    from call sites, which are themselves checked). Fixed point over simple
    assignments whose RHS mentions a helper or an already-blessed name.
    """
    blessed: set[str] = set()
    assignments: list[tuple[list[str], str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            a = node.args
            for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs]:
                if _ENV_PARAM.match(arg.arg):
                    blessed.add(arg.arg)
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if names:
                assignments.append((names, ast.get_source_segment(src, node.value) or ""))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name):
                assignments.append(
                    ([node.target.id], ast.get_source_segment(src, node.value) or "")
                )
    changed = True
    while changed:
        changed = False
        for names, rhs in assignments:
            if all(n in blessed for n in names):
                continue
            ok = bool(_HELPER_NAME.search(rhs)) or any(
                re.search(rf"\b{re.escape(b)}\b", rhs) for b in blessed
            )
            if ok:
                for n in names:
                    if n not in blessed:
                        blessed.add(n)
                        changed = True
    return blessed


def _spawn_env_violations(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    blessed = _blessed_names(src, tree)
    violations: list[str] = []

    def env_ok(expr: ast.expr | None, where: str) -> None:
        if expr is None:
            violations.append(f"{path.name}:{where}: spawn without explicit env=")
            return
        segment = ast.get_source_segment(src, expr) or ""
        if _HELPER_NAME.search(segment):
            return
        if isinstance(expr, ast.Name) and expr.id in blessed:
            return
        violations.append(
            f"{path.name}:{where}: env not derived from isolated_env/agentcore_env: {segment!r}"
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = _func_source(src, node)
        kw_env = next((k.value for k in node.keywords if k.arg == "env"), None)
        line = f"line {node.lineno}"
        if _SUBPROCESS_FUNC.match(func) or func.endswith("StdioServerParameters"):
            env_ok(kw_env, line)
        elif func.endswith("run_hook"):
            pos = node.args[2] if len(node.args) >= 3 else None
            env_ok(kw_env if kw_env is not None else pos, line)
        elif func.endswith("mcp_session"):
            pos = node.args[0] if node.args else None
            env_ok(kw_env if kw_env is not None else pos, line)
    return violations


def _environ_copy_violations(path: Path) -> list[str]:
    """Ban os.environ.copy(), dict(os.environ) and {**os.environ, ...}."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = _func_source(src, node)
            if func.endswith("environ.copy"):
                violations.append(f"{path.name}:{node.lineno}: os.environ.copy()")
            elif func == "dict" and any(
                "environ" in (ast.get_source_segment(src, a) or "") for a in node.args
            ):
                violations.append(f"{path.name}:{node.lineno}: dict(os.environ)")
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None and "environ" in (ast.get_source_segment(src, value) or ""):
                    violations.append(f"{path.name}:{node.lineno}: {{**os.environ, ...}}")
    return violations


def _e2e_python_files() -> list[Path]:
    return sorted(p for p in E2E_DIR.rglob("*.py") if "__pycache__" not in p.parts)


class TestContractCSingleChokePoint:
    def test_e2e_dir_exists_and_scanned(self) -> None:
        """Anti-vacuity: the scan target exists and includes the helpers."""
        files = {p.name for p in _e2e_python_files()}
        assert "_env.py" in files
        assert "conftest.py" in files

    def test_every_spawn_site_uses_the_helper(self) -> None:
        violations = [v for p in _e2e_python_files() for v in _spawn_env_violations(p)]
        assert not violations, "\n".join(violations)

    def test_no_os_environ_copy_patterns(self) -> None:
        violations = [v for p in _e2e_python_files() for v in _environ_copy_violations(p)]
        assert not violations, "\n".join(violations)

    def test_checker_flags_hand_rolled_env_dict(self, tmp_path: Path) -> None:
        """The lint itself must go red on the banned patterns (guard against
        the checker decaying into a green-always ritual)."""
        bad = tmp_path / "test_bad.py"
        bad.write_text(
            "import subprocess, sys, os\n"
            "def test_x(tmp_path):\n"
            "    env = {'HOME': str(tmp_path)}\n"
            "    subprocess.run([sys.executable, '-V'], env=env)\n"
            "    subprocess.run([sys.executable, '-V'])\n"
            "    e2 = {**os.environ, 'HOME': str(tmp_path)}\n"
            "    subprocess.run([sys.executable, '-V'], env=os.environ.copy())\n",
            encoding="utf-8",
        )
        spawn = _spawn_env_violations(bad)
        copies = _environ_copy_violations(bad)
        assert len(spawn) == 3, spawn  # hand-rolled name, missing env=, environ.copy expr
        assert any("**os.environ" in v for v in copies)
        assert any("environ.copy" in v for v in copies)

    def test_checker_accepts_helper_derived_env(self, tmp_path: Path) -> None:
        good = tmp_path / "test_good.py"
        good.write_text(
            "import subprocess, sys\n"
            "from tests.e2e._env import isolated_env\n"
            "from tests.e2e.conftest import mcp_session, run_hook\n"
            "def test_x(tmp_path):\n"
            "    env = isolated_env(tmp_path)\n"
            "    env2 = {**env, 'EXTRA': '1'}\n"
            "    subprocess.run([sys.executable, '-V'], env=env2)\n"
            "    run_hook('m', {}, env)\n"
            "    subprocess.run([sys.executable, '-V'], env=isolated_env(tmp_path))\n",
            encoding="utf-8",
        )
        assert _spawn_env_violations(good) == []
        assert _environ_copy_violations(good) == []


# ---------------------------------------------------------------------------
# Contract test D — skip-decoration checks
# ---------------------------------------------------------------------------


class TestContractDSkipDecorations:
    def test_setup_sh_module_skips_without_bash(self) -> None:
        mod = E2E_DIR / "test_setup_sh.py"
        if not mod.exists():
            pytest.skip("tests/e2e/test_setup_sh.py not implemented yet (design task 9)")
        src = mod.read_text(encoding="utf-8")
        assert "skipif" in src, "setup.sh module must SKIP (not error) when bash is absent"
        assert re.search(r"which\(\s*['\"]bash['\"]\s*\)", src), (
            "expected a shutil.which('bash') presence probe driving the skipif"
        )

    def test_symlink_assertions_are_capability_probed(self) -> None:
        """Modules asserting symlink outcomes must gate them behind a probe
        (mirrors tests/cli/test_install_skill_symlink.py's requires_symlinks):
        non-Developer-Mode Windows boxes must skip the symlink ASSERT while
        still running the installer-exit-0 assertions."""
        probe = re.compile(
            r"requires_symlinks|_symlinks_available|symlinks?_(available|supported)|can_symlink"
        )
        offenders = [
            p.name
            for p in _e2e_python_files()
            if ".is_symlink()" in p.read_text(encoding="utf-8")
            and not probe.search(p.read_text(encoding="utf-8"))
        ]
        assert not offenders, (
            f"symlink assertions without a capability probe: {offenders}"
        )


# ---------------------------------------------------------------------------
# Helper smoke: run_hook plumbing + mcp_session hermetic boot
# ---------------------------------------------------------------------------


def _single_json_dict(result: Any) -> dict[str, Any]:
    content = result.content
    assert not getattr(result, "isError", False), f"tool errored: {content!r}"
    assert len(content) == 1, f"expected one content block: {content!r}"
    block = content[0]
    assert getattr(block, "type", None) == "text"
    parsed = json.loads(block.text)
    assert isinstance(parsed, dict)
    return parsed


class TestHelperSmoke:
    def test_run_hook_pipes_stdin_and_returns_triple(self, tmp_path: Path) -> None:
        """Plumbing-only smoke via a stdlib module: payload reaches the child's
        stdin, (rc, stdout, stderr) come back. Hook behavioral contracts are
        owned by the journey/contract test tasks."""
        rc, out, err = run_hook("json.tool", {"probe": 1}, isolated_env(tmp_path))
        assert rc == 0, err
        assert json.loads(out) == {"probe": 1}

    async def test_mcp_session_boots_real_server_hermetically(self, tmp_path: Path) -> None:
        """The proven-helper smoke for every T1/T2 builder: the real MCP
        server boots offline on a virgin fake home (sqlite embeddings,
        poisoned OLLAMA_HOST), answers list_tools + memory.retrieve, and all
        disk writes land under the fake home."""
        home = tmp_path / "home"
        home.mkdir()
        env = isolated_env(home)
        errlog_path = tmp_path / "server.stderr"  # outside the fake home

        with errlog_path.open("w", encoding="utf-8") as errlog:
            async with mcp_session(env, errlog=errlog) as session:
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                assert {"memory.observe", "memory.retrieve", "knowledge.search"} <= names

                payload = _single_json_dict(await session.call_tool("memory.retrieve", {}))
                assert isinstance(payload["do"], list)
                assert isinstance(payload["dont"], list)
                assert isinstance(payload["neutral"], list)

        # Hermetic ground truth: the server's DB landed under the FAKE home.
        assert (home / ".better-memory" / "memory.db").exists(), sorted(
            p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")
        )

    async def test_mcp_session_rejects_env_missing_userprofile(self, tmp_path: Path) -> None:
        """Guard branch: the helper refuses an env without USERPROFILE/HOME
        instead of letting the SDK leak the real profile underneath it."""
        env = isolated_env(tmp_path)
        stripped = {k: v for k, v in env.items() if k.upper() != "USERPROFILE"}
        with pytest.raises(ValueError, match="USERPROFILE"):
            async with mcp_session(stripped):
                pass  # pragma: no cover — must not spawn

    async def test_mcp_session_rejects_fileless_errlog(self, tmp_path: Path) -> None:
        """Guard branch: errlog must have a real fileno() — the SDK hands it
        to subprocess creation as the stderr handle; StringIO silently loses
        every server-side traceback."""
        with pytest.raises(io.UnsupportedOperation):
            async with mcp_session(isolated_env(tmp_path), errlog=io.StringIO()):
                pass  # pragma: no cover — must not spawn
