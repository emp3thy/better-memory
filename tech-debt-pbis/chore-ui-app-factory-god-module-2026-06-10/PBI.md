---
id: chore-ui-app-factory-god-module-2026-06-10
type: chore
status: inbox
severity: normal
attempts: 0
depends_on: []
target_repo: https://github.com/emp3thy/better-memory
source_design: C:\Users\gethi\source\better-memory\.tech-debt\design.md
category: god-modules
debt_type: design
effort: M
---

# ui/app.py factory mixes 33 routes, CSRF, filters, watchdog, and DB setup

### Reasoning

Highest-churn file in the repo (77 commits) â€” every UI change pays this debt, giving it top interest despite moderate severity. Effort M makes it deliverable soon, unlike the L-sized reflection.py split it edged out: Flask blueprints are a well-trodden, low-risk refactor.

### Evidence

- `better_memory/ui/app.py:22` - create_app() registers 33 routes (episodes, reflections, observations, semantic CRUD, diagnostics) inline, plus CSRF guards, Jinja filters, an inactivity watchdog thread, and direct DB connection management. 675 LOC, 1355 complexity, 77 churn; all routes share state via app.extensions.

### Suggested fix

Extract routes into blueprints (episodes_bp, reflections_bp, observations_bp, semantic_bp, diagnostics_bp). Move CSRF guards and Jinja filters to a config module, the inactivity watchdog into a Watchdog class, and leave create_app() as a ~100 LOC factory.
