"""e2e-sqlite-ollama-absent-default-backend (design §1C row C8, task 5).

Task 2 (remove-ollama-embeddings) deleted ``create_server``'s embedder
construction and Ollama probe entirely: no code path builds an
``OllamaEmbedder``/``SyncEmbedder`` or contacts Ollama any more, regardless
of ``BETTER_MEMORY_EMBEDDINGS_BACKEND``. The original "ollama absent
default backend degrades in-band" contract this module pinned no longer
applies -- there is nothing left to degrade.

This module now pins the inverse, stronger contract: the pip-install
default path (no ``BETTER_MEMORY_EMBEDDINGS_BACKEND`` exported, no Ollama
installed) just WORKS -- ``memory.observe`` succeeds, fast, with no stderr
warning and no orphan episode -- because no embedding is ever attempted.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path

from tests.e2e._env import isolated_env
from tests.e2e.conftest import mcp_session


def _text_of(result: object) -> str:
    return "".join(
        getattr(block, "text", "") for block in getattr(result, "content", [])
    )


async def test_ollama_absent_default_backend_succeeds_without_embedder(
    clean_slate_home: Path, tmp_path: Path
) -> None:
    """Default (unset) BETTER_MEMORY_EMBEDDINGS_BACKEND + no Ollama running:
    memory.observe succeeds -- no probe, no embed attempt, no stderr
    warning, no orphan episode.

    Regressions this flips red on (design C8, inverted by Task 2):
    * embedder construction or a startup probe being reintroduced into
      mcp/server.py -- would reintroduce a stderr warning and/or slow /
      error the observe call;
    * retry/timeout inflation reappearing (~90s stalls per observe) -- the
      generous <10s budget catches it while staying non-flaky;
    * the KNOWN-DEFECT orphan-episode behaviour reappearing -- observe now
      writes both the background episode AND the observation row in the
      same call, since nothing fails partway through any more.
    """
    env = isolated_env(
        clean_slate_home,
        # Remove the harness default 'sqlite' pin: the whole point is the
        # real out-of-the-box default ('ollama' per config.py), which must
        # now behave identically because nothing reads it for embedder
        # construction any more.
        BETTER_MEMORY_EMBEDDINGS_BACKEND=None,
    )
    errlog_path = tmp_path / "server.stderr"  # outside the fake home

    with errlog_path.open("w", encoding="utf-8") as errlog:
        async with mcp_session(env, errlog=errlog) as session:
            # Boot survived (initialize happened inside mcp_session) and the
            # embedding-dependent tool is still advertised.
            listed = await session.list_tools()
            assert "memory.observe" in {tool.name for tool in listed.tools}

            t0 = time.monotonic()
            r_observe = await session.call_tool(
                "memory.observe", {"content": "e2e negative-path observation"}
            )
            observe_elapsed = time.monotonic() - t0

            r_knowledge = await session.call_tool("knowledge.list", {})

    assert observe_elapsed < 10.0, (
        f"observe took {observe_elapsed:.1f}s — no-embedder wiring lost? "
        "(embedder construction reintroduced into mcp/server.py)"
    )

    assert not r_observe.isError, _text_of(r_observe)
    assert not r_knowledge.isError, _text_of(r_knowledge)

    # No startup probe left a stderr breadcrumb -- _probe_ollama is gone.
    stderr_text = errlog_path.read_text(encoding="utf-8", errors="replace")
    assert "Ollama unreachable" not in stderr_text

    # No orphan episode any more: the observe call commits both the
    # background episode and the observation row -- nothing fails
    # partway through, because no embedding is ever attempted.
    db = clean_slate_home / ".better-memory" / "memory.db"
    with closing(sqlite3.connect(db)) as conn:
        (episodes,) = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()
        (observations,) = conn.execute(
            "SELECT COUNT(*) FROM observations"
        ).fetchone()
    assert episodes == 1
    assert observations == 1
