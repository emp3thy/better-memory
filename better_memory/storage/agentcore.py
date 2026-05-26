"""AgentCoreBackend — boto3 wrapper satisfying StorageBackend Protocol.

Constructor takes pre-built boto3 clients (data plane + control plane)
plus the loaded AgentCoreConfig. Tests inject MagicMock clients; the
factory (Task 13) constructs real clients via boto3.client(...). This
inversion keeps tests fast and free of botocore deps.

Capability flags:
- supports_synthesis = False — AgentCore's built-in episodicMemoryStrategy
  performs extraction internally; the MCP synthesize_next_* tools are not
  registered in agentcore mode.
- supports_episodes = False — AgentCore manages event grouping via
  sessionId; the better-memory episodes table has no equivalent record
  type. Episode lifecycle methods are no-ops returning synthetic ids /
  empty results; the management UI hides the Episodes tab.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from better_memory.storage.agentcore_persistence import AgentCoreConfig
from better_memory.storage.protocol import Outcome, UseOutcome


class AgentCoreBackend:
    """boto3-backed StorageBackend implementation."""

    def __init__(
        self,
        *,
        config: AgentCoreConfig,
        data_client: Any,
        control_client: Any,
        session_id: str | None,
        project: str,
    ) -> None:
        self._cfg = config
        self._data = data_client
        self._control = control_client
        self._session_id = session_id
        self._project = project

    # ----- Capability flags -----

    @property
    def supports_synthesis(self) -> bool:
        return False

    @property
    def supports_episodes(self) -> bool:
        return False

    # ----- Observations: filled in by Tasks 5-6 -----

    async def observe(self, **kwargs: Any) -> str:
        raise NotImplementedError("Implemented in Task 5")

    def retrieve(self, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        # Signature matches the Plan-1-amended Protocol (Task 0): sync,
        # returns dict[polarity, list[reflection_dict]].
        raise NotImplementedError("Implemented in Task 7")

    async def list_observations(self, **kwargs: Any) -> list[dict[str, Any]]:
        raise NotImplementedError("Implemented in Task 6")

    def record_use(
        self, observation_id: str, *, outcome: UseOutcome | None = None
    ) -> None:
        raise NotImplementedError("Implemented in Task 8")

    # ----- Semantic memories: Task 11 -----

    def semantic_observe(self, **kwargs: Any) -> str:
        raise NotImplementedError("Implemented in Task 11")

    def semantic_list(self, **kwargs: Any) -> list[Any]:
        raise NotImplementedError("Implemented in Task 11")

    def semantic_update_text(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 11")

    def semantic_set_scope(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 11")

    def semantic_delete(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 11")

    # ----- Episodes: no-ops in agentcore mode (this task) -----

    def open_background_episode(
        self, *, session_id: str, project: str
    ) -> str:
        # No real episode; return a synthetic id so the MCP tool path
        # works. The id is not used anywhere downstream in agentcore mode.
        return f"agentcore-noop-bg-{uuid4().hex[:12]}"

    def start_foreground_episode(
        self,
        *,
        session_id: str,
        project: str,
        goal: str,
        tech: str | None = None,
    ) -> str:
        return f"agentcore-noop-fg-{uuid4().hex[:12]}"

    def close_active_episode(
        self,
        *,
        session_id: str,
        outcome: str,
        close_reason: str,
        summary: str | None = None,
    ) -> str:
        return ""

    def close_episode_by_id(
        self,
        *,
        episode_id: str,
        outcome: str,
        close_reason: str,
        summary: str | None = None,
    ) -> str:
        return ""

    def list_episodes(
        self,
        *,
        project: str | None = None,
        outcome: str | None = None,
        only_open: bool = False,
    ) -> list[Any]:
        return []

    # ----- Reflection lifecycle: Task 10 -----

    def promote_reflection(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 10")

    def retire_reflection(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 10")

    # ----- Session lifecycle: Tasks 9, 12 -----

    def session_bootstrap(self, **kwargs: Any) -> Any:
        raise NotImplementedError("Implemented in Task 12")

    def list_session_exposures(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError("Implemented in Task 9")

    def apply_session_ratings(self, **kwargs: Any) -> Any:
        raise NotImplementedError("Implemented in Task 9")

    def credit_one(self, **kwargs: Any) -> Any:
        raise NotImplementedError("Implemented in Task 9")

    # ----- Synthesis: no-ops in agentcore mode -----

    def synthesize_next_get_context(self, *, project: str) -> Any:
        # No-op in agentcore mode: AgentCore's built-in episodicMemoryStrategy
        # performs extraction internally, so there is never a "pending
        # episode" to drain. Return None — sqlite mode also returns None when
        # nothing is pending, so callers that don't check supports_synthesis
        # see a quiet "nothing to do" signal instead of a crash.
        return None

    def synthesize_next_apply(
        self, *, episode_id: str, response: Any, project: str
    ) -> Any:
        # No-op in agentcore mode. Return the empty SynthesisStep-shaped dict
        # ("nothing applied, nothing skipped") matching the rest of the
        # agentcore-mode no-op contract (list_session_exposures empty
        # envelope, list_episodes empty list, episode close empty string).
        return {"applied": 0, "skipped": 0}
