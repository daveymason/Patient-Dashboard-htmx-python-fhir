import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
from app import create_app  # noqa: E402
from config import TestConfig  # noqa: E402

FHIR_BASE = TestConfig.FHIR_SERVER_URL


@pytest.fixture
def app(tmp_path):
    class _Config(TestConfig):
        DATABASE_PATH = str(tmp_path / 'test.db')
        RATELIMIT_ENABLED = False

    application = create_app(_Config)
    yield application


@pytest.fixture
def client(app):
    return app.test_client()


TEST_PASSWORD = 'correct horse battery staple'


@pytest.fixture
def active_user(app):
    """An approved account, and a client already signed in as them."""
    username = 'activeuser'
    with app.app_context():
        import auth
        db.register_pending_user(username, auth.hash_password(TEST_PASSWORD))
        db.set_user_status(username, db.STATUS_ACTIVE)
    return username


@pytest.fixture
def admin_user(app):
    username = 'adminuser'
    with app.app_context():
        import auth
        db.register_pending_user(username, auth.hash_password(TEST_PASSWORD))
        db.set_user_status(username, db.STATUS_ACTIVE)
        db.set_user_role(username, db.ROLE_ADMIN)
    return username


@pytest.fixture
def auth_client(app, active_user):
    test_client = app.test_client()
    with test_client.session_transaction() as session:
        session['_user_id'] = active_user
        session['_fresh'] = True
    return test_client


@pytest.fixture
def admin_client(app, admin_user):
    test_client = app.test_client()
    with test_client.session_transaction() as session:
        session['_user_id'] = admin_user
        session['_fresh'] = True
    return test_client


def bundle(*resources, total=None, next_url=None):
    """Build a FHIR searchset Bundle."""
    payload = {
        'resourceType': 'Bundle',
        'type': 'searchset',
        'entry': [{'resource': resource} for resource in resources],
    }
    if total is not None:
        payload['total'] = total
    if next_url:
        payload['link'] = [{'relation': 'next', 'url': next_url}]
    return payload
