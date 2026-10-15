"""SQLite-backed user store and audit log.

The app previously had no user store at all -- `User.get()` fabricated a user
from whatever ID it was handed, so proving control of any wallet granted access
to every patient record. This module gives identity somewhere to live and makes
access reviewable after the fact. Authentication has since moved from wallet
signatures to username/password, but the store and its pending/active/disabled
approval model are unchanged -- only the identity column and how it's proven
are different.

The audit log is append-only, enforced by triggers rather than convention: even
a bug or a direct sqlite3 session cannot rewrite history.
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

from flask import current_app, g
from werkzeug.security import generate_password_hash

logger = logging.getLogger(__name__)

ROLE_ADMIN = 'admin'
ROLE_CLINICIAN = 'clinician'
ROLE_VIEWER = 'viewer'
ROLES = (ROLE_ADMIN, ROLE_CLINICIAN, ROLE_VIEWER)

STATUS_PENDING = 'pending'
STATUS_ACTIVE = 'active'
STATUS_DISABLED = 'disabled'

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username       TEXT PRIMARY KEY,
    password_hash  TEXT NOT NULL,
    display_name   TEXT,
    role           TEXT NOT NULL DEFAULT 'viewer',
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TEXT NOT NULL,
    last_login_at  TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at   TEXT NOT NULL,
    actor         TEXT,
    action        TEXT NOT NULL,
    resource_type TEXT,
    resource_id   TEXT,
    outcome       TEXT NOT NULL,
    remote_addr   TEXT,
    user_agent    TEXT,
    detail        TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_occurred_at ON audit_log(occurred_at);
CREATE INDEX IF NOT EXISTS idx_audit_actor       ON audit_log(actor);
CREATE INDEX IF NOT EXISTS idx_audit_resource    ON audit_log(resource_type, resource_id);

-- The audit trail is evidence. Make it structurally impossible to edit.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

-- Every exchange with a model, kept so a bad answer can be investigated after
-- the fact: which patient, which prompt, which model, what it said. Without
-- this a clinician reporting "the assistant told me the wrong thing" leaves
-- nothing to examine. Separate from audit_log because the columns are
-- different and the retention decision will be too.
CREATE TABLE IF NOT EXISTS ai_exchange (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at   TEXT NOT NULL,
    actor         TEXT NOT NULL,
    patient_ref   TEXT NOT NULL,
    feature       TEXT NOT NULL,
    model         TEXT NOT NULL,
    prompt_hash   TEXT NOT NULL,
    prompt_version TEXT,
    deidentified  INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    response      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_exchange_occurred_at ON ai_exchange(occurred_at);
CREATE INDEX IF NOT EXISTS idx_ai_exchange_actor       ON ai_exchange(actor);
CREATE INDEX IF NOT EXISTS idx_ai_exchange_patient     ON ai_exchange(patient_ref);

CREATE TRIGGER IF NOT EXISTS ai_exchange_no_update
BEFORE UPDATE ON ai_exchange
BEGIN
    SELECT RAISE(ABORT, 'ai_exchange is append-only');
END;

CREATE TRIGGER IF NOT EXISTS ai_exchange_no_delete
BEFORE DELETE ON ai_exchange
BEGIN
    SELECT RAISE(ABORT, 'ai_exchange is append-only');
END;
"""


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def get_connection():
    """Per-request SQLite connection, created on first use."""
    if 'db' not in g:
        path = current_app.config['DATABASE_PATH']
        if path != ':memory:':
            os.makedirs(os.path.dirname(path), exist_ok=True)
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute('PRAGMA journal_mode = WAL')
        g.db = connection
    return g.db


def close_connection(_exception=None):
    connection = g.pop('db', None)
    if connection is not None:
        connection.close()


def _retire_wallet_era_users_table(connection):
    """Rename a users table left over from the wallet-auth era.

    That schema keyed users on wallet_address with no password_hash column.
    `CREATE TABLE IF NOT EXISTS` silently keeps it, after which every seed
    INSERT and login query crashes on the missing username column. Wallet
    identities cannot be carried into password auth (there is no password to
    migrate), so the old table is preserved under a backup name for the
    operator and a fresh one is created.
    """
    columns = {
        row['name']
        for row in connection.execute("PRAGMA table_info(users)").fetchall()
    }
    if not columns or 'username' in columns:
        return
    backup_name = f"users_wallet_era_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    connection.execute(f'ALTER TABLE users RENAME TO {backup_name}')
    connection.commit()
    logger.warning(
        'Found a users table from the wallet-auth era; renamed it to %s. '
        'Accounts must be re-created (passwords cannot be derived from '
        'wallet identities).', backup_name,
    )


def init_db(app):
    """Create the schema and seed the bootstrap administrator."""
    with app.app_context():
        connection = get_connection()
        _retire_wallet_era_users_table(connection)
        connection.executescript(SCHEMA)
        connection.commit()

        username = app.config.get('INITIAL_ADMIN_USERNAME')
        password = app.config.get('INITIAL_ADMIN_PASSWORD')
        if username and password:  # already validated by config._resolve_initial_admin
            existing = get_user(username)
            if existing is None:
                connection.execute(
                    'INSERT INTO users'
                    ' (username, password_hash, display_name, role, status, created_at)'
                    ' VALUES (?, ?, ?, ?, ?, ?)',
                    (username, generate_password_hash(password), 'Initial administrator',
                     ROLE_ADMIN, STATUS_ACTIVE, _utcnow()),
                )
                connection.commit()
                logger.info('Seeded initial administrator account')
            elif existing['status'] != STATUS_ACTIVE or existing['role'] != ROLE_ADMIN:
                connection.execute(
                    'UPDATE users SET role = ?, status = ?, password_hash = ?'
                    ' WHERE username = ?',
                    (ROLE_ADMIN, STATUS_ACTIVE, generate_password_hash(password),
                     username),
                )
                connection.commit()
                logger.info(
                    'Synchronized administrator credential to configured INITIAL_ADMIN_PASSWORD.'
                )
        close_connection()


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

def get_user(username):
    if not username:
        return None
    row = get_connection().execute(
        'SELECT * FROM users WHERE username = ?',
        (username.strip().lower(),),
    ).fetchone()
    return dict(row) if row else None


def register_pending_user(username, password_hash, display_name=None):
    """Record a first-time registration as pending approval.

    Registering is deliberately not the same as granting access -- a new row
    lands as `pending` and cannot sign in until an administrator activates it.
    """
    username = username.strip().lower()
    connection = get_connection()
    connection.execute(
        'INSERT OR IGNORE INTO users'
        ' (username, password_hash, display_name, role, status, created_at)'
        ' VALUES (?, ?, ?, ?, ?, ?)',
        (username, password_hash, display_name, ROLE_VIEWER, STATUS_PENDING, _utcnow()),
    )
    connection.commit()
    return get_user(username)


def record_login(username):
    connection = get_connection()
    connection.execute(
        'UPDATE users SET last_login_at = ? WHERE username = ?',
        (_utcnow(), username.strip().lower()),
    )
    connection.commit()


def set_user_status(username, status):
    if status not in (STATUS_PENDING, STATUS_ACTIVE, STATUS_DISABLED):
        raise ValueError(f'Unknown status: {status}')
    connection = get_connection()
    connection.execute(
        'UPDATE users SET status = ? WHERE username = ?',
        (status, username.strip().lower()),
    )
    connection.commit()


def set_user_role(username, role):
    if role not in ROLES:
        raise ValueError(f'Unknown role: {role}')
    connection = get_connection()
    connection.execute(
        'UPDATE users SET role = ? WHERE username = ?',
        (role, username.strip().lower()),
    )
    connection.commit()


def list_users():
    rows = get_connection().execute(
        'SELECT * FROM users ORDER BY created_at DESC'
    ).fetchall()
    return [dict(row) for row in rows]


def count_active_admins():
    """How many accounts can actually administer the system right now."""
    row = get_connection().execute(
        'SELECT COUNT(*) AS n FROM users WHERE role = ? AND status = ?',
        (ROLE_ADMIN, STATUS_ACTIVE),
    ).fetchone()
    return row['n'] if row else 0


def set_password(username, password_hash):
    """Reset one account's credential, leaving role and status untouched.

    Distinct from `grant_admin`, which also promotes: using that to reset a
    clinician's forgotten password would silently make them an administrator.
    Returns the updated user, or None if no such account exists.
    """
    username = username.strip().lower()
    connection = get_connection()
    cursor = connection.execute(
        'UPDATE users SET password_hash = ? WHERE username = ?',
        (password_hash, username),
    )
    connection.commit()
    return get_user(username) if cursor.rowcount else None


def grant_admin(username, password_hash):
    """Create or promote a user to an active administrator, (re)setting
    their password.

    The recovery path when nobody can sign in -- reachable from the CLI
    without hand-editing the database.
    """
    username = username.strip().lower()
    connection = get_connection()
    connection.execute(
        'INSERT INTO users (username, password_hash, display_name, role, status, created_at)'
        ' VALUES (?, ?, ?, ?, ?, ?)'
        ' ON CONFLICT(username) DO UPDATE SET'
        ' password_hash = excluded.password_hash, role = excluded.role,'
        ' status = excluded.status',
        (username, password_hash, 'Administrator', ROLE_ADMIN, STATUS_ACTIVE, _utcnow()),
    )
    connection.commit()
    return get_user(username)


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------

def write_audit(action, outcome, actor=None, resource_type=None, resource_id=None,
                remote_addr=None, user_agent=None, detail=None):
    """Append one entry to the audit trail.

    Auditing must never take the request down with it, so a failure here is
    logged and swallowed rather than raised.
    """
    try:
        connection = get_connection()
        connection.execute(
            'INSERT INTO audit_log'
            ' (occurred_at, actor, action, resource_type, resource_id, outcome,'
            '  remote_addr, user_agent, detail)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                _utcnow(), actor, action, resource_type, resource_id, outcome,
                remote_addr, (user_agent or '')[:300],
                json.dumps(detail) if detail is not None else None,
            ),
        )
        connection.commit()
    except sqlite3.Error:
        logger.exception('Failed to write audit entry for action=%s', action)


def write_ai_exchange(actor, patient_ref, feature, model, prompt_hash, response,
                      prompt_version=None, deidentified=False,
                      input_tokens=None, output_tokens=None):
    """Append one model exchange to the review trail.

    `patient_ref` must already be pseudonymised by the caller -- this table is
    read by whoever is investigating a complaint, which is not the same
    audience as the record itself.

    Like `write_audit`, a failure here is logged rather than raised: losing the
    trail is bad, but taking a clinician's screen down because a log write
    failed is worse. Returns the new row id, or None if it could not be
    written, so a caller that wants to react has something to check.
    """
    try:
        connection = get_connection()
        cursor = connection.execute(
            'INSERT INTO ai_exchange'
            ' (occurred_at, actor, patient_ref, feature, model, prompt_hash,'
            '  prompt_version, deidentified, input_tokens, output_tokens, response)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                _utcnow(), actor, patient_ref, feature, model, prompt_hash,
                prompt_version, 1 if deidentified else 0,
                input_tokens, output_tokens, response,
            ),
        )
        connection.commit()
        return cursor.lastrowid
    except sqlite3.Error:
        logger.exception('Failed to record AI exchange for feature=%s', feature)
        return None


def read_ai_exchange(limit=100, actor=None, patient_ref=None, feature=None):
    query = 'SELECT * FROM ai_exchange'
    clauses, params = [], []
    if actor:
        clauses.append('actor = ?')
        params.append(actor)
    if patient_ref:
        clauses.append('patient_ref = ?')
        params.append(patient_ref)
    if feature:
        clauses.append('feature = ?')
        params.append(feature)
    if clauses:
        query += ' WHERE ' + ' AND '.join(clauses)
    query += ' ORDER BY id DESC LIMIT ?'
    params.append(limit)

    rows = get_connection().execute(query, params).fetchall()
    return [dict(row) for row in rows]


def read_audit(limit=100, actor=None, resource_id=None):
    query = 'SELECT * FROM audit_log'
    clauses, params = [], []
    if actor:
        clauses.append('actor = ?')
        params.append(actor)
    if resource_id:
        clauses.append('resource_id = ?')
        params.append(resource_id)
    if clauses:
        query += ' WHERE ' + ' AND '.join(clauses)
    query += ' ORDER BY id DESC LIMIT ?'
    params.append(limit)

    rows = get_connection().execute(query, params).fetchall()
    return [dict(row) for row in rows]
