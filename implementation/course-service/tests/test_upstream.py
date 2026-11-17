"""Synthetic upstream HTTP only; these tests never open a network socket."""
from email.message import Message
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import requests
from requests.adapters import BaseAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import upstream
from chaoxing_direct import ChaoxingDirectClient


LOGIN_HTML = ''.join(f'<input type="hidden" name="{key}" value="{value}">'
    for key, value in {"fid": "-1", "refer": "https://i.chaoxing.com", "t": "true",
                       "needVcode": ""}.items())
PROFILE = {"result": 1, "msg": {"uid": "100", "puid": "101", "fid": "200", "name": "测试同学"}}
IDENTITY = {"uid": "100", "fid": "200", "name": "测试同学"}
COOKIES = [{"name": "_uid", "value": "100", "domain": ".chaoxing.com", "path": "/",
            "secure": True, "expires": None, "rest": {"HttpOnly": None}}]
SIGN = {"kind": "ordinary", "activeId": "300", "courseId": "400", "classId": "500"}
PRESIGN = '<html><body>签到<input name="activeId" value="300"></body></html>'
ORDINARY_DETAIL = {"data": {"activeId": "300", "courseId": "400", "clazzid": "500",
    "activeType": 2, "otherId": 0, "ifphoto": 0, "ifopenAddress": 0,
    "openCheckFaceFlag": 0, "starttime": 1_800_000_000_000,
    "endtime": 1_800_001_000_000, "nowTime": 1_800_000_500_000}}


class InMemoryAdapter(BaseAdapter):
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        if not self.responses:
            raise AssertionError("Unexpected upstream request")
        spec = self.responses.pop(0)
        if isinstance(spec, Exception):
            raise spec
        result = requests.Response()
        result.status_code = spec.get("status", 200)
        result.url = request.url
        result.request = request
        result.headers.update(spec.get("headers", {}))
        body = spec.get("body", "")
        result._content = (json.dumps(body) if isinstance(body, (dict, list)) else body).encode()
        result.encoding = "utf-8"
        message = Message()
        for cookie in spec.get("cookies", []):
            message.add_header("Set-Cookie", cookie)
        result.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=message))
        return result

    def close(self):
        pass


class UpstreamTests(unittest.TestCase):
    def test_transport_mapping_keeps_safe_stage_and_drops_exception_text(self):
        error = upstream.DirectClientError('network_outcome_unknown', 'private-password private-cookie')
        error.details = {'transport_reason': 'tls'}
        result = upstream._map_transport(error, 'login_submit')
        self.assertEqual(result.code, 'network_outcome_unknown')
        self.assertEqual(result.details['phase'], 'login_submit')
        self.assertEqual(result.details['transport_reason'], 'tls')
        self.assertIn('TLS', str(result))
        self.assertIn('提交登录', str(result))
        self.assertNotIn('private', str(result))

    def test_cookie_domains_match_the_three_official_service_suffixes(self):
        for domain in ('.chaoxing.com', '.cxwfw.com', '.xuexitong.com'):
            jar = requests.cookies.RequestsCookieJar()
            upstream.load_cookies(jar, [{**COOKIES[0], 'domain': domain}])
            saved = upstream.dump_cookies(jar)
            self.assertEqual(saved[0]['domain'], domain)
        self.assertFalse(upstream._official_domain('chaoxing.com.other.test'))
        self.assertFalse(upstream._official_domain('notxuexitong.com'))

    def gateway(self, responses):
        session = requests.Session()
        adapter = InMemoryAdapter(responses)
        session.mount("https://", adapter)
        direct = ChaoxingDirectClient("offline-test-agent", session=session)
        replacement = patch.object(upstream, "ChaoxingDirectClient", return_value=direct)
        replacement.start()
        self.addCleanup(replacement.stop)
        return upstream.UpstreamGateway(), adapter

    def test_login_navigation_and_profile_must_both_succeed(self):
        gateway, adapter = self.gateway([
            {"body": LOGIN_HTML},
            {"body": {"status": True, "url": "https://i.chaoxing.com/home"},
             "cookies": ["_uid=100; Domain=.chaoxing.com; Path=/; Secure; HttpOnly"]},
            {"body": "home"}, {"body": PROFILE},
        ])
        account = gateway.login("synthetic-account", "synthetic-password")
        self.assertEqual((account.uid, account.fid, account.name), ("100", "200", "测试同学"))
        self.assertEqual(account.school, "")
        self.assertEqual(account.cookies[0]["rest"], {"HttpOnly": None})
        self.assertNotIn("synthetic-password", adapter.sent[1].body)
        self.assertEqual(len(adapter.sent), 4)
        self.assertNotIn("synthetic", repr(account))

    def test_arbitrary_success_json_does_not_create_account(self):
        for body in ({"result": 1}, {"msg": {}}, {"msg": {"uid": "100", "fid": "200"}},
                     {"result": 0, "msg": PROFILE["msg"]}, {"msg": {**PROFILE["msg"], "uid": True, "puid": None}}):
            with self.subTest(body=body), self.assertRaises(upstream.UpstreamError):
                upstream.parse_profile(body)

    def test_login_challenge_does_not_submit_password(self):
        gateway, adapter = self.gateway([{"body": LOGIN_HTML.replace('name="needVcode" value=""', 'name="needVcode" value="1"')}])
        with self.assertRaises(upstream.UpstreamError) as error:
            gateway.login("synthetic-account", "synthetic-password")
        self.assertEqual(error.exception.code, "challenge_required")
        self.assertEqual(len(adapter.sent), 1)

    def test_two_factor_is_not_success_and_action_token_not_exposed(self):
        gateway, adapter = self.gateway([
            {"body": LOGIN_HTML},
            {"body": {"status": True, "url": "https://i.chaoxing.com/home",
                      "containTwoFactorLogin": True,
                      "twoFactorLoginPCUrl": "https://passport2.chaoxing.com/twoFactor?token=secret"}},
        ])
        with self.assertRaises(upstream.UpstreamError) as error:
            gateway.login("synthetic-account", "synthetic-password")
        self.assertEqual(error.exception.code, "two_factor_required")
        self.assertNotIn("secret", str(error.exception.details))
        self.assertEqual(len(adapter.sent), 2)

    def test_cookie_scope_flags_and_expiry_roundtrip(self):
        jar = requests.cookies.RequestsCookieJar()
        rows = [{**COOKIES[0], "domain": "sso.chaoxing.com", "domain_specified": False,
                 "domain_initial_dot": False, "path": "/apis", "expires": 4_000_000_000,
                 "discard": False}]
        upstream.load_cookies(jar, rows)
        result = upstream.dump_cookies(jar)[0]
        for key, value in rows[0].items():
            self.assertEqual(result[key], value)

    def test_cookie_outside_official_hosts_is_rejected(self):
        gateway, adapter = self.gateway([])
        for domain in ("", ".evil.test", ".chaoxing.com.evil.test", "sso.chaoxing.com:443"):
            with self.subTest(domain=domain), self.assertRaises(upstream.UpstreamError):
                gateway.check([{**COOKIES[0], "domain": domain}])
        self.assertEqual(adapter.sent, [])

    def test_login_page_redirect_is_expired_session(self):
        gateway, _ = self.gateway([
            {"status": 302, "headers": {"Location": "https://passport2.chaoxing.com/login"}},
            {"body": LOGIN_HTML},
        ])
        with self.assertRaises(upstream.UpstreamError) as error:
            gateway.check(COOKIES)
        self.assertEqual(error.exception.code, "session_expired")

    def test_profile_forbidden_is_not_misreported_as_password_error(self):
        gateway, _ = self.gateway([{"status": 403, "body": "Forbidden"}])
        with self.assertRaises(upstream.UpstreamError) as error:
            gateway.check(COOKIES)
        self.assertEqual(error.exception.code, "upstream_forbidden")

    def test_ordinary_sign_uses_confirmed_identity_and_once_only(self):
        gateway, adapter = self.gateway([{"body": PROFILE}, {"body": ORDINARY_DETAIL},
            {"body": {"result": 0}}, {"body": "success"}])
        output = gateway.sign(COOKIES, IDENTITY, SIGN)
        self.assertEqual(output["status"], "reported_success")
        self.assertNotIn("rawBody", output)
        self.assertEqual(len(adapter.sent), 4)
        request = adapter.sent[-1]
        self.assertEqual(request.method, "GET")
        query = parse_qs(request.url.split("?", 1)[1], keep_blank_values=True)
        self.assertEqual(query["uid"], ["100"])
        self.assertEqual(query["fid"], ["200"])
        self.assertIn("mobilelearn.chaoxing.com", request.url)
        self.assertEqual(request.headers["Referer"], "https://mobilelearn.chaoxing.com/")
        self.assertFalse(any("preSign" in r.url for r in adapter.sent))

    def test_code_and_position_preserve_request_families(self):
        cases = [
            ({**SIGN, "kind": "code", "signCode": "1423"}, "GET", "signCode"),
            ({**SIGN, "kind": "position", "location": {"address": "现场", "latitude": 30, "longitude": 120}}, "POST", "locationResult"),
            ({**SIGN, "kind": "located_code", "signCode": "1423", "location": {"address": "现场", "latitude": 30, "longitude": 120}}, "POST", "location"),
        ]
        for payload, method, field in cases:
            with self.subTest(kind=payload["kind"]):
                gateway, adapter = self.gateway([{"body": PROFILE}, {"body": PRESIGN}, {"body": "success"}])
                self.assertEqual(gateway.sign(COOKIES, IDENTITY, payload)["status"], "reported_success")
                request = adapter.sent[-1]
                self.assertEqual(request.method, method)
                form = parse_qs(request.body if method == "POST" else request.url.split("?", 1)[1])
                self.assertIn(field, form)

    def test_identity_mismatch_stops_before_presign(self):
        gateway, adapter = self.gateway([{"body": PROFILE}])
        with self.assertRaises(upstream.UpstreamError) as error:
            gateway.sign(COOKIES, {**IDENTITY, "uid": "999"}, {**SIGN, "kind": "code", "signCode": "1423"})
        self.assertEqual(error.exception.code, "identity_mismatch")
        self.assertEqual(len(adapter.sent), 1)

    def test_invalid_inputs_and_identity_override_make_no_requests(self):
        gateway, adapter = self.gateway([])
        for payload in ({**SIGN, "uid": "999"}, {**SIGN, "activeId": ""},
                        {**SIGN, "kind": "position"}, {**SIGN, "kind": "code"},
                        {**SIGN, "kind": "qr", "enc": "teacher-value", "ifopenAddress": "1"}):
            with self.subTest(payload=payload), self.assertRaises(upstream.UpstreamError):
                gateway.sign(COOKIES, IDENTITY, payload)
        self.assertEqual(adapter.sent, [])

    def test_unknown_presign_or_unimplemented_analysis_still_blocks_other_sign_types(self):
        for body in ("", "<html>签到发生未知错误</html>", '<script src="/pptSign/analysis2"></script>签到'):
            with self.subTest(body=body):
                gateway, adapter = self.gateway([{"body": PROFILE}, {"body": body}])
                output = gateway.sign(COOKIES, IDENTITY, {**SIGN, "kind": "code", "signCode": "1423"})
                self.assertEqual(output["status"], "unknown")
                self.assertFalse(output["submitted"])
                self.assertEqual(len(adapter.sent), 2)

    def test_mismatched_presign_activity_is_not_submitted(self):
        gateway, adapter = self.gateway([{"body": PROFILE}, {"body": PRESIGN.replace('value="300"', 'value="999"')}])
        with self.assertRaises(upstream.UpstreamError) as error:
            gateway.sign(COOKIES, IDENTITY, {**SIGN, "kind": "code", "signCode": "1423"})
        self.assertEqual(error.exception.code, "activity_mismatch")
        self.assertEqual(error.exception.details, {"phase": "presign", "submitted": False})
        self.assertEqual(len(adapter.sent), 2)

    def test_session_error_after_submit_carries_unknown_phase(self):
        gateway, adapter = self.gateway([{"body": PROFILE}, {"body": ORDINARY_DETAIL},
            {"body": {"result": 0}}, {"body": '<input type="password">请先登录'}])
        with self.assertRaises(upstream.UpstreamError) as error:
            gateway.sign(COOKIES, IDENTITY, SIGN)
        self.assertEqual(error.exception.code, "session_expired")
        self.assertEqual(error.exception.details, {"phase": "submit", "submitted": True})
        self.assertEqual(len(adapter.sent), 4)

    def test_other_sign_types_keep_post_submission_login_redirect_protection(self):
        gateway, adapter = self.gateway([{"body": PROFILE}, {"body": PRESIGN},
            {"status": 302, "headers": {"Location": "https://passport2.chaoxing.com/login"}},
            {"body": LOGIN_HTML}])
        with self.assertRaises(upstream.UpstreamError) as error:
            gateway.sign(COOKIES, IDENTITY, {**SIGN, "kind": "code", "signCode": "1423"})
        self.assertEqual(error.exception.code, "session_expired")
        self.assertEqual(error.exception.details, {"phase": "submit", "submitted": True})
        self.assertEqual(len(adapter.sent), 4)

    def test_captcha_response_returns_challenge_without_retry(self):
        gateway, adapter = self.gateway([{"body": PROFILE}, {"body": PRESIGN}, {"body": "validate_challenge-token"}])
        output = gateway.sign(COOKIES, IDENTITY, {**SIGN, "kind": "qr", "enc": "provided-teacher-enc"})
        self.assertEqual(output["status"], "requires_validation")
        self.assertEqual(output["challenge"], {"enc2": "challenge-token"})
        self.assertEqual(len(adapter.sent), 3)
        self.assertEqual(parse_qs(adapter.sent[-1].body)["enc"], ["provided-teacher-enc"])

    def test_timeout_after_submit_is_unknown_without_retry(self):
        gateway, adapter = self.gateway([{"body": PROFILE}, {"body": ORDINARY_DETAIL},
            {"body": {"result": 0}}, requests.Timeout()])
        output = gateway.sign(COOKIES, IDENTITY, SIGN)
        self.assertEqual(output["status"], "outcome_unknown")
        self.assertTrue(output["submitted"])
        self.assertEqual(len(adapter.sent), 4)

    def test_false_positive_response_is_unknown_and_not_reflected(self):
        gateway, _ = self.gateway([{"body": PROFILE}, {"body": ORDINARY_DETAIL},
            {"body": {"result": 0}}, {"body": "not success; cookie=secret"}])
        output = gateway.sign(COOKIES, IDENTITY, SIGN)
        self.assertEqual(output["status"], "unknown")
        public = {key: value for key, value in output.items() if key != "_diagnostic"}
        self.assertNotIn("secret", json.dumps(public))
        self.assertEqual(output["_diagnostic"]["body"], "not success; cookie=secret")


if __name__ == "__main__":
    unittest.main()
