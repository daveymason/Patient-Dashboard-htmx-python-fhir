"""Tests for the AI exchange trail, and for the whole substrate composing.

The end-to-end test here runs context -> prompts -> provider -> exchange record
against a fake provider, so the pieces are proven to fit together without a
single network call.
"""
import sqlite3

import pytest

import db
from ai.context import build_patient_context
from ai.prompts import build_messages, prompt_hash
from ai.provider import ProviderStatus
from logging_setup import pseudonymize
from tests.test_ai_context import FULL_RECORD, PATIENT_ID, everything


def _write(app, **overrides):
    payload = {
        'actor': 'clinician1',
        'patient_ref': pseudonymize(PATIENT_ID),
        'feature': 'summary',
        'model': 'gpt-5.6-luna',
        'prompt_hash': 'a' * 64,
        'response': 'The record lists a penicillin allergy [AllergyIntolerance/a1].',
    }
    payload.update(overrides)
    with app.app_context():
        return db.write_ai_exchange(**payload), payload


class TestWriteAndRead:
    def test_an_exchange_is_recorded(self, app):
        row_id, payload = _write(app)
        assert row_id is not None
        with app.app_context():
            rows = db.read_ai_exchange()
        assert len(rows) == 1
        assert rows[0]['feature'] == 'summary'
        assert rows[0]['response'] == payload['response']

    def test_token_counts_and_version_are_kept(self, app):
        _write(app, input_tokens=19300, output_tokens=210,
               prompt_version='2026-08-09.1', deidentified=True)
        with app.app_context():
            row = db.read_ai_exchange()[0]
        assert row['input_tokens'] == 19300
        assert row['output_tokens'] == 210
        assert row['prompt_version'] == '2026-08-09.1'
        assert row['deidentified'] == 1

    def test_filters_narrow_the_trail(self, app):
        _write(app, actor='alice', feature='summary')
        _write(app, actor='bob', feature='chat')
        with app.app_context():
            assert len(db.read_ai_exchange(actor='alice')) == 1
            assert len(db.read_ai_exchange(feature='chat')) == 1
            assert len(db.read_ai_exchange(patient_ref=pseudonymize(PATIENT_ID))) == 2

    def test_newest_first(self, app):
        _write(app, response='first')
        _write(app, response='second')
        with app.app_context():
            assert db.read_ai_exchange()[0]['response'] == 'second'

    def test_patient_reference_is_stored_pseudonymised(self, app):
        """Whoever investigates a complaint is not necessarily cleared to read
        the record itself."""
        _write(app)
        with app.app_context():
            row = db.read_ai_exchange()[0]
        assert PATIENT_ID not in row['patient_ref']
        assert row['patient_ref'].startswith('ref_')


class TestAppendOnly:
    """Enforced by triggers, not convention: a record that can be edited after
    a complaint is not evidence of anything."""

    def test_updates_are_refused(self, app):
        _write(app)
        with app.app_context():
            connection = db.get_connection()
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("UPDATE ai_exchange SET response = 'rewritten'")

    def test_deletes_are_refused(self, app):
        _write(app)
        with app.app_context():
            connection = db.get_connection()
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute('DELETE FROM ai_exchange')

    def test_the_row_survives_the_attempt(self, app):
        _write(app, response='original')
        with app.app_context():
            connection = db.get_connection()
            for statement in ("UPDATE ai_exchange SET response = 'x'",
                              'DELETE FROM ai_exchange'):
                with pytest.raises(sqlite3.IntegrityError):
                    connection.execute(statement)
            assert db.read_ai_exchange()[0]['response'] == 'original'


class TestFailureHandling:
    def test_a_write_failure_returns_none_rather_than_raising(self, app, monkeypatch):
        """Losing the trail is bad; taking a clinician's screen down because a
        log write failed is worse. The caller gets None so it can react."""
        def _boom():
            raise sqlite3.OperationalError('disk full')

        with app.app_context():
            monkeypatch.setattr(db, 'get_connection', _boom)
            assert db.write_ai_exchange(
                actor='a', patient_ref='ref_x', feature='chat',
                model='m', prompt_hash='h', response='r',
            ) is None


class FakeProvider:
    """Stands in for a model backend. Records what it was asked, returns fixed
    text -- so a test asserts on the prompt that was built, not on a model."""

    def __init__(self, chunks=('The record lists ', 'a penicillin allergy ',
                               '[AllergyIntolerance/a1].')):
        self.chunks = chunks
        self.model = 'fake-model'
        self.seen_messages = None

    def check(self):
        return ProviderStatus(model=self.model, key_configured=True,
                              key_valid=True, model_available=True)

    def stream_chat(self, messages, **_kwargs):
        self.seen_messages = messages
        yield from self.chunks


class TestSubstrateComposes:
    """Context -> prompt -> provider -> exchange record, with no network."""

    def test_a_full_exchange_is_assembled_and_recorded(self, app):
        provider = FakeProvider()
        context = build_patient_context(everything(*FULL_RECORD), PATIENT_ID)
        messages, version = build_messages('summary', context.text)

        response = ''.join(provider.stream_chat(messages))

        with app.app_context():
            row_id = db.write_ai_exchange(
                actor='clinician1',
                patient_ref=pseudonymize(PATIENT_ID),
                feature='summary',
                model=provider.model,
                prompt_hash=prompt_hash(messages),
                prompt_version=version,
                deidentified=context.deidentified,
                input_tokens=context.estimated_tokens,
                response=response,
            )
            row = db.read_ai_exchange()[0]

        assert row_id is not None
        assert '[AllergyIntolerance/a1]' in row['response']
        assert row['prompt_version'] == version
        assert row['prompt_hash'] == prompt_hash(messages)
        assert provider.seen_messages[0]['role'] == 'system'
        assert 'Penicillin' in provider.seen_messages[1]['content']

    def test_the_stored_prompt_hash_does_not_leak_the_record(self, app):
        context = build_patient_context(everything(*FULL_RECORD), PATIENT_ID)
        messages, _version = build_messages('summary', context.text)
        with app.app_context():
            db.write_ai_exchange(
                actor='a', patient_ref=pseudonymize(PATIENT_ID), feature='summary',
                model='fake-model', prompt_hash=prompt_hash(messages), response='ok',
            )
            row = db.read_ai_exchange()[0]
        assert 'Ada Lovelace' not in row['prompt_hash']

    def test_no_network_client_is_ever_constructed(self, monkeypatch):
        """Guards the CI property directly: if a future change makes context
        assembly reach for a provider, this fails."""
        import ai.provider

        def _fail(*_args, **_kwargs):
            raise AssertionError('context assembly must not construct a client')

        monkeypatch.setattr(ai.provider.OpenAIProvider, '_client', _fail)
        context = build_patient_context(everything(*FULL_RECORD), PATIENT_ID)
        assert context.ok is True


class TestSchemaIsCreatedOnUpgrade:
    def test_an_existing_database_gains_the_table(self, tmp_path):
        """`CREATE TABLE IF NOT EXISTS` in the shared schema means an install
        from before Phase 4 picks this up on the next boot, with no migration
        step for the operator to run and forget."""
        from app import create_app
        from config import TestConfig

        path = tmp_path / 'existing.db'
        legacy = sqlite3.connect(path)
        legacy.execute('CREATE TABLE users (username TEXT PRIMARY KEY, '
                       'password_hash TEXT NOT NULL, display_name TEXT, '
                       "role TEXT NOT NULL DEFAULT 'viewer', "
                       "status TEXT NOT NULL DEFAULT 'pending', "
                       'created_at TEXT NOT NULL, last_login_at TEXT)')
        legacy.commit()
        legacy.close()

        class _Config(TestConfig):
            DATABASE_PATH = str(path)
            RATELIMIT_ENABLED = False

        application = create_app(_Config)
        with application.app_context():
            assert db.read_ai_exchange() == []
