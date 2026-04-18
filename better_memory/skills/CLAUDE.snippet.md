## better-memory

This project uses better-memory for persistent AI knowledge.

### Skills

- Starting work → read `better_memory/skills/memory-retrieve.md`
- Decision point reached → read `better_memory/skills/memory-write.md`
- Using a retrieved memory → read `better_memory/skills/memory-feedback.md`
- Session ending → read `better_memory/skills/session-close.md`

### Memory outcomes

Every observation has an `outcome`: `success`, `failure`, or `neutral`. Record failures at the same cadence as successes — `dont` bucket retrieval depends on it.

### Retrieval buckets

`memory.retrieve` returns `{do, dont, neutral, insights, knowledge}`. Treat `dont` as a hard constraint list: do not repeat approaches that live there.

### MCP tools

- `memory.observe(content, component?, theme?, trigger_type?, outcome?)`
- `memory.retrieve(query?, component?, window?='30d', scope_path?)`
- `memory.record_use(id, outcome?)`
- `knowledge.search(query, project?)`
- `knowledge.list(project?)`
