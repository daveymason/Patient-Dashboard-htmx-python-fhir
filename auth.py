"""Wallet authentication backed by a real user store.

The signature check was already correct -- it proves the caller controls the
wallet. What was missing was authorization: the previous code logged in whoever
presented a valid signature, so any wallet on earth could read every patient
record. Verification now only establishes *who* you are; the users table decides
whether that identity may enter.
"""
import binascii
import logging
import os
from functools import wraps

from eth_account import Account
from eth_account.messages import encode_defunct
from flask import abort, session
from flask_login import UserMixin, current_user

import db

logger = logging.getLogger(__name__)

NONCE_SESSION_KEY = 'auth_nonce'
NONCE_ADDRESS_KEY = 'auth_nonce_address'


class User(UserMixin):
    """A row from the users table, adapted for Flask-Login."""

    def __init__(self, record):
        self.id = record['wallet_address']
        self.wallet_address = record['wallet_address']
        self.display_name = record.get('display_name') or record['wallet_address']
        self.role = record['role']
        self.status = record['status']

    @property
    def is_active(self):
        """Flask-Login refuses to log in a user whose is_active is False."""
        return self.status == db.STATUS_ACTIVE

    @property
    def is_admin(self):
        return self.role == db.ROLE_ADMIN

    @property
    def short_address(self):
        if len(self.wallet_address) < 12:
            return self.wallet_address
        return f'{self.wallet_address[:6]}...{self.wallet_address[-4:]}'

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


# Address validation lives in config.py, so a value rejected at boot is also
# rejected at sign-in. Callers import it from there directly.


def issue_nonce(wallet_address):
    """Create a single-use challenge and bind it to this session.

    Stored under one fixed key rather than one key per address, so a caller
    cannot accumulate live nonces for many addresses in a single session.
    """
    nonce = (
        'Sign this message to sign in to the Patient Dashboard.\n\n'
        f'Nonce: {binascii.hexlify(os.urandom(16)).decode()}'
    )
    session[NONCE_SESSION_KEY] = nonce
    session[NONCE_ADDRESS_KEY] = wallet_address.strip().lower()
    return nonce


def consume_nonce(wallet_address):
    """Pop the pending nonce if it belongs to this address, else None."""
    stored_nonce = session.pop(NONCE_SESSION_KEY, None)
    stored_address = session.pop(NONCE_ADDRESS_KEY, None)
    if not stored_nonce or not stored_address:
        return None
    if stored_address != wallet_address.strip().lower():
        return None
    return stored_nonce


def recover_signer(nonce, signature):
    """Recover the signing address, or None if the signature is unusable."""
    try:
        return Account.recover_message(
            encode_defunct(text=nonce), signature=signature
        ).lower()
    except Exception as exc:
        logger.info('Signature recovery failed: %s', type(exc).__name__)
        return None
