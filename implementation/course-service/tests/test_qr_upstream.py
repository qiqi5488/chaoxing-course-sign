"""QR execution tests use in-memory HTTPS responses, never a real activity."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import upstream
from chaoxing_direct import ChaoxingDirectClient
from test_upstream import InMemoryAdapter, PROFILE, COOKIES, IDENTITY


QR = 'https://mobilelearn.chaoxing.com/widget/sign/e?id=300&c=42&enc=qr-ticket'
PAGE = '<input name="activeId" value="300"><input name="courseId" value="400"><input name="classId" value="500">'
INFO = {'result': 1, 'data': {'id': 300, 'courseId': 400, 'classId': 500,
                             'ifopenAddress': 0, 'openCheckFaceFlag': 0}}


class QrUpstreamTests(unittest.TestCase):
    def gateway(self, responses):
        session = requests.Session()
        adapter = InMemoryAdapter(responses)
        session.mount('https://', adapter)
        client = ChaoxingDirectClient('offline-qr-test', session=session)
        replacement = patch.object(upstream, 'ChaoxingDirectClient', return_value=client)
        replacement.start()
        self.addCleanup(replacement.stop)
        return upstream.UpstreamGateway(), adapter

    def test_teacher_url_and_signin_text_are_decoded_without_network(self):
        expected = {'activeId': '300', 'enc': 'qr-ticket', 'code': '42'}
        self.assertEqual(upstream.parse_qr_target(QR), expected)
        self.assertEqual(upstream.parse_qr_target('SIGNIN:aid=300&source=15&Code=42&enc=qr-ticket'), expected)
        self.assertEqual(upstream.parse_qr_target(QR.replace('qr-ticket', 'abc%2Bz'))['enc'], 'abc+z')

    def test_untrusted_or_ambiguous_qr_is_rejected(self):
        cases = ['', 'https://evil.test/widget/sign/e?id=300&enc=x',
                 QR + '&activeId=300', QR + '&enc=other', QR.replace('enc=qr-ticket', 'enc='),
                 QR.replace('300', '0'), QR + '&courseId=0', QR + '#enc=other',
                 QR.replace('qr-ticket', '%zz'), QR.replace('mobilelearn.', 'user@mobilelearn.'),
                 QR.replace('/widget/sign/e', '/unrelated'), QR.replace('.com/', '.com:444/'),
                 'id=300&enc=x', QR.replace('enc=qr-ticket', 'enc=x%0ay')]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(upstream.UpstreamError):
                upstream.parse_qr_target(text)

    def test_complete_qr_chain_uses_each_saved_identity_and_no_proxy(self):
        gateway, adapter = self.gateway([
            {'body': PROFILE}, {'body': INFO}, {'body': PAGE}, {'body': 'success'},
        ])
        result = gateway.sign_qr(COOKIES, IDENTITY, QR)
        self.assertEqual(result['status'], 'reported_success')
        self.assertEqual(result['activeId'], '300')
        self.assertTrue(result['submitted'])
        self.assertEqual([r.method for r in adapter.sent], ['GET', 'GET', 'GET', 'POST'])
        self.assertTrue(all(urlsplit(r.url).hostname.endswith('.chaoxing.com') for r in adapter.sent))
        sent = parse_qs(adapter.sent[-1].body, keep_blank_values=True)
        self.assertEqual(sent['uid'], ['100'])
        self.assertEqual(sent['fid'], ['200'])
        self.assertEqual(sent['activeId'], ['300'])
        self.assertEqual(sent['courseId'], ['400'])
        self.assertEqual(sent['enc'], ['qr-ticket'])
        self.assertNotIn('cookies', sent)
        self.assertNotIn('encode', sent)

    def test_official_entry_can_discover_context_when_activity_details_omit_it(self):
        gateway, adapter = self.gateway([
            {'body': PROFILE}, {'body': {'result': 1, 'data': {'id': 300}}},
            {'body': PAGE}, {'body': PAGE}, {'body': 'success'},
        ])
        self.assertEqual(gateway.sign_qr(COOKIES, IDENTITY, QR)['status'], 'reported_success')
        entry = adapter.sent[2]
        self.assertEqual(urlsplit(entry.url).path, '/widget/sign/e')
        self.assertEqual(parse_qs(urlsplit(entry.url).query), {'id': ['300'], 'enc': ['qr-ticket'], 'c': ['42']})

    def test_empty_presign_values_can_discover_context_without_guessing_zero(self):
        gateway, adapter = self.gateway([
            {'body': PROFILE}, {'body': {'result': 1, 'data': {'id': 300}}},
            {'body': PAGE}, {'body': PAGE}, {'body': 'success'},
        ])
        result = gateway.sign_qr(COOKIES, IDENTITY, 'SIGNIN:aid=300&enc=qr-ticket')
        self.assertEqual(result['status'], 'reported_success')
        first = parse_qs(urlsplit(adapter.sent[2].url).query, keep_blank_values=True)
        second = parse_qs(urlsplit(adapter.sent[3].url).query, keep_blank_values=True)
        self.assertEqual(first['courseId'], [''])
        self.assertEqual(first['classId'], [''])
        self.assertEqual(second['courseId'], ['400'])
        self.assertEqual(second['classId'], ['500'])

    def test_missing_context_never_posts(self):
        gateway, adapter = self.gateway([
            {'body': PROFILE}, {'body': {'result': 1, 'data': {'id': 300}}},
            {'body': '<input name="activeId" value="300">'},
        ])
        result = gateway.sign_qr(COOKIES, IDENTITY, 'SIGNIN:aid=300&enc=qr-ticket')
        self.assertEqual(result['status'], 'need_context')
        self.assertFalse(result['submitted'])
        self.assertTrue(all(r.method == 'GET' for r in adapter.sent))

    def test_activity_or_context_mismatch_never_posts(self):
        cases = [({'result': 1, 'data': {**INFO['data'], 'id': 301}}, PAGE),
                 (INFO, PAGE.replace('value="300"', 'value="301"')),
                 (INFO, PAGE.replace('value="400"', 'value="401"'))]
        for info, page in cases:
            with self.subTest(info=info, page=page):
                gateway, adapter = self.gateway([{'body': PROFILE}, {'body': info}, {'body': page}])
                with self.assertRaises(upstream.UpstreamError) as error:
                    gateway.sign_qr(COOKIES, IDENTITY, QR)
                self.assertIn(error.exception.code, ('activity_mismatch', 'context_mismatch'))
                self.assertFalse(error.exception.details['submitted'])
                self.assertTrue(all(r.method == 'GET' for r in adapter.sent))

    def test_face_or_location_requirement_is_not_bypassed(self):
        for key, status in [('openCheckFaceFlag', 'face_required'), ('ifopenAddress', 'location_required')]:
            with self.subTest(key=key):
                info = {'result': 1, 'data': {**INFO['data'], key: 1}}
                gateway, adapter = self.gateway([{'body': PROFILE}, {'body': info}, {'body': PAGE}])
                result = gateway.sign_qr(COOKIES, IDENTITY, QR)
                self.assertEqual(result['status'], status)
                self.assertFalse(result['submitted'])
                self.assertTrue(all(r.method == 'GET' for r in adapter.sent))

    def test_location_is_supplied_only_from_explicit_caller_input(self):
        info = {'result': 1, 'data': {**INFO['data'], 'ifopenAddress': 1}}
        gateway, adapter = self.gateway([{'body': PROFILE}, {'body': info}, {'body': PAGE}, {'body': 'success'}])
        location = {'latitude': 30.2, 'longitude': 120.1, 'address': '本人提供的现场位置'}
        result = gateway.sign_qr(COOKIES, IDENTITY, QR, location=location)
        self.assertEqual(result['status'], 'reported_success')
        sent = parse_qs(adapter.sent[-1].body)
        self.assertIn('本人提供的现场位置', sent['location'][0])

    def test_server_challenge_and_device_limits_are_returned_as_failures(self):
        for body, status in [('validate_challenge', 'requires_validation'), ('同一设备不允许重复签到', 'device_conflict')]:
            with self.subTest(body=body):
                gateway, adapter = self.gateway([{'body': PROFILE}, {'body': INFO}, {'body': PAGE}, {'body': body}])
                result = gateway.sign_qr(COOKIES, IDENTITY, QR)
                self.assertEqual(result['status'], status)
                self.assertEqual(sum(r.method == 'POST' for r in adapter.sent), 1)

    def test_submit_timeout_is_unknown_and_never_retried(self):
        gateway, adapter = self.gateway([
            {'body': PROFILE}, {'body': INFO}, {'body': PAGE}, requests.ReadTimeout('private-token'),
        ])
        result = gateway.sign_qr(COOKIES, IDENTITY, QR)
        self.assertEqual(result['status'], 'outcome_unknown')
        self.assertTrue(result['submitted'])
        self.assertNotIn('private', result['message'])
        self.assertEqual(sum(r.method == 'POST' for r in adapter.sent), 1)

    def test_wrong_identity_stops_before_activity_requests(self):
        gateway, adapter = self.gateway([{'body': PROFILE}])
        with self.assertRaises(upstream.UpstreamError) as error:
            gateway.sign_qr(COOKIES, {**IDENTITY, 'uid': '999'}, QR)
        self.assertEqual(error.exception.code, 'identity_mismatch')
        self.assertEqual(len(adapter.sent), 1)


if __name__ == '__main__':
    unittest.main()
