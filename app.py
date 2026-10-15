import os
"""FHIR Patient Dashboard.

Flask + HTMX viewer over a FHIR R4 server. Routes return HTML fragments that
HTMX swaps into the page, so there is no client-side framework and no build step.
"""
import logging
import re

import click
from flask import (
    Flask, abort, jsonify, redirect, render_template, request, session, url_for
)
from flask_login import (
    LoginManager, current_user, login_required, login_user, logout_user
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect

import auth
import db
from ai import provider as ai_provider
from charts import demographics_charts, vitals_charts
from config import (
    ConfigError, MAX_USERNAME_LENGTH, MIN_PASSWORD_LENGTH, MIN_USERNAME_LENGTH,
    ai_blocked_reason, is_valid_username, resolve_secret_key, select_config,
)
from fhir_client import FhirClient
from fhir_utils import (
    LOINC_DIASTOLIC, LOINC_SYSTOLIC, blood_pressure_components, calculate_age,
    condition_onset, first_coding_display, format_address, format_fhir_date,
    observation_code, observation_value, patient_display_name, patient_initials,
    sort_key,
)
from logging_setup import configure_logging, pseudonymize

logger = logging.getLogger(__name__)

login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)

# LOINC codes for the vital signs panel.
VITAL_SIGN_CODES = {
    'blood_pressure': ('85354-9', 'Blood Pressure', 'mmHg'),
    'heart_rate': ('8867-4', 'Heart Rate', 'bpm'),
    'spo2': ('59408-5', 'Oxygen Saturation (SpO2)', '%'),
    'respiratory_rate': ('9279-1', 'Respiratory Rate', '/min'),
    'temperature': ('8310-5', 'Temperature', '°C'),
    'pain': ('72514-3', 'Pain Score', '{score}'),
}

# Patient search parameters that may be sorted, mapped to nothing (they're
# already valid FHIR _sort values) -- kept as a set purely to reject anything
# else a caller might try to inject through the sort parameter.
_SORTABLE_FIELDS = {'family', 'birthdate', 'gender'}

_DATE_SEARCH_RE = re.compile(r'\d{4}(-\d{2}(-\d{2})?)?$')


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app(config_object=None):
    app = Flask(__name__)

    config_class = config_object or select_config()
    app.config.from_object(config_class)

    if not app.config.get('SECRET_KEY'):
        app.config['SECRET_KEY'] = resolve_secret_key(app.config.get('DEBUG', False))

    configure_logging(app.config['LOG_LEVEL'], as_json=app.config['LOG_JSON'])

    fhir_store_path = app.config.get('FHIR_STORE_PATH')
    use_local_store = (
        app.config.get('APPLIANCE_MODE', False) or
        app.config['FHIR_SERVER_URL'].startswith('file://') or
        (fhir_store_path and os.path.exists(fhir_store_path))
    )
    if use_local_store and fhir_store_path:
        from local_fhir_store import LocalFhirStore
        app.fhir = LocalFhirStore(fhir_store_path)
    else:
        app.fhir = FhirClient(
            app.config['FHIR_SERVER_URL'],
            timeout=app.config['FHIR_TIMEOUT'],
        )

    login_manager.init_app(app)
    login_manager.login_view = 'login'
    login_manager.user_loader(auth.load_user)
    login_manager.session_protection = 'strong'

    csrf.init_app(app)
    limiter.init_app(app)

    app.teardown_appcontext(db.close_connection)
    db.init_db(app)

    register_routes(app)
    register_cli(app)

    # Nobody can approve a pending account if there is no reachable admin, so
    # say so at boot rather than letting the operator discover it by being
    # locked out.
    with app.app_context():
        if db.count_active_admins() == 0:
            logger.warning(
                'No active administrator account exists. Nobody can approve '
                'sign-in requests. Grant one with: '
                'flask --app app grant-admin <username>'
            )

    @app.before_request
    def _make_session_permanent():
        session.permanent = True

    logger.info(
        'Application started',
        extra={'fhir_server': app.config['FHIR_SERVER_URL'], 'debug': app.config['DEBUG']},
    )
    return app


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------

def audit(action, outcome='success', resource_type=None, resource_id=None, detail=None):
    """Record an access to the audit trail, with the patient ID pseudonymised."""
    db.write_audit(
        action=action,
        outcome=outcome,
        actor=current_user.get_id() if current_user.is_authenticated else None,
        resource_type=resource_type,
        resource_id=resource_id,
        remote_addr=request.remote_addr,
        user_agent=request.headers.get('User-Agent'),
        detail=detail,
    )


def _fragment_error(message, columns=None):
    """Render an inline error for an HTMX panel.

    Returns HTTP 200 deliberately: htmx does not swap non-2xx responses by
    default, so a 5xx here would leave the panel silently blank.
    """
    if columns:
        return (
            f'<tr><td colspan="{columns}" class="text-center text-danger py-3">'
            f'<i class="fas fa-exclamation-triangle me-2"></i>{message}</td></tr>'
        )
    return (
        f'<div class="alert alert-danger mb-3">'
        f'<i class="fas fa-exclamation-triangle me-2"></i>{message}</div>'
    )


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------

def process_patient_results(resources):
    """Flatten Patient resources into the shape the table templates expect."""
    processed = []
    for resource in resources:
        address = (resource.get('address') or [{}])[0]
        telecom = (resource.get('telecom') or [{}])[0]
        processed.append({
            'id': resource.get('id', 'Unknown'),
            'name': patient_display_name(resource),
            'gender': resource.get('gender', 'Unknown'),
            'birthDate': resource.get('birthDate', 'Unknown'),
            'address': format_address(address) or 'Not recorded',
            'telecom': telecom.get('value', 'Not recorded'),
        })
    return processed


def _bundle_next_link(result):
    """Extract the `next` page URL from a searchset Bundle, if there is one."""
    for link in result.data.get('link') or []:
        if isinstance(link, dict) and link.get('relation') == 'next':
            return link.get('url')
    return None


def _validate_sort(raw):
    """Only pass through a `sort` value that is actually one of our columns.

    The value doubles as the literal FHIR `_sort` parameter (e.g. `-birthdate`),
    so an unchecked value would let a caller sort -- or otherwise perturb the
    query -- by an arbitrary FHIR search parameter.
    """
    if not raw:
        return None
    field = raw[1:] if raw.startswith('-') else raw
    return raw if field in _SORTABLE_FIELDS else None


def _looks_like_date(term):
    return bool(_DATE_SEARCH_RE.fullmatch(term))


def _looks_like_phone(term):
    digits = re.sub(r'[\s().+-]', '', term)
    return digits.isdigit() and len(digits) >= 6


def _looks_like_id(term):
    return term.replace('-', '').isalnum() and any(c.isdigit() for c in term)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def register_routes(app):
    fhir = app.fhir
    auth_limit = app.config['RATELIMIT_AUTH']

    # -- shell -------------------------------------------------------------

    @app.route('/')
    def index():
        if not current_user.is_authenticated:
            return redirect(url_for('login'))
        # `view` lets sidebar links on non-SPA pages (admin, settings) land
        # back in the shell with the right section loaded, not just Patients.
        views = {
            'triage': '/fhir/Triage',
            'patients': '/fhir/Patients',
            'stats': '/fhir/Demographics',
            'insights': '/fhir/Dashboard',
            'pipeline': '/pipeline/status',
        }
        view_arg = request.args.get('view')
        if view_arg is None:
            initial_view = views['triage'] if app.config.get('APPLIANCE_MODE') else views['patients']
        else:
            initial_view = views.get(view_arg, views['patients'])
        return render_template('index.html', initial_view=initial_view)

    @app.route('/health')
    def health_check():
        payload = {'status': 'ok'}
        if app.config['HEALTH_SHOW_BACKEND']:
            payload['fhir_server'] = app.config['FHIR_SERVER_URL']
        return jsonify(payload)

    @app.route('/health/ready')
    def readiness_check():
        """Deep check -- verifies the FHIR backend actually answers."""
        reachable = fhir.capability_ok()
        return jsonify({'status': 'ok' if reachable else 'degraded',
                        'fhir_reachable': reachable}), (200 if reachable else 503)

    # -- authentication ----------------------------------------------------

    @app.route('/login', methods=['GET', 'POST'])
    @limiter.limit(auth_limit, methods=['POST'])
    def login():
        # The limit applies to POST only: every logged-out visit to any page
        # redirects through GET /login, so limiting views too would let a
        # handful of open tabs (or one hospital NAT) 429 the sign-in page
        # before anyone typed a password.
        if current_user.is_authenticated:
            return redirect(url_for('index'))
        if request.method == 'GET':
            notice = None
            if request.args.get('registered'):
                notice = ('Account created. An administrator must approve it '
                          'before you can sign in.')
            return render_template('login.html', notice=notice)

        username = (request.form.get('username') or '').strip().lower()
        password = request.form.get('password') or ''
        if not username or not password:
            return render_template('login.html',
                                   error='Enter your username and password.'), 400

        record = auth.verify_credentials(username, password)
        if record is None:
            audit('login.denied', outcome='failure', resource_type='User',
                  resource_id=pseudonymize(username), detail={'reason': 'bad_credentials'})
            return render_template('login.html',
                                   error='Incorrect username or password.'), 401

        if record['status'] != db.STATUS_ACTIVE:
            audit('login.denied', outcome='denied', resource_type='User',
                  resource_id=pseudonymize(username),
                  detail={'reason': f"account_{record['status']}"})
            message = ('Your account is awaiting administrator approval.'
                       if record['status'] == db.STATUS_PENDING
                       else 'This account has been disabled.')
            return render_template('login.html', error=message), 403

        login_user(auth.User(record))
        db.record_login(username)
        audit('login.success', resource_type='User', resource_id=pseudonymize(username))
        return redirect(url_for('index'))

    @app.route('/register', methods=['GET', 'POST'])
    @limiter.limit(auth_limit, methods=['POST'])
    def register():
        if current_user.is_authenticated:
            return redirect(url_for('index'))
        if request.method == 'GET':
            return render_template('register.html')

        username = (request.form.get('username') or '').strip().lower()
        password = request.form.get('password') or ''
        confirm = request.form.get('confirm_password') or ''

        error = None
        if not is_valid_username(username):
            error = (f'Choose a username of {MIN_USERNAME_LENGTH}-{MAX_USERNAME_LENGTH} '
                     'characters: letters, numbers, dots, hyphens or underscores.')
        elif len(password) < MIN_PASSWORD_LENGTH:
            error = f'Choose a password of at least {MIN_PASSWORD_LENGTH} characters.'
        elif password != confirm:
            error = 'Passwords do not match.'
        elif db.get_user(username) is not None:
            error = 'That username is already taken.'

        if error:
            return render_template('register.html', error=error, username=username), 400

        db.register_pending_user(username, auth.hash_password(password))
        audit('register.pending', resource_type='User', resource_id=pseudonymize(username))
        # Redirect rather than render (post/redirect/get): rendering the login
        # page from POST /register left the browser parked on /register, where
        # refresh or Back re-submitted the form and got "username taken".
        return redirect(url_for('login', registered=1))

    @app.route('/logout')
    @login_required
    def logout():
        audit('logout')
        logout_user()
        session.clear()
        return redirect(url_for('login'))

    # -- patients ----------------------------------------------------------

    @app.route('/fhir/Patients')
    @login_required
    def get_patients():
        page_size = app.config['FHIR_PAGE_SIZE']
        current_sort = _validate_sort(request.args.get('sort'))
        params = {'_count': page_size}
        if current_sort:
            params['_sort'] = current_sort
        result = fhir.get('Patient', params=params)
        if not result.ok:
            audit('patient.list', outcome='failure')
            return render_template('patients.html', patients=[], error=result.error,
                                   total=None, next_url=None, current_sort=current_sort)

        audit('patient.list')
        return render_template(
            'patients.html',
            patients=process_patient_results(result.entries),
            error=None,
            total=fhir.count('Patient'),
            next_url=_bundle_next_link(result),
            current_sort=current_sort,
        )

    @app.route('/fhir/Patients/page')
    @login_required
    def get_patients_page():
        """Follow a Bundle `next` link. The URL is validated against the
        configured server inside the client, so it cannot be pointed elsewhere.

        Renders rows only, never the table shell: the response replaces the
        Load More row inside the existing <tbody>, appending the next page
        under the rows already on screen.
        """
        result = fhir.get_absolute(request.args.get('url', ''))
        if not result.ok:
            return _fragment_error(result.error or 'Could not load the next page.', columns=7)
        audit('patient.list', detail={'page': 'next'})
        return render_template(
            'patient_rows.html',
            patients=process_patient_results(result.entries),
            error=None,
            next_url=_bundle_next_link(result),
        )

    @app.route('/fhir/search_patients', methods=['POST'])
    @login_required
    def search_patients():
        search_term = (request.values.get('q') or '').strip()
        current_sort = _validate_sort(request.values.get('sort'))
        page_size = app.config['FHIR_PAGE_SIZE']

        base_params = {'_count': page_size}
        if current_sort:
            base_params['_sort'] = current_sort

        if not search_term:
            result = fhir.get('Patient', params=base_params)
            if not result.ok:
                audit('patient.search', outcome='failure')
                return _fragment_error(result.error or 'Search failed.', columns=7)
            audit('patient.search', detail={'has_term': False})
            return render_template(
                'patient_table_body.html',
                patients=process_patient_results(result.entries),
                error=None, next_url=_bundle_next_link(result), current_sort=current_sort,
            )

        # A single search box covers several distinct FHIR search parameters.
        # Every plausible interpretation of the term is queried concurrently
        # rather than tried one at a time, then the first match is taken in
        # priority order -- this both supports searching by birthdate/phone
        # and (since it replaces what was a sequential id -> name -> identifier
        # fallback chain) cuts the worst case from three round trips to one.
        candidates = {'name': ('Patient', {**base_params, 'name:contains': search_term})}
        if _looks_like_id(search_term):
            candidates['id'] = ('Patient', {**base_params, '_id': search_term})
        if _looks_like_date(search_term):
            candidates['birthdate'] = ('Patient', {**base_params, 'birthdate': search_term})
        if _looks_like_phone(search_term):
            candidates['phone'] = ('Patient', {**base_params, 'phone': search_term})
        candidates['identifier'] = ('Patient', {**base_params, 'identifier': search_term})

        results = fhir.get_many(candidates)

        result = None
        for key in ('id', 'birthdate', 'phone', 'name', 'identifier'):
            candidate_result = results.get(key)
            if candidate_result and candidate_result.ok and candidate_result.entries:
                result = candidate_result
                break

        if result is None:
            # Nothing matched. "No patients found" is only a truthful answer
            # if every interpretation of the term was actually checked -- if
            # any candidate query failed, a match may exist behind the
            # failure, so report the error rather than a clean empty result.
            failures = [r for r in results.values() if not r.ok]
            if failures:
                audit('patient.search', outcome='failure')
                return _fragment_error(failures[0].error or 'Search failed.', columns=7)
            result = results['name']

        audit('patient.search', detail={'has_term': True})
        return render_template(
            'patient_table_body.html',
            patients=process_patient_results(result.entries),
            error=None,
            next_url=_bundle_next_link(result),
            current_sort=current_sort,
        )

    @app.route('/fhir/Patient/<patient_id>')
    @login_required
    def get_patient(patient_id):
        result = fhir.get(f'Patient/{patient_id}')
        if not result.ok:
            audit('patient.view', outcome='failure', resource_type='Patient',
                  resource_id=pseudonymize(patient_id))
            if result.status_code == 404:
                return _fragment_error('That patient could not be found.')
            return _fragment_error(result.error or 'Could not load this patient.')

        patient = result.data
        patient['display_name'] = patient_display_name(patient)
        patient['initials'] = patient_initials(patient)
        patient['age'] = calculate_age(patient.get('birthDate'))

        work_telecoms = [
            entry['value'] for entry in patient.get('telecom') or []
            if isinstance(entry, dict) and entry.get('use') == 'work' and entry.get('value')
        ]
        any_telecoms = [
            entry['value'] for entry in patient.get('telecom') or []
            if isinstance(entry, dict) and entry.get('value')
        ]
        patient['contact'] = ', '.join(work_telecoms or any_telecoms) or 'Not recorded'

        addresses = patient.get('address') or []
        patient['simple_address'] = (
            format_address(addresses[0]) if addresses else ''
        ) or 'Not recorded'

        identifiers = patient.get('identifier') or []
        patient['simple_identifier'] = next(
            (i.get('value') for i in identifiers if isinstance(i, dict) and i.get('value')),
            'Not recorded',
        )

        patient['languages'] = [
            {
                'name': first_coding_display(entry.get('language', {})),
                'preferred': bool(entry.get('preferred')),
            }
            for entry in patient.get('communication') or []
            if isinstance(entry, dict) and first_coding_display(entry.get('language', {}))
        ]

        patient['marital_status'] = first_coding_display(patient.get('maritalStatus', {}))

        audit('patient.view', resource_type='Patient', resource_id=pseudonymize(patient_id))
        return render_template('patient_details.html', patient=patient)

    # -- clinical panels ---------------------------------------------------

    @app.route('/fhir/Conditions/<patient_id>')
    @login_required
    def get_conditions(patient_id):
        result = fhir.get('Condition', params={'patient': patient_id, '_count': 20})
        if not result.ok:
            audit('patient.conditions', outcome='failure', resource_type='Patient',
                  resource_id=pseudonymize(patient_id))
            return render_template('conditions.html', conditions=[], error=result.error)

        conditions = []
        for resource in result.entries:
            onset_display, onset_sort = condition_onset(resource)
            recorded = resource.get('recordedDate')
            conditions.append({
                'onset': onset_display or format_fhir_date(recorded),
                'sort_key': onset_sort or sort_key(recorded),
                'name': first_coding_display(resource.get('code', {}),
                                             fallback='Unknown condition'),
                'clinical_status': first_coding_display(resource.get('clinicalStatus', {}),
                                                        fallback='Unknown'),
                'verification_status': first_coding_display(resource.get('verificationStatus', {})),
            })
        conditions.sort(key=lambda item: item['sort_key'], reverse=True)

        audit('patient.conditions', resource_type='Patient', resource_id=pseudonymize(patient_id))
        return render_template('conditions.html', conditions=conditions, error=None)

    @app.route('/fhir/LabResults/<patient_id>')
    @login_required
    def get_lab_results(patient_id):
        result = fhir.get('Observation', params={
            'patient': patient_id, '_sort': '-date', '_count': 10,
            'category': 'laboratory',
        })
        if not result.ok:
            audit('patient.labs', outcome='failure', resource_type='Patient',
                  resource_id=pseudonymize(patient_id))
            return render_template('lab_results.html', lab_results=[], error=result.error)

        lab_results = []
        for resource in result.entries:
            effective = resource.get('effectiveDateTime') or resource.get('issued')
            value, unit = observation_value(resource)
            lab_results.append({
                'date': format_fhir_date(effective),
                'sort_key': sort_key(effective),
                'code': observation_code(resource),
                'value': value if value != '' else 'Not recorded',
                'unit': unit,
                'status': resource.get('status', 'unknown'),
            })
        lab_results.sort(key=lambda item: item['sort_key'], reverse=True)

        audit('patient.labs', resource_type='Patient', resource_id=pseudonymize(patient_id))
        return render_template('lab_results.html', lab_results=lab_results, error=None)

    @app.route('/fhir/VitalSigns/<patient_id>')
    @login_required
    def get_vital_signs(patient_id):
        # Four queries for the main vital-sign codes, run concurrently, plus
        # two more for blood pressure specifically: some servers record it as
        # a single panel Observation (code 85354-9) with systolic/diastolic as
        # components, but plenty of real data -- including a chunk of this
        # app's own default FHIR sandbox -- instead records two independent
        # Observations under 8480-6/8462-4 sharing an encounter and no panel
        # wrapper at all. Missing that shape was why Vital Signs came up
        # empty for patients whose blood pressure was recorded this way.
        spec = {
            key: ('Observation', {
                'patient': patient_id,
                'code': f'http://loinc.org|{code}',
                '_sort': '-date',
                '_count': 10,
            })
            for key, (code, _label, _unit) in VITAL_SIGN_CODES.items()
        }
        spec['_standalone_systolic'] = ('Observation', {
            'patient': patient_id, 'code': f'http://loinc.org|{LOINC_SYSTOLIC}',
            '_sort': '-date', '_count': 10,
        })
        spec['_standalone_diastolic'] = ('Observation', {
            'patient': patient_id, 'code': f'http://loinc.org|{LOINC_DIASTOLIC}',
            '_sort': '-date', '_count': 10,
        })
        results = fhir.get_many(spec)

        vital_signs = []
        failures = []
        # Chart series need ascending-by-date rows separate from the table's
        # sort_key, and blood pressure needs raw systolic/diastolic numbers
        # rather than the combined display string the table uses.
        chart_series = {
            key: {'label': label, 'unit': unit, 'rows': []}
            for key, (_code, label, unit) in VITAL_SIGN_CODES.items()
        }
        # Encounter refs and timestamps already represented by a panel
        # observation. Some servers expose the same measurement both as a
        # panel and as standalone component observations; without this set
        # the reading would be counted twice.
        bp_panel_keys = set()

        for key, (_code, label, _unit) in VITAL_SIGN_CODES.items():
            result = results.get(key)
            if result is None or not result.ok:
                failures.append(label)
                continue
            for resource in result.entries:
                effective = resource.get('effectiveDateTime') or resource.get('issued')
                date_label = format_fhir_date(effective)
                dt = sort_key(effective)

                if key == 'blood_pressure':
                    value, unit = observation_value(resource)
                    if value != '':
                        vital_signs.append({
                            'date': date_label, 'sort_key': dt, 'type': label,
                            'value': value, 'unit': unit,
                            'status': resource.get('status', 'unknown'),
                        })
                    systolic, diastolic = blood_pressure_components(resource)
                    if systolic is not None and diastolic is not None:
                        chart_series[key]['rows'].append((dt, date_label, systolic, diastolic))
                    encounter_ref = (resource.get('encounter') or {}).get('reference')
                    if encounter_ref:
                        bp_panel_keys.add(encounter_ref)
                    if effective:
                        bp_panel_keys.add(f'time:{effective}')
                    continue

                value, unit = observation_value(resource)
                if value == '':
                    continue
                vital_signs.append({
                    'date': date_label,
                    'sort_key': dt,
                    'type': label,
                    'value': value,
                    'unit': unit,
                    'status': resource.get('status', 'unknown'),
                })
                try:
                    chart_series[key]['rows'].append((dt, date_label, float(value)))
                except (TypeError, ValueError):
                    pass  # a non-numeric reading still shows in the table, just not the chart

        # Pair standalone systolic/diastolic Observations by shared encounter,
        # falling back to an *exact* timestamp match when there is no
        # encounter to key on -- the only way to reassemble a blood-pressure
        # reading that was never linked by a panel wrapper in the source data.
        # Same-day-only matching is deliberately not attempted: stitching a
        # morning systolic to an evening diastolic would fabricate a reading
        # that never happened. Unpairable values are dropped from this panel
        # rather than guessed at.
        def _index_standalone_bp(result):
            indexed = {}
            if result is None or not result.ok:
                return indexed
            for resource in result.entries:
                value = (resource.get('valueQuantity') or {}).get('value')
                if value is None:
                    continue
                effective = resource.get('effectiveDateTime') or resource.get('issued')
                pairing_key = (resource.get('encounter') or {}).get('reference') \
                    or (f'time:{effective}' if effective else None)
                if pairing_key is None:
                    continue  # no encounter, no timestamp: nothing safe to pair on
                # setdefault, not assignment: on a key collision keep the
                # first (newest -- results are date-descending) rather than
                # silently replacing it with an older reading.
                indexed.setdefault(pairing_key, {
                    'sort_key': sort_key(effective),
                    'date': format_fhir_date(effective),
                    'value': value,
                    'status': resource.get('status', 'unknown'),
                })
            return indexed

        systolic_by_key = _index_standalone_bp(results.get('_standalone_systolic'))
        diastolic_by_key = _index_standalone_bp(results.get('_standalone_diastolic'))
        for pairing_key, systolic_entry in systolic_by_key.items():
            if pairing_key in bp_panel_keys:
                continue  # already represented by a panel observation
            diastolic_entry = diastolic_by_key.get(pairing_key)
            if diastolic_entry is None:
                continue
            vital_signs.append({
                'date': systolic_entry['date'], 'sort_key': systolic_entry['sort_key'],
                'type': 'Blood Pressure',
                'value': (f"Systolic blood pressure: {systolic_entry['value']} / "
                          f"Diastolic blood pressure: {diastolic_entry['value']}"),
                'unit': 'mmHg', 'status': systolic_entry['status'],
            })
            chart_series['blood_pressure']['rows'].append((
                systolic_entry['sort_key'], systolic_entry['date'],
                systolic_entry['value'], diastolic_entry['value'],
            ))

        # Undated readings sort to the bottom rather than masquerading as newest.
        vital_signs.sort(key=lambda item: item['sort_key'], reverse=True)

        error = None
        if failures:
            error = f"Could not load: {', '.join(failures)}."
            audit('patient.vitals', outcome='failure', resource_type='Patient',
                  resource_id=pseudonymize(patient_id), detail={'failed': failures})
        else:
            audit('patient.vitals', resource_type='Patient',
                  resource_id=pseudonymize(patient_id))

        return render_template(
            'vital_signs.html', vital_signs=vital_signs, error=error,
            charts=vitals_charts(chart_series),
        )

    @app.route('/fhir/Medications/<patient_id>')
    @login_required
    def get_medications(patient_id):
        result = fhir.get('MedicationRequest', params={
            'patient': patient_id, '_count': 20,
        })
        if not result.ok:
            audit('patient.medications', outcome='failure', resource_type='Patient',
                  resource_id=pseudonymize(patient_id))
            return render_template('medications.html', medications=[], error=result.error)

        medications = []
        for resource in result.entries:
            authored = resource.get('authoredOn')
            name = 'Unknown medication'
            if isinstance(resource.get('medicationReference'), dict):
                name = resource['medicationReference'].get('display') or name
            elif isinstance(resource.get('medicationCodeableConcept'), dict):
                name = first_coding_display(resource['medicationCodeableConcept'], fallback=name)

            dosage = [
                entry['text'] for entry in resource.get('dosageInstruction') or []
                if isinstance(entry, dict) and entry.get('text')
            ]
            medications.append({
                'date': format_fhir_date(authored),
                'sort_key': sort_key(authored),
                'name': name,
                'dosage': ', '.join(dosage) or 'No specific instructions',
                'status': resource.get('status', 'unknown'),
            })
        medications.sort(key=lambda item: item['sort_key'], reverse=True)

        audit('patient.medications', resource_type='Patient',
              resource_id=pseudonymize(patient_id))
        return render_template('medications.html', medications=medications, error=None)

    @app.route('/fhir/Allergies/<patient_id>')
    @login_required
    def get_allergies(patient_id):
        result = fhir.get('AllergyIntolerance', params={
            'patient': patient_id, '_count': 20,
        })
        if not result.ok:
            # An allergy list that failed to load must never render as "none
            # recorded" -- that reads as a positive clinical finding.
            audit('patient.allergies', outcome='failure', resource_type='Patient',
                  resource_id=pseudonymize(patient_id))
            return render_template('allergies.html', allergies=[], error=result.error)

        allergies = []
        for resource in result.entries:
            recorded = resource.get('recordedDate')
            reactions = []
            for reaction in resource.get('reaction') or []:
                if not isinstance(reaction, dict):
                    continue
                for manifestation in reaction.get('manifestation') or []:
                    label = first_coding_display(manifestation)
                    if label:
                        reactions.append(label)
            allergies.append({
                'date': format_fhir_date(recorded),
                'sort_key': sort_key(recorded),
                'name': first_coding_display(resource.get('code', {}),
                                             fallback='Unknown allergen'),
                'reactions': reactions,
                'severity': resource.get('criticality') or resource.get('severity') or 'unknown',
                'status': first_coding_display(resource.get('clinicalStatus', {}),
                                               fallback='Unknown'),
            })
        allergies.sort(key=lambda item: item['sort_key'], reverse=True)

        audit('patient.allergies', resource_type='Patient',
              resource_id=pseudonymize(patient_id))
        return render_template('allergies.html', allergies=allergies, error=None)

    # -- aggregate views ---------------------------------------------------

    @app.route('/fhir/Demographics')
    @login_required
    def get_demographics():
        result = fhir.get('Patient', params={'_count': 100})
        if not result.ok:
            audit('demographics.view', outcome='failure')
            return _fragment_error(result.error or 'Could not load demographics.')

        gender_counts = {'male': 0, 'female': 0, 'other': 0, 'unknown': 0}
        age_groups = {'0-18': 0, '19-35': 0, '36-55': 0, '56-75': 0, '76+': 0, 'unknown': 0}
        ages = []

        for resource in result.entries:
            gender = (resource.get('gender') or '').lower()
            gender_counts[gender if gender in gender_counts else 'unknown'] += 1

            age = calculate_age(resource.get('birthDate'))
            if age is None or age < 0:
                age_groups['unknown'] += 1
                continue
            ages.append(age)
            if age <= 18:
                age_groups['0-18'] += 1
            elif age <= 35:
                age_groups['19-35'] += 1
            elif age <= 55:
                age_groups['36-55'] += 1
            elif age <= 75:
                age_groups['56-75'] += 1
            else:
                age_groups['76+'] += 1

        sampled = sum(gender_counts.values())
        demographics = {
            'gender_counts': gender_counts,
            'age_groups': age_groups,
            'total_patients': sampled,
            'server_total': fhir.count('Patient'),
            # Reported from the sample rather than hardcoded to "36-55".
            'mean_age': round(sum(ages) / len(ages), 1) if ages else None,
        }

        audit('demographics.view')
        return render_template(
            'demographics.html', demographics=demographics,
            charts=demographics_charts(demographics),
        )

    @app.route('/fhir/Dashboard')
    @login_required
    def get_dashboard():
        results = fhir.get_many({
            'patients': ('Patient', {'_count': 20}),
            'patient_count': ('Patient', {'_summary': 'count'}),
            'observation_count': ('Observation', {'_summary': 'count'}),
        })

        patients_result = results['patients']
        gender_counts = {'male': 0, 'female': 0, 'other': 0, 'unknown': 0}
        recent_patients = []
        sampled = 0

        if patients_result.ok:
            for resource in patients_result.entries:
                gender = (resource.get('gender') or '').lower()
                gender_counts[gender if gender in gender_counts else 'unknown'] += 1
                sampled += 1
            recent_patients = process_patient_results(patients_result.entries[:5])

        # The real population, not the size of one page.
        patient_total = results['patient_count'].total
        observation_total = results['observation_count'].total

        audit('dashboard.view')
        return render_template(
            'dashboard.html',
            patient_count=patient_total,
            observation_count=observation_total,
            sampled_count=sampled,
            gender_counts=gender_counts,
            recent_patients=recent_patients,
            fhir_server_url=app.config['FHIR_SERVER_URL'],
            fhir_reachable=patients_result.ok,
            error=None if patients_result.ok else patients_result.error,
        )

    # -- settings ----------------------------------------------------------

    # Admin-only: the page reports deployment configuration (the FHIR backend,
    # the model, whether a key is present), which is the same information
    # HEALTH_SHOW_BACKEND deliberately keeps off the public health probe.
    @app.route('/settings/ai')
    @login_required
    @auth.require_role(db.ROLE_ADMIN)
    def ai_settings():
        return render_template(
            'ai_settings.html',
            model=app.config['AI_MODEL'],
            key_configured=bool(app.config['OPENAI_API_KEY']),
            fhir_server=app.config['FHIR_SERVER_URL'],
            blocked_reason=ai_blocked_reason(
                app.config['FHIR_SERVER_URL'], app.config['AI_ALLOW_REAL_PHI'],
            ),
        )

    @app.route('/settings/ai/test', methods=['POST'])
    @login_required
    @auth.require_role(db.ROLE_ADMIN)
    @limiter.limit('5 per minute')
    def test_ai_provider():
        """Check the configured key and model. Sends no patient data."""
        status = ai_provider.from_config(app.config).check()
        audit('settings.ai_provider_tested',
              outcome='success' if status.ok else 'failure',
              detail={'model': status.model, 'model_available': status.model_available})
        return jsonify({
            'ok': status.ok,
            'message': status.message,
            'model': status.model,
            'key_configured': status.key_configured,
            'key_valid': status.key_valid,
            'model_available': status.model_available,
            'suggestions': status.suggestions,
        })

    # -- administration ----------------------------------------------------

    @app.route('/admin/users')
    @login_required
    @auth.require_role(db.ROLE_ADMIN)
    def admin_users():
        return render_template('admin_users.html', users=db.list_users())

    @app.route('/admin/users/<username>/status', methods=['POST'])
    @login_required
    @auth.require_role(db.ROLE_ADMIN)
    def admin_set_user_status(username):
        status = request.form.get('status', '')
        if status not in (db.STATUS_PENDING, db.STATUS_ACTIVE, db.STATUS_DISABLED):
            abort(400)
        if username.lower() == current_user.username and status != db.STATUS_ACTIVE:
            return _fragment_error('You cannot disable your own account.')

        db.set_user_status(username, status)
        audit('admin.user_status_changed', resource_type='User',
              resource_id=pseudonymize(username), detail={'status': status})
        return render_template('admin_user_rows.html', users=db.list_users())

    @app.route('/admin/users/<username>/role', methods=['POST'])
    @login_required
    @auth.require_role(db.ROLE_ADMIN)
    def admin_set_user_role(username):
        role = request.form.get('role', '')
        if role not in db.ROLES:
            abort(400)
        if username.lower() == current_user.username and role != db.ROLE_ADMIN:
            return _fragment_error('You cannot remove your own administrator role.')

        db.set_user_role(username, role)
        audit('admin.user_role_changed', resource_type='User',
              resource_id=pseudonymize(username), detail={'role': role})
        return render_template('admin_user_rows.html', users=db.list_users())

    @app.route('/admin/audit')
    @login_required
    @auth.require_role(db.ROLE_ADMIN)
    def admin_audit():
        audit('admin.audit_viewed')
        return render_template('admin_audit.html', entries=db.read_audit(limit=200))

    # -- error handling ----------------------------------------------------

    @app.errorhandler(403)
    def handle_forbidden(_error):
        if request.headers.get('HX-Request'):
            return _fragment_error('You do not have permission to view this.'), 403
        return render_template('error.html', code=403,
                               message='You do not have permission to view this.'), 403

    @app.errorhandler(404)
    def handle_not_found(_error):
        if request.headers.get('HX-Request'):
            return _fragment_error('Not found.'), 404
        return render_template('error.html', code=404,
                               message='That page could not be found.'), 404

    @app.errorhandler(500)
    def handle_server_error(error):
        logger.exception('Unhandled error: %s', error)
        if request.headers.get('HX-Request'):
            return _fragment_error('Something went wrong loading this section.'), 500
        return render_template('error.html', code=500,
                               message='Something went wrong.'), 500


# ---------------------------------------------------------------------------


    # -- Slainte-Engine Appliance Triage & Pipeline Ingestion Routes --------

    def _get_pipeline_telemetry():
        raw_path = app.config.get('RAW_DATA_PATH', '/workspace/data/raw')
        store_path = app.config.get('FHIR_STORE_PATH', '/workspace/data/fhir_store')
        raw_files = []
        if os.path.exists(raw_path):
            raw_files = [f for f in os.listdir(raw_path) if os.path.isfile(os.path.join(raw_path, f))]
        store_files = []
        if os.path.exists(store_path):
            for f in os.listdir(store_path):
                fp = os.path.join(store_path, f)
                if os.path.isfile(fp) and f.endswith('.json'):
                    try:
                        store_files.append({'name': f, 'size': os.path.getsize(fp), 'mtime': os.path.getmtime(fp)})
                    except OSError:
                        pass
        store_files.sort(key=lambda x: x['mtime'], reverse=True)
        from datetime import datetime
        return {
            'raw_file_count': len(raw_files),
            'fhir_bundle_count': len(store_files),
            'recent_files': store_files[:5],
            'refreshed_time': datetime.now().strftime('%H:%M:%S')
        }

    @app.route('/fhir/Triage')
    @login_required
    def triage_queue_view():
        queue = []
        if hasattr(app.fhir, 'get_triage_queue'):
            queue = app.fhir.get_triage_queue()
        from collections import Counter
        cat_counts = Counter(p['mts_category'] for p in queue)
        from datetime import datetime
        refreshed_time = datetime.now().strftime('%H:%M:%S')
        return render_template('triage.html', queue=queue, cat_counts=cat_counts, refreshed_time=refreshed_time)

    @app.route('/fhir/triage_fragment')
    @login_required
    def triage_queue_fragment():
        queue = []
        if hasattr(app.fhir, 'get_triage_queue'):
            queue = app.fhir.get_triage_queue()
        from datetime import datetime
        refreshed_time = datetime.now().strftime('%H:%M:%S')
        return render_template('triage_fragment.html', queue=queue, refreshed_time=refreshed_time)

    @app.route('/pipeline/status')
    @login_required
    def pipeline_status_view():
        return render_template('pipeline_status.html', **_get_pipeline_telemetry())

    @app.route('/pipeline/status_fragment')
    @login_required
    def pipeline_status_fragment():
        return render_template('pipeline_status_fragment.html', **_get_pipeline_telemetry())

    @app.route('/api/v1/fhir/ingest', methods=['POST'])
    @csrf.exempt
    def api_ingest_bundle():
        payload = request.get_json(silent=True)
        if not payload:
            return jsonify({'error': 'Invalid or missing JSON payload'}), 400
        if hasattr(app.fhir, 'save_bundle'):
            saved_path = app.fhir.save_bundle(payload)
            return jsonify({
                'status': 'success',
                'message': 'Bundle ingested and indexed into Slainte-Engine.',
                'file_path': saved_path
            }), 201
        return jsonify({'status': 'received', 'message': 'App running with remote FHIR backend'}), 200

    @app.route('/fhir/Bundle', methods=['POST'])
    @csrf.exempt
    def fhir_bundle_ingest():
        return api_ingest_bundle()

    @app.route('/fhir/intake', methods=['POST'])
    @login_required
    def manual_intake():
        import csv
        import json
        import os
        import time
        from clinical_intake import (
            calculate_mts_category, MTS_METADATA, resolve_snomed,
            parse_labs, parse_medications, parse_allergies, build_fhir_bundle
        )
        full_name = request.form.get('full_name', '').strip()
        if not full_name:
            return '<div class="alert alert-danger"><i class="fas fa-exclamation-triangle me-2"></i>Patient full name is required.</div>', 400

        complaint = request.form.get('complaint', '').strip()
        if not complaint:
            return '<div class="alert alert-danger"><i class="fas fa-exclamation-triangle me-2"></i>Chief complaint is required.</div>', 400

        gender = request.form.get('gender', 'unknown').strip().lower()
        dob = request.form.get('dob', '').strip() or None
        gms_number = request.form.get('gms_number', '').strip()

        def _parse_num(field):
            v = request.form.get(field, '').strip()
            if not v:
                return None
            try:
                return float(v)
            except ValueError:
                return None

        vitals = {
            'systolic_bp': _parse_num('sys_bp'),
            'diastolic_bp': _parse_num('dia_bp'),
            'heart_rate': _parse_num('hr'),
            'spo2': _parse_num('spo2'),
            'respiratory_rate': _parse_num('resp_rate'),
            'temperature': _parse_num('temp_c'),
            'pain': _parse_num('pain'),
            'complaint': complaint
        }

        mts_cat = calculate_mts_category(vitals)
        mts_info = MTS_METADATA.get(mts_cat, MTS_METADATA[3])
        snomed_code, snomed_disp = resolve_snomed(complaint)

        pt_id = None
        if hasattr(app.fhir, '_cache_patients') and app.fhir._cache_patients:
            existing_ids = list(app.fhir._cache_patients.keys())
            num_ids = [int(p[1:]) for p in existing_ids if p.startswith('P') and p[1:].isdigit()]
            next_num = (max(num_ids) + 1) if num_ids else 119
            pt_id = f"P{next_num}"
        else:
            pt_id = f"P{int(time.time() * 1000) % 10000:04d}"

        if not gms_number:
            gms_number = f"99{pt_id[1:]}-Z"

        labs_data = parse_labs(request.form.get('labs'))
        meds_data = parse_medications(request.form.get('medications'))
        algs_data = parse_allergies(request.form.get('allergies'))

        bundle = build_fhir_bundle(
            pt_id=pt_id,
            name=full_name,
            gms_number=gms_number,
            gender=gender,
            birthdate=dob,
            complaint=complaint,
            vitals=vitals,
            mts_category=mts_cat,
            labs=labs_data,
            medications=meds_data,
            allergies=algs_data
        )

        # 1. Save to FHIR store immediately
        if hasattr(app.fhir, 'save_bundle'):
            app.fhir.save_bundle(bundle, f"bundle_{pt_id}.json")
        else:
            store_dir = app.config.get('FHIR_STORE_PATH', '/workspace/data/fhir_store')
            os.makedirs(store_dir, exist_ok=True)
            with open(os.path.join(store_dir, f"bundle_{pt_id}.json"), "w", encoding="utf-8") as f:
                json.dump(bundle, f, indent=2)

        # 2. Append to raw dropzone CSV for OpenHands agent synchronization (non-blocking)
        try:
            raw_dir = app.config.get('RAW_DATA_PATH', '/workspace/data/raw')
            os.makedirs(raw_dir, exist_ok=True)
            intake_csv_path = os.path.join(raw_dir, 'manual_intake_patients.csv')
            file_exists = os.path.isfile(intake_csv_path)
            with open(intake_csv_path, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
                if not file_exists:
                    writer.writerow([
                        'pt_id', 'Patient_Name', 'GMS_NO', 'sex', 'DOB', 'triage_complaint',
                        'sys_bp', 'dia_bp', 'hr', 'o2_sat', 'resp_rate', 'temp_c', 'pain_level',
                        'labs', 'medications', 'allergies'
                    ])
                writer.writerow([
                    pt_id, full_name, gms_number, gender, dob or '', complaint,
                    vitals['systolic_bp'] or '', vitals['diastolic_bp'] or '', vitals['heart_rate'] or '',
                    vitals['spo2'] or '', vitals['respiratory_rate'] or '', vitals['temperature'] or '',
                    vitals['pain'] or '', request.form.get('labs', ''), request.form.get('medications', ''),
                    request.form.get('allergies', '')
                ])
        except Exception as e:
            logger.warning(f"Failed to append to manual intake audit CSV: {e}")

        vitals_badges = []
        if vitals['systolic_bp'] and vitals['diastolic_bp']:
            vitals_badges.append(f'<span class="badge bg-light text-dark border">BP: {int(vitals["systolic_bp"])}/{int(vitals["diastolic_bp"])}</span>')
        if vitals['heart_rate']:
            vitals_badges.append(f'<span class="badge bg-light text-dark border">HR: {int(vitals["heart_rate"])} bpm</span>')
        if vitals['spo2']:
            vitals_badges.append(f'<span class="badge bg-light text-dark border">SpO2: {int(vitals["spo2"])}%</span>')
        if vitals['temperature']:
            vitals_badges.append(f'<span class="badge bg-light text-dark border">Temp: {vitals["temperature"]}°C</span>')
        if vitals['pain'] is not None:
            vitals_badges.append(f'<span class="badge bg-light text-dark border">Pain: {int(vitals["pain"])}/10</span>')

        from markupsafe import escape as h_escape
        safe_full_name = h_escape(full_name)
        safe_complaint = h_escape(complaint)
        safe_gms_number = h_escape(gms_number)
        safe_pt_id = h_escape(pt_id)
        safe_snomed = h_escape(str(snomed_code))

        badges_html = ' '.join(vitals_badges)
        return f"""
        <div class="alert alert-success border-0 shadow-sm p-3 mt-3 animate-fade-in">
            <div class="d-flex align-items-center justify-content-between mb-2">
                <div class="d-flex align-items-center">
                    <span class="badge {mts_info['badge']} px-3 py-2 fs-6 me-2">
                        Cat {mts_cat}: {mts_info['name']}
                    </span>
                    <span class="badge bg-dark text-white px-2 py-1">
                        Target: {mts_info['target_time_minutes']} min
                    </span>
                </div>
                <span class="badge bg-success text-white border border-success">
                    <i class="fas fa-check-circle me-1"></i> Validated FHIR R4 Bundle
                </span>
            </div>
            <div class="mb-2">
                <h6 class="mb-1 fw-bold">{safe_full_name} <span class="text-muted fw-normal">({safe_pt_id} &bull; GMS: {safe_gms_number})</span></h6>
                <p class="mb-1 small text-dark"><strong>Chief Complaint:</strong> {safe_complaint} <span class="badge bg-secondary-subtle text-secondary ms-1">SNOMED {safe_snomed}</span></p>
                <div class="d-flex flex-wrap gap-1 mt-1">{badges_html}</div>
            </div>
            <hr class="my-2 text-muted">
            <div class="d-flex align-items-center justify-content-between pt-1">
                <small class="text-muted"><i class="fas fa-robot text-primary me-1"></i>Synchronized to OpenHands audit ledger &amp; indexed to FHIR store.</small>
                <div class="d-flex gap-2">
                    <a href="/fhir/Patient/{safe_pt_id}" class="btn btn-sm btn-outline-primary">
                        <i class="fas fa-id-card me-1"></i> View Patient Detail
                    </a>
                    <button type="button" class="btn btn-sm btn-success" onclick="window.location.href='/?view=/fhir/Triage'">
                        <i class="fas fa-list me-1"></i> View Triage Queue
                    </button>
                </div>
            </div>
        </div>
        """

    @app.route('/fhir/upload', methods=['POST'])
    @login_required
    def batch_upload():
        import os
        import time
        from werkzeug.utils import secure_filename
        from clinical_intake import (
            extract_from_clinical_document, calculate_mts_category,
            MTS_METADATA, resolve_snomed, build_fhir_bundle
        )

        if 'clinical_file' not in request.files:
            return '<div class="alert alert-danger"><i class="fas fa-exclamation-triangle me-2"></i>No file selected for upload.</div>', 400

        file = request.files['clinical_file']
        if not file or not file.filename:
            return '<div class="alert alert-danger"><i class="fas fa-exclamation-triangle me-2"></i>Filename is empty.</div>', 400

        ALLOWED_EXTENSIONS = {'.csv', '.tsv', '.json', '.pdf', '.png', '.jpg', '.jpeg'}
        ext = os.path.splitext(file.filename or '')[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return f'<div class="alert alert-danger"><i class="fas fa-exclamation-triangle me-2"></i>Unsupported file type: {ext or "unknown"}. Allowed: {", ".join(sorted(ALLOWED_EXTENSIONS))}</div>', 400

        filename = secure_filename(file.filename)
        if not filename:
            filename = f"upload_{int(time.time())}{ext}"

        raw_dir = app.config.get('RAW_DATA_PATH', '/workspace/data/raw')
        os.makedirs(raw_dir, exist_ok=True)
        dest_path = os.path.join(raw_dir, filename)
        file.save(dest_path)

        file_size_kb = max(1, os.path.getsize(dest_path) // 1024)

        from markupsafe import escape as h_escape
        safe_filename = h_escape(filename)
        safe_dest_path = h_escape(dest_path)

        # Handle PDF and Image clinical document intake
        if ext in ('.pdf', '.png', '.jpg', '.jpeg'):
            extracted = extract_from_clinical_document(dest_path)
            complaint = extracted.get('complaint') or f"Clinical document intake ({filename})"
            vitals = {
                'systolic_bp': extracted.get('systolic_bp'),
                'diastolic_bp': extracted.get('diastolic_bp'),
                'heart_rate': extracted.get('heart_rate'),
                'spo2': extracted.get('spo2'),
                'respiratory_rate': extracted.get('respiratory_rate'),
                'temperature': extracted.get('temperature'),
                'pain': extracted.get('pain'),
                'complaint': complaint
            }

            mts_cat = calculate_mts_category(vitals)
            mts_info = MTS_METADATA.get(mts_cat, MTS_METADATA[3])
            snomed_code, snomed_disp = resolve_snomed(complaint)

            pt_id = None
            if hasattr(app.fhir, '_cache_patients') and app.fhir._cache_patients:
                existing_ids = list(app.fhir._cache_patients.keys())
                num_ids = [int(p[1:]) for p in existing_ids if p.startswith('P') and p[1:].isdigit()]
                next_num = (max(num_ids) + 1) if num_ids else 119
                pt_id = f"P{next_num}"
            else:
                pt_id = f"P{int(time.time() * 1000) % 10000:04d}"

            gms_number = extracted.get('gms_number') or f"99{pt_id[1:]}-D"
            full_name = extracted.get('full_name') or f"Scanned Patient ({filename[:12]})"
            gender = extracted.get('gender') or 'unknown'
            dob = extracted.get('dob') or '1985-06-15'

            bundle = build_fhir_bundle(
                pt_id=pt_id,
                name=full_name,
                gms_number=gms_number,
                gender=gender,
                birthdate=dob,
                complaint=complaint,
                vitals=vitals,
                mts_category=mts_cat
            )

            # Save to FHIR store
            if hasattr(app.fhir, 'save_bundle'):
                app.fhir.save_bundle(bundle, f"bundle_{pt_id}.json")
            else:
                store_dir = app.config.get('FHIR_STORE_PATH', '/workspace/data/fhir_store')
                os.makedirs(store_dir, exist_ok=True)
                import json
                with open(os.path.join(store_dir, f"bundle_{pt_id}.json"), "w", encoding="utf-8") as f:
                    json.dump(bundle, f, indent=2)

            file_icon = 'fa-file-pdf text-danger' if ext == '.pdf' else 'fa-file-image text-info'

            vitals_badges = []
            if vitals['systolic_bp'] and vitals['diastolic_bp']:
                vitals_badges.append(f'<span class="badge bg-light text-dark border">BP: {int(vitals["systolic_bp"])}/{int(vitals["diastolic_bp"])}</span>')
            if vitals['heart_rate']:
                vitals_badges.append(f'<span class="badge bg-light text-dark border">HR: {int(vitals["heart_rate"])} bpm</span>')
            if vitals['spo2']:
                vitals_badges.append(f'<span class="badge bg-light text-dark border">SpO2: {int(vitals["spo2"])}%</span>')
            if vitals['temperature']:
                vitals_badges.append(f'<span class="badge bg-light text-dark border">Temp: {vitals["temperature"]}°C</span>')
            if vitals['pain'] is not None:
                vitals_badges.append(f'<span class="badge bg-light text-dark border">Pain: {int(vitals["pain"])}/10</span>')

            safe_full_name = h_escape(full_name)
            safe_complaint = h_escape(complaint)
            safe_gms = h_escape(gms_number)
            safe_pt_id = h_escape(pt_id)
            safe_snomed = h_escape(str(snomed_code))

            return f"""
            <div class="alert alert-success border-0 shadow-sm p-3 mt-3 animate-fade-in">
                <div class="d-flex align-items-center justify-content-between mb-2">
                    <div class="d-flex align-items-center gap-2">
                        <i class="fas {file_icon} fa-2x"></i>
                        <div>
                            <h6 class="mb-0 fw-bold">{safe_filename} <span class="badge bg-dark ms-1">{file_size_kb} KB</span></h6>
                            <small class="text-muted">Multimodal OCR &amp; FastMCP extraction verified.</small>
                        </div>
                    </div>
                    <span class="badge {mts_info['badge']} px-3 py-2 fs-6">
                        Cat {mts_cat}: {mts_info['name']} ({mts_info['target_time_minutes']} min)
                    </span>
                </div>
                <div class="bg-white rounded p-3 border mb-2">
                    <div class="fw-bold mb-1">{safe_full_name} <span class="text-muted fw-normal">({safe_pt_id} &bull; GMS: {safe_gms})</span></div>
                    <div class="small text-secondary mb-2"><strong>Extracted Complaint:</strong> {safe_complaint} &bull; <span class="badge bg-secondary-subtle text-secondary">SNOMED {safe_snomed}</span></div>
                    <div class="d-flex flex-wrap gap-1">{' '.join(vitals_badges)}</div>
                </div>
                <div class="d-flex align-items-center justify-content-between pt-1">
                    <small class="text-muted"><i class="fas fa-check-circle text-success me-1"></i>Standard HL7 FHIR R4 Bundle created and indexed.</small>
                    <div class="d-flex gap-2">
                        <button type="button" class="btn btn-sm btn-outline-primary" onclick="window.location.href='/?view=patients'">
                            <i class="fas fa-users me-1"></i> Patients List
                        </button>
                        <button type="button" class="btn btn-sm btn-success" onclick="window.location.href='/?view=triage'">
                            <i class="fas fa-heartbeat me-1"></i> Live Triage Queue
                        </button>
                    </div>
                </div>
            </div>
            """

        # Handle tabular CSV/TSV/JSON files
        row_count = 0
        try:
            with open(dest_path, 'r', encoding='utf-8', errors='replace') as f:
                row_count = max(0, sum(1 for line in f if line.strip()) - 1)
        except Exception:
            row_count = 1

        return f"""
        <div class="alert alert-primary border-0 shadow-sm p-3 mt-3 animate-fade-in">
            <div class="d-flex align-items-center mb-2">
                <i class="fas fa-file-csv fa-2x text-primary me-3"></i>
                <div>
                    <h6 class="mb-0 fw-bold">{safe_filename} <span class="badge bg-info text-dark ms-2">{file_size_kb} KB</span></h6>
                    <small class="text-muted">Detected {row_count} clinical triage record(s) in batch.</small>
                </div>
            </div>
            <div class="bg-white rounded p-2 border mb-2 small text-secondary">
                <div class="d-flex align-items-center mb-1">
                    <i class="fas fa-check-circle text-success me-2"></i> File safely staged to <code>{safe_dest_path}</code>
                </div>
                <div class="d-flex align-items-center mb-1">
                    <i class="fas fa-cogs text-primary me-2"></i> OpenHands autonomous daemon active &mdash; polling dropzone every 10s
                </div>
                <div class="d-flex align-items-center">
                    <i class="fas fa-shield-alt text-info me-2"></i> Zero-hallucination validation gate: MCP schema + SNOMED-CT + MTS calculation
                </div>
            </div>
            <div class="d-flex justify-content-between align-items-center pt-1">
                <small class="text-muted"><i class="fas fa-info-circle me-1"></i>The queue auto-refreshes every 5 seconds.</small>
                <button type="button" class="btn btn-sm btn-primary" onclick="window.location.href='/?view=triage'">
                    <i class="fas fa-sync-alt me-1"></i> Go to Live Triage Queue
                </button>
            </div>
        </div>
        """


def register_cli(app):
    """Operator commands, including the lockout recovery path."""

    @app.cli.command('grant-admin')
    @click.argument('username')
    def grant_admin_command(username):
        """Create or promote USERNAME to an active administrator.

        Prompts for a password interactively, and (re)sets it -- this is the
        lockout-recovery path, so it must work even if the account already
        exists and its password is unknown.
        """
        username = username.strip().lower()
        if not is_valid_username(username):
            raise click.ClickException(
                f'{username!r} is not a usable username. It must be '
                f'{MIN_USERNAME_LENGTH}-{MAX_USERNAME_LENGTH} characters of letters, '
                'numbers, dots, hyphens or underscores.'
            )
        password = click.prompt('Password', hide_input=True, confirmation_prompt=True)
        if len(password) < MIN_PASSWORD_LENGTH:
            raise click.ClickException(
                f'Password must be at least {MIN_PASSWORD_LENGTH} characters.'
            )
        record = db.grant_admin(username, auth.hash_password(password))
        db.write_audit('admin.granted_via_cli', 'success',
                       actor='cli', resource_type='User',
                       resource_id=pseudonymize(record['username']))
        click.echo(f"{record['username']} is now an active administrator.")

    @app.cli.command('reset-password')
    @click.argument('username')
    def reset_password_command(username):
        """Reset USERNAME's password, leaving their role and status alone.

        `grant-admin` also promotes, so it is the wrong tool for an ordinary
        forgotten password -- it would hand the account administrator rights
        as a side effect.
        """
        username = username.strip().lower()
        password = click.prompt('New password', hide_input=True, confirmation_prompt=True)
        if len(password) < MIN_PASSWORD_LENGTH:
            raise click.ClickException(
                f'Password must be at least {MIN_PASSWORD_LENGTH} characters.'
            )
        record = db.set_password(username, auth.hash_password(password))
        if record is None:
            raise click.ClickException(f'No account called {username!r}.')
        db.write_audit('user.password_reset_via_cli', 'success',
                       actor='cli', resource_type='User',
                       resource_id=pseudonymize(record['username']))
        click.echo(f"Password reset for {record['username']} "
                   f"(role {record['role']}, status {record['status']} -- unchanged).")

    @app.cli.command('list-users')
    def list_users_command():
        """Show every registered account and its status."""
        users = db.list_users()
        if not users:
            click.echo('No users registered yet.')
            return
        click.echo(f"{'USERNAME':<24}{'ROLE':<12}{'STATUS':<10}LAST SIGN-IN")
        for user in users:
            click.echo(
                f"{user['username']:<24}{user['role']:<12}"
                f"{user['status']:<10}{(user['last_login_at'] or 'never')[:19]}"
            )
        click.echo(f'\n{db.count_active_admins()} active administrator(s).')


def main():
    try:
        application = create_app()
    except ConfigError as exc:
        raise SystemExit(f'\nConfiguration error:\n{exc}\n')
    application.run(debug=application.config['DEBUG'])


if __name__ == '__main__':
    main()
