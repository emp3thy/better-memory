"""Verify install_hooks creates the rate-session-memories skill symlink."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


def _symlinks_available() -> bool:
    """Return True if the current process can create directory symlinks."""
    try:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            lnk = Path(td) / "lnk"
            lnk.symlink_to(src, target_is_directory=True)
            return lnk.is_symlink()
    except (OSError, NotImplementedError):
        return False


_SYMLINKS_AVAILABLE = _symlinks_available()

requires_symlinks = pytest.mark.skipif(
    not _SYMLINKS_AVAILABLE,
    reason="Symlink creation requires admin rights or Developer Mode on Windows",
)


@pytest.fixture
def tmp_skills_dir(tmp_path: Path):
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    return skills


@requires_symlinks
def test_install_creates_rate_session_memories_symlink(
    tmp_skills_dir: Path, monkeypatch,
):
    """After install_hooks runs, the user-level skill symlink exists and
    points at the in-repo SKILL.md."""
    from better_memory.cli import install_hooks as ih

    monkeypatch.setattr(ih, "_resolve_user_skills_dir", lambda: tmp_skills_dir)
    ih.install_skill_symlinks()

    link = tmp_skills_dir / "rate-session-memories"
    assert link.is_symlink()
    target = link.resolve()
    assert target.name == "rate-session-memories"
    assert (target / "SKILL.md").exists()
