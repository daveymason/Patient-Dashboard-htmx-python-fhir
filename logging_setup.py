"""Structured logging with patient identifiers redacted.

Application logs are routinely shipped to systems with a far wider audience
than the record itself, so a patient ID in a log line is a quiet disclosure.
Identifiers are replaced with a salted digest: still stable enough to correlate
one patient's activity across log lines, but not reversible to an ID.
"""
import hashlib
import json
import logging
import os

_SALT = os.environ.get('LOG_HASH_SALT', '').encode() or os.urandom(16)

_RESERVED = {
    'args', 'asctime', 'created', 'exc_info', 'exc_text', 'filename', 'funcName',
    'levelname', 'levelno', 'lineno', 'module', 'msecs', 'message', 'msg', 'name',
    'pathname', 'process', 'processName', 'relativeCreated', 'stack_info',
    'thread', 'threadName', 'taskName',
}


def pseudonymize(value):
    """Stable, non-reversible reference for a patient or resource identifier."""
    if value is None:
        return None
    digest = hashlib.sha256(_SALT + str(value).encode()).hexdigest()
    return f'ref_{digest[:12]}'


class JsonFormatter(logging.Formatter):
    """One JSON object per line, including any `extra` fields."""

    def format(self, record):
        payload = {
            'ts': self.formatTime(record, '%Y-%m-%dT%H:%M:%S%z'),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith('_'):
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level='INFO', as_json=True):
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter() if as_json
        else logging.Formatter('%(asctime)s %(levelname)-8s %(name)s: %(message)s')
    )

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # urllib3 logs full request URLs at DEBUG, which would defeat the redaction.
    logging.getLogger('urllib3').setLevel(logging.WARNING)
