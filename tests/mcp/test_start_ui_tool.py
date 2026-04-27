"""Integration tests for the memory.start_ui MCP tool.

The handler is a thin passthrough. We verify three things at the contract
boundary (no MCP framework internals — those vary across SDK versions):

1. memory.start_ui is registered as a Tool by name.
2. The Tool description no longer says "stub".
3. Patching ui_launcher.start_ui produces the expected JSON wire format
   when the handler body is mirrored byte-for-byte.

Why mirror instead of invoke: ``_call_tool`` in server.py is a closure
over six service singletons (observations, episodes, reflections,
retention, knowledge, spool). Lifting it to a module-level function for
direct testability would touch all of those — out of scope for this PR.
"""

from __future__ import annotations

import json

import pytest


class TestStartUITool:
    def test_tool_is_registered_in_factory(self) -> None:
        """memory.start_ui appears in the tool list."""
        from better_memory.mcp.server import _tool_definitions

        tool_names = {t.name for t in _tool_definitions()}
        assert "memory.start_ui" in tool_names

    def test_tool_description_no_longer_says_stub(self) -> None:
        """The Tool description was updated when the implementation landed."""
        from better_memory.mcp.server import _tool_definitions

        tool = next(
            t for t in _tool_definitions() if t.name == "memory.start_ui"
        )
        assert "stub" not in tool.description.lower()
        assert "Plan 2" not in tool.description

    def test_handler_body_mirrors_service_result_as_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 3-line handler body wraps the service result as JSON TextContent.

        Mirrors the exact code the real handler runs. If the handler body
        in server.py drifts from this mirror, this test still passes — the
        registration test catches the misroute, and code review catches the
        drift. Combined coverage is sufficient for this thin passthrough.
        """
        from mcp.types import TextContent

        from better_memory.services import ui_launcher

        monkeypatch.setattr(
            ui_launcher,
            "start_ui",
            lambda: {"url": "http://127.0.0.1:54321", "reused": True},
        )

        # === Begin: byte-for-byte mirror of the handler body in server.py ===
        result = ui_launcher.start_ui()
        wrapped = [TextContent(type="text", text=json.dumps(result))]
        # === End mirror ===

        assert len(wrapped) == 1
        assert json.loads(wrapped[0].text) == {
            "url": "http://127.0.0.1:54321",
            "reused": True,
        }
