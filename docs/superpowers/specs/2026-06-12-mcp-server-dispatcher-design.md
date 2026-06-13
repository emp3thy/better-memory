# MCP server dispatcher refactor

Status: design
Date: 2026-06-12
Target file: `better_memory/mcp/server.py`
Related tech-debt finding: `mcp-server-god-module-dispatcher`
Source report: `.tech-debt/reports/mcp-server-god-module-dispatcher.md`

## Problem

`better_memory/mcp/server.py` is the repo's worst hotspot: 1617 LOC, complexity 4880, 52 commits in 12 months, hotspot score 67.5 (2.1× the runner-up). The single function `_call_tool` (lines 1052-1525) is a 470-LOC `if name == "..."` chain dispatching 22 tools, inline-importing services in 7 places, instantiating `SemanticMemoryService` 4 times per call (lines 1086, 1101, 1118, 1124) and `SessionBootstrapService` 2 times (lines 1392, 1493). Schemas live in a sibling `_tool_definitions` block (lines 259-806, 550 LOC of hand-rolled JSON-Schema dicts) ~750 lines from the handler that uses them. `create_server` (lines 953-1561) is a 600-LOC god-factory.

Consequence: every new tool, every schema tweak, and every service-wiring change re-pays the same tax. Contributors have already worked around this in test code — `_dispatch_for_tests` (line 1564) exists only because `_call_tool` cannot be called any other way, and `tests/mcp/test_synthesize_tools.py:17-22` documents the workaround verbatim.

## Goals

- Decompose `_call_tool` into a `ToolDispatcher` + per-domain handler modules.
- Build every long-lived service exactly once and hand them to handlers through a `ServiceContainer`.
- Co-locate each tool's `inputSchema` with the handler method that uses it.
- Preserve every behavioural invariant the current dispatcher upholds: connection ownership, capability gating, SDK error surface, audit-log ordering, `memory.retrieve` background-work order.
- Land as a single PR. There is no in-flight work on `_call_tool` to conflict with.

## Non-goals

- Not changing the MCP wire format or any tool name / schema field.
- Not introducing `pydantic` / `msgspec` argument models (tracked separately if desired).
- Not fixing `test_server_integration.py`'s stale skip (tracked under `mcp-integration-tests-stale-skip`).
- Not refactoring `services/reflection.py` or any other hotspot (tracked separately).
- Not changing the `_dispatch_for_tests` test entry point's signature.

## Locked design choices

The 5 open questions in the source report are resolved as follows:

| # | Question | Choice |
|---|----------|--------|
| 1 | Handler granularity | **Per-domain class with methods (~9 files)** |
| 2 | Schema location | **Co-located in each handler module as module-level constants** |
| 3 | Migration shape | **Single big-bang PR** (no in-flight conflicts) |
| 4 | `_resolve_session_id` location | **Free function in `mcp/_session.py`** |
| 5 | `_dispatch_for_tests` | **Keep as a thin shim over `dispatcher.call`** |

## Architecture

### Target module layout

```
better_memory/mcp/
├── __init__.py
├── __main__.py                # unchanged
├── server.py                  # ~150 LOC: create_server, run, cleanup
├── _session.py                # _resolve_session_id moved here (free function)
├── container.py               # ServiceContainer dataclass
├── dispatcher.py              # ToolDispatcher + Handler dataclass
└── handlers/
    ├── __init__.py            # collects domain-handler instances → list[Handler]
    ├── _audit.py              # _audit_synth_call (moved from server.py)
    ├── observations.py        # ObservationHandlers (4 tools)
    ├── semantics.py           # SemanticHandlers (4 tools)
    ├── episodes.py            # EpisodeHandlers (4 tools)
    ├── reflections.py         # ReflectionHandlers (2 tools)
    ├── retention.py           # RetentionHandlers (1 tool)
    ├── knowledge.py           # KnowledgeHandlers (2 tools)
    ├── ratings.py             # RatingHandlers (3 tools)
    └── session.py             # SessionHandlers (memory.session_bootstrap, memory.start_ui)
```

### Core types

```python
# container.py
@dataclass(frozen=True)
class ServiceContainer:
    """All long-lived services + connections, built once in create_server."""
    config: Config
    memory_conn: sqlite3.Connection
    backend: StorageBackend
    episodes: EpisodeService
    observations: ObservationService
    reflections: ReflectionSynthesisService
    retention: RetentionService
    memory_rating: MemoryRatingService
    knowledge: KnowledgeService
    spool: SpoolService
    semantic: SemanticMemoryService              # built ONCE (was 4× inline)
    session_bootstrap: SessionBootstrapService    # built ONCE (was 2× inline)
```

```python
# dispatcher.py
@dataclass(frozen=True)
class Handler:
    name: str
    schema: dict[str, Any]                       # MCP inputSchema JSON-Schema
    call: Callable[[ServiceContainer, dict[str, Any]], Awaitable[list[TextContent]]]
    requires_synthesis: bool = False             # capability gate

class ToolDispatcher:
    def __init__(self, services: ServiceContainer, handlers: list[Handler]):
        self._services = services
        self._handlers = {h.name: h for h in handlers}

    def tool_definitions(self) -> list[Tool]:
        supports = self._services.backend.supports_synthesis
        return [
            Tool(name=h.name, description=..., inputSchema=h.schema)
            for h in self._handlers.values()
            if supports or not h.requires_synthesis
        ]

    async def call(self, name: str, args: dict[str, Any]) -> list[TextContent]:
        handler = self._handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown tool: {name}")
        if handler.requires_synthesis and not self._services.backend.supports_synthesis:
            raise ValueError(f"Unknown tool: {name}")     # same shape as today
        return await handler.call(self._services, args)
```

```python
# handlers/semantics.py
_OBSERVE_SCHEMA = {"type": "object", "properties": {...}, "required": ["content"]}
_RETRIEVE_SCHEMA = {...}
_UPDATE_SCHEMA = {...}
_DELETE_SCHEMA = {...}

class SemanticHandlers:
    """All memory.semantic_* tools."""

    async def observe(self, services: ServiceContainer, args: dict) -> list[TextContent]:
        memory_id = services.semantic.create(
            content=args["content"],
            project=services.config.project_name,
            scope=args.get("scope") or "project",
        )
        return [TextContent(type="text", text=json.dumps({"id": memory_id}))]

    async def retrieve(self, services, args) -> list[TextContent]: ...
    async def update(self, services, args) -> list[TextContent]: ...
    async def delete(self, services, args) -> list[TextContent]: ...

    def handlers(self) -> list[Handler]:
        return [
            Handler("memory.semantic_observe",  _OBSERVE_SCHEMA,  self.observe),
            Handler("memory.semantic_retrieve", _RETRIEVE_SCHEMA, self.retrieve),
            Handler("memory.semantic_update",   _UPDATE_SCHEMA,   self.update),
            Handler("memory.semantic_delete",   _DELETE_SCHEMA,   self.delete),
        ]
```

```python
# handlers/__init__.py
def all_handlers() -> list[Handler]:
    return [
        *ObservationHandlers().handlers(),
        *SemanticHandlers().handlers(),
        *EpisodeHandlers().handlers(),
        *ReflectionHandlers().handlers(),
        *RetentionHandlers().handlers(),
        *KnowledgeHandlers().handlers(),
        *RatingHandlers().handlers(),
        *SessionHandlers().handlers(),
    ]
```

### `create_server` after refactor

```python
# server.py — full file, ~150 LOC
def create_server() -> tuple[Server, Callable[..., Awaitable[None]], ServerContext]:
    config = get_config()
    memory_conn, audit_conn = _open_connections(config)
    embedder = _build_embedder(config)
    services = _build_services(config, memory_conn, audit_conn, embedder)
    _best_effort_knowledge_reindex(services.knowledge, config)

    dispatcher = ToolDispatcher(services, all_handlers())

    server = Server(name="better-memory")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return dispatcher.tool_definitions()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        return await dispatcher.call(name, arguments or {})

    cleanup = _build_cleanup(memory_conn, audit_conn, embedder)
    return server, cleanup, ServerContext(backend=services.backend, dispatcher=dispatcher)


def _build_services(cfg, memory_conn, audit_conn, embedder) -> ServiceContainer:
    """Construct every service ONCE. Replaces all inline service-build smells."""
    backend = build_backend(cfg, memory_conn, audit_conn, embedder)
    return ServiceContainer(
        config=cfg,
        memory_conn=memory_conn,
        backend=backend,
        episodes=EpisodeService(memory_conn, backend),
        observations=ObservationService(memory_conn, backend, embedder),
        reflections=ReflectionSynthesisService(memory_conn, backend),
        retention=RetentionService(memory_conn, backend),
        memory_rating=MemoryRatingService(memory_conn),
        knowledge=KnowledgeService(memory_conn),
        spool=SpoolService(memory_conn),
        semantic=SemanticMemoryService(memory_conn),         # was 4× inline
        session_bootstrap=SessionBootstrapService(...),       # was 2× inline
    )
```

### Data flow per tool call

```
MCP stdio frame
   │
   ▼
Server SDK ──── _call_tool(name, args)
   │
   ▼
ToolDispatcher.call(name, args)
   │  O(1) lookup in self._handlers dict
   ▼
Handler.call(services, args)
   │
   ▼
domain method  (e.g. SemanticHandlers.observe)
   │  uses services.semantic / services.memory_conn
   ▼
list[TextContent]  ──── SDK serialises ──── stdio
```

## Invariants preserved

| Invariant | Where it lives today | How we preserve it |
|-----------|---------------------|--------------------|
| **Connection ownership.** One shared `memory_conn`, MCP stdio serialises requests, SAVEPOINT safety. | `server.py:988-999` | Connection lives on `ServiceContainer` as a single attr; all services share it; handlers reach it only through the container; no new threads. |
| **Capability gating.** Synthesis tools hidden and rejected when `backend.supports_synthesis` is `False`. | Filter at `_list_tools` time; `_call_tool` falls through to `ValueError("Unknown tool")` | `Handler(requires_synthesis=True)` flag. Dispatcher filters in both `tool_definitions()` and `call()` with the same `ValueError("Unknown tool: {name}")` shape. |
| **SDK error surface.** Exceptions from handlers surface as `CallToolResult(isError=True)`. | SDK catches whatever `_call_tool` raises | Dispatcher does not catch — exceptions from handler methods propagate up through `dispatcher.call` to the SDK unchanged. |
| **Audit log ordering.** `_audit_synth_call` writes a `start` JSONL line then a `complete` line. | `server.py:176-221`, used by 2 synthesis handlers | Move to `mcp/handlers/_audit.py` with the same context manager signature. Used identically by `ReflectionHandlers.synthesize_next_get_context` and `.synthesize_next_apply`. `test_synth_audit_log.py` passes byte-for-byte. |
| **`memory.retrieve` background-work order.** `spool.drain` → `retention.maybe_schedule` → `backend.retrieve`. | `server.py:1143-1163` | Lifted verbatim into `ObservationHandlers.retrieve`. Order asserted by a new test using a recording stub container. |

## Seams surfaced

- `ReflectionSynthesisService._read_queue_counts` is called from 2 dispatcher branches (lines 1263, 1422). Promote to public `read_queue_counts` on the service — small ahead-of-time commit so the handler split is clean.
- `_resolve_session_id` (server.py:147) moves to `mcp/_session.py` as a free function. Handlers import directly; no container hop.
- `_audit_synth_call` moves to `mcp/handlers/_audit.py`. Reflection-specific; co-locate with the only handler module that uses it.

## Testing

### Existing tests — change matrix

| File | What it covers | Refactor impact |
|------|---------------|-----------------|
| `test_server_sqlite.py` | Happy-path roundtrip via `_dispatch_for_tests` | Unchanged. Shim preserves entry point. |
| `test_server_backend_dispatch.py` | Capability gating, `create_server` return shape | Update assertions on the dispatcher field of `ServerContext`. |
| `test_synth_audit_log.py` | Audit log byte-shape | Update import path to `better_memory.mcp.handlers._audit`. |
| `test_rating_tools.py` | Rating tools via `_dispatch_for_tests` | Unchanged. |
| `test_episode_tools.py`, `test_semantic_tools.py`, `test_retention_tool.py`, `test_session_bootstrap_tool.py`, `test_start_ui_tool.py` (5 files) | Service-level wrappers | Unchanged. The "tool is a thin wrapper" docstring workaround no longer needs to apologise. |
| `test_synthesize_tools.py` | Serializer helpers + service round-trip | Update imports for any serializer helpers that move into `handlers/reflections.py`. |
| `test_server_integration.py` | Subprocess MCP integration — currently `pytest.mark.skip` | Out of scope (tracked under `mcp-integration-tests-stale-skip`). |

### New test files (4)

```
tests/mcp/
├── test_tool_dispatcher.py        NEW
├── test_service_container.py      NEW
├── handlers/
│   ├── test_semantics_handler.py  NEW
│   └── test_observations_handler.py NEW
```

| New file | Asserts |
|----------|---------|
| `test_tool_dispatcher.py` | Parametrise over all 22 tool names: registered iff capability allows, `call("nope", {})` raises `ValueError("Unknown tool: nope")`, capability-gated tool with capability off raises same shape, every `Handler` obeys the dataclass contract. |
| `test_service_container.py` | Build a container, monkeypatch each service `__init__` to count calls. Assert **each service constructed exactly once** — kills the 4× `SemanticMemoryService` and 2× `SessionBootstrapService` regressions permanently. |
| `handlers/test_semantics_handler.py` | Per-method tests with a stub container. Covers `scope=None` fallback to `"project"` (regresses the PR #25 BugBot fix). One happy + at least one error path per tool. |
| `handlers/test_observations_handler.py` | Recording stub container asserts `memory.retrieve` calls `spool.drain` → `retention.maybe_schedule` → `backend.retrieve` in that order. |

### Sibling-finding sequencing

The sibling tech-debt finding `mcp-server-tool-error-path-tests` (PBI in `top5.json`) gives the per-tool error coverage for all 22 tools. **Run it first.** Each green error-path test guards a domain we then migrate so no PR ships handlers with weaker coverage than today.

## Risk

- **Test churn.** ~10 existing test files. 7 unchanged, 3 small import / assertion updates. Risk: low.
- **Audit-log byte-shape regression.** `_audit_synth_call` is the largest preserved invariant. Mitigated by keeping the context manager signature identical and re-running `test_synth_audit_log.py`.
- **Capability-gate double check.** Dispatching a synthesis-only tool when the backend has no synthesis support must raise the same `ValueError("Unknown tool: ...")` as today, not a new `ValueError("Capability disabled")`. Test in `test_tool_dispatcher.py` pins this.
- **Inline-service regression.** Future contributors could re-introduce per-call `SemanticMemoryService(memory_conn)` in a handler. `test_service_container.py` catches this directly.

## Effort

Large (L), ~7-10 focused days, single PR:

| Phase | Days | Output |
|-------|------|--------|
| A — Scaffolding | 1.5 | `container.py`, `dispatcher.py`, `_session.py`, `handlers/__init__.py`, `handlers/_audit.py`, `Handler` dataclass, `ToolDispatcher`, `ServiceContainer`. Tests: `test_tool_dispatcher.py`, `test_service_container.py`. |
| B — Bulk migration | 4-5 | 8 handler modules (observations, semantics, knowledge, episodes, reflections, retention, ratings, session). Promote `_read_queue_counts`. Migrate `_audit_synth_call`. ~0.5-1 d per domain. |
| C — Cleanup | 1.5-2 | Delete `_call_tool` if-chain, delete inline-import lines, retire `_tool_definitions`, slim `_dispatch_for_tests` to a 3-liner, update test docstrings, write `test_observations_handler.py` + `test_semantics_handler.py`. |

The sibling `mcp-server-tool-error-path-tests` PBI should land **before** Phase A so each domain we migrate has error-path coverage to fall back on.

## Out of scope (follow-ups)

- Replacing hand-rolled JSON-Schema dicts with pydantic / msgspec argument models. Genuinely better long-term; bundling here risks blowing up scope on a "schema shape changed" debate.
- Splitting `services/reflection.py` (its own god-module finding, `reflection-service-god-module`).
- Fixing `test_server_integration.py` (`mcp-integration-tests-stale-skip` finding).
