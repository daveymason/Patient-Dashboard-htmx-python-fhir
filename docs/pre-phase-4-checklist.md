# Pre-Phase-4 checklist

Status of everything that should be true before AI work (Phase 4) starts.
Produced from a full code review of the Phase 3 + password-auth working tree
(2026-08-08); every item marked fixed has a regression test and was verified
against the live HAPI server or in a real browser.

## Fixed in this pass

### User-reported

- [x] **Sidebar dead on admin/settings pages.** The hx-get links target
  `#content`, which only exists on the SPA shell; on `base.html` pages htmx
  cancelled the click and found no target. Links now carry real
  `/?view=...` hrefs everywhere and htmx enhancement only on the SPA shell;
  the index route maps `view` to the right initial fragment.
- [x] **Load More pagination never worked.** Three stacked bugs: the SSRF
  check rejected every real HAPI cursor (https upgrade + no slash after
  `baseR4`); the paged response rendered a full `<table>` into a `<tr>`
  target; and the default server URL was http, which HAPI 301s. Cursor
  validation is now structural, paged responses are rows-only
  (`patient_rows.html`), default URL is https. Verified three pages deep
  live and by clicking in a browser (21 → 41 rows, one table).

### Found by the code review

- [x] **Upgrading a wallet-era database crashed boot/login** — old `users`
  table survived `CREATE TABLE IF NOT EXISTS`; now detected and renamed to
  `users_wallet_era_<ts>`, fresh schema created. (Confirmed by reproduction.)
- [x] **Conditions panel 500 on real data** — tz-aware `onsetDateTime` mixed
  with naive `recordedDate` fallback made the sort raise `TypeError`; onset
  sort values now go through `sort_key()`. (Confirmed by reproduction.)
- [x] **Search masked upstream failures as "No patients found"** — a failed
  name query was hidden whenever another candidate succeeded empty; any
  failed candidate with no match now renders an error. (Confirmed by
  reproduction.)
- [x] **Admin takeover via pre-registration** — seeding promoted an existing
  account without resetting its password; promotion now also resets the
  credential to the configured `INITIAL_ADMIN_PASSWORD`.
- [x] **Blood pressure double-counted** when a server stores both the panel
  and standalone observations — standalone pairs already covered by a panel
  (same encounter or timestamp) are skipped.
- [x] **Fabricated BP readings** — date-only fallback pairing could stitch a
  morning systolic to an evening diastolic; pairing now requires a shared
  encounter or an exact timestamp, and drops what it cannot pair safely.
- [x] **Login page could 429 itself** — GET /login shared the POST rate
  limit, so logged-out browsing burned the auth budget; the limit now
  applies to POST only (same for /register).
- [x] **Register broke refresh/Back** — POST rendered the login page in
  place; now redirects (post/redirect/get).
- [x] **Typing in search dropped the active sort** — the sort now rides along
  via a hidden input the search box includes.
- [x] **Garbled log paths on paged requests** — request logging now uses the
  URL path (also keeps query params out of logs).

Suite: **163 tests passing**, pyflakes clean.

## Still open before Phase 4 starts

Decisions and setup, not bugs — Phase 4's plan depends on these
(see [phase-4-ai-plan.md](phase-4-ai-plan.md)):

- [ ] **Merge Phase 3.** Everything above is uncommitted on
  `feat/phase3-clinical-usability`. Commit, push, PR, merge; CI green.
- [ ] **Close the stale bot PR** (`feat/ai-patient-summary`) — superseded;
  merging it would revert the entire hardening work.
- [ ] **Decide AI key scope** — per-user or per-practice. Blocks the
  `secrets` table schema (Phase 4 open decision #1).
- [ ] **Add `SECRET_ENCRYPTION_KEY`** to the environment contract (separate
  from `SECRET_KEY`) and the `cryptography` dependency, for encrypted key
  storage.
- [ ] **Compliance posture** — confirm demo-only for now, or start the
  BAA/DPIA conversation before any real PHI reaches a model provider.
- [ ] **Pick AI rate/spend limits** — the per-request Flask-Limiter numbers
  exist, but LLM endpoints need a budget answer too.
- [ ] **No password-reset flow yet** (noted in SECURITY.md). Fine for a demo;
  decide whether it must precede giving accounts to more people.

## Verified platform facts Phase 4+ can rely on

Checked live against `https://hapi.fhir.org/baseR4` on 2026-08-08:

| Capability | Result |
|---|---|
| `GET /Patient/{id}/$everything` | works (Patient + Encounter + Observations in one call) |
| `POST Condition` | 201 with ETag `W/"1"` |
| `PUT` with `If-Match` | 200, version increments |
| `PUT` with stale `If-Match` | rejected — but as **409**, not the spec's 412; conflict handling must accept both |
| `DELETE Condition` | 200 |
