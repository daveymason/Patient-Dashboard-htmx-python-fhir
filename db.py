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
                # Promoting an existing row must also reset its credential to
                # the operator-configured password. Without this, anyone who
                # pre-registered the configured admin username would have
                # *their* password promoted to an active admin login.
                connection.execute(
                    'UPDATE users SET role = ?, status = ?, password_hash = ?'
                    ' WHERE username = ?',
                    (ROLE_ADMIN, STATUS_ACTIVE, generate_password_hash(password),
                     username),
                )
                connection.commit()
                logger.warning(
                    'Promoted existing account to administrator and reset its '
                    'password to the configured INITIAL_ADMIN_PASSWORD.'
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
