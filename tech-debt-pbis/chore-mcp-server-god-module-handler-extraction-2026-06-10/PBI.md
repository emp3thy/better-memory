---
id: chore-mcp-server-god-module-handler-extraction-2026-06-10
type: chore
status: inbox
severity: high
attempts: 0
depends_on: []
target_repo: https://github.com/emp3thy/better-memory
source_design: C:\Users\gethi\source\better-memory\.tech-debt\design.md
category: god-modules
debt_type: architecture
effort: L
---

# mcp/server.py god module: 22 inline tool handlers mix dispatch, I/O, logic

### Reasoning

Merged three findings (god-module, god-node architecture, per-call SemanticMemoryService instantiation) sharing one root cause: a monolithic _call_tool. Highest-interest file in the repo (churn 52, complexity 4880, hotspot score 67.5 â€” double the next), so this debt is re-paid on every change. Effort is L but the impact dwarfs the rest of the list; handlers are stateless so extraction is mechanical.

### Evidence

- `better_memory/mcp/server.py:1053` - 22 tool handlers (if name == 'memory.*') in _call_tool dispatcher mixing observation lifecycle, semantic CRUD, episode management, reflection synthesis, retention, spool drain, Ollama probing, session bootstrap, knowledge search. 1617 LOC, 52 churn, 4880 complexity; cross-community bridge with 15+ edges.
- `better_memory/mcp/server.py:1084` - SemanticMemoryService(memory_conn) instantiated inline in 4 separate handlers (lines 1084, 1099, 1118, 1122) instead of once in create_server().
- `graphify-out/GRAPH_REPORT.md:100` - create_server() is a god node with 18 edges, ranked 2nd; betweenness centrality 0.375 across 9 communities.

### Suggested fix

Extract tool handlers into per-domain handler classes (observations, reflections, episodes, semantics, knowledge) registered in a {tool_name: handler} dict, reducing _call_tool to ~50 LOC of pure dispatch. Move service construction (incl. SemanticMemoryService) into create_server() and inject into handlers.
