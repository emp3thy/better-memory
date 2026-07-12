"""agentcore-mode extension of the e2e env choke point.

``agentcore_env`` layers the T2 lockdown contract on top of
``tests.e2e._env.isolated_env`` (everything still flows through the single
allowlist choke point, so the e2e_meta contract tests bless spawns whose
env came from here):

* ``BETTER_MEMORY_STORAGE_BACKEND=agentcore`` (pin ``None`` to test the
  settings.json activation path instead — see ``write_backend_settings``);
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

Region is single-sourced from ``agentcore.json`` (the file
``write_fake_agentcore_json`` fabricates): the product's agentcore region
env var and the vestigial memory-ID env vars were deleted by the
onboarding-fix wave (2026-07-12-agentcore-onboarding-fix-design.md,
Task 1), so this helper no longer sets any of them — the tripwire in
``tests/e2e/test_tripwires_aws.py`` grep-pins their absence from the whole
e2e suite, and the poison entries in ``tests/e2e_meta/test_env_bleed.py``
keep proving the removed knobs stay inert.
"""

from __future__ import annotations

import json
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

#: Default region written into fabricated agentcore.json files. Purely a
#: test-data default — the product reads the region exclusively from
#: agentcore.json, so tests that assert region provenance override this
#: with a distinctive value (e.g. us-east-1).
DEFAULT_TEST_REGION = "eu-west-2"


def agentcore_env(
    tmp_home: Path, fake_port: int, **pins: str | None
) -> dict[str, str]:
    """Hermetic agentcore-mode child env homed at ``tmp_home``.

    ``fake_port`` is the local :class:`FakeAgentCore` port. ``pins``
    override or extend the defaults exactly like ``isolated_env`` pins
    (``None`` removes a key — e.g. ``BETTER_MEMORY_STORAGE_BACKEND=None``
    for the settings.json-activation and sqlite-safety cases)."""
    defaults: dict[str, str | None] = {
        "BETTER_MEMORY_STORAGE_BACKEND": "agentcore",
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
    }
    defaults.update(pins)
    return isolated_env(tmp_home, **defaults)


def write_backend_settings(bm_home: Path) -> Path:
    """Write ``settings.json`` activating the agentcore backend.

    Fabricates exactly what ``better-memory agentcore init`` persists
    (UD-3): ``{"storage_backend": "agentcore"}`` at
    ``<bm_home>/settings.json``, where ``bm_home`` is the
    BETTER_MEMORY_HOME dir (``<tmp_home>/.better-memory``). Composes with
    ``BETTER_MEMORY_STORAGE_BACKEND=None`` pins to prove the env-var-free
    onboarding path: ``resolve_storage_backend()`` falls through to this
    file. Returns the settings.json path.
    """
    bm_home.mkdir(parents=True, exist_ok=True)
    settings_path = bm_home / "settings.json"
    settings_path.write_text(
        json.dumps({"storage_backend": "agentcore"}), encoding="utf-8"
    )
    return settings_path


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
