"""HTTP client for the FHIR server.

Two things this exists to guarantee:

1. Every request has a timeout. A hung upstream must never pin a worker.
2. Callers can tell "the server said there are no medications" apart from
   "we could not reach the server". Rendering the second as the first is a
   patient-safety problem, not a cosmetic one.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_WORKERS = 6


class FhirResult:
    """Outcome of a single FHIR read.

    `ok` False means the request failed. `ok` True with an empty `entries`
    means the server genuinely holds no matching resources.
    """

    __slots__ = ('ok', 'data', 'status_code', 'error')

    def __init__(self, ok, data=None, status_code=None, error=None):
        self.ok = ok
        self.data = data or {}
        self.status_code = status_code
        self.error = error

    @property
    def entries(self):
        """Resources from a searchset Bundle, in order."""
        if not self.ok:
            return []
        return [
            entry['resource']
            for entry in self.data.get('entry') or []
            if isinstance(entry, dict) and isinstance(entry.get('resource'), dict)
        ]

    @property
    def total(self):
        """Server-reported match count, when it sent one."""
        return self.data.get('total') if self.ok else None

    def __repr__(self):
        return f'<FhirResult ok={self.ok} status={self.status_code} error={self.error!r}>'


class FhirClient:
    """Connection-pooled, retrying, always-timed-out FHIR client."""

    def __init__(self, base_url, timeout=DEFAULT_TIMEOUT, max_workers=DEFAULT_MAX_WORKERS):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.max_workers = max_workers

        self._session = requests.Session()
        self._session.headers.update({'Accept': 'application/fhir+json'})

        retry = Retry(
            total=2,
            backoff_factor=0.3,
            status_forcelist=(429, 502, 503, 504),
            allowed_methods=frozenset(['GET']),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
        self._session.mount('http://', adapter)
        self._session.mount('https://', adapter)

    def get(self, path, params=None):
        """GET a FHIR path. Never raises -- failures come back as FhirResult(ok=False)."""
        return self._request(f'{self.base_url}/{path.lstrip("/")}', params)

    def _request(self, url, params=None):
        # Log the URL path only -- slicing len(base_url) garbles upgraded
        # https pagination URLs, and dropping the query string keeps
        # patient-scoped search parameters out of the logs.
        path = urlsplit(url).path or '/'
        try:
            response = self._session.get(url, params=params, timeout=self.timeout)
        except requests.exceptions.Timeout:
            logger.warning('FHIR request timed out', extra={'fhir_path': path})
            return FhirResult(False, error='The record server did not respond in time.')
        except requests.exceptions.RequestException as exc:
            logger.warning('FHIR request failed: %s', exc, extra={'fhir_path': path})
            return FhirResult(False, error='Could not reach the record server.')
        except Exception:
            # Anything requests did not wrap. Logged at exception level so the
            # cause stays visible, but a single panel must not take the page down.
            logger.exception('Unexpected error during FHIR request',
                             extra={'fhir_path': path})
            return FhirResult(False, error='Could not reach the record server.')

        if response.status_code == 404:
            return FhirResult(False, status_code=404, error='Not found on the record server.')

        if not response.ok:
            logger.warning(
                'FHIR request returned %s', response.status_code,
                extra={'fhir_path': path},
            )
            return FhirResult(
                False,
                status_code=response.status_code,
                error=f'The record server returned an error ({response.status_code}).',
            )

        try:
            payload = response.json()
        except ValueError:
            logger.warning('FHIR response was not valid JSON', extra={'fhir_path': path})
            return FhirResult(
                False,
                status_code=response.status_code,
                error='The record server returned a malformed response.',
            )

        return FhirResult(True, data=payload, status_code=response.status_code)

    def get_absolute(self, url):
        """Follow an absolute Bundle link (pagination).

        Restricted to the configured server. Bundle links are server-supplied
        data, so following one unchecked would turn pagination into a
        server-side request forgery primitive.

        The check is structural rather than a naive string prefix, because real
        servers mint cursor links that a prefix test wrongly rejects: HAPI
        answers an http:// deployment with https:// links (an upgrade we allow;
        a downgrade we do not), and its paging cursors hang the query string
        directly off the base path with no trailing slash
        (`.../baseR4?_getpages=...`).
        """
        if not isinstance(url, str) or not url:
            return FhirResult(False, error='Missing pagination link.')

        base = urlsplit(self.base_url)
        target = urlsplit(url)
        scheme_ok = target.scheme == base.scheme or (
            base.scheme == 'http' and target.scheme == 'https'
        )
        base_path = base.path.rstrip('/')
        path_ok = target.path.rstrip('/') == base_path or \
            target.path.startswith(base_path + '/')

        if not (scheme_ok and target.netloc == base.netloc and path_ok):
            logger.warning('Refused to follow off-server pagination link')
            return FhirResult(False, error='Invalid pagination link.')
        return self._request(url)

    def get_many(self, requests_spec):
        """Run several reads concurrently.

        `requests_spec` maps a caller-chosen key to (path, params). Returns a
        dict of the same keys to FhirResult. Used so that a panel needing four
        queries costs one round trip's latency rather than four.
        """
        if not requests_spec:
            return {}

        results = {}
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(requests_spec))) as pool:
            futures = {
                key: pool.submit(self.get, path, params)
                for key, (path, params) in requests_spec.items()
            }
            for key, future in futures.items():
                try:
                    results[key] = future.result()
                except Exception as exc:  # defensive: get() already swallows its own
                    logger.exception('Concurrent FHIR read failed for %s', key)
                    results[key] = FhirResult(False, error=str(exc))
        return results

    def count(self, resource_type, params=None):
        """Server-side total for a resource type, or None if unavailable.

        Uses `_summary=count` so the server reports the real population rather
        than however many entries happened to fit in one page.
        """
        query = dict(params or {})
        query['_summary'] = 'count'
        result = self.get(resource_type, params=query)
        return result.total if result.ok else None

    def capability_ok(self):
        """Cheap liveness probe against the server's metadata endpoint."""
        return self.get('metadata', params={'_summary': 'true'}).ok
