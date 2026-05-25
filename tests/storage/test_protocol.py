"""Tests that the StorageBackend Protocol shape is what consumers expect."""

from __future__ import annotations

from better_memory.storage import StorageBackend


def test_protocol_is_runtime_checkable() -> None:
    """The Protocol must be runtime_checkable so MCP server can isinstance()."""
    # If StorageBackend isn't decorated with @runtime_checkable this will
    # raise TypeError at isinstance() time, not at import. We force the check.
    class _Stub:
        @property
        def supports_synthesis(self) -> bool:
            return False

    # We don't actually expect _Stub to satisfy the full protocol; this just
    # verifies the protocol accepts an isinstance probe at all.
    try:
        isinstance(_Stub(), StorageBackend)
    except TypeError as exc:
        raise AssertionError(
            "StorageBackend must be decorated with @runtime_checkable"
        ) from exc


def test_protocol_declares_capability_flag() -> None:
    """supports_synthesis is the capability used by MCP for conditional tool registration."""
    assert hasattr(StorageBackend, "supports_synthesis")


def test_protocol_declares_hot_path_methods() -> None:
    """Read/write/credit path methods that every backend MUST implement."""
    required = {
        "observe", "retrieve", "retrieve_observations",
        "record_use",
        "semantic_observe", "semantic_retrieve", "semantic_update", "semantic_delete",
        "start_episode", "list_episodes", "close_episode",
        "session_bootstrap",
        "list_session_exposures", "apply_session_ratings",
        "promote_reflection", "retire_reflection",
    }
    actual = set(dir(StorageBackend))
    missing = required - actual
    assert not missing, f"Protocol missing methods: {sorted(missing)}"


def test_protocol_declares_synthesis_methods() -> None:
    """Synthesis methods exist on the protocol but only sqlite-backed
    implementations are expected to implement them."""
    synthesis = {"synthesize_next_get_context", "synthesize_next_apply"}
    actual = set(dir(StorageBackend))
    missing = synthesis - actual
    assert not missing, f"Protocol missing synthesis methods: {sorted(missing)}"
