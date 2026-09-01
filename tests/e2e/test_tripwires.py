"""Network tripwires (design §1F).

This module currently owns ONLY ``e2e-ollama-zero-traffic-tripwire``
(design task 5). The AWS lockdown tripwire (``e2e-aws-lockdown-tripwire``)
is owned by the fake-agentcore task (design task 6) and is added to this
module separately — do not duplicate it here.

Mechanism: point OLLAMA_HOST at a local recording HTTP server instead of the
usual ``.invalid`` poison, run a mini sqlite journey through the real MCP
server plus both synchronous hooks, then assert ZERO requests were recorded.
Strictly stronger than the poison host: it proves no attempt was ever made,
not merely that a failed attempt was survivable (and a poisoned attempt
retried 3x adds ~90s of latency that masquerades as flakiness).
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.e2e._env import isolated_env
from tests.e2e.conftest import mcp_session, run_hook, text_of


class _RecordingHandler(BaseHTTPRequestHandler):
    """Record every request under the server lock; always answer 200 '{}'."""

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        server: _RecorderServer = self.server  # type: ignore[assignment]
        with server.lock:
            server.requests.append((self.command, self.path))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = _handle
    do_POST = _handle
    do_HEAD = _handle
    do_PUT = _handle

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — base class parameter name; silence stderr chatter
        pass


class _RecorderServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        # 127.0.0.1:0 — OS-assigned ephemeral port, no collisions, no DNS.
        super().__init__(("127.0.0.1", 0), _RecordingHandler)


async def test_ollama_zero_traffic_across_journey_and_sync_hooks(
    clean_slate_home: Path, tmp_path: Path
) -> None:
    """e2e-ollama-zero-traffic-tripwire: the T1 journey never contacts Ollama.

    Task 2 (remove-ollama-embeddings) deleted OllamaEmbedder construction
    and the /api/tags startup probe from mcp/server.py entirely — there is
    no longer an ``embeddings_backend == 'ollama'`` branch to skip, so this
    holds regardless of BETTER_MEMORY_EMBEDDINGS_BACKEND (the key no longer
    exists in config.py, and isolated_env carries no pin for it either --
    the pin was removed this branch). Regressions this flips red on:
    reintroducing embedder construction or a startup probe into
    mcp/server.py, or contextual_inject growing an embedding-based scorer —
    either records a request here.

    Anti-vacuity (a dead server also makes zero requests): the recorder is
    first proven to record via a direct self-check request, and the journey
    itself carries positive assertions (observe returns an id, the trigram
    drill-down surfaces it, retrieve returns well-formed buckets, both hooks
    exit 0 with exactly one JSON envelope line).
    """
    recorder = _RecorderServer()
    thread = threading.Thread(target=recorder.serve_forever, daemon=True)
    thread.start()
    try:
        port = recorder.server_address[1]
        recorder_url = f"http://127.0.0.1:{port}"

        # --- recorder self-check: prove the tripwire can fire at all ------
        with urllib.request.urlopen(  # noqa: S310 — loopback literal
            recorder_url + "/__selfcheck", timeout=5
        ) as resp:
            assert resp.status == 200
        with recorder.lock:
            assert recorder.requests == [("GET", "/__selfcheck")]
            recorder.requests.clear()

        env = isolated_env(clean_slate_home, OLLAMA_HOST=recorder_url)
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        errlog_path = tmp_path / "server.stderr"  # outside the fake home

        # --- mini journey through the real MCP server ---------------------
        with errlog_path.open("w", encoding="utf-8") as errlog:
            async with mcp_session(env, errlog=errlog) as session:
                r_obs = await session.call_tool(
                    "memory.observe",
                    {"content": "tripwire observation", "outcome": "success"},
                )
                assert not r_obs.isError, r_obs.content
                obs_id = json.loads(text_of(r_obs.content[0]))["id"]
                assert obs_id

                # Trigram drill-down (no embedder is ever constructed any
                # more; migration 0011's insert triggers make this
                # synchronous with the observe above).
                r_drill = await session.call_tool(
                    "memory.retrieve_observations", {"query": "tripwire"}
                )
                assert not r_drill.isError, r_drill.content
                drilled = json.loads(text_of(r_drill.content[0]))
                matched = [o for o in drilled if o["id"] == obs_id]
                assert matched, f"observation {obs_id} not surfaced: {drilled!r}"
                assert "tripwire observation" in matched[0]["content"]

                # Bucketed retrieve stays well-formed (schema forbids extra
                # args — reflections filters only, so call it bare).
                r_buckets = await session.call_tool("memory.retrieve", {})
                assert not r_buckets.isError, r_buckets.content
                buckets = json.loads(text_of(r_buckets.content[0]))
                assert {"do", "dont", "neutral"} <= set(buckets)

        # --- both synchronous hooks under the same recorder env -----------
        rc_boot, out_boot, err_boot = run_hook(
            "better_memory.hooks.session_bootstrap",
            {
                "source": "startup",
                "session_id": "e2e-session-1",
                "cwd": str(proj_dir),
            },
            env,
        )
        assert rc_boot == 0, err_boot
        boot_lines = out_boot.strip().splitlines()
        assert len(boot_lines) == 1, out_boot
        boot_envelope = json.loads(boot_lines[0])
        # DB was migrated by the server boot above, so bootstrap succeeds
        # (positive journey assertion, not the degraded fallback).
        assert "Episode:" in boot_envelope["hookSpecificOutput"]["additionalContext"]

        rc_inject, out_inject, err_inject = run_hook(
            "better_memory.hooks.contextual_inject",
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "zero traffic tripwire prompt",
                "session_id": "e2e-session-1",
                "cwd": str(proj_dir),
            },
            env,
        )
        assert rc_inject == 0, err_inject
        inject_lines = out_inject.strip().splitlines()
        assert len(inject_lines) == 1, out_inject
        assert "hookSpecificOutput" in json.loads(inject_lines[0])

        # --- the tripwire itself -------------------------------------------
        with recorder.lock:
            recorded = list(recorder.requests)
        assert recorded == [], (
            "Ollama traffic detected on a sqlite-embeddings journey — the "
            f"probe/embed path has become unconditional again: {recorded!r}"
        )
    finally:
        recorder.shutdown()
        recorder.server_close()
