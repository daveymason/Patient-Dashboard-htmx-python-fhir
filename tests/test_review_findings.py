"""Regression tests for the pre-Phase-4 code-review findings.

Each class matches one finding; the docstring says what used to go wrong.
"""
import sqlite3

import responses

import auth
import db
from conftest import FHIR_BASE, TEST_PASSWORD, bundle


class TestWalletEraMigration:
    """A users table from the wallet-auth era (keyed on wallet_address, no
    password columns) survived CREATE TABLE IF NOT EXISTS untouched, then
    crashed the seed INSERT at boot and every login query after it."""

    def test_old_schema_is_retired_and_fresh_one_created(self, tmp_path):
        db_path = tmp_path / 'legacy.db'
        legacy = sqlite3.connect(db_path)
        legacy.execute(
            'CREATE TABLE users ('
            ' wallet_address TEXT PRIMARY KEY, display_name TEXT,'
            " role TEXT NOT NULL DEFAULT 'viewer',"
            " status TEXT NOT NULL DEFAULT 'pending',"
            ' created_at TEXT NOT NULL, last_login_at TEXT)'
        )
        legacy.execute(
            "INSERT INTO users VALUES ('0x' || lower(hex(randomblob(20))),"
            " 'Old wallet user', 'admin', 'active', '2026-01-01', NULL)"
        )
        legacy.commit()
        legacy.close()

        from app import create_app
        from config import TestConfig

        class _Config(TestConfig):
            DATABASE_PATH = str(db_path)

        application = create_app(_Config)  # must not raise
        with application.app_context():
            # New table is empty and password-based; old data kept as backup.
            assert db.list_users() == []
            record = db.register_pending_user('fresh', auth.hash_password(TEST_PASSWORD))
            assert record['status'] == db.STATUS_PENDING
            names = [r['name'] for r in db.get_connection().execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
        assert any(n.startswith('users_wallet_era_') for n in names)

    def test_seeding_still_works_after_migration(self, tmp_path):
        db_path = tmp_path / 'legacy2.db'
        legacy = sqlite3.connect(db_path)
        legacy.execute('CREATE TABLE users (wallet_address TEXT PRIMARY KEY)')
        legacy.commit()
        legacy.close()

        from app import create_app
        from config import TestConfig

        class _Config(TestConfig):
            DATABASE_PATH = str(db_path)
            INITIAL_ADMIN_USERNAME = 'bootadmin'
            INITIAL_ADMIN_PASSWORD = TEST_PASSWORD

        application = create_app(_Config)
        with application.app_context():
            assert db.count_active_admins() == 1


class TestAdminSeedTakeover:
    """Seeding promoted a pre-existing account without touching its password,
    so pre-registering the configured admin username handed the attacker an
    active admin login under their own credential."""

    def test_promotion_resets_the_password_to_the_configured_one(self, tmp_path):
        from app import create_app
        from config import TestConfig

        db_path = str(tmp_path / 'seed.db')

        class _First(TestConfig):
            DATABASE_PATH = db_path

        first = create_app(_First)
        with first.app_context():
            # Attacker grabs the username before the operator configures it.
            db.register_pending_user('admin', auth.hash_password('attacker-password-1'))

        class _Second(TestConfig):
            DATABASE_PATH = db_path
            INITIAL_ADMIN_USERNAME = 'admin'
            INITIAL_ADMIN_PASSWORD = 'operator-password-1'

        second = create_app(_Second)
        with second.app_context():
            assert auth.verify_credentials('admin', 'attacker-password-1') is None
            record = auth.verify_credentials('admin', 'operator-password-1')
            assert record is not None
            assert record['role'] == db.ROLE_ADMIN
            assert record['status'] == db.STATUS_ACTIVE


class TestConditionSortMixedTimezones:
    """Tz-aware onsetDateTime mixed with the naive recordedDate fallback made
    conditions.sort() raise TypeError, 500ing the whole panel."""

    @responses.activate
    def test_aware_onset_and_naive_fallback_sort_together(self, auth_client):
        responses.add(
            responses.GET, f'{FHIR_BASE}/Condition',
            json=bundle(
                {'resourceType': 'Condition', 'id': 'aware',
                 'code': {'text': 'Hypertension'},
                 'onsetDateTime': '2015-10-31T00:00:00-04:00'},
                {'resourceType': 'Condition', 'id': 'naive-fallback',
                 'code': {'text': 'Diabetes'}, 'recordedDate': '2019-06-15'},
                {'resourceType': 'Condition', 'id': 'no-dates',
                 'code': {'text': 'Asthma'}},
            ),
            status=200,
        )
        response = auth_client.get('/fhir/Conditions/p1')
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert 'Hypertension' in body and 'Diabetes' in body and 'Asthma' in body


class TestSearchPartialFailure:
    """A failed name query was masked by any other candidate succeeding empty,
    rendering existing patients as 'No patients found'."""

    @responses.activate
    def test_failed_name_query_is_an_error_not_an_empty_result(self, auth_client):
        def callback(request):
            if 'name:contains' in request.params:
                return (503, {}, 'upstream error')
            return (200, {}, '{"resourceType": "Bundle", "entry": []}')

        responses.add_callback(responses.GET, f'{FHIR_BASE}/Patient',
                               callback=callback, content_type='application/json')
        body = auth_client.post('/fhir/search_patients',
                                data={'q': 'smith'}).get_data(as_text=True)
        assert 'no patients found' not in body.lower()
        assert 'error' in body.lower() or 'could not' in body.lower()


class TestStandaloneBpPairingIntegrity:
    """Date-only fallback pairing could stitch a morning systolic to an
    evening diastolic -- a reading that never happened."""

    @responses.activate
    def test_different_times_same_day_are_not_paired(self, auth_client):
        def callback(request):
            code = request.params.get('code', '')
            if code.endswith('8480-6'):  # systolic, morning, no encounter
                return (200, {}, '{"resourceType":"Bundle","entry":[{"resource":'
                        '{"resourceType":"Observation","status":"final",'
                        '"code":{"text":"SBP"},'
                        '"effectiveDateTime":"2026-08-01T08:00:00Z",'
                        '"valueQuantity":{"value":120,"unit":"mmHg"}}}]}')
            if code.endswith('8462-4'):  # diastolic, evening, no encounter
                return (200, {}, '{"resourceType":"Bundle","entry":[{"resource":'
                        '{"resourceType":"Observation","status":"final",'
                        '"code":{"text":"DBP"},'
                        '"effectiveDateTime":"2026-08-01T20:00:00Z",'
                        '"valueQuantity":{"value":80,"unit":"mmHg"}}}]}')
            return (200, {}, '{"resourceType":"Bundle","entry":[]}')

        responses.add_callback(responses.GET, f'{FHIR_BASE}/Observation',
                               callback=callback, content_type='application/json')
        body = auth_client.get('/fhir/VitalSigns/p1').get_data(as_text=True)
        assert 'Systolic blood pressure: 120' not in body

    @responses.activate
    def test_panel_and_standalone_same_encounter_not_double_counted(self, auth_client):
        panel = ('{"resourceType":"Observation","status":"final",'
                 '"code":{"text":"BP"},'
                 '"encounter":{"reference":"Encounter/e1"},'
                 '"effectiveDateTime":"2026-08-01T08:00:00Z",'
                 '"component":['
                 '{"code":{"coding":[{"system":"http://loinc.org","code":"8480-6"}]},'
                 '"valueQuantity":{"value":120,"unit":"mmHg"}},'
                 '{"code":{"coding":[{"system":"http://loinc.org","code":"8462-4"}]},'
                 '"valueQuantity":{"value":80,"unit":"mmHg"}}]}')
        standalone_s = ('{"resourceType":"Observation","status":"final",'
                        '"code":{"coding":[{"system":"http://loinc.org","code":"8480-6"}]},'
                        '"encounter":{"reference":"Encounter/e1"},'
                        '"effectiveDateTime":"2026-08-01T08:00:00Z",'
                        '"valueQuantity":{"value":120,"unit":"mmHg"}}')
        standalone_d = ('{"resourceType":"Observation","status":"final",'
                        '"code":{"coding":[{"system":"http://loinc.org","code":"8462-4"}]},'
                        '"encounter":{"reference":"Encounter/e1"},'
                        '"effectiveDateTime":"2026-08-01T08:00:00Z",'
                        '"valueQuantity":{"value":80,"unit":"mmHg"}}')

        def callback(request):
            code = request.params.get('code', '')
            if code.endswith('85354-9'):
                return (200, {}, '{"resourceType":"Bundle","entry":[{"resource":%s}]}' % panel)
            if code.endswith('8480-6'):
                return (200, {}, '{"resourceType":"Bundle","entry":[{"resource":%s}]}' % standalone_s)
            if code.endswith('8462-4'):
                return (200, {}, '{"resourceType":"Bundle","entry":[{"resource":%s}]}' % standalone_d)
            return (200, {}, '{"resourceType":"Bundle","entry":[]}')

        responses.add_callback(responses.GET, f'{FHIR_BASE}/Observation',
                               callback=callback, content_type='application/json')
        body = auth_client.get('/fhir/VitalSigns/p1').get_data(as_text=True)
        table = body[body.index('<tbody>'):]
        assert table.count('Blood Pressure') == 1, 'reading was double-counted'


class TestAuthRateLimitScope:
    """GET /login shared the POST rate limit, so ordinary logged-out browsing
    could 429 the sign-in page before a password was ever typed."""

    def test_login_page_views_are_not_rate_limited(self, app):
        app.config['RATELIMIT_ENABLED'] = True
        client = app.test_client()
        # Well past the '10 per minute' auth budget.
        for _ in range(15):
            response = client.get('/login')
            assert response.status_code == 200


class TestSearchKeepsSort:
    """Typing in the search box dropped the active column sort because the
    request carried only q."""

    @responses.activate
    def test_sorted_table_renders_a_sort_carrier_for_the_search_box(self, auth_client):
        responses.add(responses.GET, f'{FHIR_BASE}/Patient',
                      json=bundle(), status=200)
        body = auth_client.post('/fhir/search_patients',
                                data={'q': '', 'sort': '-birthdate'}).get_data(as_text=True)
        assert 'id="current-sort"' in body
        assert 'value="-birthdate"' in body
