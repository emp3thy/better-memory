# Promote a project-scoped reflection to general scope

Date: 2026-05-09
Status: design — pending implementation

## Problem

The Reflections tab (`/reflections`) lets the user `Confirm`, `Retire`, and
`Edit` a reflection from the drawer. The reflection schema already has a
`scope` column (`'project' | 'general'`, default `'project'`) — but no UI
affordance and no service method to flip it.

Synthesis only emits a `general`-scope reflection when **every** source
observation is itself general (`_derive_new_reflection_scope`). When a
useful project-scoped lesson turns out to apply cross-project, the user
currently has no way to promote it short of writing SQL.

The existing pattern on the semantic-memories page is a bidirectional
toggle (`Make general` / `Make project`). For reflections we want the
weaker, one-directional version: **promote project → general**, no
demote.

## Decisions (settled with user 2026-05-09)

- **Direction:** promote-only. No demote (general → project) action.
  Treat promotion as a deliberate curation step.
- **UI placement:** drawer button only — alongside `Confirm`, `Retire`,
  `Edit`. No row badge, no row toggle. The reflection row's whole-row
  `hx-get` makes embedded buttons awkward, and the user already opens
  the drawer to make any other lifecycle change.
- **Status guard:** active-only (`pending_review`, `confirmed`). The
  button is hidden on `retired` and `superseded`, mirroring the gate
  on the existing actions block. The service method enforces the same
  guard so direct API calls can't bypass the UI.
- **Already-general:** the button is hidden when `scope == 'general'`.
  Service is idempotent (no-op, no `updated_at` bump) on already-general
  for safety against direct calls.
- **Provenance:** only `scope` flips. `reflections.project` keeps the
  originating project as historical context. This matches
  `_derive_new_reflection_scope` and `SemanticMemoryService.set_scope`.

## Out of scope

- Reverse direction (`general` → `project`) and any general-side toggle UX.
- Row-level scope badge or row-level toggle button.
- Bulk promote (multi-select on the panel).
- A dedicated audit-log entry. The `updated_at` bump and the `scope`
  field itself are the only persisted trace.
- MCP-side `reflection.promote_to_general` tool. UI-only for now.

## Architecture

### Data layer

No schema migration. `reflections.scope` already satisfies the
`CHECK(scope IN ('project','general'))` constraint introduced by
migration `0007_reflection_scope.sql`.

### Service layer — `ReflectionService.promote_to_general`

New method on the existing UI-facing `ReflectionService` class
(`better_memory/services/reflection.py`), sibling of `confirm`, `retire`,
`update_text`:

```python
def promote_to_general(self, *, reflection_id: str) -> None:
    """project → general; idempotent on already-general; raise on retired/superseded."""
```

Behaviour:

1. `SELECT scope, status FROM reflections WHERE id = ?`. Row is `None`
   → `ValueError("Reflection not found: <id>")`.
2. `status in ('retired', 'superseded')` →
   `ValueError("Cannot promote reflection in status <status>")`. Same
   shape as existing `Cannot confirm` / `Cannot edit` messages.
3. `scope == 'general'` → return without writing. No `updated_at` bump,
   matching the no-op semantics of `confirm` on already-confirmed and
   `retire` on already-retired.
4. Otherwise: `UPDATE reflections SET scope='general', updated_at=? WHERE id=?`
   with `self._clock().isoformat()`, then `self._conn.commit()`.

The method is small enough that no helper extraction is justified. The
status- and scope-guards each get their own branch in the body.

### Route — `POST /reflections/<id>/promote`

New route in `better_memory/ui/app.py`, modelled on `reflection_confirm`:

```python
@app.post("/reflections/<id>/promote")
def reflection_promote(id: str) -> tuple[str, int, dict[str, str]]:
    conn = app.extensions["db_connection"]
    if queries.reflection_detail(conn, reflection_id=id) is None:
        abort(404)
    try:
        app.extensions["reflection_service"].promote_to_general(reflection_id=id)
    except ValueError as exc:
        return (
            f'<div class="card card-error"><p>{escape(str(exc))}</p></div>',
            409, {},
        )
    detail = queries.reflection_detail(conn, reflection_id=id)
    rendered = render_template("fragments/reflection_drawer.html", detail=detail)
    return rendered, 200, {"HX-Trigger": "reflection-changed"}
```

Notes:

- Status code shape matches existing reflection routes:
  - 404 on missing id (existence check before service call).
  - 409 on lifecycle violation (retired/superseded), with the standard
    `card card-error` body fragment so htmx can render it.
  - 200 with the re-rendered drawer fragment on success.
- The `HX-Trigger: reflection-changed` header reuses the existing
  panel-refresh hook the other reflection actions already fire.
- No origin check is needed beyond the global `_origin_check` already
  registered for non-GET requests.

### Read model — `queries.reflection_detail` / `ReflectionFull`

The drawer needs to render the current `scope` and the template needs
to gate the button on it. Currently `ReflectionFull`
(`better_memory/ui/queries.py:298-314`) does **not** include `scope`,
and the `reflection_detail` SELECT doesn't fetch it.

Changes:

- Add `scope: str` to the `ReflectionFull` dataclass.
- Add `scope` to the SELECT column list in `reflection_detail`.
- Pass `scope=r_row["scope"]` to the constructor.

`reflection_list_for_ui` does not need changes (no row badge in this
spec).

### Templates

`better_memory/ui/templates/fragments/reflection_drawer.html`:

1. Add a meta entry inside the `<dl class="drawer-meta">` block,
   placed after `Status` (so lifecycle metadata clusters together):

   ```html
   <dt>Scope</dt><dd>{{ detail.reflection.scope }}</dd>
   ```

2. Inside the existing `{% if detail.reflection.status in ('pending_review','confirmed') %}`
   actions block, after the `Edit` button, add the promote button —
   gated by `scope`:

   ```html
   {% if detail.reflection.scope == 'project' %}
     <button type="button"
             class="action-promote"
             hx-post="{{ url_for('reflection_promote', id=detail.reflection.id) }}"
             hx-target="#reflection-drawer"
             hx-swap="innerHTML">
       Promote to general
     </button>
   {% endif %}
   ```

   The status guard is provided by the surrounding `{% if %}`; the
   scope guard hides the button when already general. Together they
   implement: "show only on active project-scoped reflections".

No CSS additions required — `.action-promote` reuses the default
button styling already applied to sibling action buttons.

### URL helper / shutdown / origin / watchdog

No changes. The new route inherits all of these from existing app
plumbing.

## Tests

Following the existing layout (one test module per service / route
file).

### Service tests — `tests/services/test_reflection_writes.py`

This is where the existing `confirm` / `retire` / `update_text` tests
live; add a new `class TestPromoteToGeneral` alongside them.

- `test_promote_to_general_flips_scope_and_bumps_updated_at`:
  insert a `pending_review` row with `scope='project'`, advance the
  clock, call `promote_to_general`, assert `scope='general'` and
  `updated_at` matches the new clock reading.
- `test_promote_to_general_works_on_confirmed`:
  insert with `status='confirmed'`, promote, assert success.
- `test_promote_to_general_idempotent_on_already_general`:
  insert with `scope='general'`, capture `updated_at`, advance clock,
  call `promote_to_general`, assert no exception, `scope` unchanged,
  `updated_at` **not** bumped.
- `test_promote_to_general_rejects_retired`:
  insert with `status='retired'`, expect `ValueError` whose message
  contains `'retired'`.
- `test_promote_to_general_rejects_superseded`:
  same shape with `status='superseded'`.
- `test_promote_to_general_rejects_missing`:
  call with a fabricated id, expect `ValueError` whose message
  contains `'not found'`.

### Route tests — `tests/ui/test_reflections.py`

This holds the existing route + drawer-render tests for the reflections
tab; add the promote-route assertions here.

- `test_promote_route_200_renders_drawer_with_general_scope`:
  POST `/reflections/<id>/promote` for a project-scoped pending_review
  row. Assert 200, response body contains `scope='general'` (or the
  drawer's scope `<dd>` text), and the response headers include
  `HX-Trigger: reflection-changed`.
- `test_promote_route_404_on_missing`:
  POST with a fabricated id, assert 404.
- `test_promote_route_409_on_retired`:
  insert `status='retired'`, POST, assert 409 and the response body
  contains the standard `card card-error` shape.
- `test_promote_button_hidden_in_drawer_when_already_general`:
  GET `/reflections/<id>/drawer` for a `scope='general'` row, assert
  the response body does **not** contain `action-promote` /
  `Promote to general`.
- `test_promote_button_hidden_in_drawer_when_retired`:
  same shape for `status='retired'`.

### Read-model test — `tests/ui/test_queries_reflections.py`

Add an assertion to the existing `reflection_detail` test that the
returned `ReflectionFull.scope` field is populated from the row.

## File-by-file impact summary

| File | Change |
| --- | --- |
| `better_memory/services/reflection.py` | Add `ReflectionService.promote_to_general` |
| `better_memory/ui/app.py` | Add `POST /reflections/<id>/promote` route |
| `better_memory/ui/queries.py` | Add `scope` to `ReflectionFull` + `reflection_detail` SELECT and constructor |
| `better_memory/ui/templates/fragments/reflection_drawer.html` | Add `Scope` meta row + gated `Promote to general` button |
| `tests/services/test_reflection_writes.py` | Add 6 service tests (`TestPromoteToGeneral`) |
| `tests/ui/test_reflections.py` | Add 5 route/template tests |
| `tests/ui/test_queries_reflections.py` | Add `scope` assertion to existing `reflection_detail` test |

No migrations, no MCP server changes, no Skill changes, no CSS.

## Risks and mitigations

- **Flag drift between UI and service guard.** The drawer template
  hides the button on retired/superseded but the service also raises
  on those statuses. Both must agree. Mitigation: the route test
  `test_promote_route_409_on_retired` exercises the service guard
  directly, and `test_promote_button_hidden_in_drawer_when_retired`
  exercises the template guard. If either drifts, one of the tests
  fails.
- **`updated_at` semantics.** Two existing siblings (`confirm`,
  `retire`) bump `updated_at` only when the row actually changes.
  `promote_to_general` follows the same rule — explicitly tested by
  `test_promote_to_general_idempotent_on_already_general`. Diverging
  here would surprise audit-trail consumers.
- **Cross-project visibility for promoted reflections is immediate.**
  Once `scope='general'`, the next `memory.retrieve` call from any
  project sees the lesson (subject to the existing tech / phase /
  polarity filters). There is no review queue for the promotion
  itself. This is intentional — promote is a deliberate user action,
  not a synthesis-emitted suggestion.
