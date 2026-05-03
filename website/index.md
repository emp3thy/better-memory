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
    <p class="brut-lede">A semantic + episodic memory manager for Claude Code. SQLite, local Ollama, no cloud.</p>
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
    <p class="brut-bucket-sub">reinforcement-weighted — memory.record_use promotes signal, demotes noise.</p>
  </div>
</section>
