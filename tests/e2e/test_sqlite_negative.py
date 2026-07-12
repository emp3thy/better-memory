"""e2e-sqlite-ollama-absent-default-backend (design §1C row C8, task 5).

The path a pip-install user actually hits: no Ollama installed, no
BETTER_MEMORY_EMBEDDINGS_BACKEND exported, so the DEFAULT backend
('ollama', config.py _DEFAULT_EMBEDDINGS_BACKEND) is in force and
OLLAMA_HOST points nowhere. Pins the documented partial-availability
contract:

* server boot survives (startup probe is warn-only — mcp/server.py
  _probe_ollama) with a clear stderr warning;
* memory.observe fails IN BAND (CallToolResult.isError, EmbeddingError
  text from embeddings/ollama.py) — never a protocol crash;
* the failure is fast: <30s timed ONLY around the observe call;
* knowledge tools keep working;
* KNOWN-DEFECT PIN (design §4 item 10): the failed observe leaves an
  orphan episode row behind.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path

from tests.e2e._env import isolated_env
from tests.e2e.conftest import mcp_session

#: Loopback discard port: no listener on dev/CI boxes, so connects fail with
#: an instant ECONNREFUSED — no DNS, no route, no 30s connect timeout. If a
#: machine ever runs something on port 9 the failure mode is a clear
#: assertion message, not a hang (design C8 determinism note).
OLLAMA_ABSENT_HOST = "http://127.0.0.1:9"


def _text_of(result: object) -> str:
    return "".join(
        getattr(block, "text", "") for block in getattr(result, "content", [])
    )


async def test_ollama_absent_default_backend_degrades_in_band(
    clean_slate_home: Path, tmp_path: Path
) -> None:
    """Default (ollama) embeddings + unreachable daemon: warn, degrade, persist.

    Regressions this flips red on (design C8):
    * fatal startup probe — initialize() would raise, bricking every
      Ollama-less install;
    * EmbeddingError escaping as a server crash instead of isError=True;
    * retry/timeout inflation (~90s stalls per observe) — the <30s budget;
    * silent trigram auto-fallback (observe stops erroring — a deliberate
      contract change that must be made consciously);
    * the orphan-episode fix landing (see the pinned assertion below).

    KNOWN-DEFECT PIN — orphan episode on failed observe (design §4 item 10):
    ObservationService.create commits a background episode in step 1, then
    the embed in step 2 fails against the dead host, so the episode row is
    orphaned (episodes == 1, observations == 0). When the product fix lands
    (episode write deferred/rolled back on embed failure), DELETE the two
    orphan-pin assertions at the bottom of this test — everything else here
    is the enduring degradation contract.
    """
    env = isolated_env(
        clean_slate_home,
        # Remove the harness default 'sqlite' pin: the whole point is the
        # real out-of-the-box default ('ollama').
        BETTER_MEMORY_EMBEDDINGS_BACKEND=None,
        OLLAMA_HOST=OLLAMA_ABSENT_HOST,
    )
    errlog_path = tmp_path / "server.stderr"  # outside the fake home

    with errlog_path.open("w", encoding="utf-8") as errlog:
        async with mcp_session(env, errlog=errlog) as session:
            # Boot survived (initialize happened inside mcp_session) and the
            # embedding-dependent tool is still advertised.
            listed = await session.list_tools()
            assert "memory.observe" in {tool.name for tool in listed.tools}

            # Latency budget timed ONLY around the observe call — never the
            # spawn or initialize (design/judge fix). Measured baseline on a
            # warm Windows dev machine: 7.7s (3 connection-refused attempts,
            # 1.5s total fixed backoff, MCP round-trip). 30s keeps loud
            # failure on the ~90s retry-inflation regression this targets.
            t0 = time.monotonic()
            r_observe = await session.call_tool(
                "memory.observe", {"content": "e2e negative-path observation"}
            )
            observe_elapsed = time.monotonic() - t0

            r_knowledge = await session.call_tool("knowledge.list", {})

    assert observe_elapsed < 30.0, f"observe took {observe_elapsed:.1f}s (budget 30s)"

    # In-band tool error, not a protocol crash (ollama.py raises
    # EmbeddingError('Failed to reach Ollama at <host> after 3 attempts: ...')).
    assert r_observe.isError is True
    assert f"Failed to reach Ollama at {OLLAMA_ABSENT_HOST}" in _text_of(r_observe)

    # Partial-availability contract: knowledge tools are Ollama-independent.
    assert not r_knowledge.isError, _text_of(r_knowledge)

    # Warn-only startup probe left its breadcrumb on stderr
    # (mcp/server.py _probe_ollama; subset match on the host, not the
    # full message).
    stderr_text = errlog_path.read_text(encoding="utf-8", errors="replace")
    assert f"Ollama unreachable at {OLLAMA_ABSENT_HOST}" in stderr_text

    # KNOWN-DEFECT PIN (see docstring): orphan episode committed, no
    # observation row. Post-exit read — the server process is gone, so this
    # also proves the episode commit was durable.
    db = clean_slate_home / ".better-memory" / "memory.db"
    with closing(sqlite3.connect(db)) as conn:
        (episodes,) = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()
        (observations,) = conn.execute(
            "SELECT COUNT(*) FROM observations"
        ).fetchone()
    assert episodes == 1  # DELETE when the orphan-episode defect is fixed
    assert observations == 0
