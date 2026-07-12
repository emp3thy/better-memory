"""agentcore-mode extension of the e2e env choke point.

``agentcore_env`` layers the T2 lockdown contract on top of
``tests.e2e._env.isolated_env`` (everything still flows through the single
allowlist choke point, so the e2e_meta contract tests bless spawns whose
env came from here):

* ``BETTER_MEMORY_STORAGE_BACKEND=agentcore`` + a pinned test region;
* ``AWS_ENDPOINT_URL`` → the local :class:`tests.e2e._fake_agentcore.
  FakeAgentCore` port, so every boto3 request from every child process
  lands on loopback (verified for both agentcore planes at boto3 1.43.14);
* credential lockdown: fake static keys (``bm-e2e-fake``), shared
  credentials / config files pointed at *nonexistent* tmp paths, IMDS
  disabled — the SigV4 ``Credential=bm-e2e-fake/...`` scope on captured
  requests proves the default chain never reached real creds;
* ``AWS_IGNORE_CONFIGURED_ENDPOINT_URLS`` (and every other outer ``AWS_*``
  var) never survives: ``isolated_env`` builds from an allowlist and drops
  all ``AWS_*`` keys case-insensitively.

This module is THE ONLY place the two dummy memory-ID vars are set — see
the FIXME below. ``tests/e2e/test_tripwires_aws.py`` grep-pins that
exclusivity.
"""

from __future__ import annotations

from pathlib import Path

from better_memory.storage.agentcore_persistence import (
    AgentCoreConfig,
    MemoryRecord,
    save_agentcore_config,
)
from tests.e2e._env import isolated_env

#: Fake static credentials. The value is asserted inside SigV4 Credential
#: scopes by the AWS lockdown tripwire — keep them greppable and unique.
FAKE_ACCESS_KEY_ID = "bm-e2e-fake"
FAKE_SECRET_ACCESS_KEY = "bm-e2e-fake-secret"

#: Region pinned into the child env (BETTER_MEMORY_AGENTCORE_REGION). Note
#: this matches the product default in better_memory/config.py so removing
#: the pin (pin=None) still signs eu-west-2 — the region-split-brain test
#: relies on that.
DEFAULT_TEST_REGION = "eu-west-2"

# FIXME(idvar-gate): the two BETTER_MEMORY_AGENTCORE_*_MEMORY_ID env vars
# below are a WORKAROUND for the dead presence-only gate at
# better_memory/config.py:293-301. Their values are consumed by NOTHING —
# runtime memory IDs come exclusively from agentcore.json (see
# better_memory/storage/factory.py; the region-split-brain test proves the
# dummies never reach the wire). This is the ONLY location that sets them
# (grep-pinned by tests/e2e/test_tripwires_aws.py). DELETE these two
# entries together with tests/e2e/test_agentcore_neg.py's idvar-gate
# defect-pin test when the product fix (remove the gate, or repoint it at
# agentcore.json existence) lands.
DUMMY_ID_VARS: dict[str, str] = {
    "BETTER_MEMORY_AGENTCORE_SEMANTIC_MEMORY_ID": "DUMMY-SEM",
    "BETTER_MEMORY_AGENTCORE_EPISODIC_MEMORY_ID": "DUMMY-EPI",
}

#: Dummy values, exported so wire-absence assertions ("the dead env values
#: never appear in any request") don't have to hardcode them.
DUMMY_SEMANTIC_MEMORY_ID = DUMMY_ID_VARS["BETTER_MEMORY_AGENTCORE_SEMANTIC_MEMORY_ID"]
DUMMY_EPISODIC_MEMORY_ID = DUMMY_ID_VARS["BETTER_MEMORY_AGENTCORE_EPISODIC_MEMORY_ID"]


def remove_dummy_id_vars_pins() -> dict[str, None]:
    """Pins that REMOVE the two dummy ID vars (misconfig scenarios).

    Kwarg-splat into :func:`agentcore_env` so tests never spell the var
    names out (the tripwire grep-pins the literals to this module plus the
    one defect-pin test)::

        env = agentcore_env(home, fake.port, **remove_dummy_id_vars_pins())
    """
    return dict.fromkeys(DUMMY_ID_VARS)


def agentcore_env(
    tmp_home: Path, fake_port: int, **pins: str | None
) -> dict[str, str]:
    """Hermetic agentcore-mode child env homed at ``tmp_home``.

    ``fake_port`` is the local :class:`FakeAgentCore` port. ``pins``
    override or extend the defaults exactly like ``isolated_env`` pins
    (``None`` removes a key — e.g. ``BETTER_MEMORY_STORAGE_BACKEND=None``
    for the session-close env-gate case)."""
    defaults: dict[str, str | None] = {
        "BETTER_MEMORY_STORAGE_BACKEND": "agentcore",
        "BETTER_MEMORY_AGENTCORE_REGION": DEFAULT_TEST_REGION,
        "AWS_ENDPOINT_URL": f"http://127.0.0.1:{fake_port}",
        "AWS_ACCESS_KEY_ID": FAKE_ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": FAKE_SECRET_ACCESS_KEY,
        # Nonexistent-by-construction paths (never created by anything):
        # the default credential/config chain finds nothing outside tmp.
        "AWS_SHARED_CREDENTIALS_FILE": str(
            Path(tmp_home) / "no-such-aws-credentials"
        ),
        "AWS_CONFIG_FILE": str(Path(tmp_home) / "no-such-aws-config"),
        "AWS_EC2_METADATA_DISABLED": "true",
        **DUMMY_ID_VARS,  # FIXME(idvar-gate) — see module comment above.
    }
    defaults.update(pins)
    return isolated_env(tmp_home, **defaults)


def write_fake_agentcore_json(
    bm_home: Path,
    *,
    region: str = DEFAULT_TEST_REGION,
    episodic_memory_id: str = "EPI-FAKE-0001",
    semantic_memory_id: str = "SEM-FAKE-0001",
    episodic_strategy_id: str = "STRAT-EPI-1",
    semantic_strategy_id: str = "STRAT-SEM-1",
) -> AgentCoreConfig:
    """Fabricate a schema-1 ``agentcore.json`` under ``bm_home`` (the
    BETTER_MEMORY_HOME dir, i.e. ``<tmp_home>/.better-memory``) via the real
    ``save_agentcore_config`` writer, and return the config object."""
    cfg = AgentCoreConfig(
        schema_version=1,
        region=region,
        semantic=MemoryRecord(
            memory_id=semantic_memory_id,
            memory_arn=(
                f"arn:aws:bedrock-agentcore:{region}:000000000000:"
                f"memory/{semantic_memory_id}"
            ),
            memory_name="bm_e2e_semantic",
            strategy_id=semantic_strategy_id,
            strategy_name="bm_e2e_semantic_strategy",
            event_expiry_duration_days=365,
        ),
        episodic=MemoryRecord(
            memory_id=episodic_memory_id,
            memory_arn=(
                f"arn:aws:bedrock-agentcore:{region}:000000000000:"
                f"memory/{episodic_memory_id}"
            ),
            memory_name="bm_e2e_episodic",
            strategy_id=episodic_strategy_id,
            strategy_name="bm_e2e_episodic_strategy",
            event_expiry_duration_days=90,
        ),
    )
    save_agentcore_config(cfg, bm_home)
    return cfg
