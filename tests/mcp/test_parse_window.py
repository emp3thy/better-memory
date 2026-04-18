"""Unit tests for :func:`better_memory.mcp.server._parse_window`.

These exercise the small window-string parser used by ``memory.retrieve``.
They do not touch the MCP server itself and so run cleanly under the
non-integration default suite.
"""

from __future__ import annotations

import pytest

from better_memory.mcp.server import _parse_window


@pytest.mark.parametrize(
    "value,expected",
    [
        ("30d", 30),
        ("30D", 30),
        ("1d", 1),
        ("24h", 1),
        ("1h", 1),
        ("none", None),
        ("", None),
        (None, 30),
    ],
)
def test_parse_window_valid(value: str | None, expected: int | None) -> None:
    assert _parse_window(value) == expected


# "-5d" is included because the parser should reject negatives explicitly —
# a negative window is nonsense. "5s" is rejected because only d/h suffixes
# are supported. "abc", "30", "30x" are malformed tokens.
@pytest.mark.parametrize("value", ["abc", "30", "30x", "-5d", "5s"])
def test_parse_window_invalid(value: str) -> None:
    with pytest.raises(ValueError):
        _parse_window(value)
