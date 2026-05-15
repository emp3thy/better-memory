"""Tests for the :mod:`better_memory._diag` enablement gate."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from better_memory import _diag


@pytest.fixture(autouse=True)
def _reset_diag() -> Iterator[None]:
    """Clear the memoized gate before and after each test for isolation."""
    _diag._reset_enabled_cache()
    yield
    _diag._reset_enabled_cache()


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("BETTER_MEMORY_EMBED_LOG", "BETTER_MEMORY_DIAG_LOGGING"):
        monkeypatch.delenv(var, raising=False)


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With neither knob set, diagnostics are off."""
    _clear_env(monkeypatch)
    assert _diag.enabled() is False


def test_config_true_env_unset_enables(monkeypatch: pytest.MonkeyPatch) -> None:
    """config.diag_logging=true with no env override turns diagnostics on."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BETTER_MEMORY_DIAG_LOGGING", "1")
    assert _diag.enabled() is True


def test_env_zero_overrides_config_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """BETTER_MEMORY_EMBED_LOG=0 disables even when config.diag_logging is true."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BETTER_MEMORY_DIAG_LOGGING", "1")
    monkeypatch.setenv("BETTER_MEMORY_EMBED_LOG", "0")
    assert _diag.enabled() is False


def test_env_truthy_overrides_config_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """BETTER_MEMORY_EMBED_LOG truthy enables even when config defaults off."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BETTER_MEMORY_EMBED_LOG", "1")
    assert _diag.enabled() is True


def test_enabled_is_memoized(monkeypatch: pytest.MonkeyPatch) -> None:
    """enabled() caches its first result; later env changes need a reset."""
    _clear_env(monkeypatch)
    assert _diag.enabled() is False
    monkeypatch.setenv("BETTER_MEMORY_EMBED_LOG", "1")
    assert _diag.enabled() is False  # still the cached value
    _diag._reset_enabled_cache()
    assert _diag.enabled() is True


def test_config_exposes_diag_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Config.diag_logging resolves from BETTER_MEMORY_DIAG_LOGGING."""
    from better_memory.config import get_config

    monkeypatch.delenv("BETTER_MEMORY_DIAG_LOGGING", raising=False)
    assert get_config().diag_logging is False
    monkeypatch.setenv("BETTER_MEMORY_DIAG_LOGGING", "true")
    assert get_config().diag_logging is True
