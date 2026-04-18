## better-memory

This project uses better-memory for persistent AI knowledge.

### Skills

- Starting work → read `better_memory/skills/memory-retrieve.md`
- Decision point reached → read `better_memory/skills/memory-write.md`
- Validation arrives (evidence in hand) → read `better_memory/skills/memory-feedback.md`
- Session ending → read `better_memory/skills/session-close.md`

### Memory outcomes — the evidence-in-hand rule

Every observation has an `outcome`: `success`, `failure`, or `neutral`.

- **Default to `neutral`** at observe time. Only claim `success` or `failure` when the evidence exists RIGHT NOW (tests ran, approach reverted, user confirmed).
- For decisions whose outcome is not yet provable, write `neutral`, keep the returned id, and close the loop later with `memory.record_use(id, outcome=...)` once validation arrives.
- Record failures at the same cadence as successes — the `dont` bucket depends on it.

### Retrieval buckets

`memory.retrieve` returns `{do, dont, neutral, insights, knowledge}`. Treat `dont` as a hard constraint list: do not repeat approaches that live there.

### MCP tools

- `memory.observe(content, component?, theme?, trigger_type?, outcome?)`
- `memory.retrieve(query?, component?, window?='30d', scope_path?)`
- `memory.record_use(id, outcome?)`
- `knowledge.search(query, project?)`
- `knowledge.list(project?)`
