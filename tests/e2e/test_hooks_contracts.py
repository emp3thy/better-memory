"""E2E contracts for the synchronous per-prompt hook (design task 5).

Scenario ``e2e-sqlite-contextual-inject-contract`` from
``docs/superpowers/specs/2026-07-12-e2e-clean-slate-smoke-design.md`` §1C:

(a) virgin home + **default mode** (env var unset = ``'both'``,
    ``config.py _DEFAULT_CONTEXT_INJECT_MODE``): exit 0, exactly one JSON
    envelope line on stdout, never a traceback. A regression here breaks
    *every prompt* in *every session* for every new user.
(b) migrated home + seeded matching semantic memory (seeded through the real
    server via ``memory.semantic_observe``): injection envelope carries the
    ``<project-memory>`` block and a ``session_memory_exposure`` row lands
    with ``source='contextual'``.
(c) ``mode=off``: exit 0, byte-exact empty envelope, ``BETTER_MEMORY_HOME``
    never created — verified zero side effects.

Plus the outer never-fail guard branch (``except BaseException`` in
``better_memory/hooks/contextual_inject.py:main``) gets its own triggering
test — it is the mutation-smoke M3 sentinel surface (exactly-one-JSON-line
stdout).

The degraded / heal SessionStart contracts (design C2/C3) are owned by the
sqlite-journey task and land in this module separately — do not fold them
into these classes.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from tests.e2e._env import isolated_env
from tests.e2e.conftest import mcp_session, run_hook

INJECT_HOOK = "better_memory.hooks.contextual_inject"

PROJECT_MEMORY_OPEN = '<project-memory source="better-memory">'
PROJECT_MEMORY_CLOSE = "</project-memory>"


def _prompt_payload(prompt: str, cwd: Path) -> dict[str, Any]:
    """A UserPromptSubmit payload exactly as Claude Code delivers it."""
    return {
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "session_id": "e2e-session-1",
        "cwd": str(cwd),
    }


def _single_envelope(stdout: str) -> dict[str, Any]:
    """Enforce the hook's stdout contract: exactly one JSON line.

    This is the mutation-smoke M3 sentinel assertion (design §5): a
    regression where the except path exits without printing the envelope
    must flip this red, so the check is strict — one line, valid JSON,
    ``hookSpecificOutput`` as the only top-level key.
    """
    lines = stdout.strip().splitlines()
    assert len(lines) == 1, f"expected exactly one JSON line on stdout, got: {stdout!r}"
    parsed = json.loads(lines[0])
    assert isinstance(parsed, dict)
    assert set(parsed) == {"hookSpecificOutput"}
    return parsed


def _empty_envelope(event: str = "UserPromptSubmit") -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": ""}}


class TestContextualInjectContract:
    """e2e-sqlite-contextual-inject-contract cases (a)/(b)/(c)."""

    def test_a_default_mode_virgin_home_exits_zero_with_envelope(
        self, clean_slate_home: Path
    ) -> None:
        """Case (a): a brand-new user's very first prompt, mode var unset.

        The default is 'both' (config.py), so the hook genuinely executes its
        injection path against a home with no DB at all. Contract: exit 0,
        single JSON envelope, no traceback. Nothing exists to inject, so
        additionalContext is exactly ''.
        """
        env = isolated_env(clean_slate_home)
        # The default-mode leg is the point: the choke-point helper must not
        # be pinning the mode var for us.
        assert not any(k.upper() == "BETTER_MEMORY_CONTEXT_INJECT_MODE" for k in env)

        rc, out, err = run_hook(
            INJECT_HOOK,
            _prompt_payload("hello brand new better-memory world", clean_slate_home),
            env,
        )

        assert rc == 0, err
        assert "Traceback" not in err
        assert _single_envelope(out) == _empty_envelope()

    def test_a_config_error_swallowed_envelope_still_printed(
        self, clean_slate_home: Path
    ) -> None:
        """Guard branch: the outer never-fail wrapper in contextual_inject.main.

        An invalid BETTER_MEMORY_CONTEXT_INJECT_MODE value makes get_config()
        raise ValueError *inside* the try — the hook must swallow it,
        record_hook_error, and still print the envelope with exit 0.

        Anti-vacuity: the schema-less memory.db alone is NOT discriminating —
        the happy path on a virgin home creates an identical file via its own
        connect(). The discriminator is ``state/``: SeenStore.bump_turn()
        mkdirs it on the happy path (contextual_inject.py sets it up right
        after get_config()), but an invalid mode raises inside get_config()
        BEFORE that block — so memory.db present + state/ absent proves the
        except path actually executed. (This test is the M3 mutation sentinel;
        without the state/ assertion it would silently degrade into a
        happy-path duplicate if the mode validation were ever loosened.)
        """
        env = isolated_env(
            clean_slate_home, BETTER_MEMORY_CONTEXT_INJECT_MODE="aggressive"
        )

        rc, out, err = run_hook(
            INJECT_HOOK, _prompt_payload("any prompt at all", clean_slate_home), env
        )

        assert rc == 0, err
        assert "Traceback" not in err
        assert _single_envelope(out) == _empty_envelope()

        # Proof the except path ran (not a silently-valid mode value):
        db = clean_slate_home / ".better-memory" / "memory.db"
        assert db.exists()
        with closing(sqlite3.connect(db)) as conn:
            (tables,) = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        assert tables == 0
        # state/ is created by the happy path's SeenStore but never reached
        # when get_config() raises — the genuinely discriminating assertion.
        assert not (clean_slate_home / ".better-memory" / "state").exists()

    async def test_b_seeded_memory_injects_and_records_contextual_exposure(
        self, clean_slate_home: Path, tmp_path: Path
    ) -> None:
        """Case (b): migrated home + one matching memory -> injection fires.

        Seeding recipe per design task 5: the memory goes in through the real
        MCP server via memory.semantic_observe (server boot also migrates the
        DB). The prompt shares 4 distinct whole-word keywords with the memory
        content ('configure', 'widget', 'frobnicator', 'cache'), clearing the
        default context_min_hits=2 floor in services/relevant.py.
        """
        env = isolated_env(clean_slate_home)
        errlog_path = tmp_path / "seed-server.stderr"  # outside the fake home
        content = (
            "Always configure the widget frobnicator cache with LRU eviction "
            "in this project."
        )
        with errlog_path.open("w", encoding="utf-8") as errlog:
            async with mcp_session(env, errlog=errlog) as session:
                result = await session.call_tool(
                    "memory.semantic_observe", {"content": content}
                )
                assert not result.isError, result.content
                memory_id = json.loads(result.content[0].text)["id"]
        assert memory_id

        rc, out, err = run_hook(
            INJECT_HOOK,
            _prompt_payload(
                "How do I configure the widget frobnicator cache?", clean_slate_home
            ),
            env,
        )

        assert rc == 0, err
        envelope = _single_envelope(out)
        assert envelope["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        ctx = envelope["hookSpecificOutput"]["additionalContext"]
        assert PROJECT_MEMORY_OPEN in ctx
        assert PROJECT_MEMORY_CLOSE in ctx
        assert f"semantic {memory_id}" in ctx  # the [kind id ...] meta tag
        assert "widget frobnicator cache" in ctx  # the rendered memory text

        # Exposure row wire shape per migration 0012 (session_memory_exposure
        # recreated with source CHECK including 'contextual'). Subset column
        # select — never the exact column set.
        db = clean_slate_home / ".better-memory" / "memory.db"
        with closing(sqlite3.connect(db)) as conn:
            rows = conn.execute(
                "SELECT session_id, memory_kind, memory_id "
                "FROM session_memory_exposure WHERE source = 'contextual'"
            ).fetchall()
        assert ("e2e-session-1", "semantic", memory_id) in rows

    def test_c_mode_off_empty_envelope_and_zero_side_effects(
        self, clean_slate_home: Path
    ) -> None:
        """Case (c): mode=off is a true no-op — exact empty envelope AND
        BETTER_MEMORY_HOME never created (resolves phase-1 open question #8:
        off-mode must not leave state/ or DB litter). The whole fake home
        stays byte-empty.
        """
        env = isolated_env(
            clean_slate_home, BETTER_MEMORY_CONTEXT_INJECT_MODE="off"
        )

        rc, out, err = run_hook(
            INJECT_HOOK,
            _prompt_payload("a prompt that would otherwise match", clean_slate_home),
            env,
        )

        assert rc == 0, err
        assert "Traceback" not in err
        assert _single_envelope(out) == _empty_envelope()

        assert not (clean_slate_home / ".better-memory").exists()
        assert list(clean_slate_home.iterdir()) == []
