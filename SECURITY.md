# Security posture

This app defaults to `hapi.fhir.org`, a public test server holding public
synthetic data. The notes below describe what changes when you point it at real
patient data.

## In place

| Control | Implementation |
|---|---|
| Authentication | Username/password; passwords hashed with Werkzeug's `generate_password_hash` (salted PBKDF2), never stored or logged in the clear |
| Authorization | Approved-user table; a newly registered account lands as `pending` and can read nothing until an administrator activates it |
| Roles | `admin`, `clinician`, `viewer`; admin screens enforce role |
| Session integrity | `SECRET_KEY` required outside debug; `HttpOnly`, `SameSite=Lax`, `Secure` by default; 60-minute lifetime; Flask-Login "strong" protection |
| CSRF | Flask-WTF on all state-changing requests, including HTMX and `fetch` |
| Rate limiting | Auth endpoints and the AI key test |
| Audit trail | Append-only SQLite table, enforced by triggers, covering sign-in, denials, and every patient-record access |
| AI exchange trail | Separate append-only table recording actor, pseudonymised patient, feature, model, prompt hash and version, token counts and the full response — so a disputed answer can be investigated afterwards |
| Log hygiene | JSON logs; patient and account identifiers stored as salted digests, never in the clear |
| Transport safety | FHIR requests carry timeouts and bounded retries; pagination links are validated against the configured server to prevent SSRF |
| Failure semantics | An unreachable backend never renders as "no records" — critical for allergy lists. Model context marks an unreadable section as unreadable, never as empty |
| AI context minimisation | `AI_DEIDENTIFY` withholds name, address, contact details, record numbers and exact date of birth from what is sent to the model — see the caveat below |

## Not in place

Deliberate gaps. Do not treat this as production-ready for PHI.

- **No encryption at rest.** The SQLite database is unencrypted. Use an
  encrypted volume, or move the store to a managed database.
- **No BAA or processor agreement.** The AI settings check contacts the model
  provider to verify the key and model; no patient data is sent. AI features
  are gated on `AI_ALLOW_REAL_PHI`, which stays `false` unless
  `FHIR_SERVER_URL` is a known synthetic-data server — so pointing the app at
  a real server disables them rather than silently forwarding PHI. Turning
  that flag on without an agreement in place is the operator's decision, and
  the wrong one.
- **`AI_DEIDENTIFY` is not de-identification in the legal sense.** It withholds
  direct identifiers — name, address, contact details, record numbers, exact
  date of birth — and generalises ages over 89. It does **not** meet HIPAA Safe
  Harbor: every context line keeps its `[ResourceType/id]` tag, because a
  citation the reader cannot resolve is worthless, and anyone holding those ids
  plus access to the FHIR server can re-identify the patient immediately. Treat
  it as reducing what the model provider sees, not as anonymisation.
  `AI_ALLOW_REAL_PHI` is the control that actually decides whether patient data
  may leave the deployment.
- **Free-text clinical notes are not sent to the model at all.** `note` elements
  are the one place a clinician's prose can name a third party, and there is no
  reliable way to strip that, so no context builder renders them.
- **`ai_exchange` stores full model responses indefinitely.** That is deliberate
  — it is the record an investigation reads — but it is unencrypted at rest,
  grows without bound, and has no retention policy.
- **No PHI storage.** Patient data is proxied, never persisted. Adding write-back
  changes the compliance picture entirely.
- **No SMART on FHIR / OIDC.** Local username/password is not what production
  healthcare deployments use. Real deployments should use SMART on FHIR or an
  OIDC provider (SSO), not a locally-stored credential.
- **No account lockout / brute-force detection beyond rate limiting.** Failed
  logins are rate-limited and audited, but there is no progressive lockout or
  alerting on repeated failures against one account.
- **No self-service password reset.** A forgotten password has no self-service
  recovery path; an administrator resets it with
  `flask --app app reset-password <username>`, which changes the credential
  only and leaves role and status untouched. Self-service reset needs an email
  sender the app does not have.
- **No key management.** The AI settings page tests a key; it does not store one.
- **No log shipping or retention policy.** The audit table grows without bound
  and is not backed up.
- **No penetration test.**

## Reporting

Open a security advisory on the repository rather than a public issue.
