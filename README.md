# 🏥 FHIR Patient Dashboard

A Flask app for exploring patient data from a FHIR R4 server, using
[HTMX](https://htmx.org/) for interactivity and [Bootstrap 5](https://getbootstrap.com/)
for layout. Routes return HTML fragments, so there is no client-side framework
and no build step.

---

## ⚠️ Before you point this at real patient data

This is a demonstration app. It defaults to `hapi.fhir.org`, a **public test
server holding public synthetic data**. Read [SECURITY.md](SECURITY.md) before
connecting it to anything real — it lists what is in place and what is still
missing.

---

## 🚀 Features

- **Patient list & search** — by name, patient ID, identifier, birthdate or
  phone, with sortable columns and paging.
- **Patient details** — demographics, contact, identifiers, languages.
- **Conditions / problem list** — auto-loads with the patient record.
- **Lab results** — handles every FHIR `value[x]` type, including panels and
  component-based observations such as blood pressure.
- **Vital signs** — blood pressure, heart rate, temperature and respiratory
  rate, fetched concurrently, with trend charts.
- **Medications & allergies** — with upstream failures reported distinctly from
  genuinely empty records.
- **Stats & insights** — demographic breakdowns and server-side totals, with charts.
- **Access control** — username/password sign-in against an approved user list, with roles.
- **Audit log** — append-only record of who viewed which patient, when.

---

## 🛠️ Quickstart

```bash
git clone https://github.com/daveymason/Patient-Dashboard-htmx-python-fhir.git
cd Patient-Dashboard-htmx-python-fhir

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"   # paste into SECRET_KEY
# set INITIAL_ADMIN_USERNAME and INITIAL_ADMIN_PASSWORD to your own login

export $(grep -v '^#' .env | xargs)     # or use direnv / python-dotenv
FLASK_DEBUG=true python app.py
```

Visit <http://127.0.0.1:5000/>.

Sign in with the username and password you set as `INITIAL_ADMIN_USERNAME` /
`INITIAL_ADMIN_PASSWORD`. Anyone who registers an account is recorded as
**pending** and sees nothing until you approve them under **Users**.

### Fastest local check

To just click around locally without editing `.env` at all:

```bash
SECRET_KEY="dev-local-key-0123456789abcdef0123456789abcdef" \
INITIAL_ADMIN_USERNAME="admin" \
INITIAL_ADMIN_PASSWORD="dev-password-please-change" \
SESSION_COOKIE_SECURE=false \
FLASK_DEBUG=true \
python app.py
```

Then visit **<http://127.0.0.1:5000/>** and sign in with `admin` /
`dev-password-please-change`. This is a throwaway key and password for local
use only — never run a real deployment this way.

### Granting access from the command line

`INITIAL_ADMIN_USERNAME`/`INITIAL_ADMIN_PASSWORD` only apply on first boot. To
grant or recover administrator access at any time:

```bash
flask --app app grant-admin yourusername
flask --app app list-users
```

`grant-admin` creates the account if it does not exist and promotes it if it
does, prompting for a password either way — so a locked-out install never
needs the database edited by hand. The app warns at startup whenever no
active administrator exists.

### Docker

```bash
docker build -t patient-dashboard .
docker run -p 8000:8000 --env-file .env -v "$PWD/instance:/app/instance" patient-dashboard
```

### Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite runs offline — the FHIR backend is stubbed — and covers each defect
fixed in the hardening pass.

---

## ⚙️ Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SECRET_KEY` | **Yes** (prod) | — | Session signing. The app refuses to start without it outside debug. |
| `INITIAL_ADMIN_USERNAME` / `INITIAL_ADMIN_PASSWORD` | Recommended | — | Account granted admin on first boot. Must be set together; invalid or weak values are ignored, not seeded. |
| `FHIR_SERVER_URL` | No | `https://hapi.fhir.org/baseR4` | FHIR R4 base URL. |
| `FHIR_TIMEOUT` | No | `10` | Per-request timeout, seconds. |
| `FHIR_PAGE_SIZE` | No | `20` | Patients per page. |
| `DATABASE_PATH` | No | `./instance/dashboard.db` | User store and audit log. |
| `SESSION_COOKIE_SECURE` | No | `true` | Set `false` only for local HTTP. |
| `SESSION_LIFETIME_MINUTES` | No | `60` | Idle session lifetime. |
| `LOG_HASH_SALT` | Recommended | random | Salt for pseudonymised log identifiers. Keep stable and secret. |
| `LOG_JSON` | No | `true` | Structured JSON logs. |
| `HEALTH_SHOW_BACKEND` | No | `false` | Expose the FHIR URL on `/health`. |
| `RATELIMIT_AUTH` | No | `10 per minute` | Limit on auth endpoints. |
| `OPENAI_API_KEY` | No | — | AI provider key. Environment only — never stored in the database. |
| `AI_MODEL` | No | `gpt-5.6-luna` | Model ID. Verify it against your account on the AI Settings page. |
| `AI_REASONING_EFFORT` | No | `low` | `none`…`max`. Reasoning tokens bill at the output rate, so this is the cost lever. Blank omits the parameter. |
| `AI_ALLOW_REAL_PHI` | No | `false` | Permits AI against a non-demo FHIR server. Requires a BAA/DPIA. |
| `AI_DEIDENTIFY` | No | `auto` | Withholds name, address, contact and record numbers from model context. `auto` = on unless the FHIR server is a known synthetic one. |
| `AI_CONTEXT_TOKEN_CEILING` | No | `120000` | Above this, a record is curated rather than sent whole. Allergies are never dropped. |

Running multiple gunicorn workers **requires** a stable `SECRET_KEY` — otherwise
each worker signs cookies with a different key and sign-in fails intermittently.

---

## 🧱 Layout

| File | Role |
|---|---|
| `app.py` | Application factory and routes |
| `wsgi.py` | Production entry point (`gunicorn wsgi:app`) |
| `config.py` | Environment-driven configuration |
| `fhir_client.py` | HTTP client: timeouts, retries, concurrency, typed results |
| `fhir_utils.py` | Defensive parsing of FHIR resources |
| `auth.py` | Username/password sign-in and roles |
| `db.py` | SQLite user store and append-only audit log |
| `logging_setup.py` | JSON logging with identifiers pseudonymised |

---

## 🗺️ Roadmap

[docs/PROJECT-PLAN.md](docs/PROJECT-PLAN.md) is the single planning document —
phases in build order, with task checklists, technical detail, a risk register
and the verified FHIR and model facts behind the decisions.

---

## 🤝 Contributing

1. Fork & clone the repo
2. Create a feature branch
3. Run `pytest` before opening a pull request
4. Open a pull request

---

## 📄 License

MIT License. See [LICENSE](LICENSE).

---

## 🙏 Credits

- Logo by [Freepik](https://www.freepik.com/icon/computer_8811410) (Flaticon)
- Project by [daveymason.com](https://daveymason.com)

---

[Project Repository](https://github.com/daveymason/Patient-Dashboard-htmx-python-fhir)
