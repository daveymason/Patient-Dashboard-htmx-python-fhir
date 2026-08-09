"""Resetting a password must not change what the account can do.

`grant-admin` was the only reset path in the codebase, and it promotes to an
active administrator -- so using it for an ordinary forgotten password handed
out admin rights as a side effect.
"""
import auth
import db
from conftest import TEST_PASSWORD


class TestSetPassword:
    def test_role_and_status_survive_a_reset(self, app):
        with app.app_context():
            db.register_pending_user('clinician1', auth.hash_password(TEST_PASSWORD))
            db.set_user_status('clinician1', db.STATUS_ACTIVE)
            db.set_user_role('clinician1', db.ROLE_CLINICIAN)

            record = db.set_password('clinician1', auth.hash_password('brand-new-password'))

            assert record['role'] == db.ROLE_CLINICIAN, 'reset must not promote'
            assert record['status'] == db.STATUS_ACTIVE
            assert auth.verify_credentials('clinician1', 'brand-new-password') is not None
            assert auth.verify_credentials('clinician1', TEST_PASSWORD) is None

    def test_unknown_user_returns_none(self, app):
        with app.app_context():
            assert db.set_password('nobody', auth.hash_password(TEST_PASSWORD)) is None

    def test_pending_account_stays_pending(self, app):
        with app.app_context():
            db.register_pending_user('waiting', auth.hash_password(TEST_PASSWORD))
            record = db.set_password('waiting', auth.hash_password('another-password-1'))
            assert record['status'] == db.STATUS_PENDING, 'reset must not approve'


class TestResetPasswordCommand:
    def test_resets_without_promoting(self, app):
        with app.app_context():
            db.register_pending_user('viewer1', auth.hash_password(TEST_PASSWORD))
            db.set_user_status('viewer1', db.STATUS_ACTIVE)

        result = app.test_cli_runner().invoke(
            args=['reset-password', 'viewer1'],
            input='a-fresh-password-1\na-fresh-password-1\n',
        )
        assert result.exit_code == 0

        with app.app_context():
            record = db.get_user('viewer1')
            assert record['role'] == db.ROLE_VIEWER
            assert db.count_active_admins() == 0, 'reset must not create an admin'

    def test_rejects_a_short_password(self, app):
        with app.app_context():
            db.register_pending_user('viewer2', auth.hash_password(TEST_PASSWORD))
        result = app.test_cli_runner().invoke(
            args=['reset-password', 'viewer2'], input='short\nshort\n',
        )
        assert result.exit_code != 0
        assert 'at least' in result.output.lower()

    def test_unknown_account_is_an_error(self, app):
        result = app.test_cli_runner().invoke(
            args=['reset-password', 'ghost'],
            input=f'{TEST_PASSWORD}\n{TEST_PASSWORD}\n',
        )
        assert result.exit_code != 0
        assert 'no account' in result.output.lower()
