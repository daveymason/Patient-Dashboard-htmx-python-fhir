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

- **Patient list & search** — by name, patient ID, or identifier, with paging.
- **Patient details** — demographics, contact, identifiers, languages.
- **Lab results** — handles every FHIR `value[x]` type, including panels and
  component-based observations such as blood pressure.
- **Vital signs** — blood pressure, heart rate, temperature and respiratory
  rate, fetched concurrently.
- **Medications & allergies** — with upstream failures reported distinctly from
  genuinely empty records.
- **Stats & insights** — demographic breakdowns and server-side totals.
- **Access control** — wallet sign-in against an approved user list, with roles.
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
# set INITIAL_ADMIN_WALLET to your own wallet address

export $(grep -v '^#' .env | xargs)     # or use direnv / python-dotenv
FLASK_DEBUG=true python app.py
```

Visit <http://127.0.0.1:5000/>.

Sign in with MetaMask using the wallet you set as `INITIAL_ADMIN_WALLET`. Any
other wallet that signs in is recorded as **pending** and sees nothing until you
approve it under **Users**.

### Granting access from the command line

`INITIAL_ADMIN_WALLET` only applies on first boot. To grant or recover
administrator access at any time:

```bash
flask --app app grant-admin 0xYourWalletAddress
flask --app app list-users
```

`grant-admin` creates the account if it does not exist and promotes it if it
does, so a locked-out install never needs the database edited by hand. An
address that is malformed — or the zero address, which no private key maps to —
is refused rather than seeded, and the app warns at startup whenever no active
administrator exists.

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
| `INITIAL_ADMIN_WALLET` | Recommended | — | Wallet granted admin on first boot. Invalid or zero addresses are ignored, not seeded. |
| `FHIR_SERVER_URL` | No | `http://hapi.fhir.org/baseR4` | FHIR R4 base URL. |
| `FHIR_TIMEOUT` | No | `10` | Per-request timeout, seconds. |
| `FHIR_PAGE_SIZE` | No | `20` | Patients per page. |
| `DATABASE_PATH` | No | `./instance/dashboard.db` | User store and audit log. |
| `SESSION_COOKIE_SECURE` | No | `true` | Set `false` only for local HTTP. |
| `SESSION_LIFETIME_MINUTES` | No | `60` | Idle session lifetime. |
| `LOG_HASH_SALT` | Recommended | random | Salt for pseudonymised log identifiers. Keep stable and secret. |
| `LOG_JSON` | No | `true` | Structured JSON logs. |
| `HEALTH_SHOW_BACKEND` | No | `false` | Expose the FHIR URL on `/health`. |
| `RATELIMIT_AUTH` | No | `10 per minute` | Limit on auth endpoints. |

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
| `auth.py` | Wallet sign-in and roles |
| `db.py` | SQLite user store and append-only audit log |
| `logging_setup.py` | JSON logging with identifiers pseudonymised |

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
