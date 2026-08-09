"""Tests for the AI provider abstraction and the PHI gate.

No test here reaches the network: the OpenAI client is replaced with a fake.
"""
import httpx
import openai
import pytest

from ai.provider import OpenAIProvider, ProviderStatus, from_config
from config import ai_blocked_reason


class _FakeModel:
    def __init__(self, model_id):
        self.id = model_id


class _FakeModels:
    def __init__(self, ids=None, raises=None):
        self._ids = ids or []
        self._raises = raises

    def list(self):
        if self._raises is not None:
            raise self._raises
        return [_FakeModel(model_id) for model_id in self._ids]


class _FakeClient:
    def __init__(self, ids=None, raises=None):
        self.models = _FakeModels(ids, raises)


def _auth_error():
    return openai.AuthenticationError(
        'bad key', response=httpx.Response(401, request=httpx.Request('GET', 'http://x')),
        body=None,
    )


def _api_error():
    return openai.APIError('boom', request=httpx.Request('GET', 'http://x'), body=None)


def _provider(monkeypatch, model='gpt-5.6-luna', api_key='sk-test', **client_kwargs):
    provider = OpenAIProvider(api_key=api_key, model=model)
    monkeypatch.setattr(provider, '_client', lambda: _FakeClient(**client_kwargs))
    return provider


class TestProviderStatus:
    def test_ok_requires_key_and_model(self):
        assert ProviderStatus().ok is False
        assert ProviderStatus(key_configured=True, key_valid=True).ok is False
        assert ProviderStatus(key_configured=True, key_valid=True,
                              model_available=True).ok is True


class TestProviderCheck:
    def test_missing_key_is_reported_before_any_call(self):
        status = OpenAIProvider(api_key='', model='gpt-5.6-luna').check()
        assert status.ok is False
        assert status.key_configured is False
        assert 'OPENAI_API_KEY' in status.message

    def test_missing_model_is_reported(self):
        status = OpenAIProvider(api_key='sk-test', model='').check()
        assert status.ok is False
        assert status.key_configured is True
        assert 'AI_MODEL' in status.message

    def test_rejected_key(self, monkeypatch):
        provider = _provider(monkeypatch, raises=_auth_error())
        status = provider.check()
        assert status.key_valid is False
        assert 'rejected' in status.message.lower()

    def test_provider_error_does_not_raise(self, monkeypatch):
        provider = _provider(monkeypatch, raises=_api_error())
        status = provider.check()
        assert status.ok is False
        assert status.key_valid is False

    def test_unexpected_exception_is_contained(self, monkeypatch):
        provider = _provider(monkeypatch, raises=RuntimeError('something unwrapped'))
        status = provider.check()
        assert status.ok is False

    def test_valid_key_and_present_model(self, monkeypatch):
        provider = _provider(monkeypatch, ids=['gpt-5.6-luna', 'gpt-4.1'])
        status = provider.check()
        assert status.ok is True
        assert status.key_valid and status.model_available
        assert 'gpt-5.6-luna' in status.message

    def test_valid_key_but_unknown_model_is_a_distinct_failure(self, monkeypatch):
        """A wrong model ID must not read as a bad key -- different fix."""
        provider = _provider(monkeypatch, ids=['gpt-4.1', 'gpt-4o'])
        status = provider.check()
        assert status.key_valid is True      # the key is fine
        assert status.model_available is False   # the model is not
        assert status.ok is False
        assert 'gpt-5.6-luna' in status.message

    def test_near_miss_model_ids_are_suggested(self, monkeypatch):
        provider = _provider(monkeypatch, model='gpt-5.6-lunar',
                             ids=['gpt-5.6-luna', 'gpt-4.1'])
        status = provider.check()
        assert 'gpt-5.6-luna' in status.suggestions

    def test_unrelated_model_still_lists_what_is_available(self, monkeypatch):
        """With no near match, show real IDs anyway -- an empty list would
        leave the operator with nothing to correct the setting to."""
        provider = _provider(monkeypatch, model='zzzzz', ids=['gpt-4.1', 'gpt-4o'])
        status = provider.check()
        assert status.suggestions


class TestFromConfig:
    def test_reads_flask_config(self):
        provider = from_config({
            'OPENAI_API_KEY': 'sk-x', 'AI_MODEL': 'gpt-5.6-luna',
            'AI_REQUEST_TIMEOUT': 30.0, 'AI_REASONING_EFFORT': 'low',
        })
        assert provider.api_key == 'sk-x'
        assert provider.model == 'gpt-5.6-luna'
        assert provider.timeout == 30.0
        assert provider.reasoning_effort == 'low'

    def test_missing_keys_do_not_raise(self):
        provider = from_config({})
        assert provider.api_key == ''
        assert provider.check().ok is False

    @pytest.mark.parametrize('level', ['none', 'low', 'medium', 'high', 'xhigh', 'max'])
    def test_documented_reasoning_levels_pass_through(self, level):
        assert from_config({'AI_REASONING_EFFORT': level}).reasoning_effort == level

    def test_unknown_reasoning_level_is_dropped_not_forwarded(self):
        """An unrecognised value must not reach the provider as-is -- it would
        fail the request rather than the setting."""
        assert from_config({'AI_REASONING_EFFORT': 'ludicrous'}).reasoning_effort == ''


class TestReasoningEffortIsSent:
    def test_configured_effort_reaches_the_request(self, monkeypatch):
        captured = {}

        class _Completions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return []

        class _Chat:
            completions = _Completions()

        class _Client:
            chat = _Chat()

        provider = OpenAIProvider(api_key='sk-x', model='gpt-5.6-luna',
                                  reasoning_effort='low')
        monkeypatch.setattr(provider, '_client', lambda: _Client())
        list(provider.stream_chat([{'role': 'user', 'content': 'hi'}]))
        assert captured['reasoning_effort'] == 'low'
        assert captured['stream'] is True

    def test_blank_effort_omits_the_parameter(self, monkeypatch):
        """Blank is the escape hatch for a model that rejects the parameter."""
        captured = {}

        class _Completions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return []

        class _Chat:
            completions = _Completions()

        class _Client:
            chat = _Chat()

        provider = OpenAIProvider(api_key='sk-x', model='gpt-5.6-luna',
                                  reasoning_effort='')
        monkeypatch.setattr(provider, '_client', lambda: _Client())
        list(provider.stream_chat([{'role': 'user', 'content': 'hi'}]))
        assert 'reasoning_effort' not in captured


class TestPhiGate:
    """"Demo only" is enforced, not merely documented: pointing the app at a
    real FHIR server must disable AI rather than rely on someone reading a
    note in SECURITY.md."""

    def test_known_demo_server_is_allowed(self):
        assert ai_blocked_reason('https://hapi.fhir.org/baseR4', False) is None

    @pytest.mark.parametrize('url', [
        'https://fhir.myhospital.nhs.uk/R4',
        'https://internal.example.com/baseR4',
        '',
    ])
    def test_unknown_server_is_blocked(self, url):
        reason = ai_blocked_reason(url, False)
        assert reason is not None
        assert 'BAA' in reason or 'agreement' in reason

    def test_explicit_override_allows_it(self):
        assert ai_blocked_reason('https://fhir.myhospital.nhs.uk/R4', True) is None

    def test_lookalike_hostname_is_not_treated_as_demo(self):
        """hapi.fhir.org.evil.test must not pass as the demo server."""
        assert ai_blocked_reason('https://hapi.fhir.org.evil.test/baseR4', False) is not None


class TestSettingsRoutes:
    def test_settings_page_is_admin_only(self, auth_client):
        assert auth_client.get('/settings/ai').status_code == 403

    def test_test_endpoint_is_admin_only(self, auth_client):
        assert auth_client.post('/settings/ai/test').status_code == 403

    def test_admin_sees_configuration(self, admin_client, app):
        body = admin_client.get('/settings/ai').get_data(as_text=True)
        assert app.config['AI_MODEL'] in body
        assert 'OPENAI_API_KEY' in body

    def test_key_is_never_rendered_into_the_page(self, admin_client, app):
        app.config['OPENAI_API_KEY'] = 'sk-super-secret-value'
        body = admin_client.get('/settings/ai').get_data(as_text=True)
        assert 'sk-super-secret-value' not in body
        assert 'Configured' in body

    def test_check_endpoint_reports_status_without_a_key(self, admin_client, app):
        app.config['OPENAI_API_KEY'] = ''
        payload = admin_client.post('/settings/ai/test').get_json()
        assert payload['ok'] is False
        assert payload['key_configured'] is False
        assert payload['model'] == app.config['AI_MODEL']

    def test_check_endpoint_surfaces_a_wrong_model(self, admin_client, app, monkeypatch):
        app.config['OPENAI_API_KEY'] = 'sk-test'
        monkeypatch.setattr(
            OpenAIProvider, '_client',
            lambda self: _FakeClient(ids=['gpt-4.1', 'gpt-4o']),
        )
        payload = admin_client.post('/settings/ai/test').get_json()
        assert payload['key_valid'] is True
        assert payload['model_available'] is False
        assert payload['suggestions']

    def test_blocked_phi_gate_is_shown_on_the_page(self, admin_client, app):
        app.config['FHIR_SERVER_URL'] = 'https://fhir.myhospital.nhs.uk/R4'
        app.config['AI_ALLOW_REAL_PHI'] = False
        body = admin_client.get('/settings/ai').get_data(as_text=True)
        assert 'AI features are switched off' in body
