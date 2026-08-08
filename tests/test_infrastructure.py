"""Tests for the config, client, database and audit layers."""
import sqlite3

import pytest
import requests
import responses

import db
from conftest import FHIR_BASE
from config import ConfigError, resolve_secret_key
from fhir_client import FhirClient
from logging_setup import pseudonymize


class TestSecretKey:
    def test_production_refuses_to_boot_without_a_secret(self, monkeypatch):
        monkeypatch.delenv('SECRET_KEY', raising=False)
        with pytest.raises(ConfigError, match='SECRET_KEY'):
            resolve_secret_key(debug=False)

    def test_debug_generates_an_ephemeral_key(self, monkeypatch):
        monkeypatch.delenv('SECRET_KEY', raising=False)
        assert resolve_secret_key(debug=True)

    def test_environment_value_is_used(self, monkeypatch):
        monkeypatch.setenv('SECRET_KEY', 'from-the-environment')
        assert resolve_secret_key(debug=False) == 'from-the-environment'


class TestSessionHardening:
    def test_cookie_flags_are_set(self, app):
        assert app.config['SESSION_COOKIE_HTTPONLY'] is True
        assert app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'
        assert app.config['PERMANENT_SESSION_LIFETIME'].total_seconds() > 0


class TestFhirClient:
    @responses.activate
    def test_timeout_is_always_passed(self):
        captured = {}

        def callback(request):
            captured['timeout_seen'] = True
            return (200, {}, '{"resourceType":"Bundle"}')

        responses.add_callback(responses.GET, f'{FHIR_BASE}/Patient',
                               callback=callback, content_type='application/json')
        client = FhirClient(FHIR_BASE, timeout=5)
        assert client.get('Patient').ok
        assert captured['timeout_seen']

    @responses.activate
    def test_connection_error_returns_result_not_exception(self):
        responses.add(responses.GET, f'{FHIR_BASE}/Patient',
                      body=requests.exceptions.ConnectionError('boom'))
        result = FhirClient(FHIR_BASE).get('Patient')
        assert result.ok is False
        assert result.error

    @responses.activate
    def test_timeout_returns_result_not_exception(self):
        responses.add(responses.GET, f'{FHIR_BASE}/Patient',
                      body=requests.exceptions.Timeout('slow'))
        result = FhirClient(FHIR_BASE).get('Patient')
        assert result.ok is False
        assert 'time' in result.error.lower()

    @responses.activate
    def test_unexpected_exception_is_contained(self):
        responses.add(responses.GET, f'{FHIR_BASE}/Patient',
                      body=ValueError('something unwrapped'))
        result = FhirClient(FHIR_BASE).get('Patient')
        assert result.ok is False

    @responses.activate
    def test_malformed_json_is_handled(self):
        responses.add(responses.GET, f'{FHIR_BASE}/Patient',
                      body='<html>not json</html>', status=200)
        result = FhirClient(FHIR_BASE).get('Patient')
        assert result.ok is False
        assert 'malformed' in result.error.lower()

    @responses.activate
    def test_empty_bundle_is_success_not_failure(self):
        responses.add(responses.GET, f'{FHIR_BASE}/Patient',
                      json={'resourceType': 'Bundle', 'entry': []}, status=200)
        result = FhirClient(FHIR_BASE).get('Patient')
        assert result.ok is True
        assert result.entries == []

    @responses.activate
    def test_get_many_runs_concurrently_and_isolates_failures(self):
        responses.add(responses.GET, f'{FHIR_BASE}/A',
                      json={'resourceType': 'Bundle'}, status=200)
        responses.add(responses.GET, f'{FHIR_BASE}/B', status=500)
        results = FhirClient(FHIR_BASE).get_many({
            'a': ('A', None),
            'b': ('B', None),
        })
        assert results['a'].ok is True
        assert results['b'].ok is False

    def test_pagination_link_off_server_is_refused(self):
        """Bundle links are server-supplied; following one blindly is SSRF."""
        client = FhirClient(FHIR_BASE)
        result = client.get_absolute('http://evil.example.com/Patient')
        assert result.ok is False
        assert 'invalid' in result.error.lower()

    @responses.activate
    def test_pagination_link_on_server_is_followed(self):
        responses.add(responses.GET, f'{FHIR_BASE}/Patient',
                      json={'resourceType': 'Bundle'}, status=200)
        assert FhirClient(FHIR_BASE).get_absolute(f'{FHIR_BASE}/Patient').ok


class TestInitialAdminBootstrap:
    """A bad INITIAL_ADMIN_WALLET must not seed an unusable administrator.

    Seeding the zero address created an 'active admin' nobody could ever sign
    in as, so every real clinician stayed pending with no one able to approve
    them -- a bricked install recoverable only by editing SQLite by hand.
    """

    def test_zero_address_is_rejected(self):
        from config import ZERO_ADDRESS, is_valid_wallet_address
        assert is_valid_wallet_address(ZERO_ADDRESS) is False

    @pytest.mark.parametrize('value', [
        '', '   ', 'not-an-address', '0x123', '0x' + 'z' * 40, '0x' + '0' * 40,
        '0x' + 'a' * 41, None, 12345,
    ])
    def test_malformed_addresses_are_rejected(self, value):
        from config import is_valid_wallet_address
        assert is_valid_wallet_address(value) is False

    def test_real_address_is_accepted(self):
        from config import is_valid_wallet_address
        assert is_valid_wallet_address('0x' + 'a1B2' * 10) is True

    def test_zero_address_seeds_no_admin(self, tmp_path, monkeypatch):
        from app import create_app
        from config import TestConfig, ZERO_ADDRESS

        monkeypatch.setenv('INITIAL_ADMIN_WALLET', ZERO_ADDRESS)

        class _Config(TestConfig):
            DATABASE_PATH = str(tmp_path / 'zero.db')
            INITIAL_ADMIN_WALLET = ''  # what _resolve_initial_admin returns

        application = create_app(_Config)
        with application.app_context():
            assert db.list_users() == []
            assert db.count_active_admins() == 0

    def test_grant_admin_recovers_a_locked_out_install(self, app):
        wallet = '0x' + 'e' * 40
        with app.app_context():
            assert db.count_active_admins() == 0
            record = db.grant_admin(wallet)
            assert record['role'] == db.ROLE_ADMIN
            assert record['status'] == db.STATUS_ACTIVE
            assert db.count_active_admins() == 1

    def test_grant_admin_promotes_an_existing_pending_user(self, app):
        wallet = '0x' + 'f' * 40
        with app.app_context():
            db.register_pending_user(wallet)
            assert db.get_user(wallet)['status'] == db.STATUS_PENDING
            db.grant_admin(wallet)
            assert db.get_user(wallet)['role'] == db.ROLE_ADMIN
            assert db.get_user(wallet)['status'] == db.STATUS_ACTIVE

    def test_cli_rejects_an_unusable_address(self, app):
        runner = app.test_cli_runner()
        result = runner.invoke(args=['grant-admin', '0x' + '0' * 40])
        assert result.exit_code != 0
        assert 'not a usable wallet address' in result.output

    def test_cli_grants_and_lists(self, app):
        runner = app.test_cli_runner()
        wallet = '0x' + 'c' * 40
        granted = runner.invoke(args=['grant-admin', wallet])
        assert granted.exit_code == 0
        assert 'now an active administrator' in granted.output

        listed = runner.invoke(args=['list-users'])
        assert wallet in listed.output
        assert '1 active administrator(s)' in listed.output


class TestUserStore:
    def test_new_wallet_lands_as_pending(self, app):
        with app.app_context():
            record = db.register_pending_user('0x' + 'd' * 40)
            assert record['status'] == db.STATUS_PENDING
            assert record['role'] == db.ROLE_VIEWER

    def test_wallet_address_is_normalised(self, app):
        with app.app_context():
            mixed = '0x' + 'AbCdEf' + '0' * 34
            db.register_pending_user(mixed)
            assert db.get_user(mixed.lower()) is not None
            assert db.get_user(mixed.upper()) is not None

    def test_unknown_user_is_none(self, app):
        with app.app_context():
            assert db.get_user('0x' + 'f' * 40) is None
            assert db.get_user('') is None


class TestAuditLog:
    def test_entries_are_written_and_read(self, app):
        with app.app_context():
            db.write_audit('patient.view', 'success', actor='0xabc',
                           resource_type='Patient', resource_id='ref_123')
            entries = db.read_audit()
            assert len(entries) == 1
            assert entries[0]['action'] == 'patient.view'

    def test_log_is_append_only(self, app):
        """Enforced by trigger, so a bug cannot rewrite the trail."""
        with app.app_context():
            db.write_audit('patient.view', 'success', actor='0xabc')
            connection = db.get_connection()
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("UPDATE audit_log SET action = 'tampered'")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute('DELETE FROM audit_log')

    def test_audit_failure_does_not_break_the_request(self, app, monkeypatch):
        with app.app_context():
            monkeypatch.setattr(db, 'get_connection',
                                lambda: (_ for _ in ()).throw(sqlite3.Error('down')))
            db.write_audit('patient.view', 'success')  # must not raise


class TestPseudonymisation:
    def test_identifier_is_not_recoverable_but_is_stable(self):
        first = pseudonymize('patient-12345')
        assert first == pseudonymize('patient-12345')
        assert 'patient-12345' not in first
        assert first != pseudonymize('patient-99999')

    def test_none_passes_through(self):
        assert pseudonymize(None) is None


class TestAuditingOfAccess:
    @responses.activate
    def test_viewing_a_patient_is_audited_without_the_raw_id(self, auth_client, app):
        responses.add(
            responses.GET, f'{FHIR_BASE}/Patient/secret-patient-id',
            json={'resourceType': 'Patient', 'id': 'secret-patient-id',
                  'name': [{'text': 'Test'}]},
            status=200,
        )
        auth_client.get('/fhir/Patient/secret-patient-id')

        with app.app_context():
            entries = db.read_audit()
        views = [e for e in entries if e['action'] == 'patient.view']
        assert views, 'patient view was not audited'
        assert views[0]['resource_id'].startswith('ref_')
        assert 'secret-patient-id' not in views[0]['resource_id']
