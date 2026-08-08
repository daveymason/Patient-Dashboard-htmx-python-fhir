"""FHIR Patient Dashboard.

Flask + HTMX viewer over a FHIR R4 server. Routes return HTML fragments that
HTMX swaps into the page, so there is no client-side framework and no build step.
"""
import logging

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
from config import (
    ConfigError, is_valid_wallet_address, resolve_secret_key, select_config
)
from fhir_client import FhirClient
from fhir_utils import (
    calculate_age, first_coding_display, format_address, format_fhir_date,
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
    'blood_pressure': ('85354-9', 'Blood Pressure'),
    'heart_rate': ('8867-4', 'Heart Rate'),
    'temperature': ('8310-5', 'Temperature'),
    'respiratory_rate': ('9279-1', 'Respiratory Rate'),
}


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
                'flask --app app grant-admin <wallet-address>'
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
        return render_template('index.html')

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

    @app.route('/login')
    def login():
        if current_user.is_authenticated:
            return redirect(url_for('index'))
        return render_template('login.html')

    @app.route('/get_nonce_to_sign/<wallet_address>')
    @limiter.limit(auth_limit)
    def get_nonce_to_sign(wallet_address):
        if not is_valid_wallet_address(wallet_address):
            return jsonify({'error': 'Invalid wallet address format.'}), 400
        nonce = auth.issue_nonce(wallet_address)
        audit('login.nonce_issued', resource_type='Wallet',
              resource_id=pseudonymize(wallet_address))
        return jsonify({'nonce': nonce})

    @app.route('/verify_signature', methods=['POST'])
    @limiter.limit(auth_limit)
    def verify_signature():
        data = request.get_json(silent=True) or {}
        signature = data.get('signed_message')
        wallet_address = data.get('wallet_address')

        if not signature or not wallet_address:
            return jsonify({'success': False,
                            'error': 'Missing required parameters.'}), 400
        if not is_valid_wallet_address(wallet_address):
            return jsonify({'success': False,
                            'error': 'Invalid wallet address format.'}), 400

        wallet_address = wallet_address.strip().lower()
        pseudo = pseudonymize(wallet_address)

        # The nonce is taken from the session, never from the request body --
        # accepting a client-supplied nonce would let a caller replay one.
        nonce = auth.consume_nonce(wallet_address)
        if nonce is None:
            audit('login.denied', outcome='failure', resource_type='Wallet',
                  resource_id=pseudo, detail={'reason': 'no_pending_nonce'})
            return jsonify({'success': False,
                            'error': 'No sign-in challenge is pending. Please try again.'}), 400

        recovered = auth.recover_signer(nonce, signature)
        if recovered != wallet_address:
            audit('login.denied', outcome='failure', resource_type='Wallet',
                  resource_id=pseudo, detail={'reason': 'signature_mismatch'})
            return jsonify({'success': False,
                            'error': 'Signature verification failed.'}), 401

        # Signature proves control of the wallet. It does not grant access --
        # the users table decides that.
        record = db.get_user(wallet_address) or db.register_pending_user(wallet_address)

        if record['status'] != db.STATUS_ACTIVE:
            audit('login.denied', outcome='denied', resource_type='Wallet',
                  resource_id=pseudo, detail={'reason': f"account_{record['status']}"})
            message = ('This wallet is not yet approved for access. '
                       'An administrator must activate it.')
            if record['status'] == db.STATUS_DISABLED:
                message = 'This account has been disabled.'
            return jsonify({'success': False, 'error': message}), 403

        login_user(auth.User(record))
        db.record_login(wallet_address)
        audit('login.success', resource_type='Wallet', resource_id=pseudo)
        return jsonify({'success': True, 'message': 'Signed in.'})

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
        result = fhir.get('Patient', params={'_count': page_size})
        if not result.ok:
            audit('patient.list', outcome='failure')
            return render_template('patients.html', patients=[], error=result.error,
                                   total=None, next_url=None)

        audit('patient.list')
        return render_template(
            'patients.html',
            patients=process_patient_results(result.entries),
            error=None,
            total=fhir.count('Patient'),
            next_url=_bundle_next_link(result),
        )

    @app.route('/fhir/Patients/page')
    @login_required
    def get_patients_page():
        """Follow a Bundle `next` link. The URL is validated against the
        configured server inside the client, so it cannot be pointed elsewhere."""
        result = fhir.get_absolute(request.args.get('url', ''))
        if not result.ok:
            return _fragment_error(result.error or 'Could not load the next page.', columns=7)
        audit('patient.list', detail={'page': 'next'})
        return render_template(
            'patient_table_body.html',
            patients=process_patient_results(result.entries),
            error=None,
            next_url=_bundle_next_link(result),
        )

    @app.route('/fhir/search_patients', methods=['POST'])
    @login_required
    def search_patients():
        search_term = (request.form.get('q') or '').strip()
        page_size = app.config['FHIR_PAGE_SIZE']

        if not search_term:
            result = fhir.get('Patient', params={'_count': page_size})
        else:
            # Search name, identifier and ID together. The previous version only
            # queried name, so looking up a patient by ID reported "not found".
            params = {'_count': page_size}
            if search_term.replace('-', '').isalnum() and any(c.isdigit() for c in search_term):
                params['_id'] = search_term
                by_id = fhir.get('Patient', params=params)
                if by_id.ok and by_id.entries:
                    audit('patient.search', detail={'mode': 'id'})
                    return render_template('patient_table_body.html',
                                           patients=process_patient_results(by_id.entries),
                                           error=None, next_url=None)
            result = fhir.get('Patient', params={
                '_count': page_size, 'name:contains': search_term,
            })
            if result.ok and not result.entries:
                identifier_result = fhir.get('Patient', params={
                    '_count': page_size, 'identifier': search_term,
                })
                if identifier_result.ok and identifier_result.entries:
                    result = identifier_result

        if not result.ok:
            audit('patient.search', outcome='failure')
            return _fragment_error(result.error or 'Search failed.', columns=7)

        audit('patient.search', detail={'has_term': bool(search_term)})
        return render_template(
            'patient_table_body.html',
            patients=process_patient_results(result.entries),
            error=None,
            next_url=_bundle_next_link(result),
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
        # Four queries, run concurrently rather than one after another.
        spec = {
            key: ('Observation', {
                'patient': patient_id,
                'code': f'http://loinc.org|{code}',
                '_sort': '-date',
                '_count': 10,
            })
            for key, (code, _label) in VITAL_SIGN_CODES.items()
        }
        results = fhir.get_many(spec)

        vital_signs = []
        failures = []
        for key, (_code, label) in VITAL_SIGN_CODES.items():
            result = results.get(key)
            if result is None or not result.ok:
                failures.append(label)
                continue
            for resource in result.entries:
                effective = resource.get('effectiveDateTime') or resource.get('issued')
                value, unit = observation_value(resource)
                if value == '':
                    continue
                vital_signs.append({
                    'date': format_fhir_date(effective),
                    'sort_key': sort_key(effective),
                    'type': label,
                    'value': value,
                    'unit': unit,
                    'status': resource.get('status', 'unknown'),
                })

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

        return render_template('vital_signs.html', vital_signs=vital_signs, error=error)

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
        return render_template('demographics.html', demographics=demographics)

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

    @app.route('/settings/ai')
    @login_required
    def ai_settings():
        return render_template('ai_settings.html')

    @app.route('/test_openai_key', methods=['POST'])
    @login_required
    @limiter.limit('5 per minute')
    def test_openai_key():
        api_key = (request.get_json(silent=True) or {}).get('api_key')
        if not api_key:
            return jsonify({'success': False, 'message': 'An API key is required.'}), 400

        try:
            import openai
        except ImportError:
            return jsonify({'success': False,
                            'message': 'The openai package is not installed.'}), 500

        try:
            openai.OpenAI(api_key=api_key).models.list()
        except openai.AuthenticationError:
            return jsonify({'success': False,
                            'message': 'That key was rejected as invalid or expired.'}), 401
        except openai.APIError as exc:
            logger.warning('OpenAI key test failed: %s', type(exc).__name__)
            return jsonify({'success': False,
                            'message': 'The AI provider returned an error.'}), 502
        except Exception:
            logger.exception('Unexpected error testing the OpenAI key')
            return jsonify({'success': False,
                            'message': 'The key could not be checked. Try again.'}), 500

        audit('settings.ai_key_tested')
        return jsonify({'success': True, 'message': 'Key accepted.'})

    # -- administration ----------------------------------------------------

    @app.route('/admin/users')
    @login_required
    @auth.require_role(db.ROLE_ADMIN)
    def admin_users():
        return render_template('admin_users.html', users=db.list_users())

    @app.route('/admin/users/<wallet_address>/status', methods=['POST'])
    @login_required
    @auth.require_role(db.ROLE_ADMIN)
    def admin_set_user_status(wallet_address):
        status = request.form.get('status', '')
        if status not in (db.STATUS_PENDING, db.STATUS_ACTIVE, db.STATUS_DISABLED):
            abort(400)
        if wallet_address.lower() == current_user.wallet_address and status != db.STATUS_ACTIVE:
            return _fragment_error('You cannot disable your own account.')

        db.set_user_status(wallet_address, status)
        audit('admin.user_status_changed', resource_type='User',
              resource_id=pseudonymize(wallet_address), detail={'status': status})
        return render_template('admin_user_rows.html', users=db.list_users())

    @app.route('/admin/users/<wallet_address>/role', methods=['POST'])
    @login_required
    @auth.require_role(db.ROLE_ADMIN)
    def admin_set_user_role(wallet_address):
        role = request.form.get('role', '')
        if role not in db.ROLES:
            abort(400)
        if wallet_address.lower() == current_user.wallet_address and role != db.ROLE_ADMIN:
            return _fragment_error('You cannot remove your own administrator role.')

        db.set_user_role(wallet_address, role)
        audit('admin.user_role_changed', resource_type='User',
              resource_id=pseudonymize(wallet_address), detail={'role': role})
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
    @click.argument('wallet_address')
    def grant_admin_command(wallet_address):
        """Make WALLET_ADDRESS an active administrator."""
        if not is_valid_wallet_address(wallet_address):
            raise click.ClickException(
                f'{wallet_address!r} is not a usable wallet address. It must be '
                '0x followed by 40 hex digits, and cannot be the zero address.'
            )
        record = db.grant_admin(wallet_address)
        db.write_audit('admin.granted_via_cli', 'success',
                       actor='cli', resource_type='User',
                       resource_id=pseudonymize(record['wallet_address']))
        click.echo(f"{record['wallet_address']} is now an active administrator.")

    @app.cli.command('list-users')
    def list_users_command():
        """Show every registered account and its status."""
        users = db.list_users()
        if not users:
            click.echo('No users registered yet.')
            return
        click.echo(f"{'WALLET':<44}{'ROLE':<12}{'STATUS':<10}LAST SIGN-IN")
        for user in users:
            click.echo(
                f"{user['wallet_address']:<44}{user['role']:<12}"
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
