---
id: chore-shared-helper-duplication-hooks-services-2026-06-10
type: chore
status: inbox
severity: normal
attempts: 0
depends_on: []
target_repo: https://github.com/emp3thy/better-memory
source_design: C:\Users\gethi\source\better-memory\.tech-debt\design.md
category: duplication
debt_type: code
effort: M
---

# Session-ID, clock, and spool/timestamp helpers copy-pasted across modules

### Reasoning

Merged three duplication findings with one root cause: no lightweight shared utility module, so hooks and services copy helpers (session_close.py even comments that it duplicates 'to avoid cross-module import'). Copies live in three hotspots (server.py, observation.py, reflection.py), so divergence risk is re-paid constantly â€” the session-ID fallback already has 7 copies that can drift. High confidence, M effort, mechanical fix.

### Evidence

- `better_memory/storage/session.py:35` - resolve_session_id() reads CLAUDE_SESSION_ID / CLAUDE_CODE_SESSION_ID, falls back to uuid4().hex â€” canonical copy.
- `better_memory/hooks/session_bootstrap.py:66` - Identical session-ID env-var resolution pattern duplicated.
- `better_memory/hooks/session_close.py:54` - Session-ID pattern duplicated here and again at line 270 in main().
- `better_memory/hooks/post_commit.py:150` - Same env-var resolution pattern; also duplicates _default_spool_dir (line 45) and _safe_timestamp (line 50).
- `better_memory/services/observation.py:146` - Another session-ID copy in the observation service (hotspot file).
- `better_memory/mcp/server.py:1389` - Session-ID pattern duplicated in the MCP server (top hotspot).
- `better_memory/hooks/session_close.py:29` - _default_spool_dir duplicated with a comment explicitly acknowledging duplication to avoid import; _safe_timestamp duplicated at line 41 (also in observer.py lines 27/39).
- `better_memory/services/reflection.py:39` - _default_clock() -> datetime.now(UTC) duplicated identically across 8 modules: reflection.py, observation.py:89, retention.py:16, episode.py:27, memory_rating.py:20, semantic.py:22, retention_scheduler.py:23, search/hybrid.py:419.

### Suggested fix

Create a lightweight shared utility module (e.g. better_memory/_common.py importable by hooks without heavy deps) housing get_session_id(), _default_clock(), _default_spool_dir(), and _safe_timestamp(). Replace all copies with imports; delete the 'duplicated to avoid import' workaround in hooks.
