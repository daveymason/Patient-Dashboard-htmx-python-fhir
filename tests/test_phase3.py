"""Tests for Phase 3: Conditions panel, charts, patient-list sorting, extended search."""
import json

import responses

from conftest import FHIR_BASE, bundle


class TestConditions:
    @responses.activate
    def test_renders_condition_list(self, auth_client):
        responses.add(
            responses.GET, f'{FHIR_BASE}/Condition',
            json=bundle(
                {'resourceType': 'Condition', 'id': 'c1',
                 'code': {'text': 'Hypertension'},
                 'clinicalStatus': {'coding': [{'code': 'active'}]},
                 'onsetDateTime': '2020-01-01'},
            ),
            status=200,
        )
        body = auth_client.get('/fhir/Conditions/p1').get_data(as_text=True)
        assert 'Hypertension' in body
        assert 'Active' in body

    @responses.activate
    def test_no_onset_falls_back_to_recorded_date(self, auth_client):
        responses.add(
            responses.GET, f'{FHIR_BASE}/Condition',
            json=bundle(
                {'resourceType': 'Condition', 'id': 'c1',
                 'code': {'text': 'Diabetes'}, 'recordedDate': '2019-06-15'},
            ),
            status=200,
        )
        body = auth_client.get('/fhir/Conditions/p1').get_data(as_text=True)
        assert '2019-06-15' in body

    @responses.activate
    def test_failure_is_distinguished_from_empty(self, auth_client):
        responses.add(responses.GET, f'{FHIR_BASE}/Condition', status=500)
        body = auth_client.get('/fhir/Conditions/p1').get_data(as_text=True)
        assert 'could not be loaded' in body.lower()
        assert 'no conditions recorded' not in body.lower()

    @responses.activate
    def test_genuinely_empty_says_so(self, auth_client):
        responses.add(responses.GET, f'{FHIR_BASE}/Condition', json=bundle(), status=200)
        body = auth_client.get('/fhir/Conditions/p1').get_data(as_text=True)
        assert 'no conditions recorded' in body.lower()

    @responses.activate
    def test_patient_page_auto_loads_conditions(self, auth_client):
        responses.add(
            responses.GET, f'{FHIR_BASE}/Patient/p1',
            json={'resourceType': 'Patient', 'id': 'p1', 'name': [{'text': 'Test'}]},
            status=200,
        )
        body = auth_client.get('/fhir/Patient/p1').get_data(as_text=True)
        assert 'hx-get="/fhir/Conditions/p1"' in body
        assert 'hx-trigger="load"' in body


class TestVitalsCharts:
    @responses.activate
    def test_blood_pressure_chart_has_two_series(self, auth_client):
        responses.add(
            responses.GET, f'{FHIR_BASE}/Observation',
            json=bundle(
                {'resourceType': 'Observation', 'status': 'final',
                 'effectiveDateTime': '2026-08-01T10:00:00Z',
                 'code': {'text': 'BP'},
                 'component': [
                     {'code': {'coding': [{'system': 'http://loinc.org', 'code': '8480-6'}]},
                      'valueQuantity': {'value': 120, 'unit': 'mmHg'}},
                     {'code': {'coding': [{'system': 'http://loinc.org', 'code': '8462-4'}]},
                      'valueQuantity': {'value': 80, 'unit': 'mmHg'}},
                 ]},
            ),
            status=200,
        )
        body = auth_client.get('/fhir/VitalSigns/p1').get_data(as_text=True)
        assert 'vitals-chart-blood_pressure' in body
        config = json.loads(
            body.split('data-chart-for="vitals-chart-blood_pressure">')[1].split('</script>')[0]
        )
        labels = [ds['label'] for ds in config['data']['datasets']]
        assert labels == ['Systolic', 'Diastolic']
        assert config['data']['datasets'][0]['data'] == [120]
        assert config['data']['datasets'][1]['data'] == [80]

    @responses.activate
    def test_single_series_vital_has_no_legend(self, auth_client):
        responses.add(
            responses.GET, f'{FHIR_BASE}/Observation',
            json=bundle(
                {'resourceType': 'Observation', 'status': 'final',
                 'effectiveDateTime': '2026-08-01T10:00:00Z',
                 'code': {'text': 'HR'}, 'valueQuantity': {'value': 72, 'unit': 'bpm'}},
            ),
            status=200,
        )
        body = auth_client.get('/fhir/VitalSigns/p1').get_data(as_text=True)
        config = json.loads(
            body.split('data-chart-for="vitals-chart-heart_rate">')[1].split('</script>')[0]
        )
        assert config['options']['plugins']['legend']['display'] is False
        assert len(config['data']['datasets']) == 1

    @responses.activate
    def test_chart_rows_are_chronological_not_newest_first(self, auth_client):
        responses.add(
            responses.GET, f'{FHIR_BASE}/Observation',
            json=bundle(
                {'resourceType': 'Observation', 'status': 'final',
                 'effectiveDateTime': '2026-08-05T10:00:00Z',
                 'code': {'text': 'HR'}, 'valueQuantity': {'value': 80, 'unit': 'bpm'}},
                {'resourceType': 'Observation', 'status': 'final',
                 'effectiveDateTime': '2026-08-01T10:00:00Z',
                 'code': {'text': 'HR'}, 'valueQuantity': {'value': 60, 'unit': 'bpm'}},
            ),
            status=200,
        )
        body = auth_client.get('/fhir/VitalSigns/p1').get_data(as_text=True)
        config = json.loads(
            body.split('data-chart-for="vitals-chart-heart_rate">')[1].split('</script>')[0]
        )
        assert config['data']['datasets'][0]['data'] == [60, 80]

    @responses.activate
    def test_no_data_means_no_chart_card(self, auth_client):
        responses.add(responses.GET, f'{FHIR_BASE}/Observation', json=bundle(), status=200)
        body = auth_client.get('/fhir/VitalSigns/p1').get_data(as_text=True)
        assert 'data-chart-for' not in body

    @responses.activate
    def test_non_numeric_reading_does_not_break_chart_building(self, auth_client):
        """A reading with no valueQuantity/component still renders (table-only)
        rather than crashing the whole panel."""
        responses.add(
            responses.GET, f'{FHIR_BASE}/Observation',
            json=bundle(
                {'resourceType': 'Observation', 'status': 'final',
                 'effectiveDateTime': '2026-08-01T10:00:00Z',
                 'code': {'text': 'Temp'}, 'dataAbsentReason': {'text': 'Not performed'}},
            ),
            status=200,
        )
        response = auth_client.get('/fhir/VitalSigns/p1')
        assert response.status_code == 200


class TestDemographicsCharts:
    @responses.activate
    def test_gender_and_age_charts_present(self, auth_client):
        responses.add(
            responses.GET, f'{FHIR_BASE}/Patient',
            json=bundle(
                {'resourceType': 'Patient', 'id': 'a', 'gender': 'female', 'birthDate': '2000-01-01'},
                {'resourceType': 'Patient', 'id': 'b', 'gender': 'male', 'birthDate': '1990-01-01'},
            ),
            status=200,
        )
        body = auth_client.get('/fhir/Demographics').get_data(as_text=True)
        assert 'demographics-chart-gender' in body
        assert 'demographics-chart-age' in body
        gender_config = json.loads(
            body.split('data-chart-for="demographics-chart-gender">')[1].split('</script>')[0]
        )
        assert gender_config['type'] == 'bar'
        assert gender_config['options']['plugins']['legend']['display'] is False

    @responses.activate
    def test_zero_patients_renders_no_charts(self, auth_client):
        responses.add(responses.GET, f'{FHIR_BASE}/Patient', json=bundle(total=0), status=200)
        body = auth_client.get('/fhir/Demographics').get_data(as_text=True)
        assert 'data-chart-for' not in body


class TestPatientListSorting:
    @responses.activate
    def test_sort_param_is_forwarded_as_fhir_sort(self, auth_client):
        seen = {}

        def callback(request):
            # get_patients() also calls fhir.count() for the header total,
            # a second request with no _sort -- ignore that one.
            if '_summary' not in request.params:
                seen['query'] = request.params
            return (200, {}, json.dumps(bundle()))

        responses.add_callback(responses.GET, f'{FHIR_BASE}/Patient',
                               callback=callback, content_type='application/json')
        auth_client.get('/fhir/Patients?sort=birthdate')
        assert seen['query'].get('_sort') == 'birthdate'

    @responses.activate
    def test_invalid_sort_value_is_dropped_not_forwarded(self, auth_client):
        """The sort value is also the literal FHIR _sort parameter, so an
        unchecked value would let a caller inject an arbitrary search param."""
        seen = {}

        def callback(request):
            seen['query'] = request.params
            return (200, {}, json.dumps(bundle()))

        responses.add_callback(responses.GET, f'{FHIR_BASE}/Patient',
                               callback=callback, content_type='application/json')
        auth_client.get('/fhir/Patients?sort=_lastUpdated')
        assert '_sort' not in seen['query']

    @responses.activate
    def test_descending_sort_toggle(self, auth_client):
        seen = {}

        def callback(request):
            if '_summary' not in request.params:
                seen['query'] = request.params
            return (200, {}, json.dumps(bundle()))

        responses.add_callback(responses.GET, f'{FHIR_BASE}/Patient',
                               callback=callback, content_type='application/json')
        auth_client.get('/fhir/Patients?sort=-gender')
        assert seen['query'].get('_sort') == '-gender'

    @responses.activate
    def test_sort_via_search_endpoint_combines_with_empty_query(self, auth_client):
        seen = {}

        def callback(request):
            seen['query'] = request.params
            return (200, {}, json.dumps(bundle()))

        responses.add_callback(responses.GET, f'{FHIR_BASE}/Patient',
                               callback=callback, content_type='application/json')
        auth_client.post('/fhir/search_patients', data={'q': '', 'sort': 'family'})
        assert seen['query'].get('_sort') == 'family'

    @responses.activate
    def test_header_shows_active_sort_arrow(self, auth_client):
        responses.add(responses.GET, f'{FHIR_BASE}/Patient', json=bundle(), status=200)
        body = auth_client.get('/fhir/Patients?sort=-birthdate').get_data(as_text=True)
        assert 'fa-sort-down' in body

    @responses.activate
    def test_pagination_returns_rows_only_not_a_nested_table(self, auth_client):
        """The paged response replaces a <tr> inside the existing <tbody>, so
        it must never contain a <table>/<thead> shell -- swapping a full table
        into a table row is invalid markup and broke Load More entirely."""
        from urllib.parse import quote
        next_url = f'{FHIR_BASE}/Patient?_count=20&page=2'
        responses.add(responses.GET, next_url, json=bundle(
            {'resourceType': 'Patient', 'id': 'page2-patient',
             'name': [{'text': 'Page Two'}]},
        ), status=200)
        # next_url's own '&'/'?' must be percent-encoded before embedding it as
        # a single query value (the production template's {{ next_url|urlencode }}
        # already does this).
        body = auth_client.get(
            f'/fhir/Patients/page?url={quote(next_url, safe="")}'
        ).get_data(as_text=True)
        assert 'page2-patient' in body
        assert '<table' not in body
        assert '<thead' not in body

    @responses.activate
    def test_pagination_chains_the_next_cursor(self, auth_client):
        """A paged response with more data behind it renders a fresh Load More
        row pointing at the following cursor."""
        from urllib.parse import quote
        page2 = f'{FHIR_BASE}/Patient?_count=20&page=2'
        page3 = f'{FHIR_BASE}?_getpages=abc&_getpagesoffset=40'
        responses.add(responses.GET, page2, json=bundle(
            {'resourceType': 'Patient', 'id': 'p2', 'name': [{'text': 'X'}]},
            next_url=page3,
        ), status=200)
        body = auth_client.get(
            f'/fhir/Patients/page?url={quote(page2, safe="")}'
        ).get_data(as_text=True)
        assert 'patients-load-more' in body
        assert '_getpages' in body


class TestSidebarOnEveryShell:
    """The sidebar's hx-get links target #content, which only exists on the
    SPA shell. On base.html pages (admin, settings) htmx cancelled the click
    and then found no target -- every sidebar link was dead there."""

    def test_admin_page_sidebar_uses_plain_links_not_htmx(self, admin_client):
        body = admin_client.get('/admin/users').get_data(as_text=True)
        sidebar = body.split('<nav class="sidebar">')[1].split('</nav>')[0]
        assert 'hx-get' not in sidebar
        assert 'href="/?view=patients"' in sidebar
        assert 'href="/?view=stats"' in sidebar

    @responses.activate
    def test_spa_shell_keeps_htmx_links_with_real_href_fallback(self, auth_client):
        body = auth_client.get('/').get_data(as_text=True)
        sidebar = body.split('<nav class="sidebar">')[1].split('</nav>')[0]
        assert 'hx-get="/fhir/Patients"' in sidebar
        # Real hrefs behind the hx-get: middle-click / open-in-new-tab works.
        assert 'href="/?view=patients"' in sidebar

    @responses.activate
    def test_view_param_selects_the_initial_fragment(self, auth_client):
        body = auth_client.get('/?view=stats').get_data(as_text=True)
        assert 'hx-get="/fhir/Demographics" hx-trigger="load"' in body

    @responses.activate
    def test_unknown_view_falls_back_to_patients(self, auth_client):
        body = auth_client.get('/?view=<script>').get_data(as_text=True)
        assert 'hx-get="/fhir/Patients" hx-trigger="load"' in body


class TestExtendedSearch:
    @responses.activate
    def test_search_by_birthdate(self, auth_client):
        def callback(request):
            if request.params.get('birthdate') == '1990-05-01':
                return (200, {}, json.dumps(bundle(
                    {'resourceType': 'Patient', 'id': 'p1', 'name': [{'text': 'Match'}]}
                )))
            return (200, {}, json.dumps(bundle()))

        responses.add_callback(responses.GET, f'{FHIR_BASE}/Patient',
                               callback=callback, content_type='application/json')
        body = auth_client.post('/fhir/search_patients',
                                data={'q': '1990-05-01'}).get_data(as_text=True)
        assert 'Match' in body

    @responses.activate
    def test_search_by_partial_year_birthdate(self, auth_client):
        def callback(request):
            if request.params.get('birthdate') == '1990':
                return (200, {}, json.dumps(bundle(
                    {'resourceType': 'Patient', 'id': 'p1', 'name': [{'text': 'YearMatch'}]}
                )))
            return (200, {}, json.dumps(bundle()))

        responses.add_callback(responses.GET, f'{FHIR_BASE}/Patient',
                               callback=callback, content_type='application/json')
        body = auth_client.post('/fhir/search_patients', data={'q': '1990'}).get_data(as_text=True)
        assert 'YearMatch' in body

    @responses.activate
    def test_search_by_phone(self, auth_client):
        def callback(request):
            if request.params.get('phone') == '555-123-4567':
                return (200, {}, json.dumps(bundle(
                    {'resourceType': 'Patient', 'id': 'p1', 'name': [{'text': 'PhoneMatch'}]}
                )))
            return (200, {}, json.dumps(bundle()))

        responses.add_callback(responses.GET, f'{FHIR_BASE}/Patient',
                               callback=callback, content_type='application/json')
        body = auth_client.post('/fhir/search_patients',
                                data={'q': '555-123-4567'}).get_data(as_text=True)
        assert 'PhoneMatch' in body

    @responses.activate
    def test_short_digit_string_is_not_treated_as_phone(self, auth_client):
        """A 3-digit token is far more likely a fragment of something else
        than a phone number -- must not add spurious phone-search noise."""
        seen_params = []

        def callback(request):
            seen_params.append(dict(request.params))
            return (200, {}, json.dumps(bundle()))

        responses.add_callback(responses.GET, f'{FHIR_BASE}/Patient',
                               callback=callback, content_type='application/json')
        auth_client.post('/fhir/search_patients', data={'q': '123'})
        assert not any('phone' in params for params in seen_params)

    @responses.activate
    def test_id_search_still_takes_priority_over_name(self, auth_client):
        def callback(request):
            if request.params.get('_id') == '137300205':
                return (200, {}, json.dumps(bundle(
                    {'resourceType': 'Patient', 'id': '137300205', 'name': [{'text': 'ByID'}]}
                )))
            return (200, {}, json.dumps(bundle(
                {'resourceType': 'Patient', 'id': 'other', 'name': [{'text': 'ByName'}]}
            )))

        responses.add_callback(responses.GET, f'{FHIR_BASE}/Patient',
                               callback=callback, content_type='application/json')
        body = auth_client.post('/fhir/search_patients',
                                data={'q': '137300205'}).get_data(as_text=True)
        assert 'ByID' in body

    @responses.activate
    def test_all_candidates_failing_reports_error_not_empty(self, auth_client):
        responses.add(responses.GET, f'{FHIR_BASE}/Patient', status=503)
        body = auth_client.post('/fhir/search_patients',
                                data={'q': 'smith'}).get_data(as_text=True)
        assert 'error' in body.lower() or 'failed' in body.lower()
        assert 'no patients found' not in body.lower()

    @responses.activate
    def test_search_sort_and_term_combine(self, auth_client):
        seen = {}

        def callback(request):
            if 'name:contains' in request.params:
                seen['query'] = request.params
            return (200, {}, json.dumps(bundle()))

        responses.add_callback(responses.GET, f'{FHIR_BASE}/Patient',
                               callback=callback, content_type='application/json')
        auth_client.post('/fhir/search_patients', data={'q': 'smith', 'sort': 'family'})
        assert seen['query'].get('_sort') == 'family'
        assert seen['query'].get('name:contains') == 'smith'
