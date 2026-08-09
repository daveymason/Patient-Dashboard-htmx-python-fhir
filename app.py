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
    'temperature': ('8310-5', 'Temperature', '°C'),
    'respiratory_rate': ('9279-1', 'Respiratory Rate', '/min'),
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
            'patients': '/fhir/Patients',
            'stats': '/fhir/Demographics',
            'insights': '/fhir/Dashboard',
        }
        initial_view = views.get(request.args.get('view', ''), views['patients'])
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
