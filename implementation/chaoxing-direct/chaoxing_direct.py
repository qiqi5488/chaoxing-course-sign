"""Direct official HTTP transport and observed password-login protocol.

No business request is sent on import or construction. Public-source evidence:
analysis/upstream/public/official-login.js and official-common-util.js.
"""
from base64 import b64encode
from dataclasses import dataclass, field
from html.parser import HTMLParser
import socket
from typing import Any, Mapping
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from urllib3.util.retry import Retry


LOGIN_PAGE = "https://passport2.chaoxing.com/login?fid=&newversion=true&refer=https://i.chaoxing.com"
LOGIN_ENDPOINT = "https://passport2.chaoxing.com/fanyalogin"
PROFILE_ENDPOINT = "https://sso.chaoxing.com/apis/login/userLogin4Uname.do"
OFFICIAL_DOMAINS = ("chaoxing.com", "cxwfw.com", "xuexitong.com")
AES_KEY = b"u2oh6Vu^HWe4_AES"
LOGIN_FIELDS = (
    "fid", "refer", "t", "forbidotherlogin", "doubleFactorLogin",
    "independentId", "independentNameId",
)


class DirectClientError(Exception):
    """Errors deliberately omit URLs, request bodies, server text and credentials."""
    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)

    def __repr__(self):
        # Diagnostic fields may later grow; never include their values or the
        # exception message in a representation that a logger could serialize.
        return f"{type(self).__name__}(code={self.code!r})"


class OutcomeUnknown(DirectClientError):
    def __init__(self, details: Mapping[str, Any] | None = None):
        super().__init__("timeout_unknown", "Request timed out; its server-side outcome is unknown.", details)


@dataclass(frozen=True)
class LoginResult:
    state: str
    message: str
    action_url: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ProfileResult:
    status_code: int
    raw: Any = field(repr=False)
    schema_verified: bool = False


class _HiddenInputs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("type", "").lower() == "hidden":
            name = attrs.get("name") or attrs.get("id")
            if name:
                self.fields[name] = attrs.get("value") or ""


def encrypt_login_field(value: str) -> str:
    """Official t=true field encoding: UTF-8 / AES-CBC / PKCS7 / Base64."""
    ciphertext = AES.new(AES_KEY, AES.MODE_CBC, AES_KEY).encrypt(
        pad(value.encode("utf-8"), AES.block_size)
    )
    return b64encode(ciphertext).decode("ascii")


def _official_domain(host):
    return next((domain for domain in OFFICIAL_DOMAINS if host == domain or host.endswith('.' + domain)), 'other')


def _unsafe_url(url, issue):
    # The hostname and query can contain identity or navigation tokens. Return
    # only fixed categories, including when parsing the original URL fails.
    scheme, domain = 'other', 'other'
    if not isinstance(url, str):
        url = ''
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower() if parsed.scheme.lower() in ('http', 'https') else 'other'
        domain = _official_domain((parsed.hostname or '').lower())
    except (TypeError, ValueError):
        pass
    return DirectClientError('unsafe_url', 'Only official HTTPS URLs are permitted.',
                             {'target_scheme': scheme, 'target_domain': domain, 'url_issue': issue})


def require_official_url(url: str) -> str:
    if not isinstance(url, str) or any(ord(c) <= 32 for c in url):
        raise _unsafe_url(url, 'control')
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
    except ValueError:
        raise _unsafe_url(url, 'host') from None
    if parsed.scheme.lower() != 'https':
        raise _unsafe_url(url, 'scheme')
    if _official_domain(host) == 'other':
        raise _unsafe_url(url, 'host')
    if parsed.username is not None or parsed.password is not None:
        raise _unsafe_url(url, 'userinfo')
    try:
        if parsed.port not in (None, 443):
            raise _unsafe_url(url, 'port')
    except ValueError:
        raise _unsafe_url(url, 'port') from None
    return url


def _login_navigation_url(value: str, base=LOGIN_ENDPOINT, *, decode=True) -> str:
    """Resolve login navigation only, upgrading official HTTP before I/O."""
    if not isinstance(value, str) or not value or any(ord(c) <= 32 for c in value):
        error = _unsafe_url(value, 'control')
        error.details['phase'] = 'login_navigation'
        raise error
    try:
        decoded = unquote(value, errors="strict") if decode else value
        if any(ord(c) <= 32 for c in decoded):
            raise _unsafe_url(decoded, 'control')
        target = urljoin(base, decoded)
        parsed = urlsplit(target)
        host = (parsed.hostname or "").lower()
        if parsed.scheme.lower() == "http":
            if _official_domain(host) == 'other':
                raise _unsafe_url(target, 'host')
            if parsed.username is not None or parsed.password is not None:
                raise _unsafe_url(target, 'userinfo')
            try:
                if parsed.port not in (None, 80):
                    raise _unsafe_url(target, 'port')
            except ValueError:
                raise _unsafe_url(target, 'port') from None
            target = parsed._replace(scheme="https", netloc=host).geturl()
        return require_official_url(target)
    except DirectClientError as error:
        error.details['phase'] = 'login_navigation'
        raise
    except (ValueError, UnicodeDecodeError):
        error = _unsafe_url(value, 'host')
        error.details['phase'] = 'login_navigation'
        raise error from None


def _transport_reason(error):
    """Finite error categories only: never inspect or retain exception text."""
    for kind, reason in (
        (requests.ConnectTimeout, "connect_timeout"),
        (requests.ReadTimeout, "read_timeout"),
        (requests.Timeout, "timeout"),
        (requests.exceptions.SSLError, "tls_error"),
        (requests.exceptions.ProxyError, "proxy_error"),
        (requests.exceptions.ChunkedEncodingError, "response_interrupted"),
        (requests.exceptions.ContentDecodingError, "response_decoding_error"),
    ):
        if isinstance(error, kind):
            return reason
    if isinstance(error, requests.ConnectionError):
        pending, visited = [error], set()
        while pending and len(visited) < 16:
            current = pending.pop()
            if id(current) in visited:
                continue
            visited.add(id(current))
            if isinstance(current, socket.gaierror) or type(current).__name__ == "NameResolutionError":
                return "dns_error"
            pending.extend(value for value in (
                getattr(current, "reason", None), current.__cause__, current.__context__, *current.args
            ) if isinstance(value, BaseException))
        return "connection_error"
    return "request_error"


def _keys(value):
    if isinstance(value, Mapping):
        return value.keys()
    if isinstance(value, (str, bytes)):
        return (k for k, _ in parse_qsl(value.decode() if isinstance(value, bytes) else value))
    return (item[0] for item in (value or []))


def _check_no_proxy(url, headers, params, data, json_body):
    if any(str(k).lower().startswith("x-proxy-") for k in headers):
        raise DirectClientError("proxy_protocol", "Proxy-only headers are not supported.")
    # Cookie headers bypass Session's domain/path rules; import cookies into its jar.
    if any(str(k).lower() == "cookie" for k in headers):
        raise DirectClientError("cookie_header", "Use the Session CookieJar instead of a Cookie header.")
    for value in (dict(parse_qsl(urlsplit(url).query)), params, data, json_body):
        if any(str(k).lower() == "encode" for k in _keys(value)):
            raise DirectClientError("proxy_protocol", "Proxy encode envelopes are not supported.")


class ChaoxingDirectClient:
    def __init__(self, user_agent: str, timeout: float | tuple = (10, 30),
                 session: requests.Session | None = None):
        if not isinstance(user_agent, str) or not user_agent.strip():
            raise ValueError("A caller-provided user_agent is required.")
        self.session = session or requests.Session()
        self.session.trust_env = False  # Avoid implicit .netrc credentials.
        self.session.auth = None
        self.session.headers["User-Agent"] = user_agent
        for adapter in self.session.adapters.values():
            if isinstance(adapter, requests.adapters.HTTPAdapter):
                adapter.max_retries = Retry(total=0, connect=0, read=0, redirect=0, status=0)
        self.timeout = timeout
        self._login_fields: dict[str, str] | None = None
        self.last_login_phase = "bootstrap"

    def close(self):
        self.session.close()

    def request(self, method: str, url: str, *, params=None, data=None,
                json=None, headers: Mapping[str, str] | None = None, _login_navigation=False,
                follow_redirects=True):
        """Send with no retries; validate every redirect before sending its request."""
        method = method.upper()
        if _login_navigation and (method not in ('GET', 'HEAD') or data is not None or json is not None):
            raise DirectClientError('invalid_navigation_request', 'Login navigation must not replay a request body.')
        headers = dict(headers or {})
        require_official_url(url)
        effective_headers = dict(self.session.headers)
        effective_headers.update(headers)
        _check_no_proxy(url, effective_headers, params, data, json)
        for hop in range(11):
            require_official_url(url)
            _check_no_proxy(url, headers, params, data, json)
            try:
                response = self.session.request(
                    method, url, params=params, data=data, json=json, headers=headers,
                    timeout=self.timeout, allow_redirects=False,
                )
            except requests.Timeout as error:
                raise OutcomeUnknown({"transport_reason": _transport_reason(error)}) from None
            except requests.RequestException as error:
                raise DirectClientError("network_outcome_unknown", "HTTP transport was interrupted; the server-side outcome may be unknown. No automatic retry was attempted.",
                                        {"transport_reason": _transport_reason(error)}) from None
            if not follow_redirects or response.status_code not in (301, 302, 303, 307, 308) or not response.headers.get("Location"):
                return response
            if hop == 10:
                raise DirectClientError("redirect_limit", "Too many HTTP redirects.")
            target = (_login_navigation_url(response.headers['Location'], response.url, decode=False)
                      if _login_navigation else require_official_url(urljoin(response.url, response.headers['Location'])))
            changes_to_get = ((response.status_code in (302, 303) and method != "HEAD")
                              or (response.status_code == 301 and method == "POST"))
            if urlsplit(target).hostname.lower() != urlsplit(response.url).hostname.lower():
                if not changes_to_get and (data is not None or json is not None):
                    raise DirectClientError("cross_origin_body_redirect", "Cross-origin redirect would replay a request body; the original request outcome is unknown.")
                # None suppresses the matching Session default header too.
                headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
                headers["Authorization"] = None
            if changes_to_get:
                method, data, json = "GET", None, None
                headers = {k: v for k, v in headers.items() if k.lower() not in ("content-type", "content-length", "transfer-encoding")}
                headers.update({"Content-Type": None, "Content-Length": None, "Transfer-Encoding": None})
            params, url = None, target
        raise AssertionError("unreachable")

    @staticmethod
    def _json(response):
        if not 200 <= response.status_code < 300:
            raise DirectClientError("http_error", f"HTTP response status {response.status_code}.", {"http_status": response.status_code})
        try:
            return response.json()
        except ValueError:
            raise DirectClientError("invalid_json", "Expected a JSON response.") from None

    def bootstrap(self, url: str = LOGIN_PAGE) -> dict[str, str]:
        self.last_login_phase = "bootstrap"
        response = self.request("GET", url)
        if not 200 <= response.status_code < 300:
            raise DirectClientError("http_error", f"Login page HTTP status {response.status_code}.", {"http_status": response.status_code})
        parser = _HiddenInputs()
        parser.feed(response.text)
        if not {"fid", "refer", "t"}.issubset(parser.fields):
            raise DirectClientError("unexpected_login_page", "Required official login fields were not found.")
        if parser.fields["t"] not in ("true", "false"):
            raise DirectClientError("unexpected_login_mode", "Unrecognized login field-encoding mode.")
        self._login_fields = parser.fields
        return {k: parser.fields.get(k, "") for k in LOGIN_FIELDS + ("needVcode",)}

    def login(self, account: str, password: str, *, validate: str = "") -> LoginResult:
        if self._login_fields is None:
            self.bootstrap()
        fields = self._login_fields or {}
        if fields.get("needVcode") == "1" and not validate:
            return LoginResult("challenge_required", "Complete the official login challenge before submitting credentials.")
        if not account.strip() or not password:
            raise ValueError("Nonempty account and password are required.")
        form = {k: fields.get(k, "") for k in LOGIN_FIELDS}
        form.update(uname=account.strip(), password=password, validate=validate)
        if fields.get("t") == "true":
            form["uname"] = encrypt_login_field(form["uname"])
            form["password"] = encrypt_login_field(password)
        # Official JS conditionally encrypts only for t=true. t=false follows its
        # HTTPS form rule without field AES; only t=true was observed in our page.
        self.last_login_phase = "login_submit"
        payload = self._json(self.request("POST", LOGIN_ENDPOINT, data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"}))
        if not isinstance(payload, dict):
            raise DirectClientError("invalid_login_response", "Expected a login result object.")
        if type(payload.get("status")) is not bool:
            raise DirectClientError("invalid_login_response", "Login status has an unverified response shape.")
        # Only HTTP Set-Cookie is consumed by Session; JSON 'cookies' is ignored.
        if payload.get("status"):
            self.last_login_phase = "login_navigation"
            target = payload.get("url")
            if not isinstance(target, str) or not target:
                raise DirectClientError("invalid_login_response", "Successful login did not provide a navigation URL.")
            target = _login_navigation_url(target)
            if payload.get("containTwoFactorLogin"):
                action = require_official_url(payload.get("twoFactorLoginPCUrl"))
                action += ("&" if "?" in action else "?") + urlencode({"_blank": fields.get("_blank", ""), "refer": target})
                return LoginResult("two_factor_required", "Complete the official two-factor login flow.", action)
            navigation = self.request("GET", target, _login_navigation=True)
            if not 200 <= navigation.status_code < 300:
                raise DirectClientError("http_error", f"Login navigation HTTP status {navigation.status_code}.", {"http_status": navigation.status_code})
            return LoginResult("login_reported_success", "Official login reported success and navigation completed; profile schema remains unverified.")
        if payload.get("weakpwd"):
            return LoginResult("password_change_required", "Complete the official password-change flow.")
        return LoginResult("login_rejected", "Official login was not accepted; the cause is not inferred from arbitrary server text.")

    def get_profile(self) -> ProfileResult:
        """Official host/path confirmed; no asserted success schema or normalized identity."""
        response = self.request("GET", PROFILE_ENDPOINT)
        return ProfileResult(response.status_code, self._json(response))
