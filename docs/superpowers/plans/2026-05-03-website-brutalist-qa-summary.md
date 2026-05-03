# Website Brutalist Redesign — QA Summary (Task 15)

Date: 2026-05-03
Branch: `website-brutalist`
Commits on branch: 24 (since divergence from `main`)

This document captures the server-side QA results for the brutalist redesign and lists items that require live-browser verification before merge.

## Server-side checks

All checks were executed from the worktree root
`C:/Users/gethi/source/better-memory/.worktrees/website-brutalist`.

### 1. Build verification — PASS

```
uv run mkdocs build --strict
INFO    -  Documentation built in 0.36 seconds
```

Strict build succeeded with no warnings (other than the unrelated upstream Material 2.0 deprecation banner printed by the toolchain itself).

### 2. CSS payload size — PASS (under target)

```
site/assets/css/brutalist.css           20,673 bytes (~20.2 KB)
site/assets/css/pygments-brutalist.css   2,254 bytes (~ 2.2 KB)
combined                               ~22,927 bytes (~22.4 KB)
```

Spec target: < 30 KB combined. Actual: ~22.4 KB. Well under budget.

### 3. Font payload size — OVER (knowingly accepted)

```
Inter-ExtraBold.woff2          111,360 bytes
JetBrainsMono-Bold.woff2        94,588 bytes
JetBrainsMono-Medium.woff2      93,824 bytes
JetBrainsMono-Regular.woff2     92,164 bytes
total .woff2                  ~391,936 bytes (~382.7 KB)
```

Spec target: < 80 KB. Actual: ~383 KB. Plan acknowledged this overshoot up-front (we self-host four faces for design fidelity). All four faces are referenced via the four `@font-face` rules in `brutalist.css` and are loaded with `font-display: swap`, so render-blocking impact is bounded.

### 4. Anchor link integrity (landing → MCP tools page) — PASS

All anchors used by the landing MCP-tools section resolve to a unique element on `site/mcp-tools/index.html`:

```
anchor memoryobserve:      1
anchor memoryretrieve:     1
anchor memoryrecord_use:   1
anchor knowledgesearch:    1
anchor knowledgelist:      1
anchor memorystart_ui:     1
```

### 5. Doc card link targets — PASS

All four "Go deeper" doc cards point to pages that exist in the build:

```
architecture: exists
configuration: exists
observation-lifecycle: exists
contributing: exists
```

### 6. CSS structure sanity — PASS

```
website/assets/css/brutalist.css        829 lines
website/assets/css/pygments-brutalist.css 105 lines
^@font-face count: 4   (Inter ExtraBold + JetBrains Mono Regular/Medium/Bold)
^:root count:      1
@media (max-width: 720px) blocks: 3
```

All structural expectations met.

### 7. JS validity — PASS

```
node --check website/assets/js/install-tabs.js  -> OK
node --check website/assets/js/mermaid-init.js  -> OK
```

Both scripts parse cleanly.

### 8. Landing page section completeness — PASS

All eight sections render in `site/index.html`:

```
brut-eyebrow:    1
brut-section-h:  6
brut-buckets:    1
mermaid:         2   (lifecycle diagram + script include)
brut-tabs:       1
brut-tools:      1
brut-doc-card:   4
brut-footer:     4
```

### 9. Docs page rendering — PASS

`site/configuration/index.html`:

```
brut-docs-brand:        1
brut-docs-header-links: 1
md-nav__link:          13
admonition (raw):       2   (class="admonition" occurrences: 2)
```

`site/observation-lifecycle/index.html`:

```
admonition (raw):       2   (class="admonition" occurrences: 2)
```

Confirms the brutalist docs header is mounted, the sidebar populates with Material's nav links, and admonition boxes ship to both pages. The configuration page does include real `class="admonition"` markup (not just CSS class names — verified separately), so admonition styling will exercise on both pages.

### 10. Mobile-narrow CSS sanity — PASS

3 separate `@media (max-width: 720px)` blocks in `brutalist.css` covering nav, hero, buckets, doc cards (and other sections). Meets the spec's `>= 3` floor.

## Bugs found / fixes applied during QA

None. No server-side check surfaced a regression. No code changes committed under Task 15 prior to this summary.

## Items requiring live-browser verification (handoff)

These need a real browser session and cannot be validated server-side. Recommended order:

1. **Visual rendering of the landing page.**
   - Inter ExtraBold loads for headings; JetBrains Mono loads for body/code.
   - Amber accent (`--brut-amber`) is visible on hero CTA, eyebrow, and links.
   - Alternating shaded bands (`.brut-shade`) render with the correct neutral tone.

2. **Mermaid theming** in BOTH locations:
   - Landing section 04 (lifecycle diagram in `home.html`).
   - The `observation-lifecycle.md` docs page diagram.
   - Both must render with the brutalist ink + amber palette, not Material's default blue.
   - Cold-load risk: `mermaid-init.js` uses a 50 ms timeout before re-render — confirm no FOUC of the default theme that persists.

3. **Install tabs interactivity.** Click each of macos / linux / windows on the landing install section — the JSON snippet pane should swap to that OS's config.

4. **Material instant-nav consistency.** With instant nav enabled, navigate landing -> `/configuration/` -> back to landing. Confirm:
   - Custom landing chrome (no Material header) re-applies on return.
   - Mermaid re-themes after each navigation (the script must rerun).
   - Layout does not flash the Material default header on transition.

5. **Mobile (375 px viewport).**
   - Hero typography scales smoothly via `clamp()`.
   - Three buckets stack vertically.
   - Docs sidebar collapses to drawer correctly (Material default behavior preserved).

6. **Cross-browser parity.** Test in current Firefox + Chromium. Pay special attention to the `:has()` selector used in the Task 4 home-template fix for `.md-main` wrappers — Firefox supports it, but verify no fallthrough.

7. **Lighthouse comparison vs `main`.** Run Lighthouse on landing, configuration page, and observation-lifecycle page. Record:
   - Performance: should remain >= baseline minus the expected font-payload hit (~300 KB extra woff2).
   - Accessibility: should remain >= baseline (color contrast on amber text + ink is the main risk).
   - Best Practices / SEO: no regression.

## Final state

- Branch: `website-brutalist`, 24 commits ahead of `main`.
- Build: clean (`mkdocs build --strict` succeeds).
- CSS: ~22.4 KB combined (under 30 KB target).
- Fonts: ~383 KB combined (over 80 KB target — accepted by spec).
- No fix-now bugs identified during server-side QA.
- Next: this commit, then human browser verification of the seven items above, then `superpowers:finishing-a-development-branch`.
