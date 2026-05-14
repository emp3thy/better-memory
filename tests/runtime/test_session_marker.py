"""Unit tests for the session-id marker bridge."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from better_memory.runtime.session_marker import (
    encode_project_dir,
    marker_path,
    read_session_id,
    write_session_id,
)


class TestEncodeProjectDir:
    def test_replaces_non_alphanumeric_with_dash(self):
        assert encode_project_dir("C:\\Users\\me\\proj") == "C--Users-me-proj"

    def test_keeps_alphanumeric_intact(self):
        assert encode_project_dir("abcXYZ123") == "abcXYZ123"

    def test_posix_path(self):
        assert encode_project_dir("/home/me/proj") == "-home-me-proj"


class TestRoundTrip:
    def test_write_then_read(self, tmp_path: Path):
        write_session_id(tmp_path, "abc-123", project_dir="/p")
        assert read_session_id(tmp_path, project_dir="/p") == "abc-123"

    def test_overwrite_replaces_existing(self, tmp_path: Path):
        write_session_id(tmp_path, "first", project_dir="/p")
        write_session_id(tmp_path, "second", project_dir="/p")
        assert read_session_id(tmp_path, project_dir="/p") == "second"

    def test_per_project_isolation(self, tmp_path: Path):
        write_session_id(tmp_path, "A", project_dir="/projA")
        write_session_id(tmp_path, "B", project_dir="/projB")
        assert read_session_id(tmp_path, project_dir="/projA") == "A"
        assert read_session_id(tmp_path, project_dir="/projB") == "B"


class TestReadMissing:
    def test_returns_none_when_dir_absent(self, tmp_path: Path):
        assert read_session_id(tmp_path, project_dir="/never-written") is None

    def test_returns_none_for_empty_file(self, tmp_path: Path):
        path = marker_path(tmp_path, project_dir="/p")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        assert read_session_id(tmp_path, project_dir="/p") is None

    def test_strips_trailing_whitespace(self, tmp_path: Path):
        path = marker_path(tmp_path, project_dir="/p")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("  sid-1\n")
        assert read_session_id(tmp_path, project_dir="/p") == "sid-1"


class TestWriteRobustness:
    def test_empty_session_id_is_no_op(self, tmp_path: Path):
        write_session_id(tmp_path, "", project_dir="/p")
        # No marker file, no exception.
        assert not marker_path(tmp_path, project_dir="/p").exists()

    def test_no_tempfile_leftover_after_success(self, tmp_path: Path):
        write_session_id(tmp_path, "x", project_dir="/p")
        sessions_dir = (tmp_path / "runtime" / "sessions")
        leftover = [p for p in sessions_dir.iterdir() if p.name.startswith(".sid-")]
        assert leftover == []

    def test_closes_fd_when_fdopen_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Regression for fd leak: os.fdopen takes ownership of fd only on
        # successful return. If it raises, the caller must close fd.
        closed: list[int] = []
        real_close = os.close

        def tracking_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        def raising_fdopen(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise ValueError("simulated fdopen failure")

        monkeypatch.setattr(os, "close", tracking_close)
        monkeypatch.setattr(os, "fdopen", raising_fdopen)

        # write_session_id only suppresses OSError, so ValueError propagates.
        with pytest.raises(ValueError):
            write_session_id(tmp_path, "x", project_dir="/p")

        # The fd from mkstemp must have been closed exactly once.
        assert len(closed) == 1
        # And tmp file must have been unlinked.
        sessions_dir = (tmp_path / "runtime" / "sessions")
        if sessions_dir.exists():
            leftover = [
                p for p in sessions_dir.iterdir() if p.name.startswith(".sid-")
            ]
            assert leftover == []


class TestProjectDirResolution:
    def test_uses_claude_project_dir_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/from-env")
        write_session_id(tmp_path, "envsid")
        # Path lookup using explicit project_dir matching the env value works.
        assert read_session_id(tmp_path, project_dir="/from-env") == "envsid"

    def test_falls_back_to_cwd_without_env(
        self, tmp_path: Path, monkeypatch,
    ):
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        write_session_id(tmp_path, "cwdsid")
        # The marker is written under the cwd's encoded form.
        encoded = encode_project_dir(os.getcwd())
        path = tmp_path / "runtime" / "sessions" / encoded
        assert path.is_file()
        assert path.read_text().strip() == "cwdsid"
