"""Application configuration, loaded from the environment.

The app refuses to start in production without an explicit SECRET_KEY. The
previous `os.urandom(24)` default looked safe but silently broke sessions: each
gunicorn worker generated its own key, so a session cookie signed by one worker
could not be read by another, and every restart logged everyone out.
"""
import logging
import os
import re
from datetime import timedelta


class ConfigError(RuntimeError):
    """Raised when the environment is not safe to boot with."""


def _flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 64
MIN_PASSWORD_LENGTH = 10
_USERNAME_RE = re.compile(r'^[A-Za-z0-9_.-]+$')


def is_valid_username(value):
    """True if `value` is an acceptable username: length and character set only.

    No uniqueness or reservation check here -- that's a database lookup, done
    by the caller (self-registration checks it against existing accounts; the
    bootstrap admin is trusted to not collide).
    """
    if not isinstance(value, str):
        return False
    value = value.strip()
    return (
        MIN_USERNAME_LENGTH <= len(value) <= MAX_USERNAME_LENGTH
        and bool(_USERNAME_RE.fullmatch(value))
    )


def _resolve_initial_admin():
    """Read INITIAL_ADMIN_USERNAME/PASSWORD, rejecting values that would
    brick the install or seed a weak credential.

    Returns (username, password) -- both empty if nothing usable was provided,
    so the caller can tell "not configured" apart from "configured badly"
    without re-deriving the validation.
    """
    username = (os.environ.get('INITIAL_ADMIN_USERNAME') or '').strip().lower()
    password = os.environ.get('INITIAL_ADMIN_PASSWORD') or ''

    if not username and not password:
        return '', ''

    logger = logging.getLogger(__name__)

    if not username or not password:
        logger.error(
            'INITIAL_ADMIN_USERNAME and INITIAL_ADMIN_PASSWORD must both be set '
            'to seed an administrator. Ignoring the one that was provided.'
        )
        return '', ''

    if not is_valid_username(username):
        logger.error(
            'Ignoring INITIAL_ADMIN_USERNAME=%r: usernames must be %d-%d characters '
            'of letters, numbers, dots, hyphens or underscores.',
            username, MIN_USERNAME_LENGTH, MAX_USERNAME_LENGTH,
        )
        return '', ''

    if len(password) < MIN_PASSWORD_LENGTH:
        logger.error(
            'Ignoring INITIAL_ADMIN_PASSWORD: must be at least %d characters.',
            MIN_PASSWORD_LENGTH,
        )
        return '', ''

    return username, password


class Config:
    """Base configuration shared by every environment."""

    DEBUG = False
    TESTING = False

    # https, not http: the server 301s every plain-http call (one wasted round
    # trip per request) and mints https pagination cursors either way.
    FHIR_SERVER_URL = os.environ.get('FHIR_SERVER_URL', 'https://hapi.fhir.org/baseR4')
    FHIR_TIMEOUT = float(os.environ.get('FHIR_TIMEOUT', '10'))
    FHIR_PAGE_SIZE = int(os.environ.get('FHIR_PAGE_SIZE', '20'))

    DATABASE_PATH = os.environ.get(
        'DATABASE_PATH',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'dashboard.db'),
    )

    # Account allowlisted as an administrator on first boot, so there is a way
    # into a fresh install. Everyone else starts as 'pending' and must be
    # approved before they can sign in. Invalid or incomplete values are
    # ignored rather than seeded -- see _resolve_initial_admin.
    INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD = _resolve_initial_admin()

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
