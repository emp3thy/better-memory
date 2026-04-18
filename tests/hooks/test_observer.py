"""Tests for ``better_memory.hooks.observer``.

The observer is invoked as a subprocess (``python -m ...``) to exercise the
module-as-script behaviour that Claude Code's hook runner uses. It reads a
JSON payload from stdin, writes a file to the spool directory, and exits 0.
It must never raise — even on malformed or empty input.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _run_observer(
    tmp_spool: Path,
    *,
    stdin: str,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "BETTER_MEMORY_SPOOL_DIR": str(tmp_spool)}
    return subprocess.run(
        [sys.executable, "-m", "better_memory.hooks.observer"],
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )


@pytest.fixture
def tmp_spool(tmp_path: Path) -> Path:
    spool = tmp_path / "spool"
    spool.mkdir()
    return spool


def test_observer_writes_file_for_valid_json(tmp_spool: Path) -> None:
    payload = {
        "event_type": "tool_use",
        "tool": "Edit",
        "file": "auth.py",
        "content_snippet": "hello",
        "cwd": "/tmp",
        "session_id": "sess-1",
        "timestamp": "2026-04-18T12:00:00Z",
    }
    result = _run_observer(tmp_spool, stdin=json.dumps(payload))

    assert result.returncode == 0, result.stderr
    files = list(tmp_spool.glob("*.json"))
    assert len(files) == 1
    written = json.loads(files[0].read_text(encoding="utf-8"))
    assert written["event_type"] == "tool_use"
    assert written["tool"] == "Edit"
    assert written["file"] == "auth.py"


def test_observer_filename_contains_tool_and_json_ext(tmp_spool: Path) -> None:
    payload = {
        "event_type": "tool_use",
        "tool": "Bash",
        "timestamp": "2026-04-18T12:00:00Z",
    }
    result = _run_observer(tmp_spool, stdin=json.dumps(payload))

    assert result.returncode == 0
    files = list(tmp_spool.glob("*.json"))
    assert len(files) == 1
    name = files[0].name
    assert name.endswith(".json")
    assert "Bash" in name
    # ":" is a reserved char on NTFS; must have been scrubbed from the ts.
    assert ":" not in name


def test_observer_defaults_event_type_when_missing(tmp_spool: Path) -> None:
    payload = {
        "tool": "Edit",
        "timestamp": "2026-04-18T12:00:00Z",
    }
    result = _run_observer(tmp_spool, stdin=json.dumps(payload))
    assert result.returncode == 0
    files = list(tmp_spool.glob("*.json"))
    assert len(files) == 1
    written = json.loads(files[0].read_text(encoding="utf-8"))
    assert written["event_type"] == "tool_use"


def test_observer_empty_stdin_exits_zero(tmp_spool: Path) -> None:
    result = _run_observer(tmp_spool, stdin="")
    assert result.returncode == 0
    # Empty stdin cannot be parsed as JSON, so no file is written.
    assert list(tmp_spool.glob("*.json")) == []


def test_observer_invalid_json_exits_zero_no_file(tmp_spool: Path) -> None:
    result = _run_observer(tmp_spool, stdin="not json at all {{{")
    assert result.returncode == 0
    assert list(tmp_spool.glob("*.json")) == []


def test_observer_two_identical_payloads_produce_distinct_files(
    tmp_spool: Path,
) -> None:
    # Same tool, same timestamp — the hash component should vary and prevent
    # collisions. We distinguish payloads only by content to exercise the hash.
    payload_a = {
        "event_type": "tool_use",
        "tool": "Edit",
        "file": "a.py",
        "timestamp": "2026-04-18T12:00:00Z",
    }
    payload_b = {
        "event_type": "tool_use",
        "tool": "Edit",
        "file": "b.py",
        "timestamp": "2026-04-18T12:00:00Z",
    }
    r1 = _run_observer(tmp_spool, stdin=json.dumps(payload_a))
    r2 = _run_observer(tmp_spool, stdin=json.dumps(payload_b))
    assert r1.returncode == 0
    assert r2.returncode == 0
    files = list(tmp_spool.glob("*.json"))
    assert len(files) == 2
    names = {f.name for f in files}
    assert len(names) == 2


def test_observer_same_payload_twice_produces_distinct_filenames(
    tmp_spool: Path,
) -> None:
    # Byte-identical stdin on two successive invocations must still result in
    # two distinct spool files — the hash salt (ns clock + PID) guarantees
    # uniqueness. Without the salt, the second invocation would overwrite the
    # first, silently losing an event.
    payload = json.dumps(
        {
            "event_type": "tool_use",
            "tool": "Edit",
            "file": "auth.py",
            "timestamp": "2026-04-18T12:00:00Z",
        }
    )
    r1 = _run_observer(tmp_spool, stdin=payload)
    r2 = _run_observer(tmp_spool, stdin=payload)
    assert r1.returncode == 0, r1.stderr
    assert r2.returncode == 0, r2.stderr
    files = list(tmp_spool.glob("*.json"))
    assert len(files) == 2, [f.name for f in files]
    assert len({f.name for f in files}) == 2


def test_observer_oversized_stdin_is_dropped(tmp_spool: Path) -> None:
    # Anything strictly larger than 1 MiB should exit 0 without writing a
    # spool file — hooks never fail, but they also never allocate unbounded
    # memory. We build a valid JSON document whose serialised form is well
    # over the cap.
    huge_value = "x" * (1_048_576 + 16)
    payload = json.dumps({"event_type": "tool_use", "tool": "Edit", "blob": huge_value})
    assert len(payload) > 1_048_576  # sanity — the cap must actually be tripped

    result = _run_observer(tmp_spool, stdin=payload)
    assert result.returncode == 0, result.stderr
    assert list(tmp_spool.glob("*.json")) == []


def test_observer_missing_tool_field_still_succeeds(tmp_spool: Path) -> None:
    payload = {
        "event_type": "tool_use",
        "timestamp": "2026-04-18T12:00:00Z",
    }
    result = _run_observer(tmp_spool, stdin=json.dumps(payload))
    assert result.returncode == 0
    files = list(tmp_spool.glob("*.json"))
    assert len(files) == 1
    # Filename should include a sensible placeholder for the tool.
    assert "unknown" in files[0].name.lower()
