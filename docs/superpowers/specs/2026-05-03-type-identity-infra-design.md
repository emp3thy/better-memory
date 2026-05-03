# Phase D — Type & Identity Infra Design

**Date:** 2026-05-03
**Status:** Approved (assumptions resolved 2026-05-03)
**Bundles:** Tech-debt audit items #8 (Pyright in CI) and #12 (`_project_name()` ambiguity) into one PR.

## Goal

Two independent non-functional improvements, packaged together because both are infra-level correctness/clarity work:

1. **#8 — Add Pyright to CI in standard mode.** No type-checker is configured today. Production code is already clean (0 errors) inside the project venv; tests have 44 mechanical errors that we will fix as part of this PR.
2. **#12 — Eliminate divergence in project-name resolution.** `KnowledgeService.project_for(cwd)` reads a `<cwd>/.better-memory` override file before falling back to `cwd.name`, but six other call sites (UI, MCP server, observation service default, two hooks) just use `Path.cwd().name` directly. A user creating an override file today gets split behavior across subsystems. We promote the override semantics into a single canonical resolver and route every site through it.

## Context

### Pyright baseline (scout run)

`uv run --with pyright pyright better_memory tests` reports **44 errors**, all in `tests/`:

| Pattern | Count | Cause | Fix shape |
|---|---|---|---|
| `__getitem__` overload mismatch | ~26 | Helpers like `_single_json` typed `dict | list`; subscripted with `str` keys | Tighten helper return type to `dict[str, Any]`; provide separate `_single_json_list` variant where genuinely a list |
| `result.attr` on `None` | ~14 | Code accesses attributes on returns typed `Optional[X]` without narrowing | Insert `assert result is not None` before access. Bug-catching narrowing — if the function ever does return None in future, the test fails loudly. |
| One-off | ~4 | One `not in` against `str | None`; one Awaitable-shape mismatch around `asyncio.run`; couple of edge cases | Address per-case |

`better_memory/` package: **0 errors**.

The earlier 19-error count from `uvx pyright` was an environment artifact (uvx ran in an isolated venv without flask/httpx/sqlite-vec/mcp installed). All resolved-import errors disappear when pyright runs inside the project venv. Spec assumes the project-venv invocation.

### `_project_name()` divergence (current state)

| Site | Current code | Honors `.better-memory` override? |
|---|---|---|
| `better_memory/services/knowledge.py:308-320` (`project_for`) | reads `.better-memory` file, falls back to `cwd.name` | **YES** |
| `better_memory/ui/app.py:64-66` (`_project_name()`) | `Path.cwd().name` | NO |
| `better_memory/mcp/server.py:501,513,564` | `args.get("project") or Path.cwd().name` | NO |
| `better_memory/services/observation.py:131` | default `project_resolver = lambda: Path.cwd().name` | NO |
| `better_memory/hooks/session_start.py:58,105` | `Path(cwd).name` (cwd from hook payload) | NO |
| `better_memory/hooks/post_commit.py:157` | `Path(cwd).name` (cwd from hook payload) | NO |

The override file is implemented and tested (`tests/services/test_knowledge.py:354-366`) but undocumented in README. Real risk to existing users is treated as zero (item R2 below).

## Locked-in decisions

| Decision | Choice | Rationale |
|---|---|---|
| Pyright strictness | `standard` mode now, per-module `# pyright: strict` available later | Pragmatic for an existing codebase; doesn't gate phase D on a 200-error cleanup. |
| Scope of typecheck | `better_memory/` + `tests/` (no exclude list) | 44-error backlog is small enough to fix in-PR; no excluded-files debt to ratchet. |
| Existing test errors | Fix all 44 in this PR | Patterns are mechanical and each fix tightens the test (None-narrowing catches future regressions). |
| Project-name resolution | Promote `.better-memory` override to canonical, applied everywhere | Override is a useful feature (multi-worktree, monorepo subdirs). Bug today is that it's not applied uniformly. |
| Resolver placement | `project_name(cwd: Path \| None = None)` in `better_memory/config.py` | Already houses `resolve_home()`; same pattern of "resolve runtime context from env/cwd." Avoids one-function module. |

## Components

### Commit 1 — Pyright config + CI workflow

**`pyproject.toml`** additions:

```toml
[dependency-groups]
dev = [
    "pytest",
    "pytest-asyncio",
    "pytest-playwright",
    "ruff",
    "pyright",
]

[tool.pyright]
include = ["better_memory", "tests"]
pythonVersion = "3.12"
typeCheckingMode = "standard"
venvPath = "."
venv = ".venv"
```

**`.github/workflows/typecheck.yml`** — new workflow:

```yaml
name: Type checking

on:
  pull_request:
    paths:
      - "better_memory/**"
      - "tests/**"
      - "pyproject.toml"
      - "uv.lock"
      - ".github/workflows/typecheck.yml"
  push:
    branches: [main]
    paths:
      - "better_memory/**"
      - "tests/**"
      - "pyproject.toml"
      - "uv.lock"
      - ".github/workflows/typecheck.yml"

jobs:
  typecheck:
    name: Pyright
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v4
        with:
          python-version: "3.12"
          enable-cache: true

      - name: Install project
        run: uv sync --dev

      - name: Run pyright
        run: uv run pyright
```

Deliberately mirrors `ui-tests.yml` triggers and structure for consistency. Same path filters. Single step that fails the build on any error.

### Commit 2 — `project_name()` canonical resolver in `config.py`

Add to `better_memory/config.py`:

```python
def project_name(cwd: Path | None = None) -> str:
    """Return the canonical project name for ``cwd`` (defaults to Path.cwd()).

    A ``<cwd>/.better-memory`` file overrides the default ``cwd.name``: the
    first non-empty stripped line is used as the project name. Used uniformly
    by knowledge search, observation writes/reads, episode scoping, the UI
    panel filter, and hook payloads — every subsystem that buckets state by
    project must call this helper, never construct the name inline.
    """
    cwd = cwd if cwd is not None else Path.cwd()
    override = cwd / ".better-memory"
    if override.is_file():
        text = override.read_text(encoding="utf-8").strip()
        if text:
            return text.splitlines()[0].strip()
    return cwd.name
```

Tests in `tests/test_config.py`:

- `project_name()` defaults to `cwd.name` (uses `tmp_path` fixture; `monkeypatch.chdir(tmp_path)`)
- `project_name(explicit_cwd)` returns `explicit_cwd.name` when no override file
- `project_name(cwd)` reads `<cwd>/.better-memory` first non-empty line
- empty `.better-memory` file → falls back to `cwd.name`
- multi-line `.better-memory` → first non-empty stripped line wins
- whitespace-only `.better-memory` → falls back to `cwd.name`

### Commit 3 — Migrate all call sites

| File | Change |
|---|---|
| `better_memory/ui/app.py` | Delete the `_project_name()` helper (lines 64-66). Replace its 6 call sites (`209`, `229`, `295`, `313`, `499`, plus the def) with `project_name()`. Add `from better_memory.config import project_name` to existing imports. The wrapper added no behavior — inlining removes a single-line indirection. |
| `better_memory/mcp/server.py:501,513,564` | `args.get("project") or Path.cwd().name` → `args.get("project") or project_name()` |
| `better_memory/services/observation.py:131` | Default `project_resolver = lambda: Path.cwd().name` → `project_resolver if project_resolver is not None else project_name` (drop the lambda; pass the function reference directly — pyright-friendly) |
| `better_memory/services/knowledge.py:308-320` | `project_for(self, cwd)` body becomes `return project_name(cwd)`. Delegating wrapper preserves the public method name and existing test coverage. One-line docstring notes the delegation. |
| `better_memory/hooks/session_start.py:58,105` | `Path(cwd).name` → `project_name(Path(cwd))` |
| `better_memory/hooks/post_commit.py:157` | `Path(cwd).name` → `project_name(Path(cwd))` |

Import changes: each touched file gains `from better_memory.config import project_name` (or extends an existing config import).

### Commit 4 — Test cleanup (44 errors)

Order of attack:

1. **Subscript overload errors (~26)** — find each helper that returns `dict | list`, check actual usage at every call site, narrow return type to the precise variant. Where the same helper is used both ways, split into two helpers (e.g., `_single_json_dict`, `_single_json_list`) or have the caller cast.
2. **`Optional[X]` attribute access (~14)** — insert `assert obj is not None, "<short context message>"` immediately before access. The message names what the test is asserting (e.g., `"reflection should exist after _apply_new"`); failures get a meaningful trace, not bare `AssertionError`.
3. **One-offs (~4)** — fix per-case. Likely targets:
   - `not in` against `str | None` → guard with `if x is not None and "Plan 2" not in x`
   - Awaitable shape around `asyncio.run` → adjust signature or wrap in `cast`
   - Other small narrowings

After each batch, re-run `uv run pyright` to confirm the count drops as expected. If a fix balloons or causes test failures: per the **R1 risk acceptance** below, we accept the risk and address it as it arises (no preemptive exclude list).

### Commit 5 — README documentation

Add a section under `## Configuration` documenting the `.better-memory` override file:

```markdown
### Project-name override

Memory is bucketed by project name, derived from the cwd's leaf
directory name (`Path.cwd().name`). For situations where the leaf
name isn't right — multiple worktrees of the same logical project,
or a deeply-nested cwd — drop a `.better-memory` file at the project
root with a single line containing the desired project name:

    echo "my-project" > .better-memory

This applies uniformly to knowledge search, observation writes/reads,
episode scoping, and the UI panel filter. Note: this is a *file* in
your repo root, not the data root directory `~/.better-memory/` (set
by `BETTER_MEMORY_HOME`).
```

### Commit 6 — Spec + plan + memory

Commit the spec and plan, plus a memory observation noting the migration of `project_for` semantics into config.

## Assumptions resolved

Per the `b85a1ac4` workflow preference, three buckets surfaced and resolved.

### Real concerns (resolved)

| # | Assumption | Resolution |
|---|---|---|
| **R1** | "44 test errors are mechanical 3-pattern fixes" — long-tail risk that a fix balloons or breaks unrelated tests. | **(C) Accept the risk.** Fixes are mechanical; if one breaks something exotic, test failures surface it instantly. No safety-valve exclude list, no pre-commit scout pass. |
| **R2** | "Promoting `.better-memory` override to all subsystems is non-breaking" — risk that existing user has a stray file and silently re-buckets observations. | **(B) Ship without warning.** Override is undocumented, real user-base for it is zero. |

### Verified-safe

- ✅ **Pyright + project venv** — `uv run --with pyright pyright better_memory` reports 0 errors. Earlier `uvx pyright` 19-error report was env-only.
- ✅ **Hooks pass explicit cwd** — `hooks/session_start.py:58,105` and `hooks/post_commit.py:157` pass cwd from the hook payload; resolver signature `project_name(cwd: Path | None = None)` accommodates both UI/MCP (no arg) and hook (explicit cwd) callers.
- ✅ **`project_for` public API stays stable** — `tests/services/test_knowledge.py:354-366` assertions still pass when `project_for` body becomes `return project_name(cwd)`.

### Minor / accepted

- Pyright runs in CI on Linux but devs run it locally inside their venv — no platform-specific stub differences for the deps in use.
- `pyright` dev-dep adds ~5 MB to install footprint.
- `KnowledgeService.project_for` thin wrapper introduces small indirection; mitigated by a one-line docstring noting the delegation.

## Out of scope

- **Other type-checkers** (mypy, ty, pyrefly) — pyright was the audit-named tool and is what we ship.
- **Strict mode for any module** — explicitly deferred. We add the *option* by configuring standard mode with `# pyright: strict` available, but no module starts in strict.
- **Pyright cache in CI** — `uv` cache is enabled; pyright's own cache is small enough not to warrant separate cache config.
- **Per-tech retention or retention scheduling** — phase C territory, already shipped.
- **Renaming the `.better-memory` override file** to disambiguate from the `BETTER_MEMORY_HOME` directory name — option (c) at brainstorm time, rejected as churn for marginal clarity gain. README documentation handles the disambiguation.

## Confidence

| Commit | Confidence | Risk drivers |
|---|---|---|
| 1. Pyright config + CI workflow | 95% | Standard config block; mirrors existing `ui-tests.yml` patterns; scout proves config produces 0 errors on `better_memory/`. |
| 2. `project_name()` resolver in `config.py` | 96% | Trivial function; single-source-of-truth move; tests are straightforward. |
| 3. Migrate 6 call sites | 92% | Mostly mechanical, but the `services/observation.py` `project_resolver` lambda swap involves a function-vs-lambda type tweak that needs verification against the existing test suite for that service. Mitigation: read `tests/services/test_observation.py` (or equivalent) at plan-write time per memory `e7961c0c`. |
| 4. Test cleanup (44 errors) | 88% | Mechanical pattern fixes, but a long-tail one might balloon (e.g., a tightened helper return type breaks downstream callers in many test files). Per R1 resolution we accept the risk — no preemptive exclude list, no early-pause mitigation. If a fix breaks unrelated tests, we deal with it inline as it surfaces. |
| 5. README | 99% | Pure documentation. |
| 6. Spec + plan + memory commit | 99% | Repository hygiene step. |
| **Plan-wide** | **~92%** | Pulled down by commit 4's accepted long-tail risk. |
