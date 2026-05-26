"""Load and save `$BETTER_MEMORY_HOME/agentcore.json`.

The file is written once by `better-memory agentcore init` (Plan 3) and
read on every server boot. Schema is pinned to `schema_version: 1` for
forward-compat; loaders refuse unknown schema versions to fail loudly
rather than misinterpret an old-shape file.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

_AGENTCORE_FILE = "agentcore.json"
_CURRENT_SCHEMA_VERSION = 1


class AgentCoreConfigError(Exception):
    """Raised when agentcore.json is missing required fields, corrupt,
    or carries an unsupported schema_version."""


@dataclass(frozen=True)
class MemoryRecord:
    """Per-memory metadata persisted at init time and consumed at startup."""

    memory_id: str
    memory_arn: str
    memory_name: str
    strategy_id: str
    strategy_name: str
    event_expiry_duration_days: int


@dataclass(frozen=True)
class AgentCoreConfig:
    """Top-level shape of `agentcore.json`."""

    schema_version: int
    region: str
    semantic: MemoryRecord
    episodic: MemoryRecord


def _config_path(home: Path) -> Path:
    return home / _AGENTCORE_FILE


def save_agentcore_config(cfg: AgentCoreConfig, home: Path) -> None:
    """Write the config to `<home>/agentcore.json` atomically (write to
    tmp, then rename)."""
    home.mkdir(parents=True, exist_ok=True)
    target = _config_path(home)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(asdict(cfg), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(target)


def load_agentcore_config(home: Path) -> AgentCoreConfig | None:
    """Load the config from `<home>/agentcore.json`. Returns None if the
    file does not exist. Raises AgentCoreConfigError on corruption or
    unsupported schema version."""
    target = _config_path(home)
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AgentCoreConfigError(
            f"failed to parse {target}: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise AgentCoreConfigError(f"{target} is not a JSON object")

    schema_version = raw.get("schema_version")
    if schema_version != _CURRENT_SCHEMA_VERSION:
        raise AgentCoreConfigError(
            f"{target} has unsupported schema_version={schema_version!r}; "
            f"expected {_CURRENT_SCHEMA_VERSION}"
        )

    for required in ("region", "semantic", "episodic"):
        if required not in raw:
            raise AgentCoreConfigError(
                f"{target} missing required field {required!r}"
            )

    try:
        return AgentCoreConfig(
            schema_version=schema_version,
            region=raw["region"],
            semantic=MemoryRecord(**raw["semantic"]),
            episodic=MemoryRecord(**raw["episodic"]),
        )
    except TypeError as exc:
        raise AgentCoreConfigError(
            f"{target} has malformed semantic / episodic block: {exc}"
        ) from exc
