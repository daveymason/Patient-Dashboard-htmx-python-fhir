# Phase 5 — Management, clinical breadth, and admin

> **Ordering note:** [phase-6-clinical-actions.md](phase-6-clinical-actions.md)
> pulls two workflows out of this catalogue and sequences them concretely —
> diagnosis (Condition write, this doc's 5a) first, then prescriptions
> (MedicationRequest write, which this doc's table omitted). This document
> remains the deep design for the write infrastructure and the wider catalogue.

Status: **planned, not started**. Everything in this document assumes
[Phase 4](phase-4-ai-plan.md) has landed, though the two phases are largely
independent and could be reordered.

## Scope

Two things this app has never done, plus everything they imply:

1. **Write-back.** Every route through Phase 4 is read-only against FHIR.
   Phase 5 is where the app stops being a viewer and starts being a system
   clinicians chart into.
2. **Clinical and administrative breadth.** More FHIR resources (appointments,
   notes, immunizations, imaging, billing) and more practice-management tooling
   beyond the user/role admin screens Phase 2 already built.

This is the largest phase by a wide margin — treat the sequencing table at the
end as the actual plan, not the full feature list as something to build in one
pass.

## What Phase 5 builds on

| Existing piece | Where | Why it matters here |
|---|---|---|
| Role model + admin screens | `auth.py`, `db.py`, `templates/admin_*.html` | Write permissions extend the same role check, not a new system |
| Append-only audit log | `db.py` | Writes get audited with the *old and new value*, not just "who viewed what" |
| `FhirClient` | `fhir_client.py` | Needs new methods: `post()`, `put()` with `If-Match` support (currently GET-only) |
| Conditions panel (read-only) | `app.py:get_conditions`, `templates/conditions.html` | First write target — becomes read/write in 5a |
| Vitals/demographics charts | `charts.py` | The charting infra generalises directly to appointment calendars, care-plan timelines |

## Write-back architecture

This is the one piece of genuinely new infrastructure; every subsequent
feature in this phase reuses it.

### `FhirClient` additions

```python
def post(self, resource_type, body) -> FhirResult
def put(self, resource_type, resource_id, body, if_match=None) -> FhirResult
```

- `put()` requires the caller to pass the resource's current `ETag` (FHIR
  `versionId`) as `if_match`. A version-conflict response from the server means
  someone else edited the record since it was loaded — the UI must surface
  this as "this record changed since you opened it," not a generic error.
  Treat both **409 and 412** as version conflicts: the spec says 412, but the
  live HAPI server was probed (2026-08-08) and returns 409 for a stale
  `If-Match`.
  This is optimistic concurrency, not a lock: nothing blocks a second editor
  from *opening* the record, only from silently overwriting a conflicting edit.
- Both methods go through the same timeout/retry/pseudonymised-logging path
  `get()` already has. No parallel HTTP-calling code path.

### Write route pattern

Every write route follows the same shape (illustrated with Conditions, the
first target):

```
GET  /fhir/Conditions/<patient_id>/new        -> render a form (conditions_form.html)
POST /fhir/Conditions/<patient_id>            -> validate, POST to FHIR, audit, re-render the panel
GET  /fhir/Conditions/<patient_id>/<id>/edit  -> render the form pre-filled, carrying the current ETag
PUT  /fhir/Conditions/<patient_id>/<id>       -> validate, PUT with If-Match, audit, re-render
```

- Validation happens server-side against the resource's required fields before
  the FHIR call — the FHIR server's own validation is the backstop, not the
  first line.
- Permission: `@auth.require_role(db.ROLE_CLINICIAN, db.ROLE_ADMIN)` on every
  write route. `viewer` accounts never see a write control rendered, not just
  blocked at the route — a rendered-but-403'd button is a worse UX than one
  that doesn't appear.
- Audit: writes log both the before and after state (or at minimum a diff of
  changed fields), not just "condition updated." This is the piece read-only
  Phase 1–4 auditing didn't need.

### Concrete effort: this piece alone

Building `post()`/`put()`, the ETag-conflict UI pattern, one write-enabled form
(Conditions), and its audit trail is roughly a week — most of that is the
conflict-handling UI and tests, not the HTTP calls themselves.

## Clinical resources, in priority order

Ranked the same way as the original feature review: clinical value against
effort, using resources not yet touched by Phases 1–4.

| Feature | FHIR resource(s) | Effort | Why this position |
|---|---|---|---|
| Condition write-back | `Condition` | Low (once write infra exists) | Turns the Phase 3 read-only problem list into a real one; the reference implementation for every write route after it |
| Clinical notes | `DocumentReference`, `Composition` | Medium | First free-text write path; makes the app a system of record rather than a structured-data-only view |
| Immunizations | `Immunization` | Low | Same read-only rendering pattern as Medications (Phase 1) — near-free |
| Procedures | `Procedure` | Low | Same pattern again; surgical/dental history |
| Appointments & scheduling | `Appointment`, `Slot`, `Schedule` | High | Pulls in three resources and a calendar UI; biggest scope jump in this table |
| Recall & reminders | `Task` + `Appointment` | Medium | Depends on Appointments existing first; the core revenue loop for a dental practice specifically |
| Care plans | `CarePlan`, `Goal` | Medium | Longitudinal management rather than point-in-time snapshots |
| Imaging & reports | `DiagnosticReport`, `ImagingStudy` | Medium | Radiographs are central to dentistry; likely needs a viewer widget, not just a list |
| Odontogram / dental chart | `Procedure` + `bodySite` (SNOMED tooth codes) | High | FHIR has no dedicated resource — this is modeled on top of existing ones, so it's UI effort more than data-model effort |
| Billing & coverage | `Claim`, `Coverage` | High | Where private practices feel the most real-world pain, and the least like the rest of this app |
| Unified patient timeline | All of the above, merged by date | Low | Mostly a presentation layer over data every panel already fetches |
| Printable patient summary | International Patient Summary Bundle | Low | A published profile — assembly work, not new data access |
| Saved cohorts | Stored search parameters | Low | "Diabetics overdue a review" as a one-click list, once Conditions + Appointments both exist |

## Practice administration

Beyond the user/role screens Phase 2 built (`admin_users.html`, `admin_audit.html`):

- **Practice-level settings** — a `practice` concept doesn't exist yet; today
  every user shares one global FHIR server and AI configuration. Multi-practice
  support (if ever needed) is a genuine architecture decision, not a small
  addition — see [Open decisions](#open-decisions).
- **Reporting/export** — CSV export of a saved cohort or the audit log, for
  compliance reporting outside the app.
- **Configurable roles** — the current three roles (`admin`/`clinician`/`viewer`)
  are hardcoded in `db.py`. A practice that wants a `front_desk` role that can
  see Appointments but not clinical notes needs either a fourth hardcoded role
  or a real permissions table — the latter is more work and should only be
  built if a fourth role is actually needed, not speculatively.

## Data model additions

New tables, added to `db.py`'s `SCHEMA` following the existing pattern:

```sql
-- Generalises across every write-enabled resource type, rather than one
-- table per resource.
CREATE TABLE resource_edit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at   TEXT NOT NULL,
    actor         TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id   TEXT NOT NULL,       -- pseudonymised, like audit_log
    action        TEXT NOT NULL,       -- 'create' | 'update'
    before_value  TEXT,                -- JSON, null for create
    after_value   TEXT NOT NULL        -- JSON
);
-- Same append-only triggers as audit_log.
```

Appointments, care plans, and billing likely need their own lightweight local
tables too (e.g. cached Slot availability for the calendar UI) — deferred
until those sub-phases are actually scoped, since the shape depends on UI
decisions not yet made.

## Open decisions

1. **Single-practice or multi-tenant?** Today's model (one FHIR server, one
   user table, shared globally) is single-practice. Appointments and billing
   both get meaningfully more complex if multiple practices ever need to share
   one deployment. Decide this *before* building Appointments, not after.
2. **Which clinical resource first** — Conditions write-back is the obvious
   starting point (infra proof), but after that: Notes (system-of-record value)
   or Appointments (front-desk daily-use value) is a real product tradeoff,
   not an engineering one.
3. **Dental-specific investment** — the odontogram is high-effort and only
   worth building if this is actually targeting dental practices specifically,
   versus general practice/clinic use.
4. **Configurable roles** — build the fourth hardcoded role, or the permissions
   table, only when a concrete need appears; don't build it speculatively.

## Suggested sequencing

| Sub-phase | Delivers | Depends on |
|---|---|---|
| 5a | `FhirClient.post()`/`put()`, ETag-conflict UI pattern, Condition write-back, `resource_edit_log` | Nothing outside existing Phases 1–3 |
| 5b | Immunizations, Procedures (read-only) | Nothing — can run in parallel with 5a |
| 5c | Clinical notes (DocumentReference/Composition) | 5a |
| 5d | Unified patient timeline | 5b, 5c (more data sources to merge) |
| 5e | Appointments & scheduling | 5a; **resolve the multi-tenant question first** |
| 5f | Recall & reminders | 5e |
| 5g | Care plans, imaging, billing, odontogram | 5a; each independently schedulable, in whatever order matches actual user demand |
| 5h | Saved cohorts, printable IPS summary, practice reporting | 5a onward — low effort, can slot in anywhere once their data dependencies exist |

## Testing

- Every write route gets an ETag-conflict test: two sequential edits from
  stale data, second one must 412 and surface the conflict — this is the one
  behavior in this phase most likely to be silently broken by a future change.
- `resource_edit_log` entries are asserted the same way `TestAuditingOfAccess`
  in `tests/test_infrastructure.py` already asserts `audit_log` entries:
  pseudonymised identifier, correct action, present in both create and update
  paths.
- Role-gating tests mirror `TestAuthorization` in `tests/test_routes.py` —
  every new write route needs a "viewer cannot" test alongside its "clinician
  can" test.
