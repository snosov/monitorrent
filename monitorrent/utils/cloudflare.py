# -*- coding: utf-8 -*-
"""HTTP for trackers behind a Cloudflare challenge that a plain client cannot pass.

Cloudflare answers python-requests on the protected paths with a 403 challenge
page, and neither browser headers nor a copied cf_clearance change that.
What works is a pair of pieces:

* FlareSolverr, which drives a real browser through the challenge and returns
  the cf_clearance cookie together with the User-Agent it was issued for;
* curl_cffi impersonating Chrome, because Cloudflare only honours that cookie
  from a client whose TLS/HTTP2 fingerprint matches a browser. The same cookie
  sent from python-requests is refused.

A clearance is cached per host and reused until Cloudflare challenges again,
at which point it is solved once more and the request retried.

Nothing changes unless MONITORRENT_FLARESOLVERR_URL is set.
"""
import math
import os
import threading

import requests
import structlog

try:
    from six.moves.urllib.parse import urlparse
except ImportError:  # pragma: no cover
    from urllib.parse import urlparse

try:
    from curl_cffi import requests as cffi_requests, CurlOpt
    from curl_cffi.requests.exceptions import RequestException as CffiRequestException
except ImportError:  # pragma: no cover - optional dependency
    cffi_requests = None
    CurlOpt = None
    CffiRequestException = None

log = structlog.get_logger()

FLARESOLVERR_URL_ENV = 'MONITORRENT_FLARESOLVERR_URL'
FLARESOLVERR_TIMEOUT_ENV = 'MONITORRENT_FLARESOLVERR_TIMEOUT'

# callers that used to catch requests' errors need curl_cffi's as well: they
# are not subclasses of requests.exceptions.RequestException
HTTP_ERRORS = tuple(e for e in (requests.exceptions.RequestException, CffiRequestException) if e is not None)


def is_cloudflare_challenge(resp):
    """Whether this response is an interstitial challenge rather than the page.

    The old check was `'Cloudflare' in resp.text`, which the current managed
    challenge does not satisfy: it spells the name in lowercase only, so every
    challenge looked like a clean page and the solver was never invoked.
    Match on the response metadata first and fall back to case-insensitive
    body markers.
    """
    if resp.headers.get('cf-mitigated') == 'challenge':
        return True
    if resp.status_code not in (403, 503):
        return False
    body = (resp.text or '').lower()
    return any(marker in body for marker in ('just a moment', 'challenge-platform', '__cf_chl', 'cloudflare'))


class CloudflareSolverError(Exception):
    pass


def _is_cloudflare_cookie(name):
    return name.startswith(('cf_', '__cf'))


def curl_timeout_options(timeout):
    """python-requests timeout semantics, expressed as curl options.

    requests treats a timeout as connect time plus the longest gap between
    bytes, with no cap on the transfer as a whole. curl_cffi turns a scalar
    timeout into CURLOPT_TIMEOUT, which caps the entire transfer - so a slow
    but steady torrent download that requests would have finished was cut off
    after 10 seconds with bytes still arriving. LOW_SPEED_LIMIT of one byte a
    second over LOW_SPEED_TIME is curl's equivalent of the read timeout.
    """
    if timeout is None:
        return None
    connect, read = timeout if isinstance(timeout, (tuple, list)) else (timeout, timeout)
    return {
        CurlOpt.CONNECTTIMEOUT_MS: int(connect * 1000),
        CurlOpt.LOW_SPEED_LIMIT: 1,
        CurlOpt.LOW_SPEED_TIME: max(1, int(math.ceil(read))),
    }


class CloudflareSolverSession(object):
    def __init__(self, solver_url, impersonate='chrome', max_timeout=60000):
        self.solver_url = solver_url
        self.impersonate = impersonate
        self.max_timeout = max_timeout
        self._clearance = {}
        self._lock = threading.Lock()

    def new_session(self):
        """A cookie-keeping session, for flows such as login that span redirects."""
        return cffi_requests.Session(impersonate=self.impersonate)

    def request(self, method, url, cookies=None, headers=None, solve_url=None, session=None, **kwargs):
        """Send a request, solving the challenge once and retrying if one comes back.

        :param solve_url: page to put through the solver when this url itself is
            unsuitable - a download link makes the solver's browser save a file
            instead of rendering a page
        """
        host = urlparse(url).hostname
        response = self._send(method, url, host, cookies, headers, session, **kwargs)
        if not is_cloudflare_challenge(response):
            return response
        self.solve(solve_url or url, cookies)
        return self._send(method, url, host, cookies, headers, session, **kwargs)

    def solve(self, url, cookies=None):
        host = urlparse(url).hostname
        with self._lock:
            payload = {'cmd': 'request.get', 'url': url, 'maxTimeout': self.max_timeout}
            session_cookies = {k: v for k, v in (cookies or {}).items() if not _is_cloudflare_cookie(k)}
            if session_cookies:
                # the page has to be solved as the signed in user, or kinozal
                # hands the browser its login form instead. A caller's own
                # cloudflare cookies are left out: they are what failed.
                payload['cookies'] = [{'name': k, 'value': v, 'domain': host} for k, v in session_cookies.items()]
            try:
                reply = requests.post(self.solver_url, json=payload, timeout=self.max_timeout / 1000.0 + 30)
                reply.raise_for_status()
                data = reply.json()
            except (requests.exceptions.RequestException, ValueError) as e:
                log.error('FlareSolverr is unreachable', solver_url=self.solver_url, error=str(e))
                raise CloudflareSolverError(u"FlareSolverr at {0} is unreachable: {1}".format(self.solver_url, e))
            if data.get('status') != 'ok':
                log.error('FlareSolverr could not solve the challenge', url=url, message=data.get('message'))
                raise CloudflareSolverError(u"FlareSolverr could not solve {0}: {1}".format(url, data.get('message')))
            solution = data.get('solution') or {}
            self._clearance[host] = {
                'cookies': {c['name']: c['value'] for c in solution.get('cookies', [])
                            if _is_cloudflare_cookie(c['name'])},
                'user_agent': solution.get('userAgent'),
            }
            log.info('Solved cloudflare challenge', host=host)

    def _send(self, method, url, host, cookies, headers, session, **kwargs):
        clearance = self._clearance.get(host, {})
        # the solver's clearance wins over one the caller brings along - e.g.
        # a cf_clearance pasted into rutracker's settings, long since expired
        all_cookies = dict(cookies or {})
        all_cookies.update(clearance.get('cookies', {}))
        all_headers = dict(headers or {})
        if clearance.get('user_agent'):
            # cf_clearance is bound to the agent it was issued for
            all_headers['User-Agent'] = clearance['user_agent']
        if session is not None:
            # Session.request takes no per-request curl options, so a session
            # keeps curl's whole-transfer timeout. It is only used for login,
            # whose pages are small enough for that not to matter.
            return session.request(method, url, cookies=all_cookies or None, headers=all_headers or None, **kwargs)
        timeout = kwargs.pop('timeout', None)
        return cffi_requests.request(method, url, cookies=all_cookies or None, headers=all_headers or None,
                                     impersonate=self.impersonate, timeout=None,
                                     curl_options=curl_timeout_options(timeout), **kwargs)


class SolverRequest(object):
    """A download for ExecuteWithHashChangeMixin that goes through the solver session.

    monitorrent.utils.downloader.download sends a requests.PreparedRequest with
    plain requests, which Cloudflare refuses; it calls fetch() on this instead.
    """
    def __init__(self, session, method, url, headers=None, cookies=None, solve_url=None):
        self.session = session
        self.method = method
        self.url = url
        self.headers = headers
        self.cookies = cookies
        self.solve_url = solve_url

    def fetch(self, **kwargs):
        return self.session.request(self.method, self.url, cookies=self.cookies, headers=self.headers,
                                    solve_url=self.solve_url, **kwargs)


_solver_session = None
_solver_session_lock = threading.Lock()


def get_solver_session():
    """The process-wide solver session, or None when no solver is configured.

    Shared so that one clearance serves every request to a host rather than
    each call paying for its own trip through the browser.
    """
    global _solver_session
    url = os.environ.get(FLARESOLVERR_URL_ENV)
    if not url or cffi_requests is None:
        return None
    with _solver_session_lock:
        if _solver_session is None or _solver_session.solver_url != url:
            timeout = int(os.environ.get(FLARESOLVERR_TIMEOUT_ENV) or 60000)
            _solver_session = CloudflareSolverSession(url, max_timeout=timeout)
        return _solver_session


def get_cookie(jar, name):
    """Read a cookie from a requests or curl_cffi jar.

    .get() raises when the same name is set for more than one domain, which
    kinozal does for uid/pass across www and the bare host.
    """
    try:
        return jar.get(name)
    except Exception:
        for cookie in getattr(jar, 'jar', jar):
            if getattr(cookie, 'name', None) == name:
                return cookie.value
    return None
