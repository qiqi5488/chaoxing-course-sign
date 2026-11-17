"""Course queries are tested with an in-memory HTTP adapter, never real accounts."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import requests
import upstream
from chaoxing_direct import ChaoxingDirectClient
from course_protocol import (CoursePayloadError, display_profile, image_url, parse_normal,
                             parse_mirror, parse_meet, merge_courses)
from test_upstream import InMemoryAdapter, PROFILE, IDENTITY, COOKIES


NORMAL = {'channelList': [{'content': {'id': 500, 'name': '软件一班', 'state': 0,
    'course': {'data': [{'id': 400, 'name': '软件工程', 'teacherfactor': '测试教师',
        'courseSquareUrl': 'https://mobilelearn.chaoxing.com?courseId=400&classId=500',
        'imageurl': 'http://p1.chaoxing.com/cover.jpg'}]}}}]}
MEET = {'data': {'listData': [{'title': '软件工程', 'creatorName': '会议教师', 'meetClasses': [
    {'courseId': '400', 'id': '500', 'name': '软件一班', 'termName': '实际学期', 'title': '软件工程'}]}]}}


class CourseTests(unittest.TestCase):
    def gateway(self, responses):
        session = requests.Session()
        adapter = InMemoryAdapter(responses)
        session.mount('https://', adapter)
        direct = ChaoxingDirectClient('offline-course-tests', session=session)
        replacement = patch.object(upstream, 'ChaoxingDirectClient', return_value=direct)
        replacement.start()
        self.addCleanup(replacement.stop)
        return upstream.UpstreamGateway(), adapter

    def test_normal_fields_do_not_invent_term_or_meaning_of_status(self):
        row = parse_normal(NORMAL)[0]
        self.assertEqual((row['courseId'], row['classId']), ('400', '500'))
        self.assertEqual(row['name'], '软件工程')
        self.assertEqual(row['teacher'], '测试教师')
        self.assertEqual(row['className'], '软件一班')
        self.assertIsNone(row['term'])
        self.assertIsNone(row['status'])
        self.assertEqual(row['statusCode'], 0)
        self.assertEqual(row['coverUrl'], 'https://p1.chaoxing.com/cover.jpg')

    def test_merge_uses_course_and_class_and_fills_only_missing_fields(self):
        merged = merge_courses([parse_normal(NORMAL), parse_meet(MEET)])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['teacher'], '测试教师')
        self.assertEqual(merged[0]['term'], '实际学期')
        self.assertEqual(merged[0]['sourceLabels'], ['普通课程', '泛雅课堂'])
        other = parse_normal(NORMAL)[0]
        other = dict(other, classId='501', key='400_501')
        self.assertEqual(len(merge_courses([merged, [other]])), 2)

    def test_valid_empty_and_unrecognized_payload_are_different(self):
        self.assertEqual(parse_normal({'channelList': []}), [])
        self.assertEqual(parse_meet({'data': {'listData': []}}), [])
        for payload in ({}, {'result': 0, 'msg': 'private-message'}, {'channelList': None}, {'channelList': [{'content': {'course': {}}}]}):
            with self.subTest(payload=payload), self.assertRaises(CoursePayloadError):
                parse_normal(payload)

    def test_mirror_normalizes_explicit_course_and_compound_class_identifier(self):
        payload = {'data': [{'clazz': {'data': [{'courseid': '400', 'id': 'school_500',
            'name': '软件一班', 'course': {'data': [{'name': '软件工程', 'teacherfactor': '教师'}]}}]}}]}
        row = parse_mirror(payload)[0]
        self.assertEqual(row['key'], '400_500')
        self.assertEqual(row['sourceLabels'], ['镜像课程'])

    def test_profile_only_safe_fields_and_masked_student_number(self):
        profile = display_profile({'msg': {'name': '测试用户', 'uid': 'private-uid', 'fid': 'private-fid',
            'phone': 'private-phone', 'email': 'private-email', 'studentNumber': '202612345',
            'schoolName': '实际学校', 'department': '实际院系', 'major': '实际专业'}})
        self.assertEqual(profile['studentNumberMasked'], '20****45')
        self.assertEqual(profile['school'], '实际学校')
        self.assertNotIn('uid', profile)
        self.assertNotIn('phone', profile)
        self.assertNotIn('email', profile)
        self.assertNotIn('private', str(profile))

    def test_image_urls_reject_external_hosts_schemes_userinfo_and_ports(self):
        for value in ('javascript:alert(1)', 'https://not-chaoxing.com/image.jpg', 'https://p1.chaoxing.com.evil.test/a',
                      'https://user@p1.chaoxing.com/a', 'https://p1.chaoxing.com:8443/a'):
            with self.subTest(value=value):
                self.assertIsNone(image_url(value))

    def test_detail_query_confirms_identity_then_reads_and_merges_only_get_requests(self):
        gateway, adapter = self.gateway([{'body': PROFILE}, {'body': NORMAL}, {'body': MEET}])
        result = gateway.get_account_details(COOKIES, IDENTITY)
        self.assertEqual(len(result['courses']), 1)
        self.assertEqual([s['status'] for s in result['sources']], ['ok', 'ok', 'unsupported'])
        self.assertEqual(result['profile']['name'], '测试同学')
        self.assertTrue(all(r.method == 'GET' for r in adapter.sent))
        self.assertFalse(any('stuSign' in r.url for r in adapter.sent))
        self.assertTrue(result['cookies'])

    def test_identity_mismatch_never_queries_courses(self):
        gateway, adapter = self.gateway([{'body': PROFILE}])
        with self.assertRaises(upstream.UpstreamError) as error:
            gateway.get_account_details(COOKIES, {**IDENTITY, 'uid': '999'})
        self.assertEqual(error.exception.code, 'identity_mismatch')
        self.assertEqual(len(adapter.sent), 1)

    def test_source_failure_does_not_claim_empty_success_or_leak_server_text(self):
        gateway, _ = self.gateway([{'body': PROFILE},
            {'body': {'result': 0, 'errorMsg': 'private-user private-cookie'}},
            {'body': {'data': {'listData': []}}}])
        result = gateway.get_account_details(COOKIES, IDENTITY)
        self.assertEqual(result['courses'], [])
        self.assertEqual(result['sources'][0]['status'], 'error')
        self.assertEqual(result['sources'][1]['status'], 'ok')
        self.assertEqual(result['sources'][1]['count'], 0)
        self.assertNotIn('private', str(result['sources']))

    def test_source_transport_error_does_not_abort_other_source(self):
        gateway, _ = self.gateway([{'body': PROFILE}, requests.ConnectionError('private-url private-cookie'), {'body': MEET}])
        result = gateway.get_account_details(COOKIES, IDENTITY)
        self.assertEqual(len(result['courses']), 1)
        self.assertEqual(result['sources'][0]['status'], 'error')
        self.assertEqual(result['sources'][0]['code'], 'network_outcome_unknown')
        self.assertNotIn('private', str(result['sources']))


if __name__ == '__main__':
    unittest.main()
