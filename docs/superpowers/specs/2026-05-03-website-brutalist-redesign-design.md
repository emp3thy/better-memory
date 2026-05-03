# Website redesign — terminal brutalist

**Date:** 2026-05-03
**Branch:** `website-brutalist`
**URL:** https://emp3thy.github.io/better-memory/

## Goal

Replace the generic MkDocs Material default look with a distinctive, opinionated visual identity for the project website. Aesthetic direction: *terminal brutalist* — monospace everywhere, paper + ink, amber accent, hard edges, lowercase, no decoration that isn't functional.

The current site is one of millions of MkDocs Material installs with the teal palette and Inter font. This design replaces the look without replacing the engine.

## Scope

**In scope.**
- Custom landing page (`website/index.md`) with full creative control via `md_in_html` and a custom theme template.
- Visual restyle of the five existing docs pages (Configuration, Architecture, MCP tools, Observation lifecycle, Contributing) so they share the landing page's identity.
- Custom Material theme overrides in `website/overrides/`.
- Custom CSS, fonts (self-hosted), and a small amount of vanilla JS if needed (e.g., the install-block tabs).

**Out of scope.**
- Replacing MkDocs as the doc engine.
- Adding new doc pages or rewriting prose. All copy reuses what's already in `website/*.md` or the README.
- New build dependencies beyond what `pyproject.toml [docs]` already pulls.
- Search functionality changes (just visual).
- Interactive features like dark-mode toggle, animations, scroll-driven effects.

## Technical approach

Keep the MkDocs Material engine and the existing `.github/workflows/docs.yml` deploy unchanged. Add visual identity entirely through documented Material extension points:

| Extension point | Purpose |
|---|---|
| `theme.custom_dir: overrides` | Two custom templates: `home.html` (landing page, no chrome), `main.html` (extends Material, restyles docs pages) |
| `extra_css` | One stylesheet at `website/assets/css/brutalist.css` |
| `extra_javascript` | One script at `website/assets/js/install-tabs.js` (only for the install-block OS tabs) |
| Page front-matter `template: home.html` | Opts the landing page out of Material's default chrome |
| `md_in_html` (already enabled) | Lets `index.md` contain raw HTML for hero, sections, etc. |
| Self-hosted `.woff2` fonts in `website/assets/fonts/` | No CDN, no flash-of-fallback |

No JS framework. No build-step beyond `mkdocs build`.

### File additions

```
website/
├── index.md                         # rewritten — hero + sections via md_in_html
├── overrides/
│   ├── home.html                    # full-bleed landing template
│   └── main.html                    # extends Material's main.html
├── assets/
│   ├── css/
│   │   ├── brutalist.css            # variables, typography, components, page-level styles
│   │   └── pygments-brutalist.css   # syntax highlighting palette
│   ├── js/
│   │   └── install-tabs.js          # OS tabs in the install section
│   └── fonts/
│       ├── Inter-ExtraBold.woff2
│       ├── JetBrainsMono-Regular.woff2
│       ├── JetBrainsMono-Medium.woff2
│       └── JetBrainsMono-Bold.woff2
```

### `mkdocs.yml` changes

Additions only (no removals beyond palette tweaks):

```yaml
theme:
  name: material
  custom_dir: overrides            # NEW
  font: false                      # CHANGED — disable Material's Google Fonts loader
  palette:                         # CHANGED — single light scheme, no toggle
    scheme: default
    primary: custom
    accent: custom
  features:
    - navigation.instant
    - navigation.tracking
    - navigation.sections
    - navigation.top
    - toc.follow
    - search.suggest
    - search.highlight
    - content.code.copy
    - content.action.edit

extra_css:
  - assets/css/brutalist.css
  - assets/css/pygments-brutalist.css

extra_javascript:
  - assets/js/install-tabs.js
```

## Visual system

### Typography

| Role | Family | Weight | License |
|---|---|---|---|
| Display (hero, h1 of each page, large section headlines) | **Inter** | 800 (Extra Bold) | OFL, free |
| Body, code, eyebrows, nav, tables, small labels | **JetBrains Mono** | 400, 500, 700 | OFL, free |

The display sans is the only non-mono element. Eyebrows (`// LOCAL-FIRST · MCP · CLAUDE-CODE`), navigation, code blocks, body text, table headers, footer — all stay JetBrains Mono. The contrast between Inter's tight modern sans and the surrounding mono-everywhere is the typographic system.

Both self-hosted as `.woff2` in `website/assets/fonts/` via `@font-face`. No external font loaders. `font-display: swap`. Material's own font loader is disabled with `theme.font: false`.

**Risk noted:** all-monospace body text on long technical docs is the most opinionated call. Reference docs tolerate this; if real-world reading proves uncomfortable, a single CSS variable swaps body to Inter Tight or system sans without touching anything else.

### Color tokens (CSS variables in `brutalist.css`)

```css
:root {
  --ink: #0d0d0d;
  --paper: #f5f3ee;
  --paper-shade: #ebe8e1;
  --amber: #ff8c2b;
  --amber-ink: #0d0d0d;       /* text on amber bg */
  --muted: #6b6862;
  --rule: #d8d4cb;
}
```

Amber appears in roughly 5% of pixels — links, the highlighted hero word, `do` bucket marker, active nav indicators, admonition warning bars. Everything else is paper + ink.

### Spacing & rhythm

8px base grid. Max content width: `880px` body, `1100px` hero. Vertical rhythm: 96px between landing-page sections, 56px between docs-page sections.

### Component conventions

- **Links:** `border-bottom: 2px solid var(--amber)`, no underline. Hover: amber background fill, no transition longer than `120ms`.
- **Buttons:** filled (ink bg, paper text) or outlined (paper bg, ink border). Always rectangular. No shadows.
- **Code blocks:** `--paper-shade` background, no border, copy button uses ink text on amber hover.
- **Inline code:** `--paper-shade` pill, monospace already (it's the body font).
- **Tables:** hairline `--rule` between rows. Headers in caps with `letter-spacing: 0.12em`.
- **Admonitions:** ink-bordered, amber left bar (`!!! warning`) or ink left bar (`!!! note`), uppercase mono label.
- **Mermaid:** themed via mermaid `themeVariables` to monochrome ink + amber for active states.
- **Borders:** `border-radius: 0` site-wide.

**Anti-patterns explicitly avoided:** no shadows, no gradients, no rounded corners, no emoji, no hover transforms, no scroll-triggered animations, no fade-ins.

## Landing page sections

Eight sections in alternating-bands treatment (paper · paper-shade · paper …). All copy reuses existing README/website prose — no new writing required for v1.

| # | Section | Background | Content |
|---|---|---|---|
| 01 | **Hero** | paper | Top nav (`[ better-memory ]` · `install · docs · github`); eyebrow `// LOCAL-FIRST · MCP · CLAUDE-CODE`; display `memory that <amber>sticks</amber> between sessions.`; one-sentence lede; two CTAs `[ install → ]` and `[ read the spec → ]`. |
| 02 | **Specimen** | paper-shade | Headline `what an observation looks like`. Real syntax-highlighted `memory.observe(...)` call, an arrow, and the resulting `do` bucket from `memory.retrieve` next session. |
| 03 | **Three buckets** | paper | Headline `retrieval, sorted by outcome`. 3-column layout: `do` / `dont` / `neutral`, each with role + example observation. `do` column gets the amber accent bar. Subhead: `reinforcement-weighted — memory.record_use promotes signal, demotes noise.` |
| 04 | **Lifecycle** | paper-shade | Headline `observations have a lifecycle`. Restyled mermaid state diagram from `observation-lifecycle.md` with brutalist theme. One-sentence summary. |
| 05 | **Install** | paper | Headline `three commands, one paste`. Two stacked code blocks: setup script + JSON snippet. macOS / Linux / Windows tabs above the JSON block. Link to full Configuration page. |
| 06 | **MCP tools** | paper-shade | Headline `the surface area`. Compact table of 6 MCP tools, each tool name links to `mcp-tools.md`. |
| 07 | **Go deeper** | paper | Headline `documentation`. 4 link-cards: Architecture · Configuration · Observation lifecycle · Contributing. |
| 08 | **Footer** | paper, ink top rule | Left: `better-memory · v{version} · MIT`. Right: `github` link, "built with" credits (uv, sqlite, ollama). Version is hardcoded for v1 (no build-time injection). |

### Hero copy (locked)

- **Eyebrow:** `// LOCAL-FIRST · MCP · CLAUDE-CODE`
- **Display:** `memory that sticks between sessions.` (the word "sticks" is amber-highlighted)
- **Lede:** `A semantic + episodic memory manager for Claude Code. SQLite, local Ollama, no cloud.`
- **CTAs:** `[ install → ]` (filled, scrolls to install section) and `[ read the spec → ]` (outlined, links to architecture page).

## Docs page theming

The five non-landing pages keep MkDocs Material's full structure (top tab nav, left sidebar, right TOC, search) but inherit the visual identity via CSS overrides. No template replacement beyond `main.html` extending the Material base.

| Element | Treatment |
|---|---|
| Header | Paper background, ink text, mono `[ better-memory ]` wordmark left, `install · docs · github` links right. No Material logo. |
| Search box | Paper-shade pill, mono `// search` placeholder. Material's search functionality untouched. |
| Sidebar (left nav) | JetBrains Mono, hairline `--rule` between top-level sections, active page gets a 2px amber left bar. |
| TOC (right) | Same mono treatment, active heading gets amber left bar. |
| Page title (h1) | Inter ExtraBold, large, lowercase, ink. |
| Section headings (h2/h3) | Ink, lowercase, slight negative letter-spacing. h2 gets thin top rule. |
| Body | JetBrains Mono ~15px, line-height 1.65. |
| Code blocks | `--paper-shade` background, no border. Pygments retuned to amber/ink/muted. |
| Admonitions | Ink-bordered, amber left bar for `warning`, ink left bar for `note`, uppercase mono label. |
| Tables | Hairline rules between rows, header caps with letter-spacing. |
| Mermaid | Monochrome ink + amber (configured via mermaid `themeVariables`). |
| Footer | Identical to landing footer. |

**Untouched:** Material's responsive collapse logic (mobile sidebar drawer), search functionality, edit links, instant-load nav, all existing `features:`.

## Build & deploy

No changes to `.github/workflows/docs.yml`. Same `uv sync --group docs && uv run mkdocs build --strict` flow. The `--strict` flag remains — broken links or missing files will fail CI as before.

Deploy target unchanged: GitHub Pages at https://emp3thy.github.io/better-memory/.

## Component isolation

The design is split into well-bounded units, each separately understandable and modifiable:

- **Theme tokens** (`brutalist.css :root`) — colors, fonts, spacing variables. Change the look by editing variables, not selectors.
- **Component blocks** (`brutalist.css` sections) — one block per component (links, buttons, code, tables, admonitions, mermaid). Each block is self-contained.
- **Page-level overrides** — landing-page-specific styles scoped under `body.landing` (set via the `home.html` template adding the class). Docs-page styles scoped to `body.md-page` or similar.
- **Templates** — `home.html` is a full template; `main.html` only adds blocks Material exposes. No template fork.
- **Pygments theme** — separate file (`pygments-brutalist.css`) so syntax-highlighting changes don't pollute the main stylesheet.

Each unit answers cleanly: what it does, how it's used, what it depends on.

## Testing strategy

Visual code is hard to assert in unit tests; rely on:

1. **Build assertions.** `mkdocs build --strict` must pass — same as today's CI gate.
2. **Local preview.** `uv run mkdocs serve` started during implementation, verified manually in a real browser at each step.
3. **Cross-browser smoke.** Verify in Chromium and Firefox before merge. Mobile sizing checked via DevTools responsive mode at 375px and 768px breakpoints.
4. **Lighthouse smoke.** No regression in performance, accessibility, or SEO scores vs. current site (baseline captured before changes).
5. **Link check.** All in-page anchor links and cross-page `mkdocs` links resolve (`mkdocs build --strict` covers cross-page; anchors are spot-checked manually).

No automated visual-regression tooling — overkill for a 6-page site that one person owns.

## Open questions

None blocking. One judgment call flagged above (all-mono body vs. softening with sans on docs pages) is a reversible CSS-variable swap — settle by inspection during implementation rather than up front.

## Risks

| Risk | Mitigation |
|---|---|
| Inter display reads too generic at full size | One CSS variable swap to Geist Sans (more character) or back to a mono (fully terminal). No structural change. |
| All-mono body text fatigues readers on long docs pages | One CSS variable swap to a sans body font. Component styles unchanged. |
| Material updates break our overrides | Pin Material version in `pyproject.toml [docs]`. Already pinned — no change. |
| Self-hosted fonts add page weight | Subset `.woff2` files to Latin only. Regular + Bold only for JetBrains Mono. Total font budget < 80KB. |
| Mermaid restyle is incomplete (Material's Mermaid integration limits theming) | Acceptable: settle for "monochrome with amber active states." Bigger overhauls (replacing Mermaid entirely) deferred. |

## Success criteria

- Landing page renders with the agreed hero, eight sections, alternating bands, amber accents.
- All five docs pages restyled cohesively — no Material teal or Inter font visible anywhere.
- `mkdocs build --strict` passes.
- Site loads without flash-of-fallback fonts in Chromium and Firefox.
- Mobile (375px) layout is usable — no horizontal scroll, hero readable, nav accessible.
- Total CSS payload < 30KB; total font payload < 80KB.

## Out of scope (deferred)

- Dark mode toggle.
- Animations, scroll-triggered effects.
- New doc pages or copy rewrites.
- Build-time version injection in the footer.
- Search UI changes beyond the placeholder text.
- Visual regression testing infrastructure.
