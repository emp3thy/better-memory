# Contributing

## Local development setup

```bash
git clone https://github.com/emp3thy/better-memory
cd better-memory
uv sync --dev
```

## Running tests

```bash
uv run pytest                          # full suite (integration tests need Ollama running)
uv run pytest -m "not integration"     # unit tests only — fast, no external deps
```

The integration marker gates tests that hit Ollama or spawn the MCP server end-to-end. Unit tests cover the bulk of the codebase and run in seconds.

## Linting and type-checking

```bash
uv run ruff check .                    # lint
uv run pyright                         # type check (standard mode, see pyproject.toml [tool.pyright])
```

Both are enforced in CI:

- **`.github/workflows/ui-tests.yml`** — Playwright UI test suite.
- **`.github/workflows/typecheck.yml`** — Pyright on `better_memory/` and `tests/`.
- **`.github/workflows/bugbot.yml`** — Claude BugBot review on every PR.

Cursor Bugbot also reviews PRs (configured at the org level).

## Running the MCP server standalone

For manual poking:

```bash
uv run python -m better_memory.mcp
```

It speaks JSON-RPC over stdio — pipe `initialize` / `tools/list` / `tools/call` payloads in.

## Running the management UI

```bash
BETTER_MEMORY_HOME=~/.better-memory uv run python -m better_memory.ui
```

Or call the `memory.start_ui` MCP tool from inside Claude Code, which spawns it as a subprocess and returns the URL. The UI exits after 30 minutes of inactivity or when you click **Close UI** in the header.

Stdout and stderr from the UI subprocess are written to `$BETTER_MEMORY_HOME/ui.log`.

## Adding a migration

Add a new file to `better_memory/db/migrations/` named `NNNN_<description>.sql` where `NNNN` is the next 4-digit prefix. Migrations apply lexically at boot and are idempotent — wrap any `CREATE TABLE` in `IF NOT EXISTS` and any `ALTER` behind a `pragma_table_info` check.

Update `tests/db/test_schema.py`'s hardcoded version list to include the new prefix.

## Process discipline

The repo uses a structured workflow for non-trivial work:

1. **Brainstorming** — `superpowers:brainstorming` skill turns an idea into a spec doc.
2. **Plan** — `superpowers:writing-plans` produces a TDD-shaped task list.
3. **Implementation** — `superpowers:subagent-driven-development` dispatches a fresh subagent per task with two-stage review (spec compliance → code quality).
4. **Memory** — observations recorded to better-memory as work progresses, not batched at the end.

See `CLAUDE.md` for the full discipline.

## Reporting issues

Open a GitHub issue with: a minimal reproduction, what you expected, what you got, and the relevant `audit_log` rows or `hook_errors` rows if applicable. The management UI's Diagnostics tab has both.
