"""T2 agentcore negative / misconfiguration scenarios D8-D11 (design 1D).

The failure surfaces a brand-new agentcore user actually hits:

* D8  ``e2e-ac-neg-prehandshake-config-errors`` — parametrized idvar-gate
      (KNOWN-DEFECT PIN) + backend-name typo, sharing one raw-spawn helper;
* D9  ``e2e-ac-neg-missing-agentcore-json`` — raw + SDK-client levels
      (the enduring negative that survives the idvar-gate fix);
* D10 ``e2e-ac-neg-corrupt-agentcore-json`` — truncated JSON + forward
      schema_version, both fail loudly, neither offers remediation (pin);
* D11 ``e2e-ac-neg-boto3-missing`` — PYTHONPATH shadow simulating a plain
      ``pip install better-memory`` (no [agentcore] extra), raw traceback
      with no install hint (PRODUCT GAP PIN), content-based control run.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from mcp.shared.exceptions import McpError

from tests.e2e._agentcore_env import (
    agentcore_env,
    remove_dummy_id_vars_pins,
    write_fake_agentcore_json,
)
from tests.e2e._env import isolated_env
from tests.e2e._fake_agentcore import FakeAgentCore
from tests.e2e.conftest import mcp_session, run_hook

_SERVER_MODULE = "better_memory.mcp"


def _bm_home(home: Path) -> Path:
    return home / ".better-memory"


def _table_count(db_path: Path) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()
    return int(count)


def _assert_prehandshake_death(
    rc: int, out: str, err: str, expected_stderr: list[str]
) -> None:
    """Shared oracle for a pre-JSON-RPC server death.

    ``stdout == ''`` is the load-bearing half: zero bytes means the death
    is strictly pre-handshake — Claude Code sees an opaque spawn failure,
    never a tool error."""
    assert rc == 1
    assert out == ""
    for substring in expected_stderr:
        assert substring in err, f"{substring!r} not in stderr:\n{err}"


# ---------------------------------------------------------------------------
# D8 — pre-handshake config errors (parametrized)
# ---------------------------------------------------------------------------


class TestPrehandshakeConfigErrors:
    @pytest.mark.parametrize("case", ["idvar-gate", "backend-typo"])
    def test_config_error_kills_server_prehandshake_before_any_disk_write(
        self, case: str, clean_slate_home: Path
    ) -> None:
        """Two get_config() raises sharing one spawn helper + one shared
        ordering pin (config is validated before ANY disk write — no
        memory.db afterwards, distinguishing config-stage death from the
        post-config failures in TestMissingAgentCoreJson).

        [idvar-gate] KNOWN-DEFECT PIN (design section 4 item 1): the
        presence-only gate at better_memory/config.py:293-301 is vestigial
        — its values are consumed by nothing (IDs come from
        agentcore.json; commit 0056935 switched the transport) and its own
        remediation text ('agentcore init') cannot clear it, since init
        never sets env vars. Every documented agentcore setup dies
        pre-handshake like this. DELETE this case together with the
        FIXME(idvar-gate) dummy vars in tests/e2e/_agentcore_env.py when
        the product fix lands.

        [backend-typo] 'AgentCore' (case typo) must die loudly with the
        offending value echoed — never silently coerce/default to sqlite
        (the worst-case silent misconfig: the user believes they are on
        AWS while every memory lands in a local file)."""
        bm_home = _bm_home(clean_slate_home)

        if case == "idvar-gate":
            with FakeAgentCore() as fake:
                write_fake_agentcore_json(bm_home)  # valid json — gate still kills
                env = agentcore_env(
                    clean_slate_home, fake.port, **remove_dummy_id_vars_pins()
                )
                rc, out, err = run_hook(_SERVER_MODULE, None, env)
                assert fake.requests == []
            _assert_prehandshake_death(
                rc,
                out,
                err,
                [
                    "BETTER_MEMORY_STORAGE_BACKEND=agentcore requires both",
                    "BETTER_MEMORY_AGENTCORE_SEMANTIC_MEMORY_ID",
                    "BETTER_MEMORY_AGENTCORE_EPISODIC_MEMORY_ID",
                    # Pins the remediation that cannot actually clear the
                    # gate — anyone rewording it re-reads this docstring.
                    "agentcore init",
                ],
            )
        else:
            env = isolated_env(
                clean_slate_home, BETTER_MEMORY_STORAGE_BACKEND="AgentCore"
            )
            rc, out, err = run_hook(_SERVER_MODULE, None, env)
            _assert_prehandshake_death(
                rc,
                out,
                err,
                [
                    "ValueError",
                    "is not one of",
                    "'AgentCore'",  # repr'd — the user can SEE the typo
                ],
            )

        # Shared ordering pin: get_config raises before connect() —
        # config failures leave zero disk artifacts.
        assert not (bm_home / "memory.db").exists()


# ---------------------------------------------------------------------------
# D9 — missing agentcore.json (the enduring negative)
# ---------------------------------------------------------------------------


class TestMissingAgentCoreJson:
    def test_raw_spawn_remediation_text_and_migrated_before_failure(
        self, clean_slate_home: Path
    ) -> None:
        """ID vars set but no agentcore.json (the classic second-machine
        setup failure): pre-handshake FileNotFoundError with the one
        accurate remediation hint in this flow. Plus the ordering/docs
        pin: create_server migrates BOTH sqlite DBs BEFORE build_backend
        fails, so a *migrated* memory.db exists after the failed boot
        (sqlite_master > 0 — distinguishing it from the hook's schema-less
        artifact, and pinning the false 'No SQLite traffic' docs claim,
        design section 4 item 12)."""
        bm_home = _bm_home(clean_slate_home)
        with FakeAgentCore() as fake:
            env = agentcore_env(clean_slate_home, fake.port)
            rc, out, err = run_hook(_SERVER_MODULE, None, env)
            assert fake.requests == []

        _assert_prehandshake_death(
            rc,
            out,
            err,
            [
                "FileNotFoundError",
                "agentcore.json not found",
                "better-memory agentcore init",
            ],
        )
        assert (bm_home / "memory.db").exists()
        assert _table_count(bm_home / "memory.db") > 0

    async def test_sdk_client_sees_mcperror_connection_closed(
        self, clean_slate_home: Path, tmp_path: Path
    ) -> None:
        """What Claude Code's client actually experiences: initialize()
        raises McpError code -32000 'Connection closed'; the real
        diagnostic is only recoverable via the errlog channel (a REAL file
        — the SDK hands it to subprocess creation as the stderr handle).
        Never assert an exit code here: the SDK never exposes the Process.
        McpError is caught TIGHTLY around initialize() (inside both
        contexts) — letting it escape arrives double-wrapped in
        ExceptionGroups."""
        errlog_path = tmp_path / "server_stderr.txt"
        with FakeAgentCore() as fake:
            env = agentcore_env(clean_slate_home, fake.port)
            with errlog_path.open("w", encoding="utf-8") as errlog:
                async with mcp_session(
                    env,
                    errlog=errlog,
                    initialize=False,
                    # Backstop for the theoretical registration-after-EOF
                    # race: worst case degrades to a McpError timeout, so
                    # pytest.raises(McpError) still holds.
                    read_timeout=timedelta(seconds=20),
                ) as session:
                    with pytest.raises(McpError) as exc_info:
                        await session.initialize()
                # Both context managers exit cleanly — the process is
                # already dead, so shutdown returns immediately.

        assert exc_info.value.error.code == -32000
        # The error MESSAGE ('Connection closed') is mcp-SDK internal prose —
        # not our contract. Pin only that a message exists; the -32000 code
        # plus the product's own stderr text below carry the real assertions.
        assert exc_info.value.error.message
        assert "agentcore.json not found" in errlog_path.read_text(
            encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# D10 — corrupt agentcore.json
# ---------------------------------------------------------------------------


class TestCorruptAgentCoreJson:
    @pytest.mark.parametrize(
        ("case", "case_stderr"),
        [
            ("truncated", ["failed to parse"]),
            ("schema-v2", ["unsupported schema_version=2", "expected 1"]),
        ],
    )
    def test_corrupt_json_dies_loudly_naming_the_file(
        self, case: str, case_stderr: list[str], clean_slate_home: Path
    ) -> None:
        """Truncated JSON (killed-mid-write / hand-edit) and a forward
        schema_version both raise AgentCoreConfigError naming the file —
        the fail-loud contract the persistence module docstring promises.
        NEGATIVE PIN (design section 4 item 8): unlike the missing-file
        path, the corrupt-file error offers NO 'agentcore init'
        remediation — a flagged product gap; flip the last assertion when
        remediation text is added."""
        bm_home = _bm_home(clean_slate_home)
        if case == "truncated":
            bm_home.mkdir(parents=True)
            (bm_home / "agentcore.json").write_text(
                '{"schema_version": 1, "region"', encoding="utf-8"
            )
        else:
            write_fake_agentcore_json(bm_home)
            raw = json.loads(
                (bm_home / "agentcore.json").read_text(encoding="utf-8")
            )
            raw["schema_version"] = 2
            (bm_home / "agentcore.json").write_text(
                json.dumps(raw), encoding="utf-8"
            )

        with FakeAgentCore() as fake:
            env = agentcore_env(clean_slate_home, fake.port)
            rc, out, err = run_hook(_SERVER_MODULE, None, env)
            assert fake.requests == []

        _assert_prehandshake_death(
            rc,
            out,
            err,
            ["AgentCoreConfigError", "agentcore.json", *case_stderr],
        )
        # Product-gap pin: no remediation breadcrumb on the corrupt path.
        assert "agentcore init" not in err


# ---------------------------------------------------------------------------
# D11 — boto3 missing (plain install without the [agentcore] extra)
# ---------------------------------------------------------------------------


class TestBoto3MissingImportSurface:
    def test_shadowed_boto3_yields_raw_traceback_without_install_hint(
        self, clean_slate_home: Path, tmp_path: Path
    ) -> None:
        """PRODUCT GAP PIN (design section 4 item 7): a plain
        ``pip install better-memory`` (no [agentcore] extra) +
        backend=agentcore dies at the lazy ``import boto3`` in
        better_memory/storage/factory.py with a RAW ModuleNotFoundError —
        no 'better-memory[agentcore]' / 'pip install' breadcrumb. INVERT
        the two 'not in' assertions when the friendly message lands.

        boto3 is a dev-group dep in this venv, so absence is simulated
        with a PYTHONPATH-front shadow module. The shadow's raise SITE
        differs from a genuinely absent package (module body vs import
        machinery), but the user-visible class + message are identical —
        assertions use only those, never traceback frames.

        The control run (same env minus the shadow) is CONTENT-based —
        never an rc comparison: the control server boots fully and its
        exit code on stdin EOF is SDK/anyio-dependent."""
        bm_home = _bm_home(clean_slate_home)
        shadow_dir = tmp_path / "shadow"
        shadow_dir.mkdir()
        (shadow_dir / "boto3.py").write_text(
            "raise ModuleNotFoundError(\"No module named 'boto3'\")\n",
            encoding="utf-8",
        )

        with FakeAgentCore() as fake:
            write_fake_agentcore_json(bm_home)
            env = agentcore_env(
                clean_slate_home, fake.port, PYTHONPATH=str(shadow_dir)
            )
            rc, out, err = run_hook(_SERVER_MODULE, None, env)
            assert fake.requests == []

            _assert_prehandshake_death(
                rc,
                out,
                err,
                ["ModuleNotFoundError", "No module named 'boto3'"],
            )
            # PRODUCT GAP PIN — inverted assertions (see docstring).
            assert "better-memory[agentcore]" not in err
            assert "pip install" not in err

            # Vacuity guard: the same env WITHOUT the shadow must not die
            # on the boto3 import — proving the shadow (not some other
            # misconfig) caused the failure above. Also catches boto3
            # becoming a top-level import reachable from module import.
            control_env = agentcore_env(clean_slate_home, fake.port)
            _rc, _out, control_err = run_hook(_SERVER_MODULE, None, control_env)
            assert "ModuleNotFoundError" not in control_err
            assert "No module named 'boto3'" not in control_err
