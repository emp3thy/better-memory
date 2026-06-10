---
id: chore-mcp-integration-tests-stale-skip-2026-06-10
type: chore
status: inbox
severity: high
attempts: 0
depends_on: []
target_repo: https://github.com/emp3thy/better-memory
source_design: C:\Users\gethi\source\better-memory\.tech-debt\design.md
category: test-gaps
debt_type: test
effort: M
---

# MCP integration suite skipped with stale reason; handler error paths untested

### Reasoning

Merged the stale-skip half-finished finding with the MCP handler error-path test gap â€” same root cause: the integration suite was parked 'awaiting Phase 2' and never revived, so error handling in the repo's #1 hotspot (server.py) ships unverified, and one test now asserts a stub error for a tool that is fully implemented. Removing the skip is S; adding targeted error-path tests brings it to M â€” a fast, high-impact win.

### Evidence

- `tests/mcp/test_server_integration.py:29` - pytestmark skips entire module citing 'Awaiting Phase 2 episodic service layer' but Phase 2 has shipped; test_memory_start_ui_returns_stub_error (line 205) expects a stub error from a now-implemented tool and would fail if run.
- `better_memory/mcp/server.py:1446` - SynthesisResponseError handling in synthesize_next_apply has no test for a malformed decision dict.
- `better_memory/mcp/server.py:1467` - ValueError handling for stale episode_id only reachable via the skipped integration suite.

### Suggested fix

Remove the module-level skip marker, fix or delete test_memory_start_ui_returns_stub_error, and get the suite green. Then add error-path tests for tool handlers: invalid JSON arguments, missing required params, stale episode_id, closed episode, missing reflection, and DB constraint violations.
