"""Application configuration, loaded from the environment.

Configured for Slainte-Engine Appliance:
- Local FHIR Store persistence (/workspace/data/fhir_store)
- Raw clinical dropzone (/workspace/data/raw)
- Pluggable AI / LiteLLM providers
- Automatic bootstrap admin credential seeding
"""
import logging
import os
import re
from datetime import timedelta
from urllib.parse import urlsplit


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
    if not isinstance(value, str):
        return False
    value = value.strip()
    return (
        MIN_USERNAME_LENGTH <= len(value) <= MAX_USERNAME_LENGTH
        and bool(_USERNAME_RE.fullmatch(value))
    )


def _resolve_initial_admin():
    username = (os.environ.get('INITIAL_ADMIN_USERNAME') or 'admin').strip().lower()
    password = os.environ.get('INITIAL_ADMIN_PASSWORD') or 'ClinicalAdmin2026!'

    if not is_valid_username(username):
        username = 'admin'
    if len(password) < MIN_PASSWORD_LENGTH:
        password = 'ClinicalAdmin2026!'

    return username, password


DEMO_FHIR_HOSTS = frozenset({'hapi.fhir.org'})
AI_REASONING_LEVELS = ('none', 'low', 'medium', 'high', 'xhigh', 'max')
AI_INPUT_TOKEN_CLIFF = 272_000


def is_demo_fhir_host(fhir_server_url):
    host = (urlsplit(fhir_server_url or '').hostname or '').lower()
    return host in DEMO_FHIR_HOSTS


def deidentify_enabled(setting, fhir_server_url):
    value = (setting or 'auto').strip().lower()
    if value in ('1', 'true', 'yes', 'on', 'always'):
        return True
    if value in ('0', 'false', 'no', 'off', 'never'):
        return False
    return not is_demo_fhir_host(fhir_server_url)


def ai_blocked_reason(fhir_server_url, allow_real_phi):
    """Why AI features must stay off for this deployment, or None if allowed.

    A note in a document saying "demo only" does not stop anyone pointing
    FHIR_SERVER_URL at a real server with AI switched on, so the rule is
    enforced instead of written down: patient data only reaches a model
    provider when it came from a known-synthetic server, unless the operator
    explicitly accepts responsibility with AI_ALLOW_REAL_PHI.
    """
    if allow_real_phi:
        return None
    if is_demo_fhir_host(fhir_server_url):
        return None
    host = (urlsplit(fhir_server_url or '').hostname or '').lower()
    return (
        f'AI features are disabled because {host or "this FHIR server"} is not '
        'a known synthetic-data server. Real patient data must not reach a '
        'model provider without a BAA (US) or processor agreement and DPIA '
        '(UK/EU) in place. Set AI_ALLOW_REAL_PHI=true only once that exists.'
    )


class Config:
    """Base configuration shared by every environment."""

    DEBUG = False
    TESTING = False

    # Appliance Data Directories
    FHIR_STORE_PATH = os.environ.get('FHIR_STORE_PATH', '/workspace/data/fhir_store')
    RAW_DATA_PATH = os.environ.get('RAW_DATA_PATH', '/workspace/data/raw')
    APPLIANCE_MODE = _flag('APPLIANCE_MODE', default=True)
    AUTH_DISABLED = _flag('AUTH_DISABLED', default=False)

    # FHIR Backend
    FHIR_SERVER_URL = os.environ.get('FHIR_SERVER_URL', 'file:///workspace/data/fhir_store')
    FHIR_TIMEOUT = float(os.environ.get('FHIR_TIMEOUT', '10'))
    FHIR_PAGE_SIZE = int(os.environ.get('FHIR_PAGE_SIZE', '20'))

    DATABASE_PATH = os.environ.get(
        'DATABASE_PATH',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'dashboard.db'),
    )

    INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD = _resolve_initial_admin()

    # Session hardening
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = _flag('SESSION_COOKIE_SECURE', default=False)
    PERMANENT_SESSION_LIFETIME = timedelta(
        minutes=int(os.environ.get('SESSION_LIFETIME_MINUTES', '120'))
    )
    SESSION_REFRESH_EACH_REQUEST = True
    WTF_CSRF_TIME_LIMIT = None

    RATELIMIT_STORAGE_URI = os.environ.get('RATELIMIT_STORAGE_URI', 'memory://')
    RATELIMIT_AUTH = os.environ.get('RATELIMIT_AUTH', '30 per minute')

    # AI Provider settings
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
    AI_MODEL = os.environ.get('AI_MODEL', 'gpt-4o')
    AI_REQUEST_TIMEOUT = float(os.environ.get('AI_REQUEST_TIMEOUT', '60'))
    AI_ALLOW_REAL_PHI = _flag('AI_ALLOW_REAL_PHI', default=False)
    AI_REASONING_EFFORT = os.environ.get('AI_REASONING_EFFORT', 'low').strip().lower()
    AI_DEIDENTIFY = os.environ.get('AI_DEIDENTIFY', 'auto').strip().lower()
    AI_CONTEXT_TOKEN_CEILING = int(os.environ.get('AI_CONTEXT_TOKEN_CEILING', '120000'))

    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
    LOG_JSON = _flag('LOG_JSON', default=False)
    HEALTH_SHOW_BACKEND = _flag('HEALTH_SHOW_BACKEND', default=False)


class ProductionConfig(Config):
    pass


class DevelopmentConfig(Config):
    DEBUG = True
    SESSION_COOKIE_SECURE = False


class TestConfig(Config):
    TESTING = True
    APPLIANCE_MODE = False
    FHIR_STORE_PATH = None
    SECRET_KEY = 'test-secret-key'
    DATABASE_PATH = ':memory:'
    SESSION_COOKIE_SECURE = False
    WTF_CSRF_ENABLED = False
    FHIR_SERVER_URL = 'http://fhir.test/baseR4'
    LOG_JSON = False
    AI_MODEL = 'gpt-5.6-luna'
    INITIAL_ADMIN_USERNAME = ''
    INITIAL_ADMIN_PASSWORD = ''


def resolve_secret_key(debug):
    """Return the signing key, or raise if running unsafely without one."""
    env_secret = os.environ.get('SECRET_KEY')
    if env_secret:
        return env_secret
    if debug:
        import logging
        logging.getLogger(__name__).warning(
            'SECRET_KEY is not set. Generating an ephemeral development key: '
            'sessions will not survive a restart and will not work across '
            'multiple workers. Set SECRET_KEY before deploying.'
        )
        return os.urandom(32).hex()
    raise ConfigError('SECRET_KEY must be set in production.')


def select_config():
    env = os.environ.get('FLASK_ENV', os.environ.get('ENV', '')).lower()
    if env in ('prod', 'production'):
        return ProductionConfig
    if env in ('test', 'testing'):
        return TestConfig
    if _flag('FLASK_DEBUG') or env == 'development':
        return DevelopmentConfig
    return ProductionConfig
