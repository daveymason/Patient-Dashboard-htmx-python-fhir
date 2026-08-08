# Phase 6 — Essential clinical actions

Status: **planned**. The action list that follows Phase 4 (AI) and pulls the
two highest-value write workflows out of [Phase 5](phase-5-management-plan.md)'s
broad catalogue into a concrete order: **diagnosis first, then prescription**.
Diagnosis leads because it is the data-heavy step — a diagnosis is made *in the
context of* previous diagnoses, current medications, allergies and recent
results, all of which the app already fetches — and because the prescription
workflow itself wants a diagnosis to point at (`reasonReference`).

## What the server actually holds (verified live, 2026-08-08)

The user-facing question was: does the server have the data a diagnosis step
needs — especially previous diagnoses? Checked against
`https://hapi.fhir.org/baseR4`:

| Question | Answer |
|---|---|
| Are there previous diagnoses to show? | Yes — 2,951 Conditions; in a 100-condition sample, 17 patients had more than one (max 6 per patient) |
| Which coding systems? | ICD-10-CM (39%), SNOMED CT (34%), ICD-10 (11%), rest local/misc — the picker must handle both major systems |
| Status fields populated? | `clinicalStatus` 68%, `verificationStatus` 42%, `onsetDateTime` 44%, `recordedDate` 21% — every field is optional in practice, render accordingly |
| Encounter linkage? | Weak — 18% of Conditions, 1% of MedicationRequests. Do not build anything that requires an encounter |
| Prescription data? | 1,418 MedicationRequests; `dosageInstruction` 82%; RxNorm is the dominant medication coding; 156 Medication resources; 605 Practitioners |
| Writes? | Full cycle verified: POST → 201 + ETag; PUT with `If-Match` → 200; stale `If-Match` → **409** (not the spec's 412 — handle both); DELETE → 200 |

## The action list

### 1. Diagnosis (Condition write) — first

- [ ] **Diagnosis context panel** — before any form: previous diagnoses
  (the Phase 3 Conditions panel, reused), active medications, allergies,
  latest observations, side by side. The data questions come before the
  data entry.
- [ ] **Duplicate guard** — on code selection, check the patient's existing
  Conditions for the same/related code; warn "already on the problem list
  (active, onset 2020)" instead of silently adding a twin.
- [ ] **Condition form** — code search (ICD-10-CM + SNOMED, served from the
  terminology the server already uses), clinical status, verification status
  (`provisional` vs `confirmed` — real diagnoses start provisional),
  onset date, free-text note.
- [ ] **Write path** — `FhirClient.post()` / `put(if_match=...)` (the Phase 5
  design), treating 409 *and* 412 as version conflicts per the live probe.
- [ ] **Edit/resolve flow** — mark resolved, correct a code, with the
  before/after recorded in `resource_edit_log` (Phase 5 schema).
- [ ] **Audit + role gate** — clinician/admin only; viewers never see the
  form.
- [ ] **AI assist (uses Phase 4)** — "summarise relevant history for this
  presenting complaint" button on the diagnosis screen; strictly advisory.

### 2. Prescription (MedicationRequest write) — second

- [ ] **Prescriber identity decision** — `requester` should reference a
  Practitioner, and app accounts are not FHIR Practitioners. Either map
  users to Practitioner resources (605 exist server-side; create one per
  clinician account) or record requester as a display-only string. Decide
  before building.
- [ ] **Medication picker** — RxNorm-coded search (matches the server's
  dominant coding); free-text fallback for what isn't coded.
- [ ] **Allergy hard-stop** — prescribing screen cross-checks
  AllergyIntolerance before submit; a match blocks with an explicit
  override + reason, and the override lands in the audit log.
- [ ] **Dosage builder** — structured `dosageInstruction` (dose, route,
  frequency, duration) plus the free-text sig line; 82% of server data has
  this shape to mirror.
- [ ] **Link to diagnosis** — `reasonReference` → the Condition from step 1;
  this is why diagnosis ships first.
- [ ] **Status lifecycle** — active → completed/stopped, with stop-reason;
  renewals clone-with-new-dates rather than edit history.
- [ ] **Printable prescription** — the paper artifact practices still need.

### 3. Close behind (same shape, cheaper)

- [ ] **Clinical note on the encounter** (`DocumentReference`) — diagnosis
  and prescription both want a narrative home.
- [ ] **Visit/encounter creation** — server data shows encounter linkage is
  rare, but *our* writes should link what the source data didn't: one
  encounter per sitting ties diagnosis + prescription + note together.
- [ ] **Task/recall follow-up** — "review in 2 weeks" as a `Task`, feeding
  the Phase 5 recall loop.
- [ ] **Drug-interaction check** — needs an external interactions source
  (out of FHIR scope); scope it before promising it.

## Ordering rationale

Diagnosis → prescription → note/encounter is the order a consultation
actually runs, and each step produces the reference the next one consumes.
Building in this order means no step ever writes a dangling reference, and
the AI features from Phase 4 gain grounded context (a coded diagnosis) at
the moment they're most useful — at the point of prescribing.
