"""Offline tests: no sockets and no real accounts; requests prepares real cookies."""
import contextlib
import io
import json
import socket
import unittest
from email.message import Message
from types import SimpleNamespace
from urllib.parse import parse_qs, quote

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from chaoxing_direct import (
    ChaoxingDirectClient, DirectClientError, OutcomeUnknown,
    LOGIN_ENDPOINT, PROFILE_ENDPOINT, encrypt_login_field, require_official_url,
)


HTML = ''.join(f'<input type="hidden" id="{k}" name="{k}" value="{v}">'
    for k, v in {"fid":"-1", "refer":"https%3A%2F%2Fi.chaoxing.com", "t":"true",
        "forbidotherlogin":"0", "doubleFactorLogin":"0", "independentId":"0",
        "independentNameId":"0", "needVcode":"", "_blank":"1"}.items())


class FakeAdapter(BaseAdapter):
    """In-memory official HTTPS responses, including requests-compatible Set-Cookie."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append((request, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected HTTP request")
        spec = self.responses.pop(0)
        if isinstance(spec, Exception):
            raise spec
        response = requests.Response()
        response.status_code = spec.get("status", 200)
        response.url = request.url
        response.request = request
        response.headers = CaseInsensitiveDict(spec.get("headers", {}))
        body = spec.get("body", "")
        response._content = (json.dumps(body) if isinstance(body, (dict, list)) else body).encode()
        response.encoding = "utf-8"
        message = Message()
        for cookie in spec.get("cookies", []):
            message.add_header("Set-Cookie", cookie)
        response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=message))
        return response

    def close(self):
        pass


def client(responses):
    session = requests.Session()
    adapter = FakeAdapter(responses)
    session.mount("https://", adapter)
    return ChaoxingDirectClient("offline-test-agent", timeout=7, session=session), adapter


class DirectClientTests(unittest.TestCase):
    def test_aes_known_vector_matches_existing_node_reference(self):
        self.assertEqual(encrypt_login_field("offline-example"), "5buRxnKECG2TgfeTi+V3gg==")

    def test_allowlist(self):
        for url in ["https://chaoxing.com/", "https://passport2.chaoxing.com/a"]:
            self.assertEqual(require_official_url(url), url)
        for url in ["http://passport2.chaoxing.com/", "https://chaoxing.com.evil.test/",
            "https://badchaoxing.com/", "https://a@chaoxing.com/", "https://chaoxing.com:444/",
            "https://localhost/", "https://chaoxing.com/\nsecret"]:
            with self.subTest(url=url), self.assertRaises(DirectClientError):
                require_official_url(url)

    def test_full_login_uses_cookie_jar_and_dynamic_fields(self):
        c, a = client([
            {"body":HTML.replace('value="-1"','value="123"'), "cookies":["bootstrap=one; Path=/; Secure"]},
            {"body":{"status":True,"url":quote("https://i.chaoxing.com/home", safe=""),"cookies":"FORGED=yes"},
             "cookies":["session=real; Domain=.chaoxing.com; Path=/; Secure"]},
            {"status":302,"headers":{"Location":"https://sso.chaoxing.com/finish"}},
            {"body":"done"}, {"body":{"msg":{"puid":"sample"}}},
        ])
        result = c.login(" sample-user ", " spaced password ")
        self.assertEqual(result.state, "login_reported_success")
        login = a.sent[1][0]
        self.assertEqual(login.url, LOGIN_ENDPOINT)
        form = parse_qs(login.body, keep_blank_values=True)
        self.assertEqual(form["fid"], ["123"])
        self.assertEqual(form["uname"], [encrypt_login_field("sample-user")])
        self.assertEqual(form["password"], [encrypt_login_field(" spaced password ")])
        self.assertIn("bootstrap=one", login.headers["Cookie"])
        self.assertNotIn("bootstrap=", a.sent[2][0].headers.get("Cookie", ""))
        self.assertIn("session=real", a.sent[2][0].headers["Cookie"])
        self.assertNotIn("FORGED", c.session.cookies.get_dict())
        profile = c.get_profile()
        self.assertEqual(profile.raw, {"msg":{"puid":"sample"}})
        self.assertFalse(profile.schema_verified)
        self.assertNotIn("sample", repr(profile))
        self.assertEqual(a.sent[-1][0].url, PROFILE_ENDPOINT)
        self.assertTrue(all(x[1]["timeout"] == 7 for x in a.sent))

    def test_captcha_requires_token_before_password_post(self):
        c, a = client([{"body":HTML.replace('id="needVcode" name="needVcode" value=""',
            'id="needVcode" name="needVcode" value="1"')}])
        self.assertEqual(c.login("example", "secret").state, "challenge_required")
        self.assertEqual(len(a.sent), 1)

    def test_result_branches_do_not_expose_sensitive_server_fields(self):
        cases = [({"status":False,"msg2":"secret-user secret-password"}, "login_rejected"),
            ({"status":False,"weakpwd":True,"uid":"secret-user"}, "password_change_required"),
            ({"status":True,"containTwoFactorLogin":True,"url":"https://i.chaoxing.com/",
              "twoFactorLoginPCUrl":"https://passport2.chaoxing.com/two?token=secret-token"}, "two_factor_required")]
        for payload, state in cases:
            c, a = client([{"body":HTML}, {"body":payload}])
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = c.login("secret-user", "secret-password")
            self.assertEqual(result.state, state)
            self.assertEqual(len(a.sent), 2)
            self.assertEqual(output.getvalue(), "")
            self.assertNotIn("secret", repr(result))

    def test_external_redirect_blocked_before_second_request(self):
        c, a = client([{"status":302,"headers":{"Location":"https://outside.test/?token=secret"}}])
        with self.assertRaises(DirectClientError) as error:
            c.request("GET", PROFILE_ENDPOINT)
        self.assertEqual(error.exception.code, "unsafe_url")
        self.assertEqual(len(a.sent), 1)
        self.assertNotIn("secret", str(error.exception))

    def test_redirect_method_and_cross_host_authorization(self):
        c, a = client([{"status":302,"headers":{"Location":"https://sso.chaoxing.com/ok"}}, {"body":"ok"}])
        c.request("POST", LOGIN_ENDPOINT, data={"a":"b"}, headers={"Authorization":"Bearer secret"})
        self.assertEqual(a.sent[1][0].method, "GET")
        self.assertIsNone(a.sent[1][0].body)
        self.assertNotIn("Authorization", a.sent[1][0].headers)
        c, a = client([{"status":307,"headers":{"Location":"/next"}}, {"body":"ok"}])
        c.request("POST", LOGIN_ENDPOINT, data={"a":"b"})
        self.assertEqual(a.sent[1][0].method, "POST")
        self.assertEqual(a.sent[1][0].body, "a=b")

    def test_proxy_protocol_rejected(self):
        for kwargs in [{"headers":{"X-Proxy-Cookie":"secret"}}, {"params":{"encode":"secret"}},
            {"data":"encode=secret"}, {"json":{"encode":"secret"}}, {"headers":{"Cookie":"raw=secret"}}]:
            c, a = client([])
            with self.subTest(kwargs=list(kwargs)), self.assertRaises(DirectClientError):
                c.request("POST", LOGIN_ENDPOINT, **kwargs)
            self.assertEqual(a.sent, [])

    def test_timeouts_unknown_no_retry_or_secret_exception(self):
        c, a = client([requests.ReadTimeout("secret-url-and-password")])
        with self.assertRaises(OutcomeUnknown) as error:
            c.request("POST", LOGIN_ENDPOINT, data={"uname":"synthetic"})
        self.assertEqual(len(a.sent), 1)
        self.assertNotIn("secret", str(error.exception))

    def test_connection_interruption_also_has_unknown_outcome(self):
        c, a = client([requests.ConnectionError("secret-url-and-password")])
        with self.assertRaises(DirectClientError) as error:
            c.request("POST", LOGIN_ENDPOINT, data={"uname":"synthetic"})
        self.assertEqual(error.exception.code, "network_outcome_unknown")
        self.assertEqual(len(a.sent), 1)
        self.assertNotIn("secret", str(error.exception))

    def test_transport_diagnostics_are_categories_without_exception_text(self):
        cases = [
            (requests.ConnectTimeout("secret-url-and-password"), "connect_timeout"),
            (requests.ReadTimeout("secret-url-and-password"), "read_timeout"),
            (requests.exceptions.SSLError("secret-url-and-password"), "tls_error"),
            (requests.exceptions.ProxyError("secret-url-and-password"), "proxy_error"),
            (requests.ConnectionError(socket.gaierror(-1, "secret-host")), "dns_error"),
            (requests.ConnectionError("secret-url-and-password"), "connection_error"),
        ]
        for exception, reason in cases:
            c, a = client([exception])
            with self.subTest(reason=reason), self.assertRaises(DirectClientError) as error:
                c.request("POST", LOGIN_ENDPOINT, data={"password": "secret-password"})
            self.assertEqual(error.exception.details, {"transport_reason": reason})
            self.assertNotIn("secret", str(error.exception))
            self.assertNotIn("secret", repr(error.exception))
            self.assertEqual(len(a.sent), 1)
        private = DirectClientError("test_error", "secret-message", {"private": "secret-detail"})
        self.assertNotIn("secret", repr(private))

    def test_http_failure_diagnostic_preserves_only_status(self):
        c, a = client([{"status":503, "body":"secret-response"}])
        with self.assertRaises(DirectClientError) as error:
            c.bootstrap()
        self.assertEqual(error.exception.details, {"http_status":503})
        self.assertEqual(c.last_login_phase, "bootstrap")
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(len(a.sent), 1)

    def test_login_navigation_resolves_relative_and_upgrades_http_before_sending(self):
        cases = [
            ("/finish?ticket=synthetic", "https://passport2.chaoxing.com/finish?ticket=synthetic"),
            ("finish", "https://passport2.chaoxing.com/finish"),
            ("//i.chaoxing.com/finish", "https://i.chaoxing.com/finish"),
            ("http://i.chaoxing.com/finish", "https://i.chaoxing.com/finish"),
            (quote("http://i.chaoxing.com:80/finish", safe=""), "https://i.chaoxing.com/finish"),
        ]
        for target, expected in cases:
            c, a = client([{"body":HTML}, {"body":{"status":True,"url":target}}, {"body":"done"}])
            with self.subTest(target=target):
                self.assertEqual(c.login("synthetic", "synthetic").state, "login_reported_success")
            self.assertEqual(a.sent[-1][0].url, expected)
            self.assertTrue(all(row[0].url.startswith("https://") for row in a.sent))
            self.assertEqual(c.last_login_phase, "login_navigation")

    def test_login_navigation_upgrade_does_not_expand_host_or_port_allowlist(self):
        for target in ("http://outside.test/", "http://i.chaoxing.com:444/", "http://user@i.chaoxing.com/",
                       "http://chaoxing.com.evil.test/", "//outside.test/", "http://i.chaoxing.com/%0asecret"):
            c, a = client([{"body":HTML}, {"body":{"status":True,"url":target}}])
            with self.subTest(target=target), self.assertRaises(DirectClientError) as error:
                c.login("synthetic", "synthetic")
            self.assertEqual(error.exception.code, "unsafe_url")
            self.assertEqual(len(a.sent), 2)
            self.assertEqual(c.last_login_phase, "login_navigation")
        c, a = client([])
        with self.assertRaises(DirectClientError):
            c.request("GET", "http://i.chaoxing.com/")
        self.assertEqual(a.sent, [])

    def test_login_phase_identifies_submit_and_navigation_failures(self):
        cases = [
            ([requests.ConnectionError("secret")], "bootstrap"),
            ([{"body":HTML}, requests.ConnectionError("secret")], "login_submit"),
            ([{"body":HTML}, {"body":{"status":True,"url":"/finish"}}, requests.ConnectionError("secret")], "login_navigation"),
        ]
        for responses, phase in cases:
            c, a = client(responses)
            with self.subTest(phase=phase), self.assertRaises(DirectClientError):
                c.login("synthetic", "synthetic")
            self.assertEqual(c.last_login_phase, phase)

    def test_login_navigation_upgrades_every_official_location_without_decoding_tokens(self):
        c, a = client([
            {'body':HTML}, {'body':{'status':True,'url':'https://i.chaoxing.com/finish'}},
            {'status':302,'headers':{'Location':'http://sso.cxwfw.com:80/finish?ticket=synthetic%2Ftoken'}},
            {'status':307,'headers':{'Location':'http://sso.xuexitong.com/final'}},
            {'body':'done'},
        ])
        self.assertEqual(c.login('synthetic', 'synthetic').state, 'login_reported_success')
        self.assertEqual(a.sent[3][0].url, 'https://sso.cxwfw.com/finish?ticket=synthetic%2Ftoken')
        self.assertEqual(a.sent[4][0].url, 'https://sso.xuexitong.com/final')
        self.assertTrue(all(row[0].url.startswith('https://') for row in a.sent))
        self.assertTrue(all(row[0].method == 'GET' and row[0].body is None for row in a.sent[2:]))

    def test_login_navigation_rejects_unsafe_locations_with_categorical_diagnostics(self):
        cases = [
            ('http://private-name.evil.test/?token=secret', 'host', 'other', 'http'),
            ('http://user:secret@sso.chaoxing.com/finish', 'userinfo', 'chaoxing.com', 'http'),
            ('http://sso.xuexitong.com:444/finish', 'port', 'xuexitong.com', 'http'),
            ('ftp://sso.cxwfw.com/finish', 'scheme', 'cxwfw.com', 'other'),
            ('https://sso.chaoxing.com/\nsecret', 'control', 'chaoxing.com', 'https'),
        ]
        for target, issue, domain, scheme in cases:
            c, a = client([{'body':HTML}, {'body':{'status':True,'url':'/finish'}},
                           {'status':302,'headers':{'Location':target}}])
            with self.subTest(issue=issue), self.assertRaises(DirectClientError) as error:
                c.login('synthetic', 'synthetic')
            self.assertEqual(error.exception.details, {'target_scheme':scheme, 'target_domain':domain,
                                                       'url_issue':issue, 'phase':'login_navigation'})
            self.assertEqual(len(a.sent), 3)
            self.assertNotIn('secret', repr(error.exception.details))
            self.assertNotIn('private-name', repr(error.exception.details))

    def test_plain_requests_stay_https_only_and_navigation_cannot_send_post(self):
        c, a = client([{'status':302,'headers':{'Location':'http://sso.chaoxing.com/finish'}}])
        with self.assertRaises(DirectClientError) as error:
            c.request('GET', PROFILE_ENDPOINT)
        self.assertEqual(error.exception.details['url_issue'], 'scheme')
        self.assertEqual(len(a.sent), 1)
        c, a = client([])
        with self.assertRaises(DirectClientError):
            c.request('POST', LOGIN_ENDPOINT, data={'password':'synthetic'}, _login_navigation=True)
        self.assertEqual(a.sent, [])

    def test_cross_origin_body_redirect_is_not_replayed(self):
        for status, method in [(307, "POST"), (308, "POST"), (301, "PUT"), (301, "PATCH")]:
            c, a = client([{"status":status,"headers":{"Location":"https://sso.chaoxing.com/next"}}])
            with self.subTest(status=status, method=method), self.assertRaises(DirectClientError) as error:
                c.request(method, LOGIN_ENDPOINT, data={"password":"synthetic"})
            self.assertEqual(error.exception.code, "cross_origin_body_redirect")
            self.assertEqual(len(a.sent), 1)

    def test_malformed_login_status_is_not_success(self):
        for status in ("false", "true", 1, [], None):
            c, a = client([{"body":HTML}, {"body":{"status":status,"url":"https://i.chaoxing.com/"}}])
            with self.subTest(status=status), self.assertRaises(DirectClientError) as error:
                c.login("synthetic", "synthetic")
            self.assertEqual(error.exception.code, "invalid_login_response")
            self.assertEqual(len(a.sent), 2)

    def test_unknown_t_is_rejected_and_false_is_explicit_official_rule(self):
        c, a = client([{"body":HTML.replace('name="t" value="true"', 'name="t" value="unexpected"')}])
        with self.assertRaises(DirectClientError) as error:
            c.login("synthetic", "synthetic")
        self.assertEqual(error.exception.code, "unexpected_login_mode")
        self.assertEqual(len(a.sent), 1)
        c, a = client([{"body":HTML.replace('name="t" value="true"', 'name="t" value="false"')},
                       {"body":{"status":False}}])
        c.login("synthetic", "synthetic")
        self.assertEqual(parse_qs(a.sent[1][0].body)["password"], ["synthetic"])

    def test_unexpected_response(self):
        for spec, code in [({"status":500}, "http_error"), ({"body":"not-json"}, "invalid_json"),
                           ({"body":[]}, "invalid_login_response")]:
            c, _ = client([{"body":HTML}, spec])
            with self.subTest(code=code), self.assertRaises(DirectClientError) as error:
                c.login("synthetic", "synthetic")
            self.assertEqual(error.exception.code, code)


if __name__ == "__main__":
    unittest.main()
