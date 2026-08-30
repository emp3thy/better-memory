# Self-managing setup & doctor (Approach A)

Date: 2026-08-30
Status: approved by user (brainstorming session); concerns 1-3 resolved below.

## Goal

better-memory owns its entire Claude Code wiring: one declarative manifest drives
first-time install, per-session drift detection, and automatic repair. A new PC
reaches parity with the reference machine with one command; an existing machine
can never silently drift. Manual setup steps are eliminated.

## Locked decisions (from user Q&A)

- Memory stores are **independent per machine**. Storage backend
  (`sqlite` / `agentcore`) stays an orthogonal knob in
  `~/.better-memory/settings.json`; the wiring layer is identical for both and
  never dictates a backend. New installs default to `sqlite`.
- **Windows-first** installer (pure Python CLI). `scripts/setup.sh` becomes a
  thin wrapper for future macOS/Linux use.
- **Auto-check + auto-repair** at session start. Repairs are hands-off and
  reported in injected context.
- The `~/.claude/CLAUDE.md` protocol section becomes a **managed block**
  between `<!-- BEGIN better-memory (managed) -->` / `<!-- END better-memory (managed) -->`
  markers. Content outside the markers is never touched.
- **Ollama is removed from the setup path.** No install, no model pull, no
  doctor check. Runtime default `embeddings_backend="ollama"` string stays
  (the embed-down breaker already degrades gracefully); retrieval on the
  reference machine rides backend ranking (agentcore) + BM25/keyword legs.
- **Ralph: local only.** Container path (Docker/Kubernetes `CLAUDE_CONFIG_DIR`
  isolation) is explicitly deferred to a future PBI.

## Resolved design concerns

1. **Git post-commit episode-close hook (per-repo)** → *auto-install at session
   start*: `session_bootstrap` installs the hook into the cwd repo's
   `.git/hooks/post-commit` when missing. Creates the file if absent; if a
   foreign post-commit hook exists, chains ours after it; never overwrites.
2. **Echo reminder hooks** → *convert to Python modules*:
   `better_memory.hooks.commit_checkpoint` (PreToolUse on `git commit`) and
   `better_memory.hooks.stop_sweep` (Stop). Message text ships with the
   package; recognition is uniform module-name matching like every other hook.
3. **`~/.claude.json` auto-repair** → *full auto everywhere* (user chose the
   stronger option over the split-targets recommendation). Mitigations: patch
   only the `mcpServers.better-memory` key; re-read the file immediately
   before an atomic temp+replace write; retry once on detected concurrent
   modification (mtime/content changed between read and write); timestamped
   backup before every write.
4. **Repo-move bootstrap paradox** (hooks embed the venv's absolute path, so
   moving/re-cloning the repo breaks every hook — including the doctor) →
   *accept one-command heal*: run `uv run better-memory doctor --fix` once
   from the new location; it rewrites all absolute paths in the managed
   surface. Hook failures are visible in Claude Code until then. No shim
   layer.
5. **Post-commit auto-install vs `core.hooksPath` / non-sh hooks** → *detect
   and adapt*: honor `core.hooksPath` when set and writable; chain only when
   the existing post-commit is a plain sh script; otherwise skip with a
   one-line warning in the bootstrap report. Never edit hook files we cannot
   safely parse.
6. **Fresh PC may lack `uv`** → *setup.ps1 bootstraps uv* when missing
   (winget, falling back to the official installer script), then proceeds.

## Architecture

```
manifest.py  ──render(machine params)──►  desired state
                                             │
engine.py:  inspect() live config ──► diff() ──► apply()
                                             │
        ┌────────────────────┬───────────────┴──────────────┐
   cli setup            cli doctor [--fix]        session_bootstrap auto-check
 (first install)      (report / repair)         (fingerprint, auto-repair, report)
```

New package `better_memory/setup/`:

- **`manifest.py`** — declarative description of every managed artifact,
  parameterized by machine-local values (venv `python.exe` / `pythonw.exe`
  paths, `BETTER_MEMORY_HOME`). Versioned with the package; changing shipped
  wiring means editing the manifest only.
- **`engine.py`** — `render()` (manifest + machine params → concrete desired
  fragments), `inspect()` (extract the managed subset from live config files),
  `diff()` (structured drift list), `apply()` (validate-all-then-write-all,
  atomic temp+replace, timestamped backups to
  `~/.better-memory/install-backups/`, file lock around apply). Folds in the
  existing `install_hooks.py` smart-merge semantics, including the
  legacy-module scrub list. `install_hooks.py` is retired: its module body
  becomes a deprecation shim that delegates to the engine and prints a notice
  pointing at `better-memory setup`, so existing docs/scripts keep working.

## Managed surface (manifest contents)

| # | Artifact | Target | Today |
|---|----------|--------|-------|
| 1 | SessionStart → `hooks.session_bootstrap` | `~/.claude/settings.json` | installer-managed |
| 2 | UserPromptSubmit → `hooks.contextual_inject` | 〃 | installer-managed |
| 3 | PreToolUse `Skill\|Task\|Write` → `hooks.contextual_inject` | 〃 | installer-managed |
| 4 | PostToolUse `Write\|Edit\|Bash` → `hooks.observer` (async, `pythonw.exe`) | 〃 | installer-managed |
| 5 | Stop → `hooks.session_close` | 〃 | installer-managed |
| 6 | PreCompact → **new** `hooks.pre_compact` (ports `~/.claude/hooks/pre-compact-better-memory.py`; loose script added to scrub list) | 〃 | manual loose script |
| 7 | PreToolUse `Bash(git commit*)` → **new** `hooks.commit_checkpoint` | 〃 | manual echo hook |
| 8 | Stop → **new** `hooks.stop_sweep` | 〃 | manual echo hook |
| 9 | `env.BETTER_MEMORY_INJECT_MODE = "deferred"` | 〃 | manual |
| 10 | MCP server entry `better-memory` (venv python, `-m better_memory.mcp`, env `BETTER_MEMORY_HOME`) | `~/.claude.json` | installer-managed |
| 11 | Protocol section as managed block | `~/.claude/CLAUDE.md` | manual; sentinel warns only |
| 12 | Skills: `better-memory-synthesize`, `rate-session-memories`, `start-better-memory-ui` (symlink/junction) | `~/.claude/skills/` | 2 of 3 managed |
| 13 | Git post-commit episode-close hook | `<cwd repo>/.git/hooks/post-commit` | manual per repo |

Rows 1-12 are machine-level and applied by setup/doctor/bootstrap. Row 13 is
repo-level and applied only by the bootstrap auto-install (concern 1).

The existing `_claude_md_sentinel` drift *warning* is retired; CLAUDE.md drift
becomes a doctor repair (managed-block rewrite from the packaged canonical
text, compared by content hash).

## CLI

- `better-memory setup` — first-time install: discover venv interpreters,
  create `~/.better-memory` layout, apply full manifest, write default
  `settings.json` (`storage_backend: sqlite`) if absent. Idempotent; running
  it twice is a no-op.
- `better-memory doctor [--fix] [--json]` — render + inspect + diff; report
  drift human-readably (or JSON); `--fix` applies. Exit code 0 clean / 1
  drift found (report mode) so it is scriptable.
- `scripts/setup.ps1` — Windows bootstrap one-liner target: install uv if
  missing (concern 6) → clone → `uv sync` → `uv run better-memory setup`
  (console script from `[project.scripts]`). Also the documented recovery
  path after a repo move (concern 4): running doctor from the new location
  rewrites all absolute paths.
- `scripts/setup.sh` — reduced to the same three steps for POSIX; all Ollama
  logic deleted.

## Session-start auto-check (in `session_bootstrap`)

1. Compute the desired-state fingerprint (stable hash of `render()` output).
2. Cheap path: `~/.better-memory/state/wiring_fingerprint.json` stores the
   last-verified fingerprint plus the mtimes of the four target files. If
   mtimes and fingerprint are unchanged, skip — near-zero per-session cost.
3. Otherwise `inspect()` + `diff()`. On drift: backup, `apply()`, update the
   fingerprint cache, and append to the bootstrap `additionalContext`:
   `better-memory doctor: repaired N item(s): <short list> (effective next session)`.
4. On clean: update fingerprint cache silently.
5. Repo-level: if cwd is a git repo without our post-commit hook, install it
   per the concern-5 rules (honor `core.hooksPath`, chain only plain sh
   scripts, warn otherwise) and mention it in the same report line.
6. Any repair failure degrades to a warning in `additionalContext`; the
   session is never blocked and the config file is left untouched (validate
   before write).

Hook/MCP changes take effect at the *next* session because Claude Code
snapshots configuration at session start; the report line says so.

## Error handling & concurrency

- Validate-all-then-write-all; atomic temp + `os.replace` per file.
- Timestamped backups in `~/.better-memory/install-backups/` before any write.
- Unparseable JSON in a target file → abort that file's repair, warn, never
  write.
- Inter-process file lock (in `~/.better-memory/state/`) around `apply()` so
  two concurrently starting sessions cannot interleave writes.
- `~/.claude.json`: patch only `mcpServers.better-memory`; re-read
  immediately before write; single retry on concurrent-modification detection
  (concern 3 mitigation).
- Only the managed subset is ever modified. Foreign hooks, MCP servers,
  skills, env keys, and all CLAUDE.md content outside the markers are
  preserved byte-for-byte.

## Testing

- Unit fixtures for `render`/`inspect`/`diff`/`apply`: fresh machine (empty
  configs), reference-machine snapshot (including the currently hand-added
  rows 6-9/11-12), corrupted JSON, foreign entries preserved, legacy scrub.
- **Golden parity test**: applying the manifest to a fixture captured from the
  reference PC's live config produces zero diff. This proves the manifest
  reproduces the known-good setup and is the regression anchor for "runs as
  well as it does on this pc".
- Integration: `setup` into a temp `HOME`/`BETTER_MEMORY_HOME`, induce each
  drift class, assert bootstrap detects and `--fix` restores; fingerprint
  cache short-circuits when nothing changed.
- Post-commit auto-install: repo without hook (creates), repo with foreign
  hook (chains, preserves original), repo with ours (no-op).
- Windows: junction fallback when symlink privilege is absent (extends
  existing tests); `pythonw.exe` resolution.

## Ralph (local)

No new wiring. Verified during design: `claude_spawn.py` copies `os.environ`,
sets no `CLAUDE_CONFIG_DIR`/`--settings`/`--mcp-config`, so spawned sessions
inherit user-scope hooks, MCP, skills, and CLAUDE.md; `BETTER_MEMORY_PROJECT`
is set per spawn and is top-precedence in `project_name()`. A healthy user
scope — which is now the doctor's guarantee — is exactly what ralph needs.

## Documentation (ships in the same PR)

Per the standing docs-sync rule: update `README.md` (setup instructions, tool
counts untouched but CLI section changes, env-var table), `website/configuration.md`
(env vars, state layout gains `wiring_fingerprint.json`), `website/architecture.md`
if it describes hooks, and `docs/hooks-setup.md` (manual instructions replaced
by `better-memory setup` / doctor; post-commit manual section replaced by
auto-install description). Interactive and scripted setup paths documented
separately.

## Assumptions

**Verified safe** (how verified):

- Ralph local spawns inherit user scope — read `ralph_executor/claude_spawn.py`:
  `os.environ.copy()`, no `CLAUDE_CONFIG_DIR`/`--settings`/`--mcp-config`.
- `BETTER_MEMORY_PROJECT` is top precedence in `project_name()` — `config.py:162`.
- Wiring is backend-agnostic — hook modules never read `storage_backend`.
- Config merge preserves foreign entries — existing two-pass merge in
  `install_hooks.py` plus its tests, folded into the engine.
- Retrieval works without Ollama — observed live on the reference machine
  (Ollama not running, contextual injection functioning via backend ranking +
  BM25/keyword legs).
- `better-memory` console script exists — `pyproject.toml [project.scripts]`.

**Minor / accepted**:

- Repairs take effect at the *next* session; Claude Code snapshots hooks/MCP
  configuration at session start. The repair report says so.
- The canonical CLAUDE.md managed-block text is captured from the reference
  machine's current section as v1; the golden parity test enforces that the
  first doctor run changes nothing on that machine.
- Managed-block markers are HTML comments — invisible in rendered markdown.
- A tiny clobber window exists if the user edits settings via `/config` at
  the exact moment of an auto-repair; timestamped backups cover recovery.
- Runtime default `embeddings_backend="ollama"` string is unchanged; the
  embed-down breaker degrades gracefully when Ollama is absent.
- Ollama remains installed on the reference machine — unused and harmless.

## Out of scope

- Ralph container path (Docker image / ConfigMap MCP + hooks) — future PBI.
- Cross-machine memory sync or shared stores.
- Storage backend migration tooling (sqlite ↔ agentcore).
- Claude Code plugin packaging (Approach C).
- macOS/Linux native installer polish beyond the thin `setup.sh` wrapper.
