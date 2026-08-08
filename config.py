"""Application configuration, loaded from the environment.

The app refuses to start in production without an explicit SECRET_KEY. The
previous `os.urandom(24)` default looked safe but silently broke sessions: each
gunicorn worker generated its own key, so a login nonce written by one worker
could not be read by another, and every restart logged everyone out.
"""
import os
from datetime import timedelta


class ConfigError(RuntimeError):
    """Raised when the environment is not safe to boot with."""


def _flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


# The burn address. It is a syntactically valid address that no private key
# maps to, so nobody can ever produce a signature for it.
ZERO_ADDRESS = '0x' + '0' * 40


def is_valid_wallet_address(address):
    """True if `address` is a well-formed, usable Ethereum address.

    The zero address is rejected: seeding it as the administrator produces an
    account that can never sign in, which locks everyone out of a fresh install.
    """
    if not isinstance(address, str):
        return False
    address = address.strip()
    if not address.startswith('0x') or len(address) != 42:
        return False
    try:
        int(address[2:], 16)
    except ValueError:
        return False
    return address.lower() != ZERO_ADDRESS


def _resolve_initial_admin():
    """Read INITIAL_ADMIN_WALLET, rejecting values that would brick the install."""
    raw = (os.environ.get('INITIAL_ADMIN_WALLET') or '').strip().lower()
    if not raw:
        return ''

    if not is_valid_wallet_address(raw):
        import logging
        reason = ('it is the zero (burn) address, which nobody can sign for'
                  if raw == ZERO_ADDRESS
                  else 'it is not a well-formed 0x-prefixed 40-hex-digit address')
        logging.getLogger(__name__).error(
            'Ignoring INITIAL_ADMIN_WALLET=%s because %s. No administrator will '
            'be seeded. Set it to your own wallet address, or grant access after '
            'startup with:  flask --app app grant-admin <address>',
            raw, reason,
        )
        return ''
    return raw


class Config:
    """Base configuration shared by every environment."""

    DEBUG = False
    TESTING = False

    FHIR_SERVER_URL = os.environ.get('FHIR_SERVER_URL', 'http://hapi.fhir.org/baseR4')
    FHIR_TIMEOUT = float(os.environ.get('FHIR_TIMEOUT', '10'))
    FHIR_PAGE_SIZE = int(os.environ.get('FHIR_PAGE_SIZE', '20'))

    DATABASE_PATH = os.environ.get(
        'DATABASE_PATH',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'dashboard.db'),
    )

    # Wallet allowlisted as an administrator on first boot, so there is a way
    # into a fresh install. Everyone else starts as 'pending' and must be
    # approved before they can sign in. Invalid values are ignored rather than
    # seeded -- see _resolve_initial_admin.
    INITIAL_ADMIN_WALLET = _resolve_initial_admin()

    # Session hardening.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = _flag('SESSION_COOKIE_SECURE', default=True)
    PERMANENT_SESSION_LIFETIME = timedelta(
        minutes=int(os.environ.get('SESSION_LIFETIME_MINUTES', '60'))
    )
    SESSION_REFRESH_EACH_REQUEST = True

    WTF_CSRF_TIME_LIMIT = None  # tie CSRF token lifetime to the session instead

    RATELIMIT_STORAGE_URI = os.environ.get('RATELIMIT_STORAGE_URI', 'memory://')
    RATELIMIT_AUTH = os.environ.get('RATELIMIT_AUTH', '10 per minute')

    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
    LOG_JSON = _flag('LOG_JSON', default=True)

    # Exposes the configured FHIR backend on /health. Off by default so the
    # probe does not advertise internal infrastructure.
    HEALTH_SHOW_BACKEND = _flag('HEALTH_SHOW_BACKEND', default=False)


class ProductionConfig(Config):
    pass


class DevelopmentConfig(Config):
    DEBUG = True
    SESSION_COOKIE_SECURE = _flag('SESSION_COOKIE_SECURE', default=False)


class TestConfig(Config):
    TESTING = True
    SECRET_KEY = 'test-secret-key'
    DATABASE_PATH = ':memory:'
    SESSION_COOKIE_SECURE = False
    WTF_CSRF_ENABLED = False
    FHIR_SERVER_URL = 'http://fhir.test/baseR4'
    LOG_JSON = False


def resolve_secret_key(debug):
    """Return the signing key, or raise if running unsafely without one.

    In debug an ephemeral key is generated with a loud warning, so `python
    app.py` still works for local development. In production a missing
    SECRET_KEY is fatal rather than silently degrading.
    """
    secret = os.environ.get('SECRET_KEY')
    if secret:
        return secret

    if debug:
        import logging
        logging.getLogger(__name__).warning(
            'SECRET_KEY is not set. Generating an ephemeral development key: '
            'sessions will not survive a restart and will not work across '
            'multiple workers. Set SECRET_KEY before deploying.'
        )
        return os.urandom(32).hex()

    raise ConfigError(
        'SECRET_KEY is not set. Refusing to start.\n'
        'Generate one with:  python -c "import secrets; print(secrets.token_hex(32))"\n'
        'then set it in the environment before starting the app.'
    )


def select_config():
    """Pick a config class from FLASK_ENV / FLASK_DEBUG."""
    if _flag('FLASK_DEBUG') or os.environ.get('FLASK_ENV') == 'development':
        return DevelopmentConfig
    return ProductionConfig
