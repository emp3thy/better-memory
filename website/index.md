---
template: home.html
title: better-memory
description: Memory that sticks between sessions.
hide:
  - navigation
  - toc
---

<nav class="brut-nav">
  <div class="brut-nav-brand">
    <span class="brut-bracket">[</span> better-memory <span class="brut-bracket">]</span>
  </div>
  <div class="brut-nav-links">
    <a href="#install">install</a>
    <a href="configuration/">docs</a>
    <a href="https://github.com/emp3thy/better-memory">github</a>
  </div>
</nav>

<section class="brut-section brut-hero-section">
  <div class="brut-container brut-wide">
    <div class="brut-eyebrow">// LOCAL-FIRST · MCP · CLAUDE-CODE</div>
    <h1 class="brut-display">memory that<br><span class="brut-hl">sticks</span> between sessions.</h1>
    <p class="brut-lede">A semantic + episodic memory manager for Claude Code. Local SQLite + FTS5 — no embedding model, no external service. Synthesis runs in your Claude Code session — no separate cloud dependency.</p>
    <div class="brut-ctas">
      <a class="brut-cta brut-cta-primary" href="#install">install →</a>
      <a class="brut-cta brut-cta-secondary" href="architecture/">read the spec →</a>
    </div>
  </div>
</section>

<section class="brut-section brut-shade">
  <div class="brut-container">
    <div class="brut-num">02 — SPECIMEN</div>
    <h2 class="brut-section-h">what an observation looks like</h2>
    <p class="brut-lede brut-muted">A real <code>memory.observe()</code> call, and what comes back from <code>memory.retrieve()</code> next session.</p>

```python
memory.observe(
    content="Growatt Timespan.day inflated; switched to Timespan.hour",
    component="growatt_client",
    outcome="failure",
    trigger_type="debugging",
)
# → returns {"id": "6aa6bf51..."}

# next session, retrieved as:
memory.retrieve(query="growatt") → do bucket
  ↳ "switched to Timespan.hour" — confidence 1.0
```

  </div>
</section>

<section class="brut-section">
  <div class="brut-container">
    <div class="brut-num">03 — RETRIEVAL</div>
    <h2 class="brut-section-h">retrieval, sorted by outcome</h2>
    <p class="brut-lede brut-muted">Three buckets, surfaced separately. The AI sees what worked, what to avoid, and what's just context.</p>
    <div class="brut-buckets">
      <div class="brut-bucket brut-bucket-do">
        <div class="brut-bucket-name">do</div>
        <div class="brut-bucket-role">prior wins</div>
        <div class="brut-bucket-ex">"Calculator uses _estimate_generation_hourly + morning_floor; charge = max(gap_pct, morning_floor_pct)."</div>
      </div>
      <div class="brut-bucket">
        <div class="brut-bucket-name">dont</div>
        <div class="brut-bucket-role">approaches to avoid</div>
        <div class="brut-bucket-ex">"Growatt Timespan.day returned inflated consumption — use Timespan.hour."</div>
      </div>
      <div class="brut-bucket">
        <div class="brut-bucket-name">neutral</div>
        <div class="brut-bucket-role">context</div>
        <div class="brut-bucket-ex">"Python ZoneInfo unavailable on Windows without tzdata package."</div>
      </div>
    </div>
    <p class="brut-bucket-sub">ranked by a Wilson-score hit-rate prior — memory.credit promotes signal, demotes noise.</p>
  </div>
</section>

<section class="brut-section brut-shade">
  <div class="brut-container">
    <div class="brut-num">04 — LIFECYCLE</div>
    <h2 class="brut-section-h">observations have a lifecycle</h2>
    <p class="brut-lede brut-muted">Synthesis consolidates. Retention archives. Prune is opt-in.</p>

```mermaid
stateDiagram-v2
    [*] --> active: memory.observe
    active --> consumed: synthesis
    consumed --> archived: retention
    archived --> [*]: prune (opt-in)
```

  </div>
</section>

<section class="brut-section" id="install">
  <div class="brut-container">
    <div class="brut-num">05 — INSTALL</div>
    <h2 class="brut-section-h">one script, zero paste</h2>
    <p class="brut-lede brut-muted">Clone, run the bootstrap script for your OS. It installs uv if needed, syncs dependencies, and wires the MCP server + hooks into your Claude Code config automatically — nothing to hand-edit.</p>

```bash
git clone https://github.com/emp3thy/better-memory
cd better-memory
```

    <div class="brut-tabs" data-brut-tabs>
      <button class="brut-tab brut-tab-active" data-os="macos">macos</button>
      <button class="brut-tab" data-os="linux">linux</button>
      <button class="brut-tab" data-os="windows">windows</button>
    </div>

    <div class="brut-tab-pane brut-tab-pane-active" data-os-pane="macos">

```bash
./scripts/setup.sh
```

    </div>

    <div class="brut-tab-pane" data-os-pane="linux">

```bash
./scripts/setup.sh
```

    </div>

    <div class="brut-tab-pane" data-os-pane="windows">

```powershell
.\scripts\setup.ps1
```

    </div>

    <p style="margin-top:18px">Idempotent and non-interactive — safe to re-run any time. Restart Claude Code afterward. Run <code>uv run better-memory doctor</code> later to check for drift.</p>
    <p style="margin-top:18px"><a href="configuration/">full setup guide →</a></p>
  </div>
</section>

<section class="brut-section brut-shade">
  <div class="brut-container">
    <div class="brut-num">06 — TOOLS</div>
    <h2 class="brut-section-h">the surface area</h2>

    <table class="brut-tools">
      <thead>
        <tr><th>tool</th><th>purpose</th></tr>
      </thead>
      <tbody>
        <tr><td><a href="mcp-tools/#memoryobserve">memory.observe</a></td><td>Record an episodic observation at a decision point.</td></tr>
        <tr><td><a href="mcp-tools/#memoryretrieve">memory.retrieve</a></td><td>Distilled reflections in three outcome buckets. Drains spool first.</td></tr>
        <tr><td><a href="mcp-tools/#memoryrecord_use">memory.record_use</a></td><td>Stamp reinforcement outcome on a memory after validation.</td></tr>
        <tr><td><a href="mcp-tools/#memorysemantic_observe">memory.semantic_observe</a></td><td>Record a user-stated fact or preference (current truth, not history).</td></tr>
        <tr><td><a href="mcp-tools/#memorysemantic_retrieve">memory.semantic_retrieve</a></td><td>Surface user-stated facts at session start.</td></tr>
        <tr><td><a href="mcp-tools/#memorystart_episode">memory.start_episode</a></td><td>Open a foreground episode for a goal; reports pending synthesis.</td></tr>
        <tr><td><a href="mcp-tools/#memorysynthesize_next_get_context">memory.synthesize_next_*</a></td><td>IDE-driven synthesis split: get_context fetches, apply commits Claude's decision.</td></tr>
        <tr><td><a href="mcp-tools/#knowledgesearch">knowledge.search</a></td><td>BM25 search against the knowledge base.</td></tr>
        <tr><td><a href="mcp-tools/#memoryrun_retention">memory.run_retention</a></td><td>Apply spec §9 retention rules; archive or hard-delete.</td></tr>
        <tr><td><a href="mcp-tools/#memorystart_ui">memory.start_ui</a></td><td>Spawn or reuse the management UI; returns {url, reused}.</td></tr>
        <tr><td><a href="mcp-tools/#memorycredit">memory.credit</a></td><td>Per-tool-use credit on a retrieved memory — closes the reinforcement loop.</td></tr>
      </tbody>
    </table>
    <p style="margin-top:18px"><a href="mcp-tools/">complete tool reference (22 tools) →</a></p>
  </div>
</section>

<section class="brut-section">
  <div class="brut-container">
    <div class="brut-num">07 — GO DEEPER</div>
    <h2 class="brut-section-h">documentation</h2>
    <div class="brut-docs-grid">
      <a href="architecture/" class="brut-doc-card">
        <div class="brut-doc-title">architecture</div>
        <div class="brut-doc-desc">Four-layer epistemic hierarchy, hybrid search, reinforcement-weighted ranking.</div>
        <div class="brut-doc-arrow">→</div>
      </a>
      <a href="configuration/" class="brut-doc-card">
        <div class="brut-doc-title">configuration</div>
        <div class="brut-doc-desc">Env vars, project-name overrides, MCP wiring.</div>
        <div class="brut-doc-arrow">→</div>
      </a>
      <a href="observation-lifecycle/" class="brut-doc-card">
        <div class="brut-doc-title">observation lifecycle</div>
        <div class="brut-doc-desc">Active → consumed → archived. Synthesis, retention, prune.</div>
        <div class="brut-doc-arrow">→</div>
      </a>
      <a href="contributing/" class="brut-doc-card">
        <div class="brut-doc-title">contributing</div>
        <div class="brut-doc-desc">Setup, testing, conventions.</div>
        <div class="brut-doc-arrow">→</div>
      </a>
    </div>
  </div>
</section>

<footer class="brut-footer">
  <div class="brut-footer-left">better-memory · v0.1.0 · MIT</div>
  <div class="brut-footer-right">
    <a href="https://github.com/emp3thy/better-memory">github</a>
    <span class="brut-footer-sep">·</span>
    <span>built with <a href="https://docs.astral.sh/uv/">uv</a> · <a href="https://www.sqlite.org/">sqlite</a></span>
  </div>
</footer>
