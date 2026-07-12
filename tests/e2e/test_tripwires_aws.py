"""e2e-aws-lockdown-tripwire (design section 1F).

Proves the T2 harness can never touch real AWS:

* helper contract — ``agentcore_env`` pins the endpoint to loopback, uses
  fake static creds, locks the credential/config files onto nonexistent
  tmp paths, disables IMDS, and lets NO outer ``AWS_*`` var survive;
* round trip — a real CLI subprocess (``better-memory agentcore status``)
  against the fake: every request's Host is ``127.0.0.1:*`` and every
  SigV4 scope is ``Credential=bm-e2e-fake/...`` (i.e. the default chain
  never reached a real ``~/.aws``, SSO cache, IMDS, or shell creds) —
  red BEFORE anything could ever be billed;
* workaround containment — the dummy ``BETTER_MEMORY_AGENTCORE_*_MEMORY_ID``
  literals (FIXME(idvar-gate) workaround) are grep-pinned to exactly two
  files: the single env helper and the one defect-pin negative test.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.e2e._agentcore_env import (
    DUMMY_ID_VARS,
    FAKE_ACCESS_KEY_ID,
    FAKE_SECRET_ACCESS_KEY,
    agentcore_env,
    write_fake_agentcore_json,
)
from tests.e2e._fake_agentcore import FakeAgentCore, RecordedRequest

#: The complete set of AWS_* keys the helper is allowed to emit. Anything
#: else (AWS_PROFILE, AWS_SESSION_TOKEN, AWS_IGNORE_CONFIGURED_ENDPOINT_URLS,
#: a real AWS_ACCESS_KEY_ID from the dev shell, ...) must never survive.
_LOCKDOWN_AWS_KEYS = frozenset(
    {
        "AWS_ENDPOINT_URL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "AWS_EC2_METADATA_DISABLED",
    }
)


class TestAgentcoreEnvLockdownContract:
    """Pure helper-contract assertions (no subprocess)."""

    def test_lockdown_contract_under_hostile_outer_shell(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Simulate the most dangerous outer shell: real-looking creds, a
        # profile, SSO/session material, and the endpoint-override kill
        # switch. NONE of it may reach a child process.
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAREALLOOKINGKEY")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "real-secret")
        monkeypatch.setenv("AWS_PROFILE", "prod")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "real-session-token")
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/real/token")
        monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/creds")
        monkeypatch.setenv("AWS_IGNORE_CONFIGURED_ENDPOINT_URLS", "true")
        monkeypatch.setenv("aws_region", "us-east-1")  # case-insensitive drop

        env = agentcore_env(tmp_path / "home", 45999)

        aws_keys = {k for k in env if k.upper().startswith("AWS_")}
        assert aws_keys == set(_LOCKDOWN_AWS_KEYS)

        assert env["AWS_ENDPOINT_URL"] == "http://127.0.0.1:45999"
        assert env["AWS_ACCESS_KEY_ID"] == FAKE_ACCESS_KEY_ID
        assert env["AWS_SECRET_ACCESS_KEY"] == FAKE_SECRET_ACCESS_KEY
        assert env["AWS_EC2_METADATA_DISABLED"] == "true"
        # Credential-chain files locked onto nonexistent-by-construction paths.
        for key in ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE"):
            locked = Path(env[key])
            assert not locked.exists()
            assert Path(str(tmp_path)) in locked.parents

        assert env["BETTER_MEMORY_STORAGE_BACKEND"] == "agentcore"
        assert env["BETTER_MEMORY_AGENTCORE_REGION"]
        # FIXME(idvar-gate) workaround values present (see _agentcore_env).
        for var_name, dummy_value in DUMMY_ID_VARS.items():
            assert env[var_name] == dummy_value

    def test_pins_can_remove_lockdown_keys_for_negative_tests(
        self, tmp_path: Path
    ) -> None:
        env = agentcore_env(
            tmp_path / "home", 45999, BETTER_MEMORY_STORAGE_BACKEND=None
        )
        assert "BETTER_MEMORY_STORAGE_BACKEND" not in env


class TestAwsLockdownRoundTrip:
    """A real CLI subprocess against the fake — the billing tripwire."""

    def test_status_roundtrip_hits_only_local_fake_with_fake_sigv4(
        self, clean_slate_home: Path
    ) -> None:
        bm_home = clean_slate_home / ".better-memory"
        with FakeAgentCore() as fake:
            write_fake_agentcore_json(bm_home)

            def _get_memory(request: RecordedRequest) -> dict:
                # Path: /memories/{memoryId}/details — echo the id back.
                memory_id = request.path.split("?", 1)[0].split("/")[2]
                return {
                    "memory": {
                        "id": memory_id,
                        "arn": f"arn:aws:fake:::memory/{memory_id}",
                        "name": f"bm_e2e_{memory_id}",
                        "status": "ACTIVE",
                        "eventExpiryDuration": 90,
                        "strategies": [
                            {
                                "strategyId": "STRAT-1",
                                "name": "bm_e2e_strategy",
                                "status": "ACTIVE",
                            }
                        ],
                    }
                }

            fake.set_response("GetMemory", _get_memory)
            env = agentcore_env(clean_slate_home, fake.port)
            proc = subprocess.run(  # noqa: S603 — test harness, fixed argv
                [
                    sys.executable,
                    "-m",
                    "better_memory.cli.main",
                    "agentcore",
                    "status",
                    "--home",
                    str(bm_home),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )

            # Anti-vacuity: the round trip must be REAL — rc 0 with both
            # memory ids reported ACTIVE, not an early error that never
            # touched the wire.
            assert proc.returncode == 0, proc.stderr
            assert "EPI-FAKE-0001" in proc.stdout
            assert "SEM-FAKE-0001" in proc.stdout
            assert "ACTIVE" in proc.stdout

            requests = list(fake.requests)
            assert len(requests) >= 2
            for request in requests:
                # Zero requests escaped to *.amazonaws.com.
                assert request.host.startswith("127.0.0.1:"), request.host
                # SigV4 scope proves the FAKE credentials were signed with:
                # the default chain never resolved real creds.
                assert f"Credential={FAKE_ACCESS_KEY_ID}/" in request.authorization
                assert request.operation == "GetMemory"


class TestDummyIdVarContainment:
    """The FIXME(idvar-gate) workaround must never metastasize per-test."""

    #: The ONLY files allowed to spell out the dummy-ID var names:
    #: the single env helper, and the defect-pin negative test that gets
    #: deleted together with the workaround when the product fix lands.
    _ALLOWED = frozenset({"_agentcore_env.py", "test_agentcore_neg.py"})

    @pytest.mark.parametrize("kind", ["SEMANTIC", "EPISODIC"])
    def test_dummy_idvar_literal_only_in_helper_and_defect_pin(
        self, kind: str
    ) -> None:
        # Built by concatenation so THIS file never matches its own grep.
        needle = "BETTER_MEMORY_AGENTCORE_" + kind + "_MEMORY_ID"
        e2e_dir = Path(__file__).resolve().parent

        containing = {
            path.name
            for path in sorted(e2e_dir.glob("*.py"))
            if needle in path.read_text(encoding="utf-8", errors="ignore")
        }

        offenders = containing - self._ALLOWED
        assert not offenders, (
            f"{needle} leaked outside the single helper + defect-pin test "
            f"(see FIXME(idvar-gate) in _agentcore_env.py): {sorted(offenders)}. "
            f"Use agentcore_env()/remove_dummy_id_vars_pins() instead."
        )
        # Anti-vacuity: the two allowed files really do carry the literal —
        # an over-eager cleanup that renamed the var would green this test
        # while breaking the workaround.
        assert self._ALLOWED <= containing, (
            f"expected {needle} in {sorted(self._ALLOWED)}, "
            f"found only in {sorted(containing)}"
        )
