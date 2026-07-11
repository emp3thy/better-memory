"""Tests that the StorageBackend Protocol shape is what consumers expect."""

from __future__ import annotations

import inspect

from better_memory.storage import StorageBackend


def test_protocol_is_runtime_checkable() -> None:
    """@runtime_checkable sets _is_runtime_protocol = True. MCP server relies on isinstance()."""
    assert getattr(StorageBackend, "_is_runtime_protocol", False), (
        "StorageBackend must be decorated with @runtime_checkable"
    )


def test_protocol_declares_capability_flag() -> None:
    """supports_synthesis is the capability MCP uses for conditional tool registration."""
    assert hasattr(StorageBackend, "supports_synthesis")


def test_protocol_declares_supports_episodes_flag() -> None:
    """supports_episodes is the capability used by management UI to hide the Episodes tab in agentcore mode."""
    assert hasattr(StorageBackend, "supports_episodes")


def test_protocol_declares_async_hot_path() -> None:
    """observe / list_observations are async to match the existing service surface.

    retrieve is intentionally excluded here — Plan 2 Task 0 amendment made it
    sync because it wraps ReflectionSynthesisService.retrieve_reflections
    (no embedder call). See test_protocol_retrieve_is_sync_with_reflection_kwargs."""
    for name in ("observe", "list_observations"):
        method = getattr(StorageBackend, name, None)
        assert method is not None, f"Protocol missing {name}"
        assert inspect.iscoroutinefunction(method), (
            f"Protocol method {name!r} must be async"
        )


def test_protocol_retrieve_is_sync_with_reflection_kwargs() -> None:
    """Protocol.retrieve is sync and wraps the bucketed reflection retrieval."""
    method = StorageBackend.retrieve
    assert not inspect.iscoroutinefunction(method)
    sig = inspect.signature(method)
    kwargs = {p.name for p in sig.parameters.values() if p.name != "self"}
    assert {
        "project", "tech", "phase", "polarity",
        "limit_per_bucket", "track_exposure",
    } <= kwargs
    # The old observation-bucketing kwargs are gone.
    assert "candidate_k" not in kwargs
    assert "reinforcement_alpha" not in kwargs
    assert "do_limit" not in kwargs
    assert "dont_limit" not in kwargs
    assert "neutral_limit" not in kwargs
    assert "window_days" not in kwargs


def test_protocol_declares_sync_record_use() -> None:
    """record_use is sync (ObservationService.record_use is sync)."""
    method = getattr(StorageBackend, "record_use", None)
    assert method is not None
    assert not inspect.iscoroutinefunction(method)


def test_protocol_declares_all_required_methods() -> None:
    """Every method backends must implement."""
    required = {
        # Observations
        "observe", "retrieve", "list_observations", "record_use",
        # Semantic memories
        "semantic_observe", "semantic_list", "semantic_update_text",
        "semantic_set_scope", "semantic_delete",
        # Episodes
        "open_background_episode", "start_foreground_episode",
        "close_active_episode", "close_episode_by_id", "list_episodes",
        # Reflection lifecycle
        "promote_reflection", "retire_reflection",
        # Session lifecycle
        "session_bootstrap", "list_session_exposures",
        "apply_session_ratings", "credit_one", "record_exposures",
        # Synthesis (sqlite-only — gated by supports_synthesis)
        "synthesize_next_get_context", "synthesize_next_apply",
    }
    actual = set(dir(StorageBackend))
    missing = required - actual
    assert not missing, f"Protocol missing methods: {sorted(missing)}"


def test_protocol_methods_are_keyword_only() -> None:
    """Stable cross-backend interface requires kwarg-only signatures."""
    for name in (
        "observe", "semantic_observe", "open_background_episode",
        "close_active_episode", "apply_session_ratings", "credit_one",
        "synthesize_next_apply", "record_exposures",
    ):
        method = getattr(StorageBackend, name)
        sig = inspect.signature(method)
        non_self = [p for p in sig.parameters.values() if p.name != "self"]
        kinds = {p.kind for p in non_self}
        assert kinds <= {inspect.Parameter.KEYWORD_ONLY}, (
            f"{name} must be keyword-only; got "
            f"{[(p.name, p.kind.name) for p in non_self]}"
        )
