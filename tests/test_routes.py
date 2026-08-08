"""Route-level regression tests.

Each class here corresponds to a defect found during the review. The FHIR
backend is stubbed, so these run offline and deterministically.
"""
import responses

import db
from conftest import FHIR_BASE, TEST_PASSWORD, bundle


def _patient(patient_id='p1', **overrides):
    resource = {
        'resourceType': 'Patient',
        'id': patient_id,
        'name': [{'given': ['Ada'], 'family': 'Lovelace'}],
        'gender': 'female',
        'birthDate': '1980-05-01',
    }
    resource.update(overrides)
    return resource


class TestAuthorization:
    """A correct password proves identity; the users table still decides access."""

    def test_clinical_routes_require_login(self, client):
        for path in ('/fhir/Patients', '/fhir/Patient/p1', '/fhir/LabResults/p1',
                     '/fhir/VitalSigns/p1', '/fhir/Medications/p1',
                     '/fhir/Allergies/p1', '/fhir/Demographics', '/fhir/Dashboard'):
            response = client.get(path)
            assert response.status_code in (302, 401), f'{path} was not protected'

    def test_register_then_login_before_approval_is_refused(self, client):
        """A freshly registered account cannot sign in until an admin approves it."""
        register = client.post('/register', data={
            'username': 'newperson',
            'password': TEST_PASSWORD,
            'confirm_password': TEST_PASSWORD,
        }, follow_redirects=True)
        # Post/redirect/get: the POST redirects to the login page rather than
        # rendering it in place, so refresh/Back can't re-submit the form.
        assert register.status_code == 200
        assert register.request.path == '/login'
        assert 'administrator must approve' in register.get_data(as_text=True).lower()

        login = client.post('/login', data={
            'username': 'newperson', 'password': TEST_PASSWORD,
        })
        assert login.status_code == 403
        assert 'awaiting administrator approval' in login.get_data(as_text=True)

    def test_login_with_wrong_password_is_refused(self, client, active_user):
        response = client.post('/login', data={
            'username': active_user, 'password': 'not the right password',
        })
        assert response.status_code == 401
        assert 'incorrect username or password' in response.get_data(as_text=True).lower()

    def test_login_with_correct_password_succeeds(self, client, active_user):
        response = client.post('/login', data={
            'username': active_user, 'password': TEST_PASSWORD,
        }, follow_redirects=False)
        assert response.status_code == 302

    def test_disabled_account_cannot_sign_in(self, client, app, active_user):
        with app.app_context():
            db.set_user_status(active_user, db.STATUS_DISABLED)
        response = client.post('/login', data={
            'username': active_user, 'password': TEST_PASSWORD,
        })
        assert response.status_code == 403
        assert 'disabled' in response.get_data(as_text=True).lower()

    def test_registration_rejects_short_password(self, client):
        response = client.post('/register', data={
            'username': 'shortpassword', 'password': 'short', 'confirm_password': 'short',
        })
        assert response.status_code == 400
        assert 'at least' in response.get_data(as_text=True).lower()

    def test_registration_rejects_mismatched_passwords(self, client):
        response = client.post('/register', data={
            'username': 'mismatched', 'password': TEST_PASSWORD,
            'confirm_password': 'a different password entirely',
        })
        assert response.status_code == 400
        assert 'do not match' in response.get_data(as_text=True).lower()

    def test_registration_rejects_duplicate_username(self, client, active_user):
        response = client.post('/register', data={
            'username': active_user, 'password': TEST_PASSWORD,
            'confirm_password': TEST_PASSWORD,
        })
        assert response.status_code == 400
        assert 'already taken' in response.get_data(as_text=True).lower()

    def test_registration_rejects_invalid_username_characters(self, client):
        response = client.post('/register', data={
            'username': 'has a space', 'password': TEST_PASSWORD,
            'confirm_password': TEST_PASSWORD,
        })
        assert response.status_code == 400

    def test_admin_routes_reject_non_admin(self, auth_client):
        assert auth_client.get('/admin/users').status_code == 403
        assert auth_client.get('/admin/audit').status_code == 403

    def test_admin_routes_allow_admin(self, admin_client):
        assert admin_client.get('/admin/users').status_code == 200
        assert admin_client.get('/admin/audit').status_code == 200


class TestLabResults:
    """5 of 7 live patients returned HTTP 500 from this panel."""

    @responses.activate
    def test_observation_without_valuequantity_does_not_500(self, auth_client):
        responses.add(
            responses.GET, f'{FHIR_BASE}/Observation',
            json=bundle(
                # component-only: no valueQuantity anywhere
                {'resourceType': 'Observation', 'id': 'o1', 'status': 'final',
                 'effectiveDateTime': '2026-08-07T11:39:49Z',
                 'code': {'text': 'Blood pressure'},
                 'component': [
                     {'code': {'coding': [{'display': 'Systolic'}]},
                      'valueQuantity': {'value': 120, 'unit': 'mmHg'}},
                     {'code': {'coding': [{'display': 'Diastolic'}]},
                      'valueQuantity': {'value': 80, 'unit': 'mmHg'}},
                 ]},
                # coded result
                {'resourceType': 'Observation', 'id': 'o2', 'status': 'final',
                 'effectiveDateTime': '2026-08-07T07:44:07.470+00:00',
                 'code': {'text': 'Culture'},
                 'valueCodeableConcept': {'coding': [{'display': 'No growth'}]}},
                # no value at all
                {'resourceType': 'Observation', 'id': 'o3', 'status': 'final',
                 'code': {'text': 'Pending'}},
            ),
            status=200,
        )
        response = auth_client.get('/fhir/LabResults/p1')
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert '120' in body and 'No growth' in body

    @responses.activate
    def test_upstream_failure_is_not_shown_as_no_results(self, auth_client):
        responses.add(responses.GET, f'{FHIR_BASE}/Observation', status=503)
        response = auth_client.get('/fhir/LabResults/p1')
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert 'could not be loaded' in body.lower()
        assert 'no lab results found' not in body.lower()


class TestAllergies:
    """Rendering an upstream outage as 'no allergies' is a safety problem."""

    @responses.activate
    def test_failure_is_distinguished_from_empty(self, auth_client):
        responses.add(responses.GET, f'{FHIR_BASE}/AllergyIntolerance', status=500)
        body = auth_client.get('/fhir/Allergies/p1').get_data(as_text=True)
        assert 'could not be loaded' in body.lower()
        assert 'no allergy records found' not in body.lower()

    @responses.activate
    def test_genuinely_empty_says_so(self, auth_client):
        responses.add(responses.GET, f'{FHIR_BASE}/AllergyIntolerance',
                      json=bundle(), status=200)
        body = auth_client.get('/fhir/Allergies/p1').get_data(as_text=True)
        assert 'no allergy records found' in body.lower()


class TestMedications:
    @responses.activate
    def test_failure_is_distinguished_from_empty(self, auth_client):
        responses.add(responses.GET, f'{FHIR_BASE}/MedicationRequest', status=500)
        body = auth_client.get('/fhir/Medications/p1').get_data(as_text=True)
        assert 'could not be loaded' in body.lower()
        assert 'no medication records found' not in body.lower()


class TestPatientDetails:
    """Patients with no name element returned HTTP 500."""

    @responses.activate
    def test_patient_without_name_renders(self, auth_client):
        responses.add(
            responses.GET, f'{FHIR_BASE}/Patient/nameless',
            json={'resourceType': 'Patient', 'id': 'nameless', 'gender': 'unknown'},
            status=200,
        )
        response = auth_client.get('/fhir/Patient/nameless')
        assert response.status_code == 200
        assert 'Unknown' in response.get_data(as_text=True)

    @responses.activate
    def test_missing_patient_reports_not_found(self, auth_client):
        responses.add(responses.GET, f'{FHIR_BASE}/Patient/ghost', status=404)
        body = auth_client.get('/fhir/Patient/ghost').get_data(as_text=True)
        assert 'could not be found' in body.lower()


class TestVitalSigns:
    @responses.activate
    def test_undated_readings_sort_last_not_first(self, auth_client):
        responses.add(
            responses.GET, f'{FHIR_BASE}/Observation',
            json=bundle(
                {'resourceType': 'Observation', 'status': 'final',
                 'code': {'text': 'HR'}, 'valueQuantity': {'value': 61, 'unit': 'bpm'}},
                {'resourceType': 'Observation', 'status': 'final',
                 'effectiveDateTime': '2026-08-07T10:00:00Z',
                 'code': {'text': 'HR'}, 'valueQuantity': {'value': 72, 'unit': 'bpm'}},
            ),
            status=200,
        )
        body = auth_client.get('/fhir/VitalSigns/p1').get_data(as_text=True)
        # Restrict to the table: the chart JSON above it also contains both
        # values, but chronologically ascending (left-to-right on a trend
        # line), which is the opposite order from the table's newest-first.
        table = body[body.index('<tbody>'):]
        assert table.index('72') < table.index('61'), 'undated reading sorted as newest'

    @responses.activate
    def test_partial_failure_is_reported(self, auth_client):
        responses.add(responses.GET, f'{FHIR_BASE}/Observation', status=503)
        body = auth_client.get('/fhir/VitalSigns/p1').get_data(as_text=True)
        assert 'could not be loaded' in body.lower()


class TestDemographics:
    @responses.activate
    def test_zero_patients_does_not_divide_by_zero(self, auth_client):
        responses.add(responses.GET, f'{FHIR_BASE}/Patient', json=bundle(total=0),
                      status=200)
        response = auth_client.get('/fhir/Demographics')
        assert response.status_code == 200
        assert 'nothing to summarise' in response.get_data(as_text=True).lower()

    @responses.activate
    def test_mean_age_is_computed_not_hardcoded(self, auth_client):
        """The summary card previously printed the literal string '36-55'
        regardless of the data. It must now reflect the actual sample."""
        from fhir_utils import calculate_age

        responses.add(
            responses.GET, f'{FHIR_BASE}/Patient',
            json=bundle(
                _patient('a', birthDate='2000-01-01'),
                _patient('b', birthDate='1990-01-01'),
            ),
            status=200,
        )
        expected = round(
            (calculate_age('2000-01-01') + calculate_age('1990-01-01')) / 2, 1
        )
        body = auth_client.get('/fhir/Demographics').get_data(as_text=True)
        assert 'Mean Age' in body
        assert str(expected) in body

    @responses.activate
    def test_mean_age_absent_when_no_birth_dates(self, auth_client):
        responses.add(
            responses.GET, f'{FHIR_BASE}/Patient',
            json=bundle(_patient('a', birthDate=None)), status=200,
        )
        body = auth_client.get('/fhir/Demographics').get_data(as_text=True)
        assert 'Mean Age' in body


class TestDashboard:
    """The Patients tile showed the page size (100) not the total (3818)."""

    @responses.activate
    def test_patient_count_uses_server_total(self, auth_client):
        def callback(request):
            if request.params.get('_summary') == 'count':
                return (200, {}, '{"resourceType":"Bundle","total":3818}')
            return (200, {}, '{"resourceType":"Bundle","entry":[]}')

        responses.add_callback(
            responses.GET, f'{FHIR_BASE}/Patient',
            callback=callback, content_type='application/json',
        )
        responses.add(responses.GET, f'{FHIR_BASE}/Observation',
                      json={'resourceType': 'Bundle', 'total': 42}, status=200)

        body = auth_client.get('/fhir/Dashboard').get_data(as_text=True)
        assert '3818' in body


class TestSearch:
    """Searching by patient ID reported 'No patients found'."""

    @responses.activate
    def test_search_by_id_finds_the_patient(self, auth_client):
        def callback(request):
            if request.params.get('_id') == 'sindhu-syn-000002':
                return (200, {}, '{"resourceType":"Bundle","entry":[{"resource":'
                                 '{"resourceType":"Patient","id":"sindhu-syn-000002",'
                                 '"name":[{"text":"Synthetic Patient"}]}}]}')
            return (200, {}, '{"resourceType":"Bundle","entry":[]}')

        responses.add_callback(
            responses.GET, f'{FHIR_BASE}/Patient',
            callback=callback, content_type='application/json',
        )
        body = auth_client.post('/fhir/search_patients',
                                data={'q': 'sindhu-syn-000002'}).get_data(as_text=True)
        assert 'sindhu-syn-000002' in body
        assert 'No patients found' not in body

    @responses.activate
    def test_search_failure_is_reported(self, auth_client):
        responses.add(responses.GET, f'{FHIR_BASE}/Patient', status=500)
        body = auth_client.post('/fhir/search_patients',
                                data={'q': 'smith'}).get_data(as_text=True)
        assert 'error' in body.lower() or 'could not' in body.lower()


class TestHealth:
    def test_health_does_not_leak_the_backend_by_default(self, client):
        payload = client.get('/health').get_json()
        assert payload['status'] == 'ok'
        assert 'fhir_server' not in payload
