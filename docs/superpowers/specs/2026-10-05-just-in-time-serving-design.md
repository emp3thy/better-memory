# Just-in-time serving: triggers, a tighter prompt gate, and a measured useful rate

**Date:** 2026-10-05
**Status:** design approved in chat 2026-10-05 (Approach A chosen over a measured-minimum and a rules-out-of-the-loop alternative; sections 1 and 2 approved as presented); written spec pending owner review before planning
**Predecessors:** 2026-06-14 contextual injection hook, 2026-07-11 attention-first injection, 2026-07-23 retrieval quality, 2026-07-23 deferred injection

## Goal

The owner's goal: better-memory should give Claude Code more targeted and appropriate memory, so that served memories are rated useful at least 33% of the time, and the absolute number of useful ratings should go up, not merely the ratio.

Metric (owner's choice): per served item, across every channel that logs an exposure. `useful = cited + shaped`; `rate = useful / (useful + ignored + misled)` over rated exposures. Unrated exposures are excluded.

Scope (owner's choice): serving only. Synthesis of the 53 un-synthesised episodes, and anything else on the supply side, is out of scope.

## Measured baseline (live `memory.db`, 2026-10-05, before this session's ratings)

| Channel | Useful | Ignored | Rate |
|---|---|---|---|
| bootstrap | 5 | 29 | 15% |
| contextual (prompt + first tool call) | 6 | 20 | 23% |
| retrieve (explicit `memory.retrieve`) | 1 | 2 | 33% |
| **all** | **12** | **51** | **19%** |

No exposure has ever been rated `misled`. The store holds 4 reflections (all general-scope, all web-research or Windows pitfalls) and 6 semantic memories (all general-scope process rules written by the owner). 224 observations exist across four projects; only 6 of 59 episodes have been synthesised, so there are no project-specific reflections to serve.

## What the replay spikes showed

Four throwaway spikes replayed the real prompts and tool calls of the 11 fully rated sessions (from `~/.claude/projects/*/<session>.jsonl`) against the memory pool as it existed at each moment, and scored each gate variant against the session's actual ratings. Scripts live in the session scratchpad and are not kept.

| Gate variant (session-level union) | Served | Useful kept | Precision |
|---|---|---|---|
| Today: bootstrap dump + contextual OR-match | 63 | 12 of 12 | 19% |
| Typed prompts only, 2+ distinct non-ubiquitous keyword hits, no bootstrap dump | 21 | 9 of 12 | 43% |
| Same with 3+ hits | 10 | 5 of 12 | 50% |
| Tool-call keyword matching, any threshold | 25 to 61 | 7 to 12 | 20% to 28% |
| Activity-signature targeting learned from past ratings (leave one session out) | 22 | 7 of 12 | 32% |

Three structural facts fall out of this:

1. **Ratings are per (session, memory), and a session serves a memory at most once.** Over a 70 to 230 prompt session, every memory eventually matches some prompt or tool call, so gate strictness alone cannot move the ratio. Only the typed-prompt channel escapes, because typed prompts are few and short.
2. **Bootstrap is the largest loser.** It dumps all six general-scope rules into every session in every project; they account for 29 of the 51 ignored ratings and were useful only in spec and plan writing sessions.
3. **Timing is the untested lever.** Every useful rating came from a memory that matched the activity of the moment: planning rules while writing specs and plans, the heredoc pitfall while dispatching subagents or writing large files, the web pitfalls in a deep-research session. Today those memories arrive at session start. Serving them at the triggering tool call is the only route to *more* useful ratings rather than fewer serves. History cannot prove this because it has never been tried.

Two further measurements shape the design:

- **The prompt hook fires on peer-agent messages.** `contextual_fired_userprompt` is 571 across the transcripted sessions, which hold 261 typed prompts and 296 peer messages ("Another Claude session sent a message: ..."). Peer messages are long status reports with vocabulary that matches everything.
- **PreToolUse cost is interpreter start, not database work.** A full PreToolUse run of the hook takes 184 ms; a latched run takes 175 ms; interpreter plus imports alone take 168 ms. Removing the once-per-session latch adds nothing measurable.

## Design

### 1. Serving model

Four channels, each with one job. The per-session `SeenStore` dedup applies across all of them, so a memory arrives once per session whichever channel claims it first. The per-firing item cap (`BETTER_MEMORY_CONTEXT_MAX_ITEMS`, default 3) is unchanged.

**Bootstrap (SessionStart).** In deferred mode, `SessionBootstrapService.bootstrap` no longer renders general-scope semantic memories in full. It emits the header, the index line ("better-memory knows N reflections + M semantic memories ...") and the footer only, and records no bootstrap exposures. The code default for `BETTER_MEMORY_INJECT_MODE` becomes `deferred`, matching what `better-memory setup` already writes into `~/.claude/settings.json` (`manifest.MANAGED_ENV`). Legacy mode is untouched: its contract is byte-identical pre-deferred behaviour and it remains an explicit opt-in.

**Typed-prompt channel (UserPromptSubmit).** Fires on every prompt as today, with two changes:

- *Non-human prompts are skipped.* The hook payload carries no origin field, so the prompt text is checked against a short prefix list before any database work: text starting with `<` (command output, task notifications, system tags) or with `Another Claude session sent a message` is not a human prompt and produces no injection and no exposure. `contextual_suppressed_nonhuman` is added to `rating_diagnostics`.
- *One evidence gate for both memory kinds.* `retrieve_relevant` replaces the reflection BM25 "any single token" qualifier and the semantic 2-keyword fallback with a single rule: tokenise the prompt with `extract_keywords`, drop any token whose document frequency across the current candidate pool (reflections plus semantics for the project) exceeds `DF_CAP = 0.5`, count distinct whole-word hits per memory, and qualify a memory when `hits >= cfg.context_min_hits` (default 2; `BETTER_MEMORY_CONTEXT_MIN_HITS` is un-deprecated for this purpose). Ranking among qualifiers stays reciprocal rank fusion of the Wilson prior with a relevance rank; the relevance rank is now the distinct-hit count (ties by BM25 where the FTS table exists). Agentcore mode keeps its `relevance_ranks` gate unchanged.

**Trigger channel (PreToolUse).** Fires on every tool call; the once-per-session latch (`SeenStore.try_claim_pretool_fired`) is removed. The hook loads the memories that carry triggers, matches each trigger against the tool name and input with the pure matcher in section 2, filters hits through `SeenStore`, caps them, records exposures with the new source value `trigger`, and renders them through `format_relevant` with one extra line per item naming the trigger that fired (for example `Triggered by: bash:<<`). A memory without triggers is never tool-triggered. `trigger_fired` and `trigger_injected` counters are added to `rating_diagnostics`; `contextual_fired_pretool` keeps counting firings for continuity.

**Explicit retrieve (`memory.retrieve`, `memory.retrieve_observations`).** Unchanged.

### 2. Triggers

**Storage.** Migration `0019_memory_triggers.sql` adds a nullable `triggers TEXT` column (JSON array of strings, or NULL) to `reflections` and `semantic_memories`. No index is needed: the candidate query is `WHERE triggers IS NOT NULL` over a pool of at most a few hundred rows.

**Grammar.** One trigger is one string. Five forms, validated on write; an unknown prefix is rejected with a `ValueError` naming the offending string.

| Trigger | Matches when |
|---|---|
| `tool:<Name>` | `tool_name == Name` (exact, case-sensitive, e.g. `tool:WebFetch`, `tool:Agent`) |
| `skill:<name>` | `tool_name == "Skill"` and `tool_input.skill == name`; a trailing `*` matches any skill with that prefix (e.g. `skill:superpowers:*`) |
| `bash:<text>` | `tool_name == "Bash"` and `text in tool_input.command` |
| `path:<text>` | `tool_name in {"Write", "Edit", "Read"}` and `text in tool_input.file_path` (separators normalised to `/`) |
| `write:large` | `tool_name == "Write"` and `len(tool_input.content) > cfg.trigger_large_write_chars` (default 8000, `BETTER_MEMORY_TRIGGER_LARGE_WRITE_CHARS`) |

**Matcher.** `better_memory/services/triggers.py`: `parse_triggers(raw) -> list[Trigger]`, `validate_triggers(list[str]) -> list[str]`, and `match(triggers, tool_name, tool_input) -> str | None` returning the first trigger string that fired (used as the display reason). Pure functions, no I/O, unit-tested in isolation. The `text` operands are matched case-insensitively.

**Setting triggers.**

- *Management UI.* The reflection edit drawer (`GET/POST /reflections/<id>/edit`) and the semantic update route (`POST /semantic/<id>/update`) gain a `triggers` textarea, one trigger per line. The drawers display current triggers. Both are gated by a new capability flag `caps.supports_triggers` (true on sqlite, false on agentcore) with the matching `abort(404)` guards, following the existing capability-gating pattern.
- *MCP.* `memory.semantic_observe` and `memory.semantic_update` accept an optional `triggers: list[str]`, validated with the same function, so Claude can attach triggers when it records a rule. Reflections get triggers through the UI only; filling them during synthesis is supply-side work and out of scope.
- *Storage protocol.* `StorageBackend` gains `supports_triggers` (property), `set_triggers(kind, id, triggers)` and `triggered_candidates(project) -> list[dict]` (id, kind, display text, triggers, rating counters). Sqlite implements them; agentcore reports `supports_triggers = False`, raises `NotImplementedError` from `set_triggers`, and returns `[]` from `triggered_candidates`, so the trigger channel is simply inactive there.

**Deployment step for the ten existing memories** (set by the owner through the UI; values proposed from each memory's text and its useful-rating evidence lines):

| Memory | Proposed triggers |
|---|---|
| Planning rule: confidence per task (`3c5faaa1`) | `skill:superpowers:writing-plans`, `path:docs/superpowers/plans` |
| Planning rule: sub-90% not acceptable (`6f890c7c`) | `skill:superpowers:writing-plans`, `path:docs/superpowers/plans` |
| No timescales (`ca5e13b1`) | `skill:superpowers:writing-plans`, `skill:superpowers:brainstorming`, `path:docs/superpowers/` |
| Design docs end with Assumptions (`f9216a31`) | `skill:superpowers:brainstorming`, `path:docs/superpowers/specs` |
| Spikes: just run them (`11efa502`) | `skill:superpowers:brainstorming`, `skill:superpowers:writing-plans` |
| Never encode input-specific rules (`63a4a799`) | `skill:superpowers:brainstorming`, `path:docs/superpowers/specs` |
| Write tool, not heredoc, for large files (`94113ed6`) | `bash:<<`, `write:large`, `tool:Agent` |
| Reddit unreachable (`98175e0f`) | `tool:WebFetch`, `tool:WebSearch`, `skill:anthropic-skills:deep-research` |
| WebFetch refuses retail domains and PDFs (`c6756ce6`) | `tool:WebFetch`, `skill:anthropic-skills:deep-research` |
| Playwright for Amazon and eBay (`561e74d4`) | `tool:WebFetch`, `skill:anthropic-skills:deep-research` |

### 3. Measurement

The Diagnostics page (`/diagnostics`) gains a **Useful rate by channel** panel, read from the local `memory.db` on both backends (it is session-operational state), with two windows: all time, and the last 15 rated sessions. Columns: source, rated, useful, ignored, misled, rate. A footer line reports useful ratings per rated session (today: 12 over 11 sessions, about 1.1), which is the guard against reaching the ratio by starving the channels. Query lives in `ui/queries.py`.

**Success criteria**, judged over the first 15 rated sessions after deployment:

- overall useful rate, all channels, at least 33%;
- useful ratings per rated session not below 1.1;
- the `trigger` channel's own rate reported, so that if the overall target is missed the next decision (retune triggers on specific memories, or add a track-record floor) is data-driven. The track-record floor is deliberately not part of this design.

### 4. Error handling and backends

- Both hooks keep the existing contract: never raise, always exit 0, failures recorded in `hook_errors`, no `additionalContext` on a failed turn.
- A malformed `triggers` JSON value on a row is treated as no triggers (logged once per hook run to `hook_errors`), never as a hook failure.
- Trigger validation errors surface as ordinary tool or form errors on write; nothing invalid reaches the column.
- Agentcore: trigger channel inactive, UI fields hidden, MCP `triggers` parameter rejected with a clear message when the backend does not support triggers. Nothing else in agentcore mode changes.

### 5. Documentation

In the same PR: `website/architecture.md` (injection strategies, trigger channel, protocol additions), `website/configuration.md` (`BETTER_MEMORY_CONTEXT_MIN_HITS` un-deprecated, `BETTER_MEMORY_TRIGGER_LARGE_WRITE_CHARS`, default inject mode), `website/mcp-tools.md` (`triggers` parameter), and the README's hooks paragraph.

## Testing

- **Unit:** trigger grammar parsing, validation and matching for every form including the `skill:*` prefix, path separator normalisation and the large-write threshold; the prompt gate's document-frequency filter and distinct-hit floor on a fixture pool built from the ten live memories and the replayed prompts (useful and ignored cases from the spike as regression examples); the non-human prefix skip; deferred bootstrap rendering with no semantic section and no bootstrap exposures.
- **Hooks:** PreToolUse without the latch fires on the second and later calls; a trigger hit records a `trigger` exposure and renders the reason line; a session with no triggered memories costs no exposure rows; UserPromptSubmit on a peer message produces no injection and bumps `contextual_suppressed_nonhuman`.
- **Migration:** `0019` applies on an existing database and is idempotent.
- **Protocol and UI:** sqlite `set_triggers` / `triggered_candidates`; agentcore capability false with 404s on the trigger routes; both drawers round-trip triggers; MCP schema tests for the `triggers` parameter.
- **Diagnostics:** the useful-rate query on a seeded ledger for both windows and the per-session guard figure.
- **E2E:** the clean-slate smoke suite unchanged plus one scenario: set a trigger on a semantic memory, fire the hook with a matching Bash heredoc payload, assert injection and exposure source.

## Out of scope

Synthesis and the un-synthesised episode backlog; automatic trigger extraction at synthesis time; changes to `memory.retrieve` ranking; a Wilson track-record suppression floor; agentcore trigger storage; the `sqlite-vec` removal (#131).

## Assumptions

Each line: the assumption, its status, and what it costs if wrong.

- **A1. Serving a memory at the triggering tool call raises how often it is applied and rated useful.** Unverified: history has no just-in-time serves to measure. Cost if wrong: the ratio stays near 20 to 25% (the trigger channel's replayed precision on session-level ratings); the fallback is the track-record floor kept out of this design.
- **A2. Session-end ratings are noisy enough that fewer than about 15 sessions cannot show a sustained rate.** Measured: sessions with near-identical skill usage rated the same planning rules useful in one and ignored in another. Cost if wrong (noise is lower): the window could be shorter; no design change.
- **A3. Removing the PreToolUse latch adds no measurable latency per tool call.** Measured: 184 ms full path against 175 ms latched, 168 ms of which is interpreter start and imports. Cost if wrong: a per-tool-call delay; mitigation is `BETTER_MEMORY_CONTEXT_INJECT_MODE=userprompt`.
- **A4. Gate constants: distinct-hit floor 2, document-frequency cap 50%.** Measured on 11 sessions and 12 useful ratings: floor 2 gives 43% precision at 75% recall, floor 3 gives 50% at 42%. This band rests on 12 positives and is thin; it is the constant most likely to need retuning once the measurement panel has more data.
- **A5. The prompt hook payload has no origin field, so non-human prompts must be detected by prefix.** Measured that the hook fires on peer messages (571 firings against 261 typed prompts plus 296 peer messages); the payload shape is as read in `contextual_inject._query_from`. Cost if wrong (a non-human prompt form without a known prefix): that form keeps leaking long texts into the gate; the diagnostics counter will show it.
- **A6. The ten existing memories map to accurate triggers by hand.** Verified by inspection of each memory's text and its useful-rating evidence lines (table in section 2). Cost if wrong for one memory: that memory under- or over-fires; fixable in the UI without code.
- **A7. Large-write threshold of 8,000 characters.** Unmeasured band, set below the roughly 30 KB heredoc failure the pitfall records and above typical source files. Cost if wrong: the heredoc pitfall over- or under-fires on Write; tunable by env without code.
- **A8. Agentcore users are unaffected by triggers being sqlite-only.** Verified that the protocol already uses capability flags for sqlite-only features and the UI gates on them. Cost if wrong: none for the owner, who runs sqlite.
- **A9. `better-memory setup` already installs deferred mode, so flipping the code default changes nothing for installed users.** Verified: `manifest.MANAGED_ENV = {"BETTER_MEMORY_INJECT_MODE": "deferred"}`. Cost if wrong: a user on a hand-edited settings file sees the dump disappear; the index line tells them how to retrieve.
- **A10. Per-prompt gating cannot by itself reach the target.** Measured in the replay (every prompt-level and tool-level keyword gate saturates at session level). This is why the design adds triggers rather than only tightening the gate.
