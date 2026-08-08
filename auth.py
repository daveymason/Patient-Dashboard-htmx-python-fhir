"""Username/password authentication backed by a real user store.

Replaces the earlier MetaMask wallet-signature flow -- proving control of a
wallet was cryptographically sound, but a browser-wallet signature is an
unusual, high-friction way to sign in to a healthcare admin tool, and it
blocked anyone without a wallet extension installed from even attempting to
use the app. This keeps the same user store, role model, and
pending-until-approved semantics from that flow; only the credential check
changes -- a password checked against a stored hash, instead of a signature
checked against a session-bound nonce.
"""
import logging
from functools import wraps

from flask import abort
from flask_login import UserMixin, current_user
from werkzeug.security import check_password_hash, generate_password_hash

import db

logger = logging.getLogger(__name__)


class User(UserMixin):
    """A row from the users table, adapted for Flask-Login."""

    def __init__(self, record):
        self.id = record['username']
        self.username = record['username']
        self.display_name = record.get('display_name') or record['username']
        self.role = record['role']
        self.status = record['status']

    @property
    def is_active(self):
        """Flask-Login refuses to log in a user whose is_active is False."""
        return self.status == db.STATUS_ACTIVE

    @property
    def is_admin(self):
        return self.role == db.ROLE_ADMIN

    def has_role(self, *roles):
        return self.role in roles


def load_user(user_id):
    """Flask-Login user loader -- reads through to the database every time.

    Deactivating an account therefore takes effect on the user's next request
    rather than whenever their session happens to expire.
    """
    record = db.get_user(user_id)
    if record is None or record['status'] != db.STATUS_ACTIVE:
        return None
    return User(record)


def require_role(*roles):
    """Restrict a route to specific roles."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if not current_user.has_role(*roles):
                logger.warning(
                    'Role check failed',
                    extra={'required_roles': list(roles), 'actual_role': current_user.role},
                )
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def hash_password(password):
    return generate_password_hash(password)


def verify_credentials(username, password):
    """Look up a user and check their password.

    Returns the user record if the password matches, regardless of account
    status -- callers decide whether a pending or disabled account may
    proceed, since "wrong password" and "not yet approved" are different
    messages to show, and conflating them would either leak which usernames
    exist or hide a legitimate approval-pending state behind a generic error.
    """
    record = db.get_user(username)
    if record is None:
        return None
    if not check_password_hash(record['password_hash'], password):
        return None
    return record
