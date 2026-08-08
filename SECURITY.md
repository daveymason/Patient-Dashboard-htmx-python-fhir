# Security posture

This app defaults to `hapi.fhir.org`, a public test server holding public
synthetic data. The notes below describe what changes when you point it at real
patient data.

## In place

| Control | Implementation |
|---|---|
| Authentication | EIP-191 wallet signature over a server-issued, session-bound, single-use nonce |
| Authorization | Approved-user table; a new wallet lands as `pending` and can read nothing until an administrator activates it |
| Roles | `admin`, `clinician`, `viewer`; admin screens enforce role |
| Session integrity | `SECRET_KEY` required outside debug; `HttpOnly`, `SameSite=Lax`, `Secure` by default; 60-minute lifetime; Flask-Login "strong" protection |
| CSRF | Flask-WTF on all state-changing requests, including HTMX and `fetch` |
| Rate limiting | Auth endpoints and the AI key test |
| Audit trail | Append-only SQLite table, enforced by triggers, covering sign-in, denials, and every patient-record access |
| Log hygiene | JSON logs; patient and wallet identifiers stored as salted digests, never in the clear |
| Transport safety | FHIR requests carry timeouts and bounded retries; pagination links are validated against the configured server to prevent SSRF |
| Failure semantics | An unreachable backend never renders as "no records" — critical for allergy lists |

## Not in place

Deliberate gaps. Do not treat this as production-ready for PHI.

- **No encryption at rest.** The SQLite database is unencrypted. Use an
  encrypted volume, or move the store to a managed database.
- **No BAA or processor agreement.** The AI key-test endpoint contacts OpenAI.
  No patient data is sent today, but nothing is in place for when it is.
- **No PHI storage.** Patient data is proxied, never persisted. Adding write-back
  changes the compliance picture entirely.
- **No SMART on FHIR / OIDC.** Wallet auth is not what healthcare uses. Real
  deployments should use SMART on FHIR or an OIDC provider.
- **No key management.** The AI settings page tests a key; it does not store one.
- **No log shipping or retention policy.** The audit table grows without bound
  and is not backed up.
- **No penetration test.**

## Reporting

Open a security advisory on the repository rather than a public issue.
