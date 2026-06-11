"""Unit tests for the synthesize JSONL audit log.

The synthesize drain loop is driven by the IDE LLM across many
round-trips. When that loop appears to freeze, the audit log is the
only server-side evidence we have. These tests cover the writer +
context manager directly — the handler wiring is exercised by the
existing ``test_synthesize_tools.py`` end-to-end suite.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from better_memory.mcp.synth_audit import (
    append_synth_audit as _append_synth_audit,
)
from better_memory.mcp.synth_audit import (
    audit_synth_call as _audit_synth_call,
)


def _read_jsonl(home: Path) -> list[dict]:
    log_path = home / "logs" / "synthesize.jsonl"
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestAppendSynthAudit:
    def test_creates_logs_dir_and_appends_one_line(self, tmp_path: Path) -> None:
        _append_synth_audit(tmp_path, {"phase": "start", "tool": "get_context"})
        rows = _read_jsonl(tmp_path)
        assert rows == [{"phase": "start", "tool": "get_context"}]

    def test_appends_in_order(self, tmp_path: Path) -> None:
        _append_synth_audit(tmp_path, {"i": 1})
        _append_synth_audit(tmp_path, {"i": 2})
        _append_synth_audit(tmp_path, {"i": 3})
        assert [r["i"] for r in _read_jsonl(tmp_path)] == [1, 2, 3]

    def test_swallows_io_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A logs path that can't be created must NOT raise.

        Simulated by passing a file (not a directory) as ``home`` so
        ``home/logs`` cannot be created. The call still returns
        normally; the failure is logged via the module logger.
        """
        not_a_dir = tmp_path / "blocker"
        not_a_dir.write_text("blocker", encoding="utf-8")

        with caplog.at_level(logging.ERROR, logger="better_memory.mcp.synth_audit"):
            _append_synth_audit(not_a_dir, {"phase": "start"})  # must not raise

        assert any(
            "synth audit write failed" in r.getMessage()
            for r in caplog.records
        )


class TestAuditSynthCall:
    def test_writes_start_and_complete_pair_on_success(self, tmp_path: Path) -> None:
        with _audit_synth_call(
            tmp_path, tool="get_context", project="p1", episode_id=None,
        ) as state:
            state["result_kind"] = "episode"
            state["episode_id"] = "ep-42"
            state["obs_count"] = 3
            state["refl_count"] = 12

        rows = _read_jsonl(tmp_path)
        assert len(rows) == 2
        start, complete = rows

        assert start["phase"] == "start"
        assert start["tool"] == "get_context"
        assert start["project"] == "p1"
        assert start["episode_id"] is None  # unknown at start for get_context
        assert "call_id" in start
        assert "ts" in start

        assert complete["phase"] == "complete"
        assert complete["call_id"] == start["call_id"]
        assert complete["tool"] == "get_context"
        assert complete["result_kind"] == "episode"
        assert complete["episode_id"] == "ep-42"
        assert complete["obs_count"] == 3
        assert complete["refl_count"] == 12
        assert isinstance(complete["latency_ms"], int)
        assert complete["latency_ms"] >= 0

    def test_complete_row_records_exception_and_re_raises(
        self, tmp_path: Path,
    ) -> None:
        with pytest.raises(RuntimeError, match="kaboom"):
            with _audit_synth_call(
                tmp_path, tool="apply", project="p1", episode_id="ep-1",
            ):
                raise RuntimeError("kaboom")

        rows = _read_jsonl(tmp_path)
        assert len(rows) == 2
        start, complete = rows
        assert complete["phase"] == "complete"
        assert complete["call_id"] == start["call_id"]
        assert complete["result_kind"] == "exception"
        assert "RuntimeError" in complete["error"]
        assert "kaboom" in complete["error"]
        # episode_id propagates through to the complete row.
        assert complete["episode_id"] == "ep-1"
        # The original exception's body still propagates.

    def test_caller_can_set_result_kind_to_validation_error_then_return(
        self, tmp_path: Path,
    ) -> None:
        """Validation/state errors are NOT exceptions — the handler
        sets ``result_kind`` and returns inside the with block. The
        complete row should reflect what the handler set, not 'exception'.
        """
        with _audit_synth_call(
            tmp_path, tool="apply", project="p1", episode_id="ep-1",
        ) as state:
            state["result_kind"] = "validation_error"
            state["error"] = "missing field"
            # Mimic the handler's early-return.

        rows = _read_jsonl(tmp_path)
        assert rows[-1]["result_kind"] == "validation_error"
        assert rows[-1]["error"] == "missing field"

    def test_call_ids_differ_across_invocations(self, tmp_path: Path) -> None:
        for _ in range(3):
            with _audit_synth_call(
                tmp_path, tool="get_context", project="p1", episode_id=None,
            ) as state:
                state["result_kind"] = "empty"

        rows = _read_jsonl(tmp_path)
        starts = [r for r in rows if r["phase"] == "start"]
        assert len({r["call_id"] for r in starts}) == 3

    def test_audit_io_failure_does_not_prevent_handler_logic(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """If the audit log path can't be written, the wrapped handler
        body still runs and its result still propagates."""
        not_a_dir = tmp_path / "blocker"
        not_a_dir.write_text("blocker", encoding="utf-8")
        ran: list[int] = []

        with caplog.at_level(logging.ERROR, logger="better_memory.mcp.synth_audit"):
            with _audit_synth_call(
                not_a_dir, tool="apply", project="p1", episode_id="ep-1",
            ) as state:
                ran.append(1)
                state["result_kind"] = "applied"

        assert ran == [1]
        # The audit failures were logged but did not raise.
        assert any(
            "synth audit write failed" in r.getMessage()
            for r in caplog.records
        )
